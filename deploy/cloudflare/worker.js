// Fixed destination: neither a query parameter nor a request header can select
// another upstream. No credentials or model configuration belong in this Worker.
const UPSTREAM_ORIGIN = "https://8080-agq4bggjaxrxosu62udnbbxfsi.instabox.tech";
const HOP_BY_HOP = [
  "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
  "te", "trailer", "transfer-encoding", "upgrade",
];

function removeHopByHop(headers) {
  for (const name of (headers.get("connection") || "").split(",")) {
    if (name.trim()) headers.delete(name.trim());
  }
  for (const name of HOP_BY_HOP) headers.delete(name);
}

function preventCaching(headers) {
  headers.set("Cache-Control", "private, no-store");
  headers.set("CDN-Cache-Control", "no-store");
  headers.set("Cloudflare-CDN-Cache-Control", "no-store");
  headers.set("Pragma", "no-cache");
  headers.set("Expires", "0");
  headers.delete("Age");
}

function rewriteLocation(headers, publicUrl, upstreamUrl) {
  const location = headers.get("Location");
  if (!location) return;
  let destination;
  try {
    destination = new URL(location, upstreamUrl);
  } catch {
    return;
  }
  // Userinfo must not escape through a redirect header. MedGuide itself uses
  // host-only session cookies, which are passed through without modification.
  let changed = Boolean(destination.username || destination.password);
  destination.username = "";
  destination.password = "";
  if (/^(https?:)?\/\//i.test(location) && destination.origin === upstreamUrl.origin) {
    destination.protocol = publicUrl.protocol;
    destination.host = publicUrl.host;
    changed = true;
  }
  if (changed) headers.set("Location", destination.toString());
}

export default {
  async fetch(request) {
    const publicUrl = new URL(request.url);
    if (publicUrl.protocol === "http:") {
      publicUrl.protocol = "https:";
      const headers = new Headers({ Location: publicUrl.toString() });
      preventCaching(headers);
      return new Response(null, { status: 308, headers });
    }

    const upstreamUrl = new URL(UPSTREAM_ORIGIN);
    // Assign components separately: a path beginning // cannot replace origin.
    upstreamUrl.pathname = publicUrl.pathname;
    upstreamUrl.search = publicUrl.search;
    const upstreamRequest = new Request(upstreamUrl, request);
    removeHopByHop(upstreamRequest.headers);
    upstreamRequest.headers.set("Host", upstreamUrl.host);

    try {
      const upstream = await fetch(upstreamRequest, {
        redirect: "manual",
        cache: "no-store",
        cf: { cacheTtlByStatus: { "100-599": -1 } },
      });
      const headers = new Headers(upstream.headers);
      removeHopByHop(headers);
      // Session Set-Cookie headers are intentional; reflected service secrets
      // are not. Do not log headers, bodies, redirect URLs, or fetch errors.
      headers.delete("Authorization");
      headers.delete("Proxy-Authorization");
      headers.delete("X-API-Key");
      rewriteLocation(headers, publicUrl, upstreamUrl);
      preventCaching(headers);
      // Forward the readable stream directly, preserving SSE/backpressure.
      return new Response(upstream.body, {
        status: upstream.status,
        statusText: upstream.statusText,
        headers,
      });
    } catch {
      const headers = new Headers({
        "Content-Type": "text/plain; charset=utf-8",
        "X-Content-Type-Options": "nosniff",
      });
      preventCaching(headers);
      return new Response("MedGuide upstream is temporarily unavailable.", {
        status: 502,
        headers,
      });
    }
  },
};
