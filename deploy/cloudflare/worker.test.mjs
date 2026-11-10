import assert from "node:assert/strict";
import { test } from "node:test";
import worker from "./worker.js";

const PUBLIC_ORIGIN = "https://medguide.kdns.fr";
const UPSTREAM_ORIGIN = "https://8080-agq4bggjaxrxosu62udnbbxfsi.instabox.tech";

function assertNoStore(response) {
  assert.equal(response.headers.get("Cache-Control"), "private, no-store");
  assert.equal(response.headers.get("CDN-Cache-Control"), "no-store");
  assert.equal(response.headers.get("Cloudflare-CDN-Cache-Control"), "no-store");
  assert.equal(response.headers.get("Age"), null);
}

test("pins origin even for network-path input and attacker-supplied Host", async (t) => {
  t.mock.method(globalThis, "fetch", async (request, options) => {
    const target = new URL(request.url);
    assert.equal(target.origin, UPSTREAM_ORIGIN);
    assert.equal(target.pathname, "//attacker.invalid/api/chat");
    assert.equal(target.search, "?url=https://attacker.invalid/");
    assert.equal(request.headers.get("Host"), new URL(UPSTREAM_ORIGIN).host);
    assert.equal(request.headers.get("Cookie"), "medguide_demo_session=synthetic-cookie");
    assert.equal(request.headers.get("Proxy-Authorization"), null);
    assert.equal(options.redirect, "manual");
    assert.equal(options.cache, "no-store");
    assert.deepEqual(options.cf.cacheTtlByStatus, { "100-599": -1 });
    return new Response("ok");
  });
  const response = await worker.fetch(new Request(`${PUBLIC_ORIGIN}//attacker.invalid/api/chat?url=https://attacker.invalid/`, {
    headers: { Host: "attacker.invalid", Cookie: "medguide_demo_session=synthetic-cookie", "Proxy-Authorization": "synthetic-hop-secret" },
  }));
  assert.equal(await response.text(), "ok");
  assertNoStore(response);
});

test("preserves method/body/status and separate session cookies without caching", async (t) => {
  const cookies = [
    "medguide_demo_session=synthetic-token; Path=/; HttpOnly; Secure; SameSite=Strict",
    "preference=compact; Expires=Wed, 21 Oct 2037 07:28:00 GMT; Path=/; Secure",
  ];
  t.mock.method(globalThis, "fetch", async (request) => {
    assert.equal(request.method, "POST");
    assert.equal(await request.text(), '{"message":"synthetic example"}');
    const headers = new Headers({ "Cache-Control": "public, max-age=3600", Age: "42", Authorization: "synthetic-secret", "X-API-Key": "synthetic-key" });
    for (const cookie of cookies) headers.append("Set-Cookie", cookie);
    return new Response("created", { status: 201, statusText: "Created", headers });
  });
  const response = await worker.fetch(new Request(`${PUBLIC_ORIGIN}/api/auth/register`, {
    method: "POST", body: '{"message":"synthetic example"}', headers: { "Content-Type": "application/json" },
  }));
  assert.equal(response.status, 201);
  assert.equal(response.statusText, "Created");
  assert.equal(await response.text(), "created");
  assert.deepEqual(response.headers.getSetCookie(), cookies);
  assert.equal(response.headers.get("Authorization"), null);
  assert.equal(response.headers.get("X-API-Key"), null);
  assertNoStore(response);
});

test("returns SSE before stream completion and preserves cancellation signal", { timeout: 1000 }, async (t) => {
  let controller;
  let forwardedSignal;
  const abort = new AbortController();
  const stream = new ReadableStream({ start(value) { controller = value; value.enqueue(new TextEncoder().encode("event: node\ndata: first\n\n")); } });
  t.mock.method(globalThis, "fetch", async (request) => {
    forwardedSignal = request.signal;
    return new Response(stream, { headers: { "Content-Type": "text/event-stream", "X-Accel-Buffering": "no" } });
  });
  const response = await worker.fetch(new Request(`${PUBLIC_ORIGIN}/api/chat?stream=true`, { signal: abort.signal }));
  assert.equal(response.body, stream);
  assert.equal(response.headers.get("Content-Type"), "text/event-stream");
  const reader = response.body.getReader();
  assert.match(new TextDecoder().decode((await reader.read()).value), /event: node/);
  controller.enqueue(new TextEncoder().encode("event: final\ndata: done\n\n"));
  controller.close();
  assert.match(new TextDecoder().decode((await reader.read()).value), /event: final/);
  assert.equal((await reader.read()).done, true);
  abort.abort();
  assert.equal(forwardedSignal.aborted, true);
  assertNoStore(response);
});

test("rewrites only upstream absolute redirects and never follows any redirect", async (t) => {
  let location = `${UPSTREAM_ORIGIN}/login?next=%2Fchat`;
  let calls = 0;
  t.mock.method(globalThis, "fetch", async (_request, options) => {
    calls += 1;
    assert.equal(options.redirect, "manual");
    return new Response(null, { status: 302, headers: { Location: location } });
  });
  const first = await worker.fetch(new Request(`${PUBLIC_ORIGIN}/private`));
  assert.equal(first.status, 302);
  assert.equal(first.headers.get("Location"), `${PUBLIC_ORIGIN}/login?next=%2Fchat`);
  assert.equal(calls, 1);
  location = "https://external.invalid/login";
  const external = await worker.fetch(new Request(`${PUBLIC_ORIGIN}/private`));
  assert.equal(external.headers.get("Location"), location);
  assert.equal(calls, 2);
  location = "/relative-login";
  const relative = await worker.fetch(new Request(`${PUBLIC_ORIGIN}/private`));
  assert.equal(relative.headers.get("Location"), location);
  location = "https://synthetic-user:synthetic-password@external.invalid/login";
  const credentialed = await worker.fetch(new Request(`${PUBLIC_ORIGIN}/private`));
  assert.equal(credentialed.headers.get("Location"), "https://external.invalid/login");
  assert.equal(calls, 4);
  assertNoStore(credentialed);
});

test("preserves bodyless statuses and turns network errors into a private generic 502", async (t) => {
  t.mock.method(globalThis, "fetch", async () => new Response(null, { status: 204 }));
  const empty = await worker.fetch(new Request(`${PUBLIC_ORIGIN}/api/empty`, { method: "HEAD" }));
  assert.equal(empty.status, 204);
  assert.equal(empty.body, null);
  t.mock.method(globalThis, "fetch", async () => { throw new Error("synthetic-secret-must-not-escape"); });
  const failed = await worker.fetch(new Request(`${PUBLIC_ORIGIN}/api/chat`));
  assert.equal(failed.status, 502);
  assert.doesNotMatch(await failed.text(), /synthetic-secret/);
  assertNoStore(failed);
});

test("redirects plaintext entry requests to HTTPS without fetching upstream", async (t) => {
  t.mock.method(globalThis, "fetch", async () => { throw new Error("must not fetch"); });
  const response = await worker.fetch(new Request("http://medguide.kdns.fr/chat?view=compact"));
  assert.equal(response.status, 308);
  assert.equal(response.headers.get("Location"), `${PUBLIC_ORIGIN}/chat?view=compact`);
  assertNoStore(response);
});
