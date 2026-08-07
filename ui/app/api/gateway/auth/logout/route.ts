import { gatewayInternalUrl, setCookieValues } from "../../_proxy";

function cookieValue(cookieHeader: string, name: string): string | undefined {
  return cookieHeader
    .split(";")
    .map((part) => part.trim())
    .map((part) => part.split("=", 2))
    .find(([key]) => key === name)?.[1];
}


export async function POST(request: Request) {
  const cookieHeader = request.headers.get("cookie") ?? "";
  const csrfCookieName =
    process.env.GATEWAY_CSRF_COOKIE ?? "alerts_gateway_csrf";
  const csrfToken = cookieValue(cookieHeader, csrfCookieName);

  const upstream = await fetch(gatewayInternalUrl("/auth/logout"), {
    method: "POST",
    headers: {
      cookie: cookieHeader,
      accept: "application/json",
      ...(csrfToken ? { "x-csrf-token": csrfToken } : {}),
    },
    cache: "no-store",
  });

  const responseHeaders = new Headers({
    "content-type": upstream.headers.get("content-type") ?? "application/json",
    "cache-control": "no-store",
  });
  for (const value of setCookieValues(upstream.headers)) {
    responseHeaders.append("set-cookie", value);
  }

  return new Response(await upstream.text(), {
    status: upstream.status,
    headers: responseHeaders,
  });
}
