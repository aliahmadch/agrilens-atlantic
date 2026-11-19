import { API } from "./irrigationConstants";

export type AgentPhase = "observe" | "validate" | "diagnose" | "plan" | "propose" | "notify";
export type AgentStepKind = "reasoning" | "message" | "tool_call" | "tool_result" | "policy" | "error" | "notify";
export type AgentActionType = "irrigate" | "wait_for_rain" | "inspect_zone" | "collect_sensor_data" | "monitor";
export type AgentActionStatus =
  | "pending_approval"
  | "approved"
  | "rejected"
  | "completed"
  | "monitoring"
  | "evaluated"
  | "superseded";

export type AgentStep = {
  step_id: string;
  seq: number;
  phase: AgentPhase;
  kind: AgentStepKind;
  tool_name: string | null;
  title: string;
  content: Record<string, unknown>;
  duration_ms: number | null;
  created_at: string;
};

export type AgentPolicyCheck = { rule: string; passed: boolean; severity: "block" | "warn"; message: string };

export type AgentOutcome = {
  verdict: "improved" | "stable" | "worsened" | "field_report_received" | "awaiting_field_report";
  message: string;
  deltas: Record<string, number | null>;
};

export type AgentAction = {
  action_id: string;
  run_id: string;
  field_id: string;
  farm_name?: string;
  action_type: AgentActionType;
  title: string;
  rationale: string;
  evidence: { source: string; observation: string }[];
  assumptions: string[];
  confidence: number;
  priority: "high" | "medium" | "low";
  water_mm: number | null;
  volume_m3: number | null;
  target_date: string | null;
  zone_id: string | null;
  requires_approval: boolean;
  status: AgentActionStatus;
  policy_checks: AgentPolicyCheck[];
  baseline: Record<string, unknown>;
  follow_up_hours: number;
  follow_up_due_at: string | null;
  decision_note: string | null;
  decided_at: string | null;
  applied_mm: number | null;
  field_note: string | null;
  completed_at: string | null;
  outcome: AgentOutcome | null;
  evaluated_at: string | null;
  created_at: string;
};

export type AgentRunSummary = {
  run_id: string;
  field_id: string;
  trigger: "manual" | "scheduled" | "follow_up" | string;
  mode: "llm" | "rules" | "llm+rules" | string;
  status: "running" | "completed" | "failed";
  model: string | null;
  headline: string | null;
  risk_level: "low" | "moderate" | "high" | "critical" | null;
  confidence: number | null;
  tool_calls: number;
  started_at: string;
  finished_at: string | null;
  error: string | null;
  action_count?: number;
};

export type AgentRun = AgentRunSummary & {
  goal: string;
  summary: string | null;
  context: Record<string, unknown>;
  input_tokens: number;
  output_tokens: number;
  steps: AgentStep[];
  actions: AgentAction[];
};

export type AgentStatus = {
  llm_enabled: boolean;
  provider?: "claude" | "gemini" | "groq" | null;
  model: string | null;
  mode: "llm" | "rules";
  monitor: { enabled: boolean; running: boolean; last_tick_at?: string | null; interval_hours?: number };
  sms_enabled?: boolean;
  telegram_enabled?: boolean;
  telegram_bot?: string | null;
};

export type AgentSettings = {
  field_id: string;
  max_application_mm: number;
  min_confidence_for_irrigation: number;
  monitor_enabled: boolean;
  farmer_notes: string | null;
  contact_name: string | null;
  contact_phone: string | null;
  alerts_enabled: boolean;
  telegram_chat_id?: string | null;
  telegram_name?: string | null;
};

export type AgentMessageAlert = { type: string; key: string; severity: "urgent" | "info"; level: number; text: string };

export type AgentMessage = {
  message_id: string;
  field_id: string;
  run_id: string | null;
  channel: "sms" | "telegram";
  recipient_name: string | null;
  recipient: string | null; // masked by the API
  body: string;
  alerts: AgentMessageAlert[];
  is_test: boolean;
  delivery_status: "sent" | "not_sent" | "failed";
  provider_message_id: string | null;
  error: string | null;
  created_at: string;
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      // keep status text
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

export type ChatTurn = { role: "user" | "assistant"; content: string };
export type ChatReply = { reply: string; mode: "llm" | "rules"; provider?: "claude" | "gemini" | "groq"; tools_used: string[] };
export type FieldSummary = { field_id: string; farm_name: string | null; crop_type?: string | null };

export const fieldAgentApi = {
  status: () => request<AgentStatus>("/agent/status"),
  listRuns: (fieldId: string, limit = 8) => request<AgentRunSummary[]>(`/field-reports/${fieldId}/agent/runs?limit=${limit}`),
  getRun: (runId: string) => request<AgentRun>(`/agent/runs/${runId}`),
  startRun: (fieldId: string, farmerMessage?: string) =>
    request<{ run_id: string; already_running: boolean; mode?: string }>(`/field-reports/${fieldId}/agent/runs`, {
      method: "POST",
      body: JSON.stringify({ farmer_message: farmerMessage || null }),
    }),
  listActions: (fieldId: string, status?: string) =>
    request<AgentAction[]>(`/field-reports/${fieldId}/agent/actions${status ? `?status=${status}` : ""}`),
  decide: (actionId: string, decision: "approve" | "reject", note?: string) =>
    request<AgentAction>(`/agent/actions/${actionId}/decision`, { method: "POST", body: JSON.stringify({ decision, note: note || null }) }),
  complete: (actionId: string, fieldNote?: string, appliedMm?: number | null) =>
    request<AgentAction>(`/agent/actions/${actionId}/complete`, {
      method: "POST",
      body: JSON.stringify({ field_note: fieldNote || null, applied_mm: appliedMm ?? null }),
    }),
  evaluate: (actionId: string) =>
    request<{ action_id: string; outcome: AgentOutcome; follow_up_run: { run_id: string } | null }>(`/agent/actions/${actionId}/evaluate`, {
      method: "POST",
    }),
  getSettings: (fieldId: string) => request<AgentSettings>(`/field-reports/${fieldId}/agent/settings`),
  updateSettings: (fieldId: string, values: Partial<AgentSettings>) =>
    request<AgentSettings>(`/field-reports/${fieldId}/agent/settings`, { method: "PUT", body: JSON.stringify(values) }),
  listMessages: (fieldId: string, limit = 5) => request<AgentMessage[]>(`/field-reports/${fieldId}/agent/messages?limit=${limit}`),
  sendTestMessage: (fieldId: string) => request<AgentMessage>(`/field-reports/${fieldId}/agent/messages/test`, { method: "POST" }),
  connectTelegram: (fieldId: string) =>
    request<{ url: string; bot: string; code: string; expires_in_minutes: number }>(`/field-reports/${fieldId}/agent/telegram/connect`, { method: "POST" }),
  chat: (message: string, fieldId: string | null, history: ChatTurn[]) =>
    request<ChatReply>("/agent/chat", { method: "POST", body: JSON.stringify({ message, field_id: fieldId, history }) }),
  listFields: () => request<FieldSummary[]>("/field-reports?limit=100"),
  inbox: () => request<AgentAction[]>("/agent/inbox?limit=50"),
  disconnectTelegram: (fieldId: string) => request<AgentSettings>(`/field-reports/${fieldId}/agent/telegram`, { method: "DELETE" }),
};
