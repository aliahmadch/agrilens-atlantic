import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useSearchParams } from "react-router-dom";
import { Bot, Loader2, SendHorizontal, Sprout, X } from "lucide-react";

import { FieldAgentPanel } from "@/components/agent/FieldAgentPanel";
import { fieldAgentApi, type ChatTurn, type FieldSummary } from "@/components/agent/fieldAgentApi";

type Tab = "chat" | "field";
type Message = ChatTurn & { id: number; failed?: boolean };

const HISTORY_TURNS = 10;
const FIELD_KEY = "agrilens.assistant.field";

const readStoredField = () => {
  try {
    return window.localStorage.getItem(FIELD_KEY);
  } catch {
    return null;
  }
};

const storeField = (fieldId: string) => {
  try {
    window.localStorage.setItem(FIELD_KEY, fieldId);
  } catch {
    // per-viewer convenience only
  }
};

const providerLabel = (provider: string | null | undefined) =>
  ({ groq: "powered by Groq", gemini: "powered by Gemini", claude: "powered by Claude" } as Record<string, string>)[provider || ""] || "basic mode";

const capitalize = (value: string | null | undefined) => (value ? value.charAt(0).toUpperCase() + value.slice(1) : "");

function TypingDots() {
  return (
    <span className="inline-flex items-center gap-1" aria-label="Assistant is typing">
      {[0, 150, 300].map((delay) => (
        <span key={delay} className="h-1.5 w-1.5 animate-bounce rounded-full bg-muted-foreground/60" style={{ animationDelay: `${delay}ms` }} />
      ))}
    </span>
  );
}

function AssistantAvatar({ size = "h-7 w-7" }: { size?: string }) {
  return (
    <span className={`flex ${size} shrink-0 items-center justify-center rounded-full bg-emerald-700 text-white`}>
      <Bot className="h-4 w-4" />
    </span>
  );
}

export default function AssistantLauncher() {
  const [searchParams] = useSearchParams();
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<Tab>("chat");
  const [fields, setFields] = useState<FieldSummary[]>([]);
  const [fieldId, setFieldId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [pendingCount, setPendingCount] = useState(0);
  const [aiLabel, setAiLabel] = useState<string>("basic mode");
  const nextId = useRef(1);
  const listRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);

  const urlField = searchParams.get("field");

  // Fields for the selector; default to the field open on the page, then the last one used.
  useEffect(() => {
    fieldAgentApi.listFields().then(setFields).catch(() => setFields([]));
  }, []);

  useEffect(() => {
    if (!fields.length) return;
    const preferred = [urlField, fieldId, readStoredField()].find((id) => id && fields.some((field) => field.field_id === id));
    setFieldId(preferred || fields[0].field_id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fields, urlField]);

  // Red dot on the icon when a recommendation is waiting for the farmer.
  const refreshInbox = useCallback(() => {
    fieldAgentApi.inbox().then((items) => setPendingCount(items.length)).catch(() => undefined);
  }, []);

  useEffect(() => {
    refreshInbox();
    const timer = window.setInterval(refreshInbox, 60000);
    return () => window.clearInterval(timer);
  }, [refreshInbox]);

  useEffect(() => {
    if (!open) return;
    refreshInbox();
    fieldAgentApi.status().then((status) => setAiLabel(providerLabel(status.llm_enabled ? status.provider : null))).catch(() => undefined);
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && setOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, refreshInbox]);

  useEffect(() => {
    if (open && tab === "chat") window.setTimeout(() => inputRef.current?.focus(), 50);
  }, [open, tab]);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, sending]);

  const selectedField = useMemo(() => fields.find((field) => field.field_id === fieldId) || null, [fields, fieldId]);
  const fieldName = capitalize(selectedField?.farm_name) || "my field";
  const suggestions = [`How is ${fieldName}?`, "Do I need to water this week?", "Will it rain soon?", "Is anything waiting for me?"];

  const send = async (text: string) => {
    const message = text.trim();
    if (!message || sending) return;
    const history: ChatTurn[] = messages.filter((item) => !item.failed).slice(-HISTORY_TURNS).map(({ role, content }) => ({ role, content }));
    setMessages((current) => [...current, { id: nextId.current++, role: "user", content: message }]);
    setInput("");
    setSending(true);
    try {
      const reply = await fieldAgentApi.chat(message, fieldId, history);
      setAiLabel(providerLabel(reply.mode === "llm" ? reply.provider : null));
      setMessages((current) => [...current, { id: nextId.current++, role: "assistant", content: reply.reply }]);
    } catch (error) {
      const detail = error instanceof Error ? error.message : "Something went wrong.";
      setMessages((current) => [...current, { id: nextId.current++, role: "assistant", content: `Sorry, I couldn't answer that. ${detail}`, failed: true }]);
    } finally {
      setSending(false);
    }
  };

  const chooseField = (id: string) => {
    setFieldId(id);
    storeField(id);
  };

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className={`relative flex h-8 w-8 items-center justify-center rounded-md border border-border text-muted-foreground hover:text-foreground ${open ? "bg-secondary text-foreground" : "bg-card"}`}
        aria-label="Open AgriLens Assistant"
        aria-expanded={open}
        title="AgriLens Assistant"
      >
        <Bot className="h-[18px] w-[18px]" />
        {pendingCount ? <span className="absolute right-1.5 top-1.5 h-2 w-2 rounded-full bg-amber-500" aria-label={`${pendingCount} waiting`} /> : null}
      </button>

      {/* Portal: the top bar's backdrop blur would otherwise trap this fixed window inside the header. */}
      {open ? createPortal(
        <div
          role="dialog"
          aria-label="AgriLens Assistant"
          className="fixed inset-x-2 bottom-2 top-14 z-[1300] flex flex-col overflow-hidden rounded-xl border border-border bg-background shadow-2xl sm:inset-x-auto sm:bottom-auto sm:right-4 sm:top-14 sm:h-[min(700px,calc(100vh-4.5rem))] sm:w-[440px]"
        >
          <header className="flex items-center gap-3 border-b border-border px-4 py-3">
            <AssistantAvatar size="h-9 w-9" />
            <div className="min-w-0 flex-1">
              <p className="text-[14px] font-semibold leading-5 text-foreground">AgriLens Assistant</p>
              <p className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
                <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
                Online · {aiLabel}
              </p>
            </div>
            {fields.length ? (
              <select
                value={fieldId || ""}
                onChange={(event) => chooseField(event.target.value)}
                className="h-8 max-w-[140px] rounded-md border border-border bg-card px-2 text-[12px] text-foreground"
                aria-label="Field"
              >
                {fields.map((field) => (
                  <option key={field.field_id} value={field.field_id}>{capitalize(field.farm_name) || field.field_id}</option>
                ))}
              </select>
            ) : null}
            <button type="button" onClick={() => setOpen(false)} className="rounded-md p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground" aria-label="Close assistant">
              <X className="h-4 w-4" />
            </button>
          </header>

          <nav className="flex gap-1 border-b border-border px-3 py-2" aria-label="Assistant sections">
            {([["chat", "Chat"], ["field", "Field status"]] as const).map(([key, label]) => (
              <button
                key={key}
                type="button"
                onClick={() => setTab(key)}
                className={`relative rounded-md px-3 py-1.5 text-[12px] font-semibold ${tab === key ? "bg-secondary text-foreground" : "text-muted-foreground hover:text-foreground"}`}
                aria-pressed={tab === key}
              >
                {label}
                {key === "field" && pendingCount ? <span className="ml-1.5 rounded-full bg-amber-100 px-1.5 text-[10px] text-amber-900">{pendingCount}</span> : null}
              </button>
            ))}
          </nav>

          {tab === "chat" ? (
            <>
              <div ref={listRef} className="flex-1 space-y-3 overflow-y-auto px-4 py-4">
                {!messages.length ? (
                  <div className="flex h-full flex-col items-center justify-center text-center">
                    <span className="flex h-12 w-12 items-center justify-center rounded-full bg-emerald-50 text-emerald-700"><Sprout className="h-6 w-6" /></span>
                    <p className="mt-3 text-[15px] font-semibold text-foreground">Hi! Ask me about your fields.</p>
                    <p className="mt-1 max-w-[280px] text-[12px] text-muted-foreground">I use your latest AgriLens data to tell you how your crops are doing and what needs attention.</p>
                    <div className="mt-4 flex flex-wrap justify-center gap-2">
                      {suggestions.map((suggestion) => (
                        <button
                          key={suggestion}
                          type="button"
                          onClick={() => void send(suggestion)}
                          className="rounded-full border border-border bg-card px-3 py-1.5 text-[12px] text-foreground hover:border-emerald-600 hover:bg-emerald-50"
                        >
                          {suggestion}
                        </button>
                      ))}
                    </div>
                  </div>
                ) : (
                  messages.map((message) =>
                    message.role === "user" ? (
                      <div key={message.id} className="flex justify-end">
                        <p className="max-w-[80%] whitespace-pre-wrap rounded-2xl rounded-tr-sm bg-emerald-700 px-3.5 py-2 text-[13px] leading-5 text-white">{message.content}</p>
                      </div>
                    ) : (
                      <div key={message.id} className="flex items-end gap-2">
                        <AssistantAvatar />
                        <p className={`max-w-[80%] whitespace-pre-wrap rounded-2xl rounded-tl-sm px-3.5 py-2 text-[13px] leading-5 ${message.failed ? "bg-rose-50 text-rose-900" : "bg-muted text-foreground"}`}>
                          {message.content}
                        </p>
                      </div>
                    ),
                  )
                )}
                {sending ? (
                  <div className="flex items-end gap-2">
                    <AssistantAvatar />
                    <span className="rounded-2xl rounded-tl-sm bg-muted px-3.5 py-2.5"><TypingDots /></span>
                  </div>
                ) : null}
              </div>

              <form
                className="border-t border-border px-3 pb-3 pt-2"
                onSubmit={(event) => {
                  event.preventDefault();
                  void send(input);
                }}
              >
                <div className="flex items-end gap-2 rounded-xl border border-border bg-card px-3 py-2 focus-within:border-emerald-600">
                  <textarea
                    ref={inputRef}
                    value={input}
                    onChange={(event) => setInput(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" && !event.shiftKey) {
                        event.preventDefault();
                        void send(input);
                      }
                    }}
                    rows={1}
                    maxLength={500}
                    placeholder={`Ask about ${fieldName}…`}
                    className="max-h-28 min-h-[22px] flex-1 resize-none bg-transparent text-[13px] leading-5 text-foreground outline-none placeholder:text-muted-foreground"
                    aria-label="Message"
                  />
                  <button
                    type="submit"
                    disabled={!input.trim() || sending}
                    className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-emerald-700 text-white disabled:opacity-40"
                    aria-label="Send"
                  >
                    {sending ? <Loader2 className="h-4 w-4 animate-spin" /> : <SendHorizontal className="h-4 w-4" />}
                  </button>
                </div>
              </form>
            </>
          ) : (
            <div className="flex-1 overflow-y-auto">
              {fieldId ? (
                <FieldAgentPanel key={fieldId} fieldId={fieldId} farmName={selectedField?.farm_name || undefined} embedded />
              ) : (
                <p className="p-4 text-[13px] text-muted-foreground">No fields yet.</p>
              )}
            </div>
          )}
        </div>,
        document.body,
      ) : null}
    </>
  );
}
