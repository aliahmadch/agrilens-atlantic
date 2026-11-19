import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  Bot,
  Brain,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleDashed,
  ClipboardCheck,
  CloudRain,
  Database,
  Droplets,
  Eye,
  Loader2,
  MessageSquare,
  Play,
  Radar,
  RefreshCw,
  Send,
  Settings2,
  ShieldAlert,
  ShieldCheck,
  Sprout,
  ThumbsDown,
  ThumbsUp,
  Wrench,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { useToast } from "@/hooks/use-toast";
import {
  fieldAgentApi,
  type AgentAction,
  type AgentActionType,
  type AgentMessage,
  type AgentRun,
  type AgentRunSummary,
  type AgentSettings,
  type AgentStatus,
  type AgentStep,
} from "./fieldAgentApi";

const POLL_MS = 2000;
const VISIBLE_STATUSES = new Set(["pending_approval", "approved", "completed", "monitoring", "evaluated"]);

const CYCLE = [
  { key: "observe", label: "Observe" },
  { key: "validate", label: "Validate" },
  { key: "diagnose", label: "Diagnose" },
  { key: "plan", label: "Plan" },
  { key: "propose", label: "Propose" },
  { key: "notify", label: "Notify farmer" },
  { key: "approve", label: "Farmer approval" },
  { key: "monitor", label: "Monitor outcome" },
] as const;

const ACTION_META: Record<AgentActionType, { label: string; icon: typeof Droplets; tone: string }> = {
  irrigate: { label: "Irrigate", icon: Droplets, tone: "border-sky-200 bg-sky-50 text-sky-950" },
  wait_for_rain: { label: "Wait for rain", icon: CloudRain, tone: "border-indigo-200 bg-indigo-50 text-indigo-950" },
  inspect_zone: { label: "Inspect zone", icon: Eye, tone: "border-amber-200 bg-amber-50 text-amber-950" },
  collect_sensor_data: { label: "Collect evidence", icon: Radar, tone: "border-violet-200 bg-violet-50 text-violet-950" },
  monitor: { label: "Monitor", icon: Sprout, tone: "border-emerald-200 bg-emerald-50 text-emerald-950" },
};

const STATUS_LABEL: Record<string, string> = {
  pending_approval: "Awaiting your approval",
  approved: "Approved",
  rejected: "Rejected",
  completed: "Done, awaiting outcome check",
  monitoring: "Auto follow-up scheduled",
  evaluated: "Outcome measured",
  superseded: "Superseded",
};

const RISK_TONE: Record<string, string> = {
  low: "bg-emerald-100 text-emerald-900 border-emerald-200",
  moderate: "bg-amber-100 text-amber-900 border-amber-200",
  high: "bg-orange-100 text-orange-900 border-orange-200",
  critical: "bg-rose-100 text-rose-900 border-rose-200",
};

const OUTCOME_TONE: Record<string, string> = {
  improved: "border-emerald-200 bg-emerald-50 text-emerald-900",
  stable: "border-slate-200 bg-slate-50 text-slate-900",
  worsened: "border-rose-200 bg-rose-50 text-rose-900",
  field_report_received: "border-sky-200 bg-sky-50 text-sky-900",
  awaiting_field_report: "border-amber-200 bg-amber-50 text-amber-900",
};

const isNumber = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);

function relativeTime(value: string | null | undefined) {
  if (!value) return "—";
  const date = new Date(value);
  const seconds = Math.round((Date.now() - date.getTime()) / 1000);
  if (Number.isNaN(seconds)) return value;
  const future = seconds < 0;
  const abs = Math.abs(seconds);
  const text = abs < 60 ? `${abs}s` : abs < 3600 ? `${Math.round(abs / 60)} min` : abs < 86400 ? `${Math.round(abs / 3600)} h` : `${Math.round(abs / 86400)} d`;
  return future ? `in ${text}` : `${text} ago`;
}

function duration(run: AgentRunSummary) {
  if (!run.finished_at) return null;
  const seconds = Math.round((new Date(run.finished_at).getTime() - new Date(run.started_at).getTime()) / 1000);
  return seconds >= 60 ? `${Math.floor(seconds / 60)}m ${seconds % 60}s` : `${seconds}s`;
}

function modeLabel(run: AgentRunSummary | AgentRun | null, status: AgentStatus | null) {
  const mode = run?.mode || status?.mode;
  if (mode === "llm") return run?.model || status?.model || "Claude";
  if (mode === "llm+rules") return `${run?.model || "Claude"} + rule fallback`;
  return "Rule-based planner";
}

// The monitor re-checks a field this many hours after its latest check (any trigger).
function nextCheckLabel(runs: AgentRunSummary[], status: AgentStatus | null) {
  if (!status?.monitor?.running || !runs.length) return null;
  const next = new Date(new Date(runs[0].finished_at || runs[0].started_at).getTime() + (status.monitor.interval_hours ?? 24) * 3600 * 1000);
  if (Number.isNaN(next.getTime())) return null;
  const today = new Date();
  const days = Math.round((new Date(next.toDateString()).getTime() - new Date(today.toDateString()).getTime()) / 86400000);
  const day = days <= 0 ? "today" : days === 1 ? "tomorrow" : next.toLocaleDateString(undefined, { weekday: "long" });
  return `${day} around ${next.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}`;
}

function triggerLabel(trigger: string) {
  if (trigger === "scheduled") return "Autonomous check";
  if (trigger === "follow_up") return "Outcome follow-up";
  return "Manual run";
}

function ConfidenceBar({ value }: { value: number | null | undefined }) {
  if (!isNumber(value)) return null;
  const pct = Math.round(value * 100);
  const color = value >= 0.7 ? "bg-emerald-600" : value >= 0.5 ? "bg-amber-500" : "bg-rose-500";
  return (
    <div className="flex items-center gap-2" title={`Confidence ${pct}%`}>
      <div className="h-1.5 w-20 rounded-full bg-muted">
        <div className={`h-1.5 rounded-full ${color}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-[11px] font-semibold tabular-nums text-muted-foreground">{pct}%</span>
    </div>
  );
}

const DELIVERY_META: Record<AgentMessage["delivery_status"], { label: string; tone: string }> = {
  sent: { label: "Sent", tone: "bg-emerald-100 text-emerald-900 border-emerald-200" },
  not_sent: { label: "Saved, not sent", tone: "bg-slate-100 text-slate-800 border-slate-200" },
  failed: { label: "Not delivered", tone: "bg-rose-100 text-rose-900 border-rose-200" },
};

// Telegram bodies are HTML; show them as plain text in the app.
const plainText = (body: string) =>
  body.replace(/<[^>]+>/g, "").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");

const channelLabel = (message: AgentMessage) => (message.channel === "telegram" ? "Telegram" : "SMS");

function FarmerMessages({ messages, contact }: { messages: AgentMessage[]; contact: string }) {
  if (!messages.length) return null;
  return (
    <div className="space-y-1.5">
      <p className="dashboard-label">Messages to {contact}</p>
      <ul className="space-y-1.5">
        {messages.map((message) => {
          const meta = DELIVERY_META[message.delivery_status] || DELIVERY_META.not_sent;
          return (
            <li key={message.message_id} className="rounded-md border border-border/70 bg-muted/20 px-3 py-2">
              <div className="flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
                <Send className="h-3 w-3" />
                <span className={`rounded-full border px-1.5 py-0.5 font-semibold ${meta.tone}`}>{meta.label} · {channelLabel(message)}</span>
                {message.is_test ? <span className="font-semibold">Test</span> : null}
                <span>{message.recipient_name || "Farmer"} {message.recipient ? `(${message.recipient})` : ""} · {relativeTime(message.created_at)}</span>
              </div>
              <p className="mt-1 whitespace-pre-line text-[12px] leading-5 text-foreground">{plainText(message.body)}</p>
              {message.delivery_status === "failed" && message.error ? <p className="mt-1 text-[11px] text-rose-800">{message.error}</p> : null}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function CycleStrip({ run, actions }: { run: AgentRun | null; actions: AgentAction[] }) {
  const seen = new Set(run?.steps.map((step) => step.phase) || []);
  const running = run?.status === "running";
  const lastPhase = run?.steps[run.steps.length - 1]?.phase;
  const runActions = actions.filter((action) => action.run_id === run?.run_id);
  const approveDone = runActions.some((a) => ["approved", "completed", "evaluated", "rejected"].includes(a.status));
  const approveActive = runActions.some((a) => a.status === "pending_approval");
  const monitorDone = runActions.some((a) => a.status === "evaluated");
  const monitorActive = runActions.some((a) => ["approved", "completed", "monitoring"].includes(a.status));

  return (
    <ol className="flex flex-wrap items-center gap-1.5" aria-label="Agent cycle">
      {CYCLE.map((phase, index) => {
        let state: "done" | "active" | "idle" = "idle";
        if (phase.key === "approve") state = approveDone ? "done" : approveActive ? "active" : "idle";
        else if (phase.key === "monitor") state = monitorDone ? "done" : monitorActive ? "active" : "idle";
        else if (running && lastPhase === phase.key) state = "active";
        else if (seen.has(phase.key)) state = "done";
        const tone =
          state === "done"
            ? "border-emerald-300 bg-emerald-50 text-emerald-900"
            : state === "active"
              ? "border-sky-400 bg-sky-50 text-sky-900"
              : "border-border bg-background text-muted-foreground";
        return (
          <li key={phase.key} className="flex items-center gap-1.5">
            <span className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-semibold ${tone}`}>
              {state === "done" ? <CheckCircle2 className="h-3 w-3" /> : state === "active" ? <Loader2 className={`h-3 w-3 ${running ? "animate-spin" : ""}`} /> : <CircleDashed className="h-3 w-3" />}
              {phase.label}
            </span>
            {index < CYCLE.length - 1 ? <ChevronRight className="h-3 w-3 text-muted-foreground" aria-hidden="true" /> : null}
          </li>
        );
      })}
    </ol>
  );
}

function StepIcon({ step }: { step: AgentStep }) {
  const className = "h-3.5 w-3.5 shrink-0";
  if (step.kind === "reasoning") return <Brain className={`${className} text-violet-600`} />;
  if (step.kind === "message") return <MessageSquare className={`${className} text-slate-600`} />;
  if (step.kind === "error") return <AlertTriangle className={`${className} text-rose-600`} />;
  if (step.kind === "policy") {
    const accepted = (step.content.result as { accepted?: boolean } | undefined)?.accepted;
    return accepted ? <ShieldCheck className={`${className} text-emerald-600`} /> : <ShieldAlert className={`${className} text-rose-600`} />;
  }
  if (step.kind === "tool_call") return <Wrench className={`${className} text-sky-700`} />;
  if (step.kind === "notify") return <Send className={`${className} text-emerald-700`} />;
  if ((step.content as { is_error?: boolean }).is_error) return <AlertTriangle className={`${className} text-rose-600`} />;
  return <Database className={`${className} text-sky-700`} />;
}

function TraceTimeline({ run }: { run: AgentRun }) {
  // Plain tool calls carry no input; the result row already names the tool, so only show calls with input.
  const steps = run.steps.filter((step) => step.kind !== "tool_call" || step.tool_name === "propose_action");
  if (!steps.length) {
    return <p className="px-1 py-2 text-[12px] text-muted-foreground">Starting monitoring cycle…</p>;
  }
  return (
    <ol className="space-y-1">
      {steps.map((step) => {
        const body =
          step.kind === "reasoning" || step.kind === "message"
            ? String((step.content as { text?: string }).text || "")
            : JSON.stringify((step.content as { result?: unknown; input?: unknown }).result ?? (step.content as { input?: unknown }).input ?? step.content, null, 2);
        return (
          <li key={step.step_id}>
            <details className="group rounded border border-transparent px-1.5 py-1 hover:border-border hover:bg-muted/40">
              <summary className="flex cursor-pointer list-none items-start gap-2">
                <span className="mt-0.5"><StepIcon step={step} /></span>
                <span className="w-16 shrink-0 pt-px text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">{step.phase}</span>
                <span className={`min-w-0 flex-1 text-[12px] leading-5 ${step.kind === "reasoning" ? "italic text-violet-900" : "text-foreground"}`}>
                  {step.tool_name && step.kind !== "policy" && step.kind !== "tool_call" ? <code className="mr-1.5 rounded bg-muted px-1 text-[11px]">{step.tool_name}</code> : null}
                  {step.title}
                </span>
                {isNumber(step.duration_ms) && step.duration_ms > 0 ? (
                  <span className="shrink-0 text-[10px] tabular-nums text-muted-foreground">{step.duration_ms >= 1000 ? `${(step.duration_ms / 1000).toFixed(1)}s` : `${step.duration_ms}ms`}</span>
                ) : null}
              </summary>
              {body && body !== "{}" ? (
                <pre className="mt-1.5 max-h-72 overflow-auto whitespace-pre-wrap break-words rounded bg-muted/60 p-2 text-[11px] leading-4 text-foreground">{body}</pre>
              ) : null}
            </details>
          </li>
        );
      })}
      {run.status === "running" ? (
        <li className="flex items-center gap-2 px-1.5 py-1 text-[12px] text-sky-800">
          <Loader2 className="h-3.5 w-3.5 animate-spin" /> Working…
        </li>
      ) : null}
    </ol>
  );
}

type DialogState =
  | { kind: "reject"; action: AgentAction }
  | { kind: "complete"; action: AgentAction }
  | null;

function ActionCard({
  action,
  busy,
  onApprove,
  onDialog,
  onEvaluate,
}: {
  action: AgentAction;
  busy: boolean;
  onApprove: (action: AgentAction) => void;
  onDialog: (state: DialogState) => void;
  onEvaluate: (action: AgentAction) => void;
}) {
  const meta = ACTION_META[action.action_type] || ACTION_META.monitor;
  const Icon = meta.icon;
  const zone = (action.baseline?.zone as { centroid?: [number, number] } | undefined)?.centroid;
  const passed = action.policy_checks.filter((check) => check.passed && check.severity === "block").length;
  const warnings = action.policy_checks.filter((check) => check.severity === "warn");

  return (
    <article className={`rounded-md border p-3 ${meta.tone}`}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="flex min-w-0 items-start gap-2">
          <Icon className="mt-0.5 h-4 w-4 shrink-0" />
          <div className="min-w-0">
            <p className="text-[11px] font-bold uppercase tracking-[0.08em] opacity-80">
              {meta.label} · {action.priority} priority
            </p>
            <h4 className="text-[14px] font-semibold leading-5">{action.title}</h4>
          </div>
        </div>
        <span className="rounded-full border border-current/20 bg-white/70 px-2 py-0.5 text-[11px] font-semibold">{STATUS_LABEL[action.status] || action.status}</span>
      </div>

      <p className="mt-2 text-[13px] leading-5">{action.rationale}</p>

      <dl className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[12px]">
        <div className="flex items-center gap-1.5"><dt className="font-semibold">Confidence</dt><dd><ConfidenceBar value={action.confidence} /></dd></div>
        {isNumber(action.water_mm) ? <div><dt className="inline font-semibold">Water </dt><dd className="inline tabular-nums">{action.water_mm.toFixed(1)} mm{isNumber(action.volume_m3) ? ` · ${Math.round(action.volume_m3).toLocaleString()} m³` : ""}</dd></div> : null}
        {action.target_date ? <div><dt className="inline font-semibold">When </dt><dd className="inline">{action.target_date}</dd></div> : null}
        {action.zone_id ? <div><dt className="inline font-semibold">Where </dt><dd className="inline capitalize">{action.zone_id.replace("_", " ")}{zone && isNumber(zone[0]) && isNumber(zone[1]) ? ` (${zone[0].toFixed(5)}, ${zone[1].toFixed(5)})` : ""}</dd></div> : null}
        {action.follow_up_due_at && action.status !== "evaluated" ? <div><dt className="inline font-semibold">Re-check </dt><dd className="inline">{relativeTime(action.follow_up_due_at)}</dd></div> : null}
      </dl>

      <details className="mt-2 text-[12px]">
        <summary className="cursor-pointer font-semibold">Evidence, assumptions & safety checks ({passed} passed{warnings.length ? `, ${warnings.length} warning` : ""})</summary>
        <div className="mt-2 grid gap-3 md:grid-cols-2">
          <div>
            <p className="font-semibold">Evidence</p>
            <ul className="mt-1 space-y-1">
              {action.evidence.map((item, index) => (
                <li key={index}><span className="font-medium">{item.source}:</span> {item.observation}</li>
              ))}
            </ul>
            {action.assumptions.length ? (
              <>
                <p className="mt-2 font-semibold">Assumptions</p>
                <ul className="mt-1 list-disc space-y-0.5 pl-4">{action.assumptions.map((item, index) => <li key={index}>{item}</li>)}</ul>
              </>
            ) : null}
          </div>
          <div>
            <p className="font-semibold">Safety policy</p>
            <ul className="mt-1 space-y-1">
              {action.policy_checks.map((check) => (
                <li key={check.rule} className="flex items-start gap-1.5">
                  {check.severity === "warn" ? <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0 text-amber-600" /> : <ShieldCheck className="mt-0.5 h-3 w-3 shrink-0 text-emerald-700" />}
                  <span>{check.message}</span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </details>

      {action.decision_note ? <p className="mt-2 text-[12px]"><span className="font-semibold">Your note:</span> {action.decision_note}</p> : null}
      {action.field_note ? <p className="mt-1 text-[12px]"><span className="font-semibold">Field report:</span> {action.field_note}{isNumber(action.applied_mm) ? ` · applied ${action.applied_mm} mm` : ""}</p> : null}
      {action.outcome ? (
        <div className={`mt-2 rounded border px-2 py-1.5 text-[12px] ${OUTCOME_TONE[action.outcome.verdict] || OUTCOME_TONE.stable}`}>
          <span className="font-semibold">Outcome: {action.outcome.verdict.replace(/_/g, " ")}.</span> {action.outcome.message}
        </div>
      ) : null}

      <div className="mt-3 flex flex-wrap gap-2">
        {action.status === "pending_approval" ? (
          <>
            <Button size="sm" disabled={busy} onClick={() => onApprove(action)}><ThumbsUp className="mr-1.5 h-3.5 w-3.5" />Approve</Button>
            <Button size="sm" variant="outline" disabled={busy} onClick={() => onDialog({ kind: "reject", action })}><ThumbsDown className="mr-1.5 h-3.5 w-3.5" />Reject</Button>
          </>
        ) : null}
        {action.status === "approved" || action.status === "monitoring" ? (
          <Button size="sm" variant="outline" disabled={busy} onClick={() => onDialog({ kind: "complete", action })}>
            <ClipboardCheck className="mr-1.5 h-3.5 w-3.5" />{action.action_type === "irrigate" ? "Log irrigation" : "Add field report"}
          </Button>
        ) : null}
        {["approved", "completed", "monitoring"].includes(action.status) ? (
          <Button size="sm" variant="outline" disabled={busy} onClick={() => onEvaluate(action)}>
            <RefreshCw className="mr-1.5 h-3.5 w-3.5" />Check outcome now
          </Button>
        ) : null}
      </div>
    </article>
  );
}

// ---------------------------------------------------------------------------
// Simple farmer view: one status, what needs a yes/no, and the last text sent.
// ---------------------------------------------------------------------------

const zoneName = (zoneId: string | null | undefined) => (zoneId ? zoneId.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase()) : "the field");

function friendly(action: AgentAction): { title: string; reason: string } {
  switch (action.action_type) {
    case "irrigate":
      return {
        title: `Water ${isNumber(action.water_mm) ? Math.round(action.water_mm) : ""} mm${action.target_date ? ` on ${action.target_date}` : ""}`,
        reason: "Soil is drying out and not enough rain is coming.",
      };
    case "wait_for_rain":
      return { title: "Don't water, rain is coming", reason: "The forecast rain should give the crop what it needs." };
    case "inspect_zone":
      return { title: `Please check ${zoneName(action.zone_id)}`, reason: "Our readings don't agree. A quick look will tell us what's going on." };
    case "collect_sensor_data":
      return { title: "Please take a soil reading", reason: "We aren't sure enough yet to decide on watering." };
    default:
      return { title: "All good, no water needed", reason: "We'll check again tomorrow." };
  }
}

const FRIENDLY_PHASE: Record<string, string> = {
  observe: "Looking at your field",
  validate: "Getting the latest data",
  diagnose: "Checking soil, drought and crop health",
  plan: "Checking the weather",
  propose: "Deciding what to do",
  notify: "Sending you a text",
};

function SimpleView({
  run,
  actions,
  messages,
  contact,
  busyAction,
  onApprove,
  onDialog,
}: {
  run: AgentRun | null;
  actions: AgentAction[];
  messages: AgentMessage[];
  contact: string;
  busyAction: string | null;
  onApprove: (action: AgentAction) => void;
  onDialog: (state: DialogState) => void;
}) {
  if (!run) {
    return <p className="rounded-md border border-dashed border-border px-3 py-4 text-[13px] text-muted-foreground">No checks yet. Press "Check now" to start.</p>;
  }
  if (run.status === "running") {
    const phase = run.steps[run.steps.length - 1]?.phase || "observe";
    return (
      <div className="flex items-center gap-3 rounded-md border border-sky-200 bg-sky-50 px-3 py-3 text-sky-950">
        <Loader2 className="h-5 w-5 shrink-0 animate-spin" />
        <div>
          <p className="text-[15px] font-semibold">Checking your field…</p>
          <p className="text-[12px] opacity-80">{FRIENDLY_PHASE[phase] || "Working"}</p>
        </div>
      </div>
    );
  }

  // One card per kind of request, newest first, so an older duplicate never shows twice.
  const seen = new Set<string>();
  const todo = actions
    .filter((action) => ["pending_approval", "approved"].includes(action.status) && action.action_type !== "monitor")
    .filter((action) => {
      const key = `${action.action_type}:${action.zone_id || ""}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  const runActions = actions.filter((action) => action.run_id === run.run_id);
  const main = runActions.find((action) => action.status === "pending_approval") || runActions[0];
  // When the latest check ended with a question, the card points to it instead of repeating it.
  const waiting = main?.status === "pending_approval";
  const status = run.status === "failed"
    ? { title: "Couldn't finish the check", reason: "Please try again in a few minutes." }
    : waiting ? { title: "Needs your attention", reason: "Please answer the question below." }
    : main ? friendly(main) : { title: run.headline || "Check finished", reason: "" };
  const meta = waiting
    ? { tone: "border-amber-200 bg-amber-50 text-amber-950", icon: AlertTriangle }
    : main && run.status !== "failed" ? ACTION_META[main.action_type] || ACTION_META.monitor : ACTION_META.monitor;
  const Icon = run.status === "failed" ? AlertTriangle : meta.icon;
  const lastText = messages[0];
  const textLines = lastText ? plainText(lastText.body).split("\n").filter((line, index) => index > 0 && !line.startsWith("Hi ")) : [];

  return (
    <div className="space-y-2.5">
      <div className={`flex items-start gap-3 rounded-md border px-3 py-3 ${run.status === "failed" ? "border-rose-200 bg-rose-50 text-rose-950" : meta.tone}`}>
        <Icon className="mt-0.5 h-6 w-6 shrink-0" />
        <div className="min-w-0">
          <p className="text-[16px] font-bold leading-6">{status.title}</p>
          {status.reason ? <p className="text-[13px] leading-5 opacity-90">{status.reason}</p> : null}
          <p className="mt-1 text-[11px] opacity-70">Checked {relativeTime(run.finished_at || run.started_at)}</p>
        </div>
      </div>

      {todo.length ? (
        <div className="space-y-1.5">
          <p className="text-[12px] font-semibold text-foreground">Needs your answer</p>
          {todo.map((action) => {
            const copy = friendly(action);
            const busy = busyAction === action.action_id;
            return (
              <div key={action.action_id} className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border bg-background px-3 py-2">
                <div className="min-w-0">
                  <p className="text-[14px] font-semibold">{copy.title}</p>
                  <p className="text-[12px] text-muted-foreground">
                    {action.status === "approved" ? (action.action_type === "irrigate" ? "Approved. Tell us when it's done." : "Approved. Tell us what you found.") : copy.reason}
                  </p>
                </div>
                <div className="flex gap-2">
                  {action.status === "pending_approval" ? (
                    <>
                      <Button size="sm" disabled={busy} onClick={() => onApprove(action)}><ThumbsUp className="mr-1.5 h-3.5 w-3.5" />Yes</Button>
                      <Button size="sm" variant="outline" disabled={busy} onClick={() => onDialog({ kind: "reject", action })}><ThumbsDown className="mr-1.5 h-3.5 w-3.5" />No</Button>
                    </>
                  ) : (
                    <Button size="sm" variant="outline" disabled={busy} onClick={() => onDialog({ kind: "complete", action })}>
                      <ClipboardCheck className="mr-1.5 h-3.5 w-3.5" />{action.action_type === "irrigate" ? "Done" : "Add note"}
                    </Button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      ) : null}

      {lastText ? (
        <div className="flex items-start gap-2 text-[12px] text-muted-foreground">
          <MessageSquare className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <p>
            <span className="font-semibold text-foreground">Last message to {contact}</span> · {channelLabel(lastText)} · {relativeTime(lastText.created_at)}
            {lastText.delivery_status !== "sent" ? <span className="text-amber-800"> · not delivered</span> : null}
            <br />
            {textLines.join(" ")}
          </p>
        </div>
      ) : null}
    </div>
  );
}

export function FieldAgentPanel({ fieldId, farmName, embedded = false }: { fieldId: string; farmName?: string; embedded?: boolean }) {
  const { toast } = useToast();
  const [status, setStatus] = useState<AgentStatus | null>(null);
  const [runs, setRuns] = useState<AgentRunSummary[]>([]);
  const [run, setRun] = useState<AgentRun | null>(null);
  const [actions, setActions] = useState<AgentAction[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [starting, setStarting] = useState(false);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [farmerMessage, setFarmerMessage] = useState("");
  const [dialog, setDialog] = useState<DialogState>(null);
  const [dialogNote, setDialogNote] = useState("");
  const [dialogMm, setDialogMm] = useState("");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settings, setSettings] = useState<AgentSettings | null>(null);
  const [showTrace, setShowTrace] = useState(true);
  const [showDetails, setShowDetails] = useState(false);
  const [messages, setMessages] = useState<AgentMessage[]>([]);
  const [contactName, setContactName] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  const [telegramLink, setTelegramLink] = useState<{ url: string; code: string; bot: string } | null>(null);
  const telegramPollRef = useRef<number | null>(null);
  const pollRef = useRef<number | null>(null);

  const fail = useCallback((title: string, error: unknown) => {
    toast({ title, description: error instanceof Error ? error.message : String(error), variant: "destructive" });
  }, [toast]);

  const loadOverview = useCallback(async (preferRunId?: string) => {
    const [nextRuns, nextActions, nextMessages] = await Promise.all([
      fieldAgentApi.listRuns(fieldId),
      fieldAgentApi.listActions(fieldId),
      fieldAgentApi.listMessages(fieldId, 3),
    ]);
    setRuns(nextRuns);
    setActions(nextActions);
    setMessages(nextMessages);
    const target = preferRunId || nextRuns[0]?.run_id || null;
    setSelectedRunId(target);
    setRun(target ? await fieldAgentApi.getRun(target) : null);
  }, [fieldId]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setRun(null);
    setRuns([]);
    setActions([]);
    setMessages([]);
    fieldAgentApi.getSettings(fieldId).then((value) => !cancelled && setContactName(value.contact_name)).catch(() => undefined);
    Promise.all([fieldAgentApi.status().then((value) => !cancelled && setStatus(value)), loadOverview()])
      .catch((error) => !cancelled && fail("Field agent unavailable", error))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [fieldId, loadOverview, fail]);

  // Live trace: poll while the selected run is still working.
  useEffect(() => {
    if (pollRef.current) window.clearTimeout(pollRef.current);
    if (!run || run.status !== "running") return;
    pollRef.current = window.setTimeout(async () => {
      try {
        const next = await fieldAgentApi.getRun(run.run_id);
        setRun(next);
        if (next.status !== "running") {
          await loadOverview(next.run_id);
          if (next.status === "failed") fail("Agent run failed", next.error);
        }
      } catch (error) {
        fail("Could not refresh agent trace", error);
      }
    }, POLL_MS);
    return () => {
      if (pollRef.current) window.clearTimeout(pollRef.current);
    };
  }, [run, loadOverview, fail]);

  const startRun = async () => {
    setStarting(true);
    try {
      const result = await fieldAgentApi.startRun(fieldId, farmerMessage.trim() || undefined);
      setFarmerMessage("");
      setSelectedRunId(result.run_id);
      setRun(await fieldAgentApi.getRun(result.run_id));
      setShowTrace(true);
      if (result.already_running) toast({ title: "Agent is already working on this field", description: "Showing the live run." });
    } catch (error) {
      fail("Could not start agent", error);
    } finally {
      setStarting(false);
    }
  };

  const selectRun = async (runId: string) => {
    setSelectedRunId(runId);
    try {
      setRun(await fieldAgentApi.getRun(runId));
    } catch (error) {
      fail("Could not load run", error);
    }
  };

  const withAction = async (action: AgentAction, work: () => Promise<unknown>, success: string) => {
    setBusyAction(action.action_id);
    try {
      await work();
      toast({ title: success });
      await loadOverview(selectedRunId || undefined);
    } catch (error) {
      fail("Action failed", error);
    } finally {
      setBusyAction(null);
    }
  };

  const approve = (action: AgentAction) => withAction(action, () => fieldAgentApi.decide(action.action_id, "approve"), "Thanks! We'll check back on how it went.");

  const evaluate = (action: AgentAction) =>
    withAction(
      action,
      async () => {
        const result = await fieldAgentApi.evaluate(action.action_id);
        if (result.follow_up_run?.run_id) {
          setSelectedRunId(result.follow_up_run.run_id);
          setRun(await fieldAgentApi.getRun(result.follow_up_run.run_id));
        }
      },
      "Outcome measured. The agent is re-planning with the result.",
    );

  const submitDialog = async () => {
    if (!dialog) return;
    const { action } = dialog;
    const note = dialogNote.trim();
    const mm = dialogMm.trim() ? Number(dialogMm) : null;
    setDialog(null);
    if (dialog.kind === "reject") {
      await withAction(action, () => fieldAgentApi.decide(action.action_id, "reject", note), "Got it. We'll keep that in mind.");
    } else {
      await withAction(action, () => fieldAgentApi.complete(action.action_id, note, isNumber(mm) ? mm : null), "Saved. Thanks!");
    }
    setDialogNote("");
    setDialogMm("");
  };

  const openSettings = async () => {
    try {
      setSettings(await fieldAgentApi.getSettings(fieldId));
      setSettingsOpen(true);
    } catch (error) {
      fail("Could not load settings", error);
    }
  };

  const saveSettings = async () => {
    if (!settings) return;
    try {
      const saved = await fieldAgentApi.updateSettings(fieldId, settings);
      setSettings(saved);
      setContactName(saved.contact_name);
      setSettingsOpen(false);
      toast({ title: "Agent settings saved", description: "The agent and its safety policy use them from the next cycle." });
    } catch (error) {
      fail("Could not save settings", error);
    }
  };

  const stopTelegramPoll = useCallback(() => {
    if (telegramPollRef.current) window.clearInterval(telegramPollRef.current);
    telegramPollRef.current = null;
  }, []);

  useEffect(() => stopTelegramPoll, [stopTelegramPoll]);

  const connectTelegram = async () => {
    try {
      const { url, code, bot } = await fieldAgentApi.connectTelegram(fieldId);
      setTelegramLink({ url, code, bot });
      window.open(url, "_blank", "noopener");
      // Wait for the farmer to press Start in Telegram (up to 3 minutes).
      stopTelegramPoll();
      const startedAt = Date.now();
      telegramPollRef.current = window.setInterval(async () => {
        if (Date.now() - startedAt > 180000) {
          stopTelegramPoll();
          return;
        }
        try {
          const latest = await fieldAgentApi.getSettings(fieldId);
          if (latest.telegram_chat_id) {
            stopTelegramPoll();
            setTelegramLink(null);
            setSettings((current) => (current ? { ...current, telegram_chat_id: latest.telegram_chat_id, telegram_name: latest.telegram_name } : latest));
            toast({ title: "Telegram connected", description: `Alerts will go to ${latest.telegram_name || "your"} Telegram.` });
          }
        } catch {
          // keep waiting
        }
      }, 3000);
    } catch (error) {
      fail("Could not connect Telegram", error);
    }
  };

  const disconnectTelegram = async () => {
    try {
      const latest = await fieldAgentApi.disconnectTelegram(fieldId);
      setSettings((current) => (current ? { ...current, telegram_chat_id: null, telegram_name: null } : latest));
      toast({ title: "Telegram disconnected" });
    } catch (error) {
      fail("Could not disconnect Telegram", error);
    }
  };

  const sendTest = async () => {
    if (!settings) return;
    setTesting(true);
    try {
      const saved = await fieldAgentApi.updateSettings(fieldId, settings);
      setSettings(saved);
      setContactName(saved.contact_name);
      const message = await fieldAgentApi.sendTestMessage(fieldId);
      setMessages(await fieldAgentApi.listMessages(fieldId, 3));
      if (message.delivery_status === "sent") toast({ title: `Test message sent on ${channelLabel(message)}` });
      else toast({ title: "Test message not delivered", description: message.error || undefined, variant: message.delivery_status === "failed" ? "destructive" : "default" });
    } catch (error) {
      fail("Could not send test message", error);
    } finally {
      setTesting(false);
    }
  };

  const visibleActions = useMemo(() => {
    const current = actions.filter((action) => VISIBLE_STATUSES.has(action.status));
    const open = current.filter((action) => action.status !== "evaluated");
    const evaluated = current.filter((action) => action.status === "evaluated").slice(0, 2);
    return [...open, ...evaluated];
  }, [actions]);
  const pendingCount = actions.filter((action) => action.status === "pending_approval").length;
  const running = run?.status === "running";

  return (
    <section className={embedded ? "bg-background" : "rounded-md border border-border bg-background"} aria-label="Field Resilience Agent">
      <header className="flex flex-wrap items-start justify-between gap-3 border-b border-border/70 px-3 py-2.5">
        <div className={`min-w-0 items-start gap-2.5 ${embedded ? "hidden" : "flex"}`}>
          <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-emerald-700 text-white"><Bot className="h-4 w-4" /></span>
          <div className="min-w-0">
            <h3 className="text-[14px] font-extrabold uppercase tracking-[0.1em] text-foreground">Field Assistant</h3>
            <p className="text-[12px] text-muted-foreground">
              Checks {farmName || "this field"} every day{contactName ? ` and messages ${contactName} after each check` : ""}.
            </p>
          </div>
        </div>
        {embedded ? (
          <p className="min-w-0 flex-1 self-center text-[12px] leading-4 text-muted-foreground">
            {nextCheckLabel(runs, status) ? <>Next check <span className="font-semibold text-foreground">{nextCheckLabel(runs, status)}</span></> : "Checks every day"}
            {contactName ? <><br />{contactName} gets a message after each check.</> : null}
          </p>
        ) : null}
        <div className="flex items-center gap-2">
          <Button size="sm" variant="ghost" onClick={openSettings} aria-label="Agent settings"><Settings2 className="h-4 w-4" /></Button>
          <Button size="sm" onClick={startRun} disabled={starting || running || loading}>
            {starting || running ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <Play className="mr-1.5 h-3.5 w-3.5" />}
            {running ? "Checking…" : "Check now"}
          </Button>
        </div>
      </header>

      <div className="space-y-3 px-3 py-3">
        {loading ? (
          <p className="flex items-center gap-2 text-[12px] text-muted-foreground"><Loader2 className="h-3.5 w-3.5 animate-spin" />Loading…</p>
        ) : (
          <SimpleView
            run={run}
            actions={actions}
            messages={messages}
            contact={contactName || "farmer"}
            busyAction={busyAction}
            onApprove={approve}
            onDialog={(state) => {
              setDialogNote("");
              setDialogMm(state?.action.water_mm ? String(state.action.water_mm) : "");
              setDialog(state);
            }}
          />
        )}

        <button
          type="button"
          onClick={() => setShowDetails((value) => !value)}
          className="flex items-center gap-1 text-[12px] font-semibold text-muted-foreground hover:text-foreground"
          aria-expanded={showDetails}
        >
          <ChevronDown className={`h-3.5 w-3.5 transition-transform ${showDetails ? "rotate-180" : ""}`} />
          {showDetails ? "Hide details" : "Show details (how the agent decided)"}
        </button>

        {showDetails ? (
        <div className="space-y-3 border-t border-border/70 pt-3">
        <p className="text-[11px] text-muted-foreground">
          {modeLabel(run, status)}{status?.monitor?.running ? ` · autonomous checks every ${status.monitor.interval_hours ?? 24} h` : ""}
          {pendingCount ? ` · ${pendingCount} awaiting approval` : ""}
        </p>
        <div className="flex flex-col gap-2 sm:flex-row">
          <Input
            value={farmerMessage}
            onChange={(event) => setFarmerMessage(event.target.value)}
            placeholder="Tell the agent something (optional), e.g. “Pump is down until Friday” or “Leaves curling in the north corner”"
            className="h-8 text-[12px]"
            maxLength={1000}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !running && !starting) void startRun();
            }}
          />
        </div>

        {loading ? (
          <p className="flex items-center gap-2 text-[12px] text-muted-foreground"><Loader2 className="h-3.5 w-3.5 animate-spin" />Loading…</p>
        ) : !run ? (
          <div className="rounded-md border border-dashed border-border px-3 py-4 text-[13px] text-muted-foreground">
            No monitoring cycles yet for this field. Run the agent to watch it observe, validate evidence, diagnose risk and propose actions.
          </div>
        ) : (
          <>
            <CycleStrip run={run} actions={actions} />

            {run.status !== "running" ? (
              <div className="rounded-md border border-border/70 bg-muted/30 px-3 py-2.5">
                <div className="flex flex-wrap items-center gap-2">
                  {run.risk_level ? <span className={`rounded-full border px-2 py-0.5 text-[11px] font-bold uppercase ${RISK_TONE[run.risk_level] || RISK_TONE.moderate}`}>{run.risk_level} risk</span> : null}
                  <ConfidenceBar value={run.confidence} />
                  <span className="text-[11px] text-muted-foreground">
                    {triggerLabel(run.trigger)} · {relativeTime(run.finished_at || run.started_at)} · {run.tool_calls} tool calls{duration(run) ? ` · ${duration(run)}` : ""}
                  </span>
                </div>
                {run.status === "failed" ? (
                  <p className="mt-1.5 text-[13px] text-rose-800">Run failed: {run.error}</p>
                ) : (
                  <>
                    <p className="mt-1.5 text-[14px] font-semibold leading-5 text-foreground">{run.headline}</p>
                    {run.summary && run.summary !== run.headline ? <p className="mt-1 text-[13px] leading-5 text-muted-foreground">{run.summary}</p> : null}
                  </>
                )}
              </div>
            ) : null}

            {visibleActions.length ? (
              <div className="space-y-2">
                <p className="dashboard-label">Recommendations</p>
                {visibleActions.map((action) => (
                  <ActionCard
                    key={action.action_id}
                    action={action}
                    busy={busyAction === action.action_id}
                    onApprove={approve}
                    onDialog={(state) => {
                      setDialogNote("");
                      setDialogMm(state?.action.water_mm ? String(state.action.water_mm) : "");
                      setDialog(state);
                    }}
                    onEvaluate={evaluate}
                  />
                ))}
              </div>
            ) : null}

            <FarmerMessages messages={messages} contact={contactName || "farmer"} />

            <div className="rounded-md border border-border/70">
              <button
                type="button"
                className="flex w-full items-center justify-between px-3 py-2 text-left"
                onClick={() => setShowTrace((value) => !value)}
                aria-expanded={showTrace}
              >
                <span className="dashboard-label">Agent reasoning trace ({run.steps.length} steps)</span>
                <ChevronRight className={`h-4 w-4 transition-transform ${showTrace ? "rotate-90" : ""}`} />
              </button>
              {showTrace ? <div className="border-t border-border/70 px-2 py-2"><TraceTimeline run={run} /></div> : null}
            </div>

            {runs.length > 1 ? (
              <details className="text-[12px]">
                <summary className="cursor-pointer font-semibold text-muted-foreground">Previous cycles ({runs.length})</summary>
                <ul className="mt-1.5 space-y-1">
                  {runs.map((item) => (
                    <li key={item.run_id}>
                      <button
                        type="button"
                        onClick={() => void selectRun(item.run_id)}
                        className={`w-full rounded px-2 py-1 text-left hover:bg-muted ${item.run_id === selectedRunId ? "bg-muted font-semibold" : ""}`}
                      >
                        <span className="text-muted-foreground">{triggerLabel(item.trigger)} · {relativeTime(item.started_at)} · </span>
                        {item.status === "running" ? "running…" : item.status === "failed" ? `failed` : item.headline}
                      </button>
                    </li>
                  ))}
                </ul>
              </details>
            ) : null}
          </>
        )}
        </div>
        ) : null}
      </div>

      <Dialog open={dialog !== null} onOpenChange={(open) => !open && setDialog(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{dialog?.kind === "reject" ? "Why not?" : dialog?.action.action_type === "irrigate" ? "How much did you water?" : "What did you see?"}</DialogTitle>
            <DialogDescription>
              {dialog?.kind === "reject"
                ? "Optional. It helps us suggest something better next time."
                : "A short note is enough. We use it on the next check."}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            {dialog?.kind === "complete" && dialog.action.action_type === "irrigate" ? (
              <div className="space-y-1">
                <Label htmlFor="agent-applied-mm">Water applied (mm)</Label>
                <Input id="agent-applied-mm" type="number" min={0} step={0.5} value={dialogMm} onChange={(event) => setDialogMm(event.target.value)} />
              </div>
            ) : null}
            <div className="space-y-1">
              <Label htmlFor="agent-note">{dialog?.kind === "reject" ? "Reason" : "Note"}</Label>
              <Textarea
                id="agent-note"
                value={dialogNote}
                onChange={(event) => setDialogNote(event.target.value)}
                placeholder={dialog?.kind === "reject" ? "e.g. Pump under repair until Friday" : "e.g. Soil moist at 20 cm; curling only on the NE edge, looks like frost"}
                rows={3}
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDialog(null)}>Cancel</Button>
            <Button onClick={() => void submitDialog()}>{dialog?.kind === "reject" ? "Send" : "Save"}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={settingsOpen} onOpenChange={setSettingsOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Agent settings</DialogTitle>
            <DialogDescription>The agent plans within these limits, and the safety policy blocks anything outside them.</DialogDescription>
          </DialogHeader>
          {settings ? (
            <div className="space-y-3">
              <div className="space-y-1">
                <Label htmlFor="agent-max-mm">Maximum water per application (mm)</Label>
                <Input id="agent-max-mm" type="number" min={1} max={150} value={settings.max_application_mm}
                  onChange={(event) => setSettings({ ...settings, max_application_mm: Number(event.target.value) })} />
              </div>
              <div className="space-y-1">
                <Label htmlFor="agent-min-conf">Minimum confidence to recommend irrigation (0–1)</Label>
                <Input id="agent-min-conf" type="number" min={0} max={1} step={0.05} value={settings.min_confidence_for_irrigation}
                  onChange={(event) => setSettings({ ...settings, min_confidence_for_irrigation: Number(event.target.value) })} />
              </div>
              <div className="flex items-center justify-between gap-3">
                <Label htmlFor="agent-monitor">Autonomous daily monitoring</Label>
                <Switch id="agent-monitor" checked={settings.monitor_enabled} onCheckedChange={(checked) => setSettings({ ...settings, monitor_enabled: checked })} />
              </div>
              <div className="space-y-1">
                <Label htmlFor="agent-notes">Notes for the agent</Label>
                <Textarea id="agent-notes" rows={2} value={settings.farmer_notes || ""} placeholder="e.g. Water licence limits us to 25 mm per week"
                  onChange={(event) => setSettings({ ...settings, farmer_notes: event.target.value || null })} />
              </div>
              <div className="space-y-2 border-t border-border/70 pt-3">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <Label htmlFor="agent-alerts">Message the field owner after each check</Label>
                    <p className="text-[11px] text-muted-foreground">What the check found, any warnings (rain, wet or dry soil, drought risk, crop health), questions for you, and the next check time.</p>
                  </div>
                  <Switch id="agent-alerts" checked={settings.alerts_enabled} onCheckedChange={(checked) => setSettings({ ...settings, alerts_enabled: checked })} />
                </div>
                <div className="rounded-md border border-border/70 px-3 py-2">
                  {settings.telegram_chat_id ? (
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <p className="text-[13px]"><CheckCircle2 className="mr-1 inline h-4 w-4 text-emerald-700" />Telegram connected{settings.telegram_name ? ` (${settings.telegram_name})` : ""}</p>
                      <Button size="sm" variant="ghost" onClick={() => void disconnectTelegram()}>Disconnect</Button>
                    </div>
                  ) : (
                    <div className="space-y-1.5">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <p className="text-[13px]">Get warnings on <span className="font-semibold">Telegram</span> (free)</p>
                        <Button size="sm" onClick={() => void connectTelegram()} disabled={!status?.telegram_enabled}>
                          <Send className="mr-1.5 h-3.5 w-3.5" />Connect Telegram
                        </Button>
                      </div>
                      {telegramLink ? (
                        <div className="rounded bg-muted/60 px-2 py-1.5 text-[12px]">
                          <p>
                            Press <span className="font-semibold">Start</span> in Telegram, or send this code to{" "}
                            <a href={telegramLink.url} target="_blank" rel="noreferrer" className="font-semibold underline">@{telegramLink.bot}</a>:
                          </p>
                          <p className="mt-1 font-mono text-[18px] font-bold tracking-[0.2em] text-foreground">{telegramLink.code}</p>
                          <p className="mt-0.5 text-[11px] text-muted-foreground"><Loader2 className="mr-1 inline h-3 w-3 animate-spin" />Waiting for Telegram…</p>
                        </div>
                      ) : null}
                    </div>
                  )}
                </div>
                <div className="grid gap-2 sm:grid-cols-2">
                  <div className="space-y-1">
                    <Label htmlFor="agent-contact-name">Owner name</Label>
                    <Input id="agent-contact-name" value={settings.contact_name || ""} placeholder="e.g. Sam" maxLength={120}
                      onChange={(event) => setSettings({ ...settings, contact_name: event.target.value })} />
                  </div>
                  <div className="space-y-1">
                    <Label htmlFor="agent-contact-phone">Mobile number (SMS backup)</Label>
                    <Input id="agent-contact-phone" type="tel" value={settings.contact_phone || ""} placeholder="+19025550123" maxLength={30}
                      onChange={(event) => setSettings({ ...settings, contact_phone: event.target.value })} />
                  </div>
                </div>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <p className="text-[11px] text-muted-foreground">
                    {settings.telegram_chat_id ? "Updates go to Telegram." : status?.sms_enabled ? "Updates go by SMS." : "No channel connected yet; updates are saved in AgriLens only."}
                  </p>
                  <Button size="sm" variant="outline" onClick={() => void sendTest()} disabled={testing || (!settings.contact_phone && !settings.telegram_chat_id)}>
                    {testing ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <Send className="mr-1.5 h-3.5 w-3.5" />}
                    Send test message
                  </Button>
                </div>
              </div>
            </div>
          ) : null}
          <DialogFooter>
            <Button variant="outline" onClick={() => setSettingsOpen(false)}>Cancel</Button>
            <Button onClick={() => void saveSettings()}>Save</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}
