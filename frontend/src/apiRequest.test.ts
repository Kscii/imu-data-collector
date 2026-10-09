import assert from "node:assert/strict";
import { createServer, type RequestListener } from "node:http";
import test from "node:test";
import { ApiRequestError, RequestTimeoutError, requestJson } from "./apiRequest.ts";

async function server(handler: RequestListener, run: (url: string) => Promise<void>) {
  const instance = createServer(handler);
  await new Promise<void>(resolve => instance.listen(0, "127.0.0.1", resolve));
  const address = instance.address();
  assert.ok(address && typeof address !== "string");
  try { await run(`http://127.0.0.1:${address.port}`); }
  finally {
    instance.closeAllConnections();
    await new Promise<void>((resolve, reject) => instance.close(error => error ? reject(error) : resolve()));
  }
}

test("请求期限包含响应头和 JSON 正文，不会无限等待正文", async () => {
  await server((req, res) => {
    if (req.url === "/headers") return;
    res.writeHead(req.url === "/error-body" ? 503 : 200, { "Content-Type": "application/json" });
    res.flushHeaders();
    if (req.url === "/slow-body") setTimeout(() => res.end('{"ok":true}'), 250).unref();
  }, async url => {
    for (const path of ["/headers", "/slow-body", "/stalled-body", "/error-body"]) {
      const started = Date.now();
      await assert.rejects(requestJson(url + path, undefined, 80), RequestTimeoutError);
      assert.ok(Date.now() - started < 1000, `${path} must settle within the deadline tolerance`);
    }
  });
});

test("成功 JSON、结构化 HTTP 错误及非 JSON 错误保持分类", async () => {
  await server((req, res) => {
    if (req.url === "/ok") return res.end('{"ok":true}');
    if (req.url === "/conflict") { res.writeHead(409); return res.end('{"detail":{"code":"conflict"}}'); }
    res.writeHead(502); res.end("gateway unavailable");
  }, async url => {
    assert.deepEqual(await requestJson(url + "/ok"), { ok: true });
    await assert.rejects(requestJson(url + "/conflict"), (e: unknown) => {
      assert.ok(e instanceof ApiRequestError);
      assert.equal(e.status, 409);
      assert.deepEqual(e.detail, { code: "conflict" });
      return true;
    });
    await assert.rejects(requestJson(url + "/html"), (e: unknown) => e instanceof ApiRequestError && e.status === 502);
  });
});

test("主动取消保留 AbortError，包括已取消的 signal 和正在读取的正文", async () => {
  await server((_req, res) => { res.writeHead(200); res.flushHeaders(); }, async url => {
    const before = new AbortController(); before.abort();
    await assert.rejects(requestJson(url, { signal: before.signal }), { name: "AbortError" });
    const during = new AbortController();
    const pending = requestJson(url, { signal: during.signal }, 500);
    const timer = setTimeout(() => during.abort(), 50);
    try { await assert.rejects(pending, { name: "AbortError" }); }
    finally { clearTimeout(timer); }
  });
});

test("成功状态的损坏 JSON 不能被当作保存成功", async () => {
  await server((_req, res) => res.end('{"incomplete":'), async url => {
    await assert.rejects(requestJson(url), SyntaxError);
  });
});
