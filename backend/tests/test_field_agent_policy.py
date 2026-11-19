import unittest
from datetime import datetime, timedelta, timezone

from agent.evidence import summarize_freshness
from agent.outcomes import judge_outcome
from agent.policy import evaluate_proposal, normalize_proposal


ALL_TOOLS = {"check_evidence_freshness", "plan_irrigation", "get_weather_outlook", "assess_soil_moisture"}
SETTINGS = {"max_application_mm": 30, "min_confidence_for_irrigation": 0.55}


def evidence(gross=22.0, net=16.5, planner_conf=0.8, rain_48h=1.0, verdict="fresh"):
    return {
        "profile": {"area_hectares": 4.0},
        "freshness": {"verdict": verdict},
        "irrigation": {"confidence": planner_conf, "gross_irrigation_mm": gross, "net_irrigation_mm": net, "full_refill_gross_mm": None},
        "weather": {"rain_next_48h_mm": rain_48h, "rain_next_7d_mm": 12.0},
        "drought": {"zones": [{"zone_id": "zone_1"}]},
    }


def irrigate(water_mm=20.0, confidence=0.8):
    proposal, errors = normalize_proposal({
        "action_type": "irrigate",
        "title": "Apply water",
        "rationale": "Depletion reaches the trigger.",
        "evidence": [{"source": "plan_irrigation", "observation": "gross 22 mm"}],
        "confidence": confidence,
        "priority": "high",
        "follow_up_hours": 24,
        "water_mm": water_mm,
    })
    assert not errors, errors
    return proposal


def blocked_rules(result):
    return {item["rule"] for item in result["checks"] if not item["passed"] and item["severity"] == "block"}


class FieldAgentPolicyTests(unittest.TestCase):
    def test_valid_irrigation_is_accepted_with_volume_and_approval(self):
        result = evaluate_proposal(irrigate(), evidence(), ALL_TOOLS, SETTINGS, [], [], refresh_attempted=False)
        self.assertTrue(result["accepted"])
        self.assertTrue(result["proposal"]["requires_approval"])
        self.assertEqual(result["proposal"]["volume_m3"], 800.0)

    def test_irrigation_requires_planner_and_forecast(self):
        result = evaluate_proposal(irrigate(), evidence(), {"check_evidence_freshness"}, SETTINGS, [], [], False)
        self.assertFalse(result["accepted"])
        self.assertTrue({"irrigation_planner_consulted", "forecast_consulted"} <= blocked_rules(result))

    def test_amount_cannot_exceed_planner_or_farmer_cap(self):
        over_planner = evaluate_proposal(irrigate(water_mm=28), evidence(gross=22), ALL_TOOLS, SETTINGS, [], [], False)
        self.assertIn("within_planner_requirement", blocked_rules(over_planner))
        over_cap = evaluate_proposal(irrigate(water_mm=32), evidence(gross=40), ALL_TOOLS, SETTINGS, [], [], False)
        self.assertIn("farmer_application_cap", blocked_rules(over_cap))

    def test_forecast_rain_covering_need_blocks_irrigation(self):
        result = evaluate_proposal(irrigate(), evidence(rain_48h=20.0), ALL_TOOLS, SETTINGS, [], [], False)
        self.assertIn("forecast_rain_covers_need", blocked_rules(result))

    def test_low_confidence_blocks_irrigation(self):
        result = evaluate_proposal(irrigate(confidence=0.4), evidence(), ALL_TOOLS, SETTINGS, [], [], False)
        self.assertIn("confidence_threshold", blocked_rules(result))

    def test_stale_core_moisture_requires_refresh(self):
        stale = evidence(verdict="stale_core_moisture")
        self.assertIn("fresh_moisture_evidence", blocked_rules(evaluate_proposal(irrigate(), stale, ALL_TOOLS, SETTINGS, [], [], False)))
        self.assertTrue(evaluate_proposal(irrigate(), stale, ALL_TOOLS, SETTINGS, [], [], True)["accepted"])

    def test_planner_with_no_requirement_blocks_irrigation(self):
        result = evaluate_proposal(irrigate(), evidence(gross=0.0, net=0.0), ALL_TOOLS, SETTINGS, [], [], False)
        self.assertIn("within_planner_requirement", blocked_rules(result))

    def test_monitor_is_informational_and_drops_water(self):
        proposal, _ = normalize_proposal({
            "action_type": "monitor", "title": "Keep monitoring", "rationale": "All good.",
            "evidence": [{"source": "x", "observation": "y"}], "confidence": 0.7, "priority": "low",
            "follow_up_hours": 2, "water_mm": 10,
        })
        result = evaluate_proposal(proposal, evidence(), ALL_TOOLS, SETTINGS, [], [], False)
        self.assertTrue(result["accepted"])
        self.assertFalse(result["proposal"]["requires_approval"])
        self.assertIsNone(result["proposal"]["water_mm"])
        self.assertEqual(result["proposal"]["follow_up_hours"], 6)

    def test_unknown_zone_is_rejected(self):
        proposal, _ = normalize_proposal({
            "action_type": "inspect_zone", "title": "Inspect", "rationale": "Conflict.", "zone_id": "zone_9",
            "evidence": [{"source": "x", "observation": "y"}], "confidence": 0.6, "priority": "high", "follow_up_hours": 24,
        })
        result = evaluate_proposal(proposal, evidence(), ALL_TOOLS, SETTINGS, [], [], False)
        self.assertIn("zone_exists", blocked_rules(result))

    def test_recent_rejection_is_a_warning_not_a_block(self):
        recent = [{"action_type": "irrigate", "status": "rejected", "decided_at": datetime.now(timezone.utc) - timedelta(hours=3), "decision_note": "pump down"}]
        result = evaluate_proposal(irrigate(), evidence(), ALL_TOOLS, SETTINGS, [], recent, False)
        self.assertTrue(result["accepted"])
        self.assertTrue(any(c["rule"] == "respect_recent_rejection" and c["severity"] == "warn" for c in result["checks"]))

    def test_freshness_verdicts(self):
        statuses = {
            source: {"last_fetched_at": "2026-11-01T00:00:00Z", "age_hours": 100, "cadence_hours": 24, "refresh_available": True}
            for source in ("smap_l4", "era5_land", "open_meteo")
        }
        self.assertEqual(summarize_freshness(statuses)["verdict"], "stale_core_moisture")
        statuses["open_meteo"]["age_hours"] = 1
        self.assertEqual(summarize_freshness(statuses)["verdict"], "partially_stale")


class OutcomeJudgeTests(unittest.TestCase):
    def test_irrigation_improvement(self):
        outcome = judge_outcome({"action_type": "irrigate", "applied_mm": 18, "water_mm": 20}, {"root_zone_depletion_mm": 30}, {"root_zone_depletion_mm": 12})
        self.assertEqual(outcome["verdict"], "improved")
        self.assertIn("18", outcome["message"])

    def test_worsening_detected(self):
        outcome = judge_outcome({"action_type": "wait_for_rain"}, {"root_zone_depletion_mm": 10, "drought_score": 20}, {"root_zone_depletion_mm": 18, "drought_score": 25})
        self.assertEqual(outcome["verdict"], "worsened")

    def test_inspection_uses_field_report(self):
        outcome = judge_outcome({"action_type": "inspect_zone", "field_note": "frost damage"}, {}, {})
        self.assertEqual(outcome["verdict"], "field_report_received")


if __name__ == "__main__":
    unittest.main()
