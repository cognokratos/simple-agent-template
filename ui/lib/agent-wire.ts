/**
 * Helpers for the agent's streaming wire format (docs/AGENT-SERVICE-CONTRACT.md).
 *
 * The agent's `/v1/workflow/full` endpoint wraps workflow output as SSE JSON:
 *   data: {"value":"<answer fragment>"}
 *
 * Once that outer envelope has been parsed, a string `value` is already
 * assistant text. It must NOT be JSON-parsed again.
 *
 * This matters because a great many single assistant tokens are *also* valid
 * JSON scalars: "100", "5", "0", "true", "null", "20250401". Parsing them
 * changes the token's runtime type from string to number/boolean/null, and the
 * downstream UI adapter — which only forwards string deltas — then silently
 * drops them. The visible symptom is an answer with every number, boolean and
 * date fragment missing: "ticket TKT- has  history events, priority ."
 *
 * Only genuine JSON *containers* are decoded, because a workflow output may
 * legitimately serialize a structured ChatResponse object into `value`.
 */
export function parseAgentOutputString(value: string): unknown {
  const trimmed = value.trim();
  if (!trimmed) return value;

  const looksLikeObject = trimmed.startsWith("{") && trimmed.endsWith("}");
  const looksLikeArray = trimmed.startsWith("[") && trimmed.endsWith("]");
  if (!looksLikeObject && !looksLikeArray) return value;

  try {
    return JSON.parse(trimmed) as unknown;
  } catch {
    return value;
  }
}
