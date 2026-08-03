import { createOpenAICompatible } from "@ai-sdk/openai-compatible";
import {
  convertToModelMessages,
  createUIMessageStreamResponse,
  streamText,
  toUIMessageStream,
  type UIMessage,
} from "ai";

export const maxDuration = 300;

const nemo = createOpenAICompatible({
  name: "nemo-agent-toolkit",
  baseURL: process.env.NEMO_BASE_URL ?? "http://agent:8000/v1",
  apiKey: "unused",
  includeUsage: true,
});

export async function POST(request: Request) {
  const body = (await request.json()) as { messages?: UIMessage[] };

  if (!Array.isArray(body.messages)) {
    return Response.json({ error: "messages must be an array" }, { status: 400 });
  }

  const result = streamText({
    model: nemo(process.env.NEMO_MODEL ?? "nemo-agent"),
    messages: await convertToModelMessages(body.messages),
  });

  return createUIMessageStreamResponse({
    stream: toUIMessageStream({ stream: result.stream }),
  });
}
