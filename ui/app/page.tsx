"use client";

import { useEffect, useMemo, useState } from "react";
import {
  AssistantRuntimeProvider,
  AuiIf,
  ComposerPrimitive,
  MessagePartPrimitive,
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

const ToolCallCard: ToolCallMessagePartComponent = ({
  toolName,
  argsText,
  result,
  status,
}) => {
  const [expanded, setExpanded] = useState(true);
  const running = status?.type === "running" || result === undefined;

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

function AssistantText() {
  return (
    <p className="assistant-text">
      <MessagePartPrimitive.Text />
    </p>
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
            if (part.type === "text") return <AssistantText />;
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
            <h1>Alert Investigation Agent</h1>
            <p>Keycloak → Rust gateway → NAT ReAct → Rust MCP → Postgres</p>
            <div className="scenario-list">
              <code>Show me my open alerts</code>
              <code>Tell me more about alert ALT-1001</code>
              <code>Show all transactions for all open alerts</code>
            </div>
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
              placeholder="Ask about alerts…"
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
        <h1>Alert Investigation Agent</h1>
        <p>
          Sign in through Keycloak before accessing alert and transaction data.
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
