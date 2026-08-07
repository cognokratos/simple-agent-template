export function gatewayInternalUrl(path: string): URL {
  const gatewayInternalUrl =
    process.env.GATEWAY_INTERNAL_URL ?? "http://gateway:8081";
  return new URL(path, gatewayInternalUrl);
}

export function setCookieValues(headers: Headers): string[] {
  const extended = headers as Headers & { getSetCookie?: () => string[] };
  const values = extended.getSetCookie?.();
  if (values && values.length > 0) return values;
  const combined = headers.get("set-cookie");
  return combined ? [combined] : [];
}

export function relayHeaders(
  upstream: Response,
  defaults: Record<string, string> = {},
): Headers {
  const headers = new Headers(defaults);
  const contentType = upstream.headers.get("content-type");
  const location = upstream.headers.get("location");
  if (contentType) headers.set("content-type", contentType);
  if (location) headers.set("location", location);
  for (const value of setCookieValues(upstream.headers)) {
    headers.append("set-cookie", value);
  }
  return headers;
}
