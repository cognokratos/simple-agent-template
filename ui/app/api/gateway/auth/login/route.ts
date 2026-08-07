import { gatewayInternalUrl, relayHeaders } from "../../_proxy";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const uiPublicUrl = process.env.UI_PUBLIC_URL ?? "http://localhost:3000";
  const upstreamUrl = gatewayInternalUrl("/auth/login");
  const requestedReturnTo = new URL(request.url).searchParams.get("return_to");
  upstreamUrl.searchParams.set("return_to", requestedReturnTo ?? uiPublicUrl);

  const upstream = await fetch(upstreamUrl, {
    method: "GET",
    headers: { accept: "text/html,application/xhtml+xml" },
    redirect: "manual",
    cache: "no-store",
  });

  return new Response(await upstream.arrayBuffer(), {
    status: upstream.status,
    headers: relayHeaders(upstream, { "cache-control": "no-store" }),
  });
}
