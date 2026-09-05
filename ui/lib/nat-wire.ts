/**
 * NAT /v1/workflow/full wraps workflow output as SSE JSON:
 *   data: {"value":"<answer string>"}
 *
 * Once the outer envelope has been parsed, a string `value` is assistant text.
 * Do NOT JSON.parse scalar-looking text such as "100", "5", "true", or
 * "20250401": doing so changes the token's runtime type and can make adapters
 * accidentally drop it. We only parse nested JSON containers because some NAT
 * workflow outputs can themselves serialize a structured ChatResponse object.
 */
export function parseNatOutputString(value: string): unknown {
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
