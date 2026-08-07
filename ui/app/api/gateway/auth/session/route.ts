import { gatewayInternalUrl } from "../../_proxy";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const upstream = await fetch(gatewayInternalUrl("/auth/session"), {
    method: "GET",
    headers: {
      cookie: request.headers.get("cookie") ?? "",
      accept: "application/json",
    },
    cache: "no-store",
  });

  return new Response(await upstream.text(), {
    status: upstream.status,
    headers: {
      "content-type": upstream.headers.get("content-type") ?? "application/json",
      "cache-control": "no-store",
    },
  });
}
