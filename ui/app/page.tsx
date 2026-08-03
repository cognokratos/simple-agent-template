"use client";

import {
  AssistantRuntimeProvider,
  AuiIf,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
} from "@assistant-ui/react";
import { useChatRuntime } from "@assistant-ui/react-ai-sdk";

function UserMessage() {
  return (
    <MessagePrimitive.Root className="message-row user-row">
      <div className="message user-message">
        <MessagePrimitive.Parts />
      </div>
    </MessagePrimitive.Root>
  );
}

function AssistantMessage() {
  return (
    <MessagePrimitive.Root className="message-row assistant-row">
      <div className="avatar" aria-hidden="true">
        AI
      </div>
      <div className="message assistant-message">
        <MessagePrimitive.Parts />
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
            <h1>Local NeMo Agent</h1>
            <p>assistant-ui → NeMo Agent Toolkit → Ollama</p>
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
              placeholder="Ask the local agent…"
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
