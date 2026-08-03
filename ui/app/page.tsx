"use client";

import { useState } from "react";
import {
  AssistantRuntimeProvider,
  AuiIf,
  ComposerPrimitive,
  MessagePartPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  type ToolCallMessagePartComponent,
} from "@assistant-ui/react";
import { useChatRuntime } from "@assistant-ui/react-ai-sdk";

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
            <p>assistant-ui → NAT ReAct → Rust MCP → Postgres</p>
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

export default function Home() {
  const runtime = useChatRuntime();

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <main className="app-shell">
        <Chat />
      </main>
    </AssistantRuntimeProvider>
  );
}
