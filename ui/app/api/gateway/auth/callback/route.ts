import { gatewayInternalUrl, relayHeaders } from "../../_proxy";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const browserUrl = new URL(request.url);
  const upstreamUrl = gatewayInternalUrl("/auth/callback");
  upstreamUrl.search = browserUrl.search;

  const upstream = await fetch(upstreamUrl, {
    method: "GET",
    headers: {
      cookie: request.headers.get("cookie") ?? "",
      accept: "text/html,application/xhtml+xml",
    },
    redirect: "manual",
    cache: "no-store",
  });

  return new Response(await upstream.arrayBuffer(), {
    status: upstream.status,
    headers: relayHeaders(upstream, { "cache-control": "no-store" }),
  });
}
