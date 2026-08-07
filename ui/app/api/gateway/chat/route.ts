import { gatewayInternalUrl } from "../_proxy";
import {
  createUIMessageStream,
  createUIMessageStreamResponse,
  type UIMessage,
  type UIMessageChunk,
} from "ai";

export const maxDuration = 300;

type JsonRecord = Record<string, unknown>;

type NatIntermediateEnvelope = {
  id?: string;
  parent_id?: string;
  name?: string;
  type?: string;
  payload?: unknown;
};


function cookieValue(cookieHeader: string, name: string): string | undefined {
  return cookieHeader
    .split(";")
    .map((part) => part.trim())
    .map((part) => part.split("=", 2))
    .find(([key]) => key === name)?.[1];
}
type ToolState = {
  name: string;
  input: unknown;
  output?: unknown;
  started: boolean;
  finished: boolean;
};

function messageText(message: UIMessage): string {
  return message.parts
    .filter((part) => part.type === "text")
    .map((part) => part.text)
    .join("");
}

function asRecord(value: unknown): JsonRecord | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as JsonRecord)
    : undefined;
}

function parseJsonIfPossible(value: unknown): unknown {
  if (typeof value !== "string") return value;

  const trimmed = value.trim();
  if (!trimmed) return value;

  try {
    return JSON.parse(trimmed) as unknown;
  } catch {
    return value;
  }
}

function decodePythonStringAt(
  source: string,
  valueStart: number,
): { value: string; end: number } | undefined {
  let cursor = valueStart;
  while (cursor < source.length && /\s/.test(source[cursor])) cursor += 1;

  if (source.startsWith("None", cursor)) {
    return { value: "", end: cursor + 4 };
  }

  const quote = source[cursor];
  if (quote !== "'" && quote !== '"') return undefined;

  cursor += 1;
  let output = "";

  while (cursor < source.length) {
    const current = source[cursor];

    if (current === quote) {
      return { value: output, end: cursor + 1 };
    }

    if (current !== "\\") {
      output += current;
      cursor += 1;
      continue;
    }

    cursor += 1;
    if (cursor >= source.length) {
      output += "\\";
      break;
    }

    const escaped = source[cursor];
    cursor += 1;

    switch (escaped) {
      case "n":
        output += "\n";
        break;
      case "r":
        output += "\r";
        break;
      case "t":
        output += "\t";
        break;
      case "b":
        output += "\b";
        break;
      case "f":
        output += "\f";
        break;
      case "v":
        output += "\v";
        break;
      case "a":
        output += "\u0007";
        break;
      case "\\":
        output += "\\";
        break;
      case "'":
        output += "'";
        break;
      case '"':
        output += '"';
        break;
      case "x": {
        const hex = source.slice(cursor, cursor + 2);
        if (/^[0-9a-fA-F]{2}$/.test(hex)) {
          output += String.fromCodePoint(Number.parseInt(hex, 16));
          cursor += 2;
        } else {
          output += "x";
        }
        break;
      }
      case "u": {
        const hex = source.slice(cursor, cursor + 4);
        if (/^[0-9a-fA-F]{4}$/.test(hex)) {
          output += String.fromCodePoint(Number.parseInt(hex, 16));
          cursor += 4;
        } else {
          output += "u";
        }
        break;
      }
      case "U": {
        const hex = source.slice(cursor, cursor + 8);
        if (/^[0-9a-fA-F]{8}$/.test(hex)) {
          output += String.fromCodePoint(Number.parseInt(hex, 16));
          cursor += 8;
        } else {
          output += "U";
        }
        break;
      }
      case "\n":
        break;
      default:
        output += escaped;
        break;
    }
  }

  return undefined;
}

function extractPythonField(source: string, fieldNames: string[]): string | undefined {
  for (const fieldName of fieldNames) {
    let searchFrom = 0;
    const marker = `${fieldName}=`;

    while (searchFrom < source.length) {
      const markerIndex = source.indexOf(marker, searchFrom);
      if (markerIndex < 0) break;

      const decoded = decodePythonStringAt(source, markerIndex + marker.length);
      if (decoded) return decoded.value;
      searchFrom = markerIndex + marker.length;
    }
  }

  return undefined;
}

/**
 * NAT 1.8 can stringify ChatResponseChunk instead of serializing it as JSON on
 * /v1/workflow/full. This extracts the actual assistant content from both the
 * correct JSON shape and that Python/Pydantic repr fallback.
 */
function extractWorkflowText(value: unknown): string {
  if (value === null || value === undefined) return "";

  if (typeof value === "string") {
    const parsed = parseJsonIfPossible(value);
    if (parsed !== value) return extractWorkflowText(parsed);

    if (
      value.includes("ChatResponseChunkChoice(") ||
      value.includes("ChoiceDelta(content=") ||
      value.includes("ChoiceMessage(content=")
    ) {
      return (
        extractPythonField(value, ["content"]) ??
        "[NAT returned an unreadable ChatResponseChunk representation]"
      );
    }

    return value;
  }

  const record = asRecord(value);
  if (!record) return "";

  if ("value" in record) return extractWorkflowText(record.value);

  const choices = Array.isArray(record.choices) ? record.choices : [];
  const firstChoice = asRecord(choices[0]);
  if (firstChoice) {
    const delta = asRecord(firstChoice.delta);
    if (delta && typeof delta.content === "string") return delta.content;

    const message = asRecord(firstChoice.message);
    if (message && typeof message.content === "string") return message.content;
  }

  if (typeof record.content === "string") return record.content;
  return "";
}

function normalizeToolName(name: string): string {
  const segments = name.split("__");
  return segments.at(-1) || name;
}

function nestedValue(record: JsonRecord | undefined, ...path: string[]): unknown {
  let current: unknown = record;
  for (const segment of path) {
    const currentRecord = asRecord(current);
    if (!currentRecord) return undefined;
    current = currentRecord[segment];
  }
  return current;
}

function normalizeToolInput(payload: JsonRecord): unknown {
  return parseJsonIfPossible(
    nestedValue(payload, "metadata", "tool_inputs") ??
      nestedValue(payload, "data", "input") ??
      nestedValue(payload, "metadata", "span_inputs") ??
      {},
  );
}

function unwrapLangChainContent(value: unknown): unknown {
  const parsed = parseJsonIfPossible(value);
  if (parsed !== value) return unwrapLangChainContent(parsed);

  if (typeof value === "string") {
    if (value.includes("ToolMessage(") || value.includes("content=")) {
      const content = extractPythonField(value, ["content"]);
      if (content !== undefined) return parseJsonIfPossible(content);
    }
    return value;
  }

  if (Array.isArray(value)) {
    if (value.length === 1) return unwrapLangChainContent(value[0]);
    return value.map(unwrapLangChainContent);
  }

  const record = asRecord(value);
  if (!record) return value;

  if ("content" in record) {
    const content = record.content;
    if (typeof content === "string") return parseJsonIfPossible(content);
    if (Array.isArray(content)) return unwrapLangChainContent(content);
  }

  if ("output" in record && Object.keys(record).length <= 4) {
    return unwrapLangChainContent(record.output);
  }

  return value;
}

function normalizeToolOutput(payload: JsonRecord): unknown {
  const value =
    nestedValue(payload, "data", "output") ??
    nestedValue(payload, "data", "payload") ??
    nestedValue(payload, "metadata", "tool_outputs") ??
    nestedValue(payload, "metadata", "span_outputs") ??
    null;

  return unwrapLangChainContent(value);
}

function toolChunk(chunk: UIMessageChunk): UIMessageChunk {
  return chunk;
}

function parseIntermediateEnvelope(block: string): NatIntermediateEnvelope | undefined {
  const rawEnvelope = block.slice("intermediate_data:".length).trim();
  const parsed = parseJsonIfPossible(rawEnvelope);
  return asRecord(parsed) as NatIntermediateEnvelope | undefined;
}

function parseIntermediatePayload(envelope: NatIntermediateEnvelope): JsonRecord {
  const parsed = parseJsonIfPossible(envelope.payload ?? {});
  return asRecord(parsed) ?? {};
}

function inferToolName(envelope: NatIntermediateEnvelope, payload: JsonRecord): string {
  const candidates = [
    envelope.name,
    payload.name,
    nestedValue(payload, "metadata", "tool_info", "name"),
    nestedValue(payload, "data", "payload", "name"),
  ];

  const candidate = candidates.find((value): value is string =>
    typeof value === "string" && value.length > 0,
  );

  return normalizeToolName(candidate ?? "unknown_tool");
}

function isKnownToolName(name: string): boolean {
  const configured = (process.env.UI_TOOL_NAMES ?? "search_alerts,get_alert")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);
  return configured.includes(normalizeToolName(name));
}

export async function POST(request: Request) {
  const body = (await request.json()) as { messages?: UIMessage[] };

  if (!Array.isArray(body.messages)) {
    return Response.json({ error: "messages must be an array" }, { status: 400 });
  }

  const messages = body.messages
    .map((message) => ({
      role: message.role,
      content: messageText(message),
    }))
    .filter((message) => message.content.length > 0);

  if (messages.length === 0) {
    return Response.json(
      { error: "at least one text message is required" },
      { status: 400 },
    );
  }

  const workflowUrl = gatewayInternalUrl("/api/chat");
  const cookieHeader = request.headers.get("cookie") ?? "";
  const csrfCookieName =
    process.env.GATEWAY_CSRF_COOKIE ?? "alerts_gateway_csrf";
  const csrfToken = cookieValue(cookieHeader, csrfCookieName);

  const stream = createUIMessageStream({
    execute: async ({ writer }) => {
      writer.write({ type: "start" });
      writer.write({ type: "start-step" });

      const tools = new Map<string, ToolState>();
      const answerPartId = crypto.randomUUID();
      let answerStarted = false;
      let answerFinished = false;

      const emitToolStart = (
        toolCallId: string,
        toolName: string,
        input: unknown,
      ) => {
        const existing = tools.get(toolCallId);
        if (existing?.started) return;

        tools.set(toolCallId, {
          name: toolName,
          input,
          output: existing?.output,
          started: true,
          finished: existing?.finished ?? false,
        });

        writer.write(
          toolChunk({
            type: "tool-input-available",
            toolCallId,
            toolName,
            input,
            providerExecuted: true,
            dynamic: true,
            title: `Calling ${toolName}`,
          }),
        );
      };

      const emitToolEnd = (
        toolCallId: string,
        toolName: string,
        input: unknown,
        output: unknown,
      ) => {
        const existing = tools.get(toolCallId);
        if (!existing?.started) emitToolStart(toolCallId, toolName, input);
        if (tools.get(toolCallId)?.finished) return;

        const current = tools.get(toolCallId);
        tools.set(toolCallId, {
          name: current?.name ?? toolName,
          input: current?.input ?? input,
          output,
          started: true,
          finished: true,
        });

        writer.write(
          toolChunk({
            type: "tool-output-available",
            toolCallId,
            output,
            providerExecuted: true,
            dynamic: true,
          }),
        );
      };

      const emitAnswerChunk = (text: string) => {
        if (!text || answerFinished) return;

        if (!answerStarted) {
          writer.write({ type: "text-start", id: answerPartId });
          answerStarted = true;
        }

        writer.write({ type: "text-delta", id: answerPartId, delta: text });
      };

      const finishAnswer = () => {
        if (answerFinished) return;
        if (answerStarted) writer.write({ type: "text-end", id: answerPartId });
        answerFinished = true;
      };

      try {
        const upstream = await fetch(workflowUrl, {
          method: "POST",
          headers: {
            "content-type": "application/json",
            accept: "text/event-stream",
            cookie: cookieHeader,
            ...(csrfToken ? { "x-csrf-token": csrfToken } : {}),
          },
          body: JSON.stringify({ messages }),
          signal: request.signal,
        });

        if (!upstream.ok || upstream.body === null) {
          const details = await upstream.text();
          throw new Error(
            `NAT workflow returned ${upstream.status}: ${details || upstream.statusText}`,
          );
        }

        const reader = upstream.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        const processEvent = (eventBlock: string) => {
          const block = eventBlock.trim();
          if (!block) return;

          if (block.startsWith("intermediate_data:")) {
            const envelope = parseIntermediateEnvelope(block);
            if (!envelope) return;

            const payload = parseIntermediatePayload(envelope);
            const eventType = String(envelope.type ?? payload.event_type ?? "").toUpperCase();
            const toolCallId = String(envelope.id ?? payload.UUID ?? crypto.randomUUID());
            const toolName = inferToolName(envelope, payload);
            const isToolEvent = eventType.startsWith("TOOL_");
            const isToolFunctionEvent =
              eventType.startsWith("FUNCTION_") && isKnownToolName(toolName);

            if (!isToolEvent && !isToolFunctionEvent) return;

            const input = normalizeToolInput(payload);

            if (eventType.endsWith("_START")) {
              emitToolStart(toolCallId, toolName, input);
              return;
            }

            if (eventType.endsWith("_END")) {
              const existing = tools.get(toolCallId);
              emitToolEnd(
                toolCallId,
                existing?.name ?? toolName,
                existing?.input ?? input,
                normalizeToolOutput(payload),
              );
            }
            return;
          }

          if (block.startsWith("data:")) {
            const rawData = block.slice("data:".length).trim();
            if (!rawData || rawData === "[DONE]") return;

            const data = parseJsonIfPossible(rawData);
            const value = asRecord(data)?.value ?? data;
            const text = extractWorkflowText(value);
            emitAnswerChunk(text);
            return;
          }

          // NAT emits workflow errors as a plain JSON object rather than an SSE
          // field. Surface it instead of silently dropping it.
          if (block.startsWith("{")) {
            const error = parseJsonIfPossible(block);
            const errorRecord = asRecord(error);
            throw new Error(
              String(
                errorRecord?.message ??
                  errorRecord?.details ??
                  "NAT workflow failed",
              ),
            );
          }
        };

        while (true) {
          const { value, done } = await reader.read();
          buffer += decoder
            .decode(value, { stream: !done })
            .replaceAll("\r\n", "\n");

          let boundary = buffer.indexOf("\n\n");
          while (boundary >= 0) {
            const eventBlock = buffer.slice(0, boundary);
            buffer = buffer.slice(boundary + 2);
            processEvent(eventBlock);
            boundary = buffer.indexOf("\n\n");
          }

          if (done) break;
        }

        if (buffer.trim()) processEvent(buffer);
        finishAnswer();
        writer.write({ type: "finish-step" });
        writer.write({ type: "finish", finishReason: "stop" });
      } catch (error) {
        finishAnswer();
        const errorText =
          error instanceof Error ? error.message : "The NAT workflow failed";
        writer.write({ type: "error", errorText });
        writer.write({ type: "finish-step" });
        writer.write({ type: "finish", finishReason: "error" });
      }
    },
  });

  return createUIMessageStreamResponse({ stream });
}
