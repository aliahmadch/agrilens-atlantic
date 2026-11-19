import unittest

from datetime import datetime, timezone
from unittest import mock

from agent import alerts as alerts_module
from agent.alerts import (
    check_status, compose_sms, compose_telegram, detect_alerts, next_check_text, preferred_channel, snapshot_from_evidence,
)
from agent.notifier import mask_phone, normalize_phone


def snap(**overrides):
    base = {
        "moisture_status": "adequate", "moisture_score": 60.0,
        "drought_class": "no_risk", "drought_score": 10.0,
        "health_class": "healthy", "health_score": 90.0, "zone_health": {"zone_1": 90.0, "zone_2": 88.0},
        "rain_next_48h_mm": 2.0, "rain_next_7d_mm": 8.0,
    }
    base.update(overrides)
    return base


def types(alerts):
    return [item["type"] for item in alerts]


class DetectAlertsTests(unittest.TestCase):
    def test_calm_field_sends_nothing(self):
        self.assertEqual(detect_alerts(snap(), snap(), []), [])

    def test_rain_wet_and_decision(self):
        alerts = detect_alerts(snap(rain_next_48h_mm=16.0, moisture_status="wet", moisture_score=81.0), None,
                               [{"title": "Inspect zone 1", "priority": "high", "requires_approval": True}])
        self.assertEqual(set(types(alerts)), {"rain", "wet", "decision"})
        self.assertEqual(alerts[0]["type"], "decision")  # urgent (high priority) sorts first

    def test_heavy_rain_is_urgent(self):
        self.assertEqual(detect_alerts(snap(rain_next_48h_mm=30.0), None, [])[0]["severity"], "urgent")

    def test_drought_high_and_rising(self):
        self.assertEqual(types(detect_alerts(snap(drought_class="high_field_risk", drought_score=60.0), None, [])), ["drought"])
        rising = detect_alerts(snap(drought_class="moderate_field_risk", drought_score=45.0), snap(drought_score=25.0), [])
        self.assertIn("going up", rising[0]["text"])
        self.assertEqual(detect_alerts(snap(drought_class="watch", drought_score=30.0), snap(drought_score=14.0), []), [])  # "watch" is not worth a text

    def test_crop_health_falling_field_and_zone(self):
        field = detect_alerts(snap(health_class="watch", health_score=72.0), snap(health_score=88.0), [])
        self.assertIn("dropping", field[0]["text"])
        zone = detect_alerts(snap(zone_health={"zone_1": 90.0, "zone_2": 70.0}), snap(), [])
        self.assertIn("zone 2 looks worse", zone[0]["text"])

    def test_snapshot_from_evidence(self):
        result = snapshot_from_evidence({
            "moisture": {"field_status": "wet", "field_score_0_100": 80.7},
            "crop_health": {"health_class": "healthy", "health_score_0_100": 92.4, "zones": [{"zone_id": "zone_1", "health_score": 91.0}]},
            "weather": {"rain_next_48h_mm": 1.6},
        })
        self.assertEqual(result["moisture_status"], "wet")
        self.assertEqual(result["zone_health"], {"zone_1": 91.0})
        self.assertIsNone(result["drought_score"])


class UpdateTests(unittest.TestCase):
    def test_check_status(self):
        self.assertEqual(check_status([])["text"], "All good, no water needed.")
        self.assertEqual(check_status([{"action_type": "wait_for_rain"}])["key"], "status:wait")
        self.assertEqual(check_status([{"action_type": "inspect_zone", "requires_approval": True}])["text"], "Needs your attention.")

    def test_next_check_in_farm_time(self):
        now = datetime(2026, 11, 12, 11, 10, tzinfo=timezone.utc)  # 7:10 AM in Halifax (AST, UTC-4)
        self.assertEqual(next_check_text(now, 24), "tomorrow around 7:10 AM")
        self.assertEqual(next_check_text(now, 2), "today around 9:10 AM")
        checked = datetime(2026, 11, 11, 23, 0, tzinfo=timezone.utc)  # 7:00 PM yesterday in Halifax
        self.assertEqual(next_check_text(now, 24, checked), "today around 7:00 PM")
        self.assertEqual(next_check_text(now, 2, checked), "soon")


class MessageTests(unittest.TestCase):
    def test_compose_is_plain_text(self):
        items = [check_status([])] + detect_alerts(snap(rain_next_48h_mm=30.0, moisture_status="wet", moisture_score=81.0), None, [])
        body = compose_sms("cavendish", "Sam", items, "tomorrow around 7:10 AM")
        self.assertEqual(body, (
            "AgriLens: cavendish\nHi Sam, I just checked your field.\nAll good, no water needed.\n"
            "Urgent: Rain coming (30 mm in 2 days). No need to water.\nSoil is wet. No need to water.\nNext check: tomorrow around 7:10 AM."
        ))
        self.assertTrue(all(ord(ch) < 128 for ch in body))

    def test_decision_text_is_short(self):
        alerts = detect_alerts(snap(), None, [
            {"action_type": "inspect_zone", "zone_id": "zone_1", "title": "Inspect zone 1 to resolve conflicting evidence"},
            {"action_type": "irrigate", "water_mm": 15.4, "title": "Apply 15.4 mm by Sprinkler"},
        ])
        self.assertEqual([a["text"] for a in alerts], ["Please check zone 1. Open AgriLens to say yes or no.", "Water 15 mm? Open AgriLens to say yes or no."])

    def test_telegram_message_has_emoji_and_escapes_html(self):
        items = [check_status([{"action_type": "wait_for_rain"}])] + detect_alerts(snap(rain_next_48h_mm=30.0), None, [])
        body = compose_telegram("a<b>&c", "Sam", items, "tomorrow around 7:10 AM")
        self.assertEqual(body, (
            "🌾 <b>AgriLens: a&lt;b&gt;&amp;c</b>\nHi Sam, I just checked your field.\n🌧️ <b>No need to water, rain is coming.</b>\n"
            "🌧️ <b>Urgent:</b> Rain coming (30 mm in 2 days). No need to water.\n🕖 Next check: tomorrow around 7:10 AM."
        ))

    def test_preferred_channel(self):
        with mock.patch.object(alerts_module.telegram, "configured", return_value=True), \
             mock.patch.object(alerts_module.notifier, "sms_configured", return_value=True):
            self.assertEqual(preferred_channel({"telegram_chat_id": "1", "contact_phone": "+19025550123"}), "telegram")
            self.assertEqual(preferred_channel({"contact_phone": "+19025550123"}), "sms")
            self.assertIsNone(preferred_channel({}))

    def test_phone_normalization(self):
        self.assertEqual(normalize_phone("+1 (902) 555-0147"), "+19025550147")
        self.assertEqual(normalize_phone("902 555 0147"), "+19025550147")
        self.assertIsNone(normalize_phone("12345"))
        self.assertEqual(mask_phone("+19025550147"), "+1902••••147")


if __name__ == "__main__":
    unittest.main()
