"use client";

import { useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  AssistantRuntimeProvider,
  AuiIf,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  type ToolCallMessagePartComponent,
} from "@assistant-ui/react";
import {
  AssistantChatTransport,
  useChatRuntime,
} from "@assistant-ui/react-ai-sdk";

type AuthenticatedUser = {
  id: string;
  username: string;
  email?: string;
  name?: string;
  roles: string[];
};

type SessionState =
  | { status: "loading" }
  | { status: "anonymous" }
  | { status: "authenticated"; user: AuthenticatedUser }
  | { status: "error"; message: string };

type ApprovalPrompt = {
  input_type?: string;
  text?: string;
  placeholder?: string | null;
  required?: boolean;
  options?: Array<{ id?: string; label?: string; value?: unknown; description?: string }>;
};

type ApprovalArgs = {
  executionId?: string;
  interactionId?: string;
  prompt?: ApprovalPrompt;
};

function parseArgs(argsText: string): ApprovalArgs {
  try {
    return JSON.parse(argsText) as ApprovalArgs;
  } catch {
    return {};
  }
}

function pretty(value: unknown): string {
  if (typeof value === "string") {
    try {
      return JSON.stringify(JSON.parse(value), null, 2);
    } catch {
      return value;
    }
  }
  return JSON.stringify(value, null, 2);
}

function HumanApprovalCard({ argsText }: { argsText: string }) {
  const args = parseArgs(argsText);
  const prompt = args.prompt ?? {};
  const [rationale, setRationale] = useState("");
  const [state, setState] = useState<"pending" | "submitting" | "submitted" | "error">("pending");
  const [message, setMessage] = useState<string>("");

  const submit = async (response: Record<string, unknown>) => {
    if (!args.executionId || !args.interactionId || state === "submitting" || state === "submitted") return;
    setState("submitting");
    setMessage("");
    try {
      const result = await fetch(
        `/api/gateway/interactions/${encodeURIComponent(args.executionId)}/${encodeURIComponent(args.interactionId)}`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ response }),
        },
      );
      if (!result.ok) {
        throw new Error((await result.text()) || `Approval failed (${result.status})`);
      }
      setState("submitted");
      setMessage("Response submitted. The workflow is continuing.");
    } catch (error) {
      setState("error");
      setMessage(error instanceof Error ? error.message : "Approval submission failed");
    }
  };

  const textPrompt = prompt.input_type === "text";
  const radioPrompt = prompt.input_type === "radio";
  const options = prompt.options ?? [];
  const confirmOption = options.find((option) => option.id === "confirm") ?? { id: "confirm", label: "Confirm", value: true };
  const cancelOption = options.find((option) => option.id === "cancel") ?? { id: "cancel", label: "Cancel", value: false };
  const disabled = state === "submitting" || state === "submitted";

  return (
    <section className="approval-card">
      <div className="approval-kicker">Human approval required</div>
      <pre className="approval-summary">{prompt.text ?? "Review the proposed research decision."}</pre>
      {radioPrompt ? (
        <div className="approval-form">
          <div className="approval-choices">
            {options
              .filter((option) => option.id !== "cancel")
              .map((option) => (
                <button
                  key={String(option.id)}
                  className="approval-choice"
                  type="button"
                  disabled={disabled}
                  onClick={() =>
                    submit({
                      type: "radio",
                      selected_option: {
                        id: String(option.id),
                        label: String(option.label ?? option.id),
                        value: String(option.value ?? option.id),
                        description: String(option.description ?? ""),
                      },
                    })
                  }
                >
                  <span className="approval-choice-label">{String(option.label ?? option.id)}</span>
                  {option.description ? (
                    <span className="approval-choice-description">{option.description}</span>
                  ) : null}
                </button>
              ))}
          </div>
          <div className="approval-actions">
            <button
              className="approval-secondary"
              type="button"
              disabled={disabled}
              onClick={() =>
                submit({
                  type: "radio",
                  selected_option: {
                    id: "cancel",
                    label: "Cancel",
                    value: "__CANCEL__",
                    description: "",
                  },
                })
              }
            >
              Cancel
            </button>
          </div>
        </div>
      ) : textPrompt ? (
        <div className="approval-form">
          <label htmlFor={`rationale-${args.interactionId}`}>Override rationale</label>
          <textarea
            id={`rationale-${args.interactionId}`}
            value={rationale}
            onChange={(event) => setRationale(event.target.value)}
            placeholder={prompt.placeholder ?? "Enter an audit-ready rationale"}
            disabled={disabled}
            rows={3}
          />
          <div className="approval-actions">
            <button
              className="approval-secondary"
              type="button"
              disabled={disabled}
              onClick={() => submit({ type: "text", text: "__CANCEL__" })}
            >
              Cancel
            </button>
            <button
              className="approval-primary"
              type="button"
              disabled={disabled || rationale.trim().length === 0}
              onClick={() => submit({ type: "text", text: rationale.trim() })}
            >
              Approve override
            </button>
          </div>
        </div>
      ) : (
        <div className="approval-actions">
          <button
            className="approval-secondary"
            type="button"
            disabled={disabled}
            onClick={() => submit({ type: "binary_choice", selected_option: cancelOption })}
          >
            Cancel
          </button>
          <button
            className="approval-primary"
            type="button"
            disabled={disabled}
            onClick={() => submit({ type: "binary_choice", selected_option: confirmOption })}
          >
            Confirm action
          </button>
        </div>
      )}
      {state === "submitting" && <p className="approval-status">Submitting…</p>}
      {message && <p className={state === "error" ? "approval-error" : "approval-status"}>{message}</p>}
    </section>
  );
}

const ToolCallCard: ToolCallMessagePartComponent = ({
  toolName,
  argsText,
  result,
  status,
}) => {
  const [expanded, setExpanded] = useState(true);
  const running = status?.type === "running" || result === undefined;

  if (toolName === "human_confirmation") {
    return <HumanApprovalCard argsText={argsText} />;
  }

  return (
    <section className="tool-card">
      <button
        className="tool-card-header"
        type="button"
        onClick={() => setExpanded((value) => !value)}
        aria-expanded={expanded}
      >
        <span className={running ? "tool-status running" : "tool-status complete"} />
        <span className="tool-title">
          {running ? "Calling" : "Called"} <strong>{toolName}</strong>
        </span>
        <span className="tool-toggle">{expanded ? "Hide" : "Show"}</span>
      </button>

      {expanded && (
        <div className="tool-card-body">
          <div>
            <div className="tool-label">Input</div>
            <pre>{argsText || "{}"}</pre>
          </div>
          {result !== undefined && (
            <div>
              <div className="tool-label">Result</div>
              <pre>{pretty(result)}</pre>
            </div>
          )}
        </div>
      )}
    </section>
  );
};

function UserMessage() {
  return (
    <MessagePrimitive.Root className="message-row user-row">
      <div className="message user-message">
        <MessagePrimitive.Parts />
      </div>
    </MessagePrimitive.Root>
  );
}

/** Render the Markdown the model emits.
 *
 * `react-markdown` does **not** render raw HTML unless `rehype-raw` is added,
 * and it deliberately is not. Assistant text can quote issuer descriptions and
 * stored research notes, which are untrusted free text from outside this system,
 * so a renderer that executed embedded HTML would turn a display concern into an
 * injection vector. Markdown formatting is rendered; HTML is escaped.
 *
 * GFM is enabled for tables and strikethrough, which the model uses when
 * comparing funds. */
function AssistantText({ text }: { text: string }) {
  return (
    <div className="assistant-text">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  );
}

function AssistantMessage() {
  return (
    <MessagePrimitive.Root className="message-row assistant-row">
      <div className="avatar" aria-hidden="true">
        AI
      </div>
      <div className="message assistant-message">
        <MessagePrimitive.Parts>
          {({ part }) => {
            if (part.type === "text") return <AssistantText text={part.text} />;
            if (part.type === "tool-call") {
              return part.toolUI ?? <ToolCallCard {...part} />;
            }
            return null;
          }}
        </MessagePrimitive.Parts>
      </div>
    </MessagePrimitive.Root>
  );
}

function Chat() {
  return (
    <ThreadPrimitive.Root className="thread-root">
      <ThreadPrimitive.Viewport className="thread-viewport">
        <AuiIf condition={(state) => state.thread.isEmpty}>
          <div className="welcome">
            <h1>ETF Research Agent</h1>
            <p>Keycloak → Rust gateway → NeMo Agent Toolkit HITL → deterministic Rust evaluation engine → Postgres</p>
            <p className="scenario-hint">Try asking:</p>
            <div className="scenario-list">
              <code>Show me the highest-rated ETF candidates</code>
              <code>Compare VWCE and IWDA against my investor profile</code>
              <code>Evaluate VWCE-XETRA and explain every score component</code>
              <code>Why is AGGH-XETRA marked research instead of shortlist?</code>
              <code>Which ETFs were rejected because of hard constraints?</code>
              <code>Which ETFs still need research?</code>
              <code>Show the decision history for VWCE-XETRA</code>
              <code>Which shortlisted ETFs are currently unassigned?</code>
            </div>
            <p className="disclaimer">
              Scores measure deterministic quality and fit against a configured
              investor profile for a dated data snapshot. They are not financial
              advice, return forecasts or trade recommendations, and this system
              cannot buy, sell or hold anything.
            </p>
          </div>
        </AuiIf>

        <ThreadPrimitive.Messages>
          {({ message }) =>
            message.role === "user" ? <UserMessage /> : <AssistantMessage />
          }
        </ThreadPrimitive.Messages>

        <ThreadPrimitive.ViewportFooter className="viewport-footer">
          <ComposerPrimitive.Root className="composer">
            <ComposerPrimitive.Input
              className="composer-input"
              placeholder="Ask about an ETF, or compare two of them…"
              rows={1}
            />
            <ComposerPrimitive.Send className="send-button">
              Send
            </ComposerPrimitive.Send>
          </ComposerPrimitive.Root>
        </ThreadPrimitive.ViewportFooter>
      </ThreadPrimitive.Viewport>
    </ThreadPrimitive.Root>
  );
}

function AuthenticatedApplication({ user }: { user: AuthenticatedUser }) {
  const transport = useMemo(
    () => new AssistantChatTransport({ api: "/api/gateway/chat" }),
    [],
  );
  const runtime = useChatRuntime({ transport });
  const [loggingOut, setLoggingOut] = useState(false);

  const logout = async () => {
    setLoggingOut(true);
    try {
      const response = await fetch("/api/gateway/auth/logout", { method: "POST" });
      const payload = (await response.json()) as { logout_url?: string };
      window.location.assign(payload.logout_url ?? "/");
    } catch {
      window.location.assign("/");
    }
  };

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <main className="app-shell">
        <header className="auth-header">
          <div>
            <strong>{user.name || user.username}</strong>
            <span>{user.email || user.username}</span>
          </div>
          <button type="button" onClick={logout} disabled={loggingOut}>
            {loggingOut ? "Signing out…" : "Sign out"}
          </button>
        </header>
        <Chat />
      </main>
    </AssistantRuntimeProvider>
  );
}

function LoginScreen({ message }: { message?: string }) {
  return (
    <main className="login-shell">
      <section className="login-card">
        <div className="login-logo" aria-hidden="true">
          AI
        </div>
        <h1>ETF Research Agent</h1>
        <p>
          Sign in through Keycloak before reviewing ETF candidates and recording
          research decisions.
        </p>
        {message && <p className="login-error">{message}</p>}
        <a className="login-button" href="/api/gateway/auth/login">
          Sign in with Keycloak
        </a>
      </section>
    </main>
  );
}

export default function Home() {
  const [session, setSession] = useState<SessionState>({ status: "loading" });

  useEffect(() => {
    let active = true;
    fetch("/api/gateway/auth/session", { cache: "no-store" })
      .then(async (response) => {
        if (!response.ok) throw new Error(`Session check failed: ${response.status}`);
        return (await response.json()) as {
          authenticated: boolean;
          user?: AuthenticatedUser;
        };
      })
      .then((payload) => {
        if (!active) return;
        if (payload.authenticated && payload.user) {
          setSession({ status: "authenticated", user: payload.user });
        } else {
          setSession({ status: "anonymous" });
        }
      })
      .catch((error: unknown) => {
        if (!active) return;
        setSession({
          status: "error",
          message: error instanceof Error ? error.message : "Authentication is unavailable",
        });
      });

    return () => {
      active = false;
    };
  }, []);

  if (session.status === "loading") {
    return (
      <main className="login-shell">
        <section className="login-card">
          <p>Checking your session…</p>
        </section>
      </main>
    );
  }
  if (session.status === "authenticated") {
    return <AuthenticatedApplication user={session.user} />;
  }
  if (session.status === "error") {
    return <LoginScreen message={session.message} />;
  }
  return <LoginScreen />;
}
