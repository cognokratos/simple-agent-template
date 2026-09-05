import { gatewayInternalUrl, relayHeaders } from "../../../_proxy";

function cookieValue(cookieHeader: string, name: string): string | undefined {
  return cookieHeader
    .split(";")
    .map((part) => part.trim())
    .map((part) => part.split("=", 2))
    .find(([key]) => key === name)?.[1];
}

type RouteContext = {
  params: Promise<{ executionId: string; interactionId: string }>;
};

export async function POST(request: Request, context: RouteContext) {
  const { executionId, interactionId } = await context.params;
  const cookieHeader = request.headers.get("cookie") ?? "";
  const csrfCookieName =
    process.env.GATEWAY_CSRF_COOKIE ?? "etf_research_gateway_csrf";
  const csrfToken = cookieValue(cookieHeader, csrfCookieName);
  const body = await request.text();

  const upstream = await fetch(
    gatewayInternalUrl(
      `/api/interactions/${encodeURIComponent(executionId)}/${encodeURIComponent(interactionId)}`,
    ),
    {
      method: "POST",
      headers: {
        "content-type": "application/json",
        cookie: cookieHeader,
        ...(csrfToken ? { "x-csrf-token": csrfToken } : {}),
      },
      body,
      cache: "no-store",
      signal: request.signal,
    },
  );

  // A 204/205/304 response must carry a null body: passing the (zero-length but
  // non-null) ArrayBuffer makes the Response constructor throw, which turned
  // NAT's successful "interaction accepted" 204 into a 500 in the browser.
  const upstreamBody = await upstream.arrayBuffer();
  const nullBodyStatus =
    upstream.status === 204 || upstream.status === 205 || upstream.status === 304;

  return new Response(nullBodyStatus ? null : upstreamBody, {
    status: upstream.status,
    headers: relayHeaders(upstream, { "cache-control": "no-store" }),
  });
}
