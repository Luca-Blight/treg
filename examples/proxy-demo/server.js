#!/usr/bin/env node
/**
 * treg proxy demo — a tiny app that calls a real API it has no key for.
 *
 *   node server.js        → the call goes out as-is. No key, so the vendor refuses.
 *   treg node server.js   → the same code, credentialed by your team, server-side.
 *
 * Nothing in this file mentions treg, reads a secret, or changes between the two runs. That is the
 * whole point: an ordinary app, and the credential appears only because treg is the parent process.
 *
 * Zero dependencies — node core only. Open http://localhost:3000 and click a button.
 */

import http from "node:http";
import https from "node:https";
import tls from "node:tls";
import fs from "node:fs";

const PORT = Number(process.env.PORT || 3000);

// The two calls the buttons make. One host is registered with your team, one is not — so you can see
// both halves of the promise: the first is credentialed, the second is untouched and unread.
const TARGETS = {
  openai: { url: "https://api.openai.com/v1/models", label: "api.openai.com", note: "registered with your team" },
  example: { url: "https://example.com/", note: "not registered — goes straight out", label: "example.com" },
};

/**
 * Node's built-in `fetch` IGNORES HTTPS_PROXY until Node 24 (`NODE_USE_ENV_PROXY`). On Node 24+ the
 * plain `fetch` below is all you need. On anything older a proxy has to be spoken to explicitly, which
 * is what this does: CONNECT to it, then run TLS over the tunnel it opens.
 *
 * Real apps get this from their HTTP library (axios, got, undici's ProxyAgent, python-requests, curl —
 * all read the environment already). It is written out here only so the demo runs on any Node.
 */
function throughProxy(targetUrl, proxyUrl) {
  const proxy = new URL(proxyUrl);
  const target = new URL(targetUrl);
  const auth = Buffer.from(`${decodeURIComponent(proxy.username)}:${decodeURIComponent(proxy.password)}`).toString("base64");

  return new Promise((resolve, reject) => {
    const req = http.request({
      host: proxy.hostname,
      port: proxy.port,
      method: "CONNECT",
      path: `${target.hostname}:443`,
      headers: { "Proxy-Authorization": `Basic ${auth}`, Host: `${target.hostname}:443` },
    });

    req.on("connect", (res, socket) => {
      if (res.statusCode !== 200) {
        socket.destroy();
        return reject(new Error(`the proxy answered ${res.statusCode} to CONNECT`));
      }
      // Trust the bundle treg gave us (system roots + its own local authority) for this connection.
      const caFile = process.env.SSL_CERT_FILE || process.env.NODE_EXTRA_CA_CERTS;
      const secure = tls.connect(
        { socket, servername: target.hostname, ca: caFile ? fs.readFileSync(caFile) : undefined },
        () => {
          secure.write(
            `GET ${target.pathname}${target.search} HTTP/1.1\r\n` +
              `Host: ${target.hostname}\r\nAccept: application/json\r\n` +
              `User-Agent: treg-proxy-demo\r\nConnection: close\r\n\r\n`,
          );
        },
      );
      let raw = "";
      secure.on("data", (chunk) => (raw += chunk));
      secure.on("end", () => {
        const [head, ...rest] = raw.split("\r\n\r\n");
        const status = Number(head.split(" ")[1]) || 0;
        const body = rest.join("\r\n\r\n");
        resolve({ status, body: /transfer-encoding:\s*chunked/i.test(head) ? unchunk(body) : body });
      });
      secure.on("error", reject);
    });
    req.on("error", reject);
    req.end();
  });
}

/**
 * Undo HTTP/1.1 chunked framing: `<hex length>\r\n<bytes>\r\n…0\r\n\r\n`. Speaking HTTP by hand means
 * unwrapping it by hand too — without this the page shows `e5 … 0` around the real JSON. An HTTP
 * library would have done this for you; see the note above.
 */
function unchunk(body) {
  let out = "";
  let rest = body;
  while (rest) {
    const nl = rest.indexOf("\r\n");
    if (nl < 0) break;
    const size = parseInt(rest.slice(0, nl).split(";")[0], 16);
    if (!Number.isFinite(size) || size === 0) break;
    out += rest.slice(nl + 2, nl + 2 + size);
    rest = rest.slice(nl + 2 + size + 2);
  }
  return out || body;
}

/** Straight out, no proxy — what `node server.js` on its own does. */
function direct(targetUrl) {
  return new Promise((resolve, reject) => {
    https
      .get(targetUrl, { headers: { Accept: "application/json", "User-Agent": "treg-proxy-demo" } }, (res) => {
        let body = "";
        res.on("data", (c) => (body += c));
        res.on("end", () => resolve({ status: res.statusCode, body }));
      })
      .on("error", reject);
  });
}

async function callTarget(which) {
  const target = TARGETS[which];
  const proxy = process.env.HTTPS_PROXY || process.env.https_proxy;
  const started = Date.now();
  const result = proxy ? await throughProxy(target.url, proxy) : await direct(target.url);
  return { ...result, url: target.url, ms: Date.now() - started, viaTreg: Boolean(proxy) };
}

const PAGE = `<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>treg proxy demo</title>
<style>
  :root { --bg:#14120f; --card:#1c1a16; --line:#2e2a24; --ink:#f0ebe3; --muted:#a99e88;
          --clay:#e0703f; --green:#7fae72; --amber:#d0a24a; --mono:ui-monospace,SFMono-Regular,Menlo,monospace }
  * { box-sizing:border-box }
  body { margin:0; padding:48px 20px; background:var(--bg); color:var(--ink);
         font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; display:flex; justify-content:center }
  main { width:100%; max-width:720px }
  h1 { font-size:20px; margin:0 0 4px; font-weight:650 }
  h1 span { color:var(--clay) }
  p.sub { color:var(--muted); margin:0 0 28px; font-size:13.5px }
  .mode { display:inline-flex; align-items:center; gap:8px; padding:6px 12px; border-radius:999px;
          border:1px solid var(--line); background:var(--card); font-size:12.5px; margin-bottom:24px }
  .dot { width:7px; height:7px; border-radius:50%; background:var(--muted) }
  .on .dot { background:var(--green) } .on { color:var(--green) }
  .row { display:flex; gap:10px; flex-wrap:wrap; margin-bottom:22px }
  button { font:inherit; font-size:13.5px; padding:10px 16px; border-radius:8px; cursor:pointer;
           border:1px solid var(--line); background:var(--card); color:var(--ink); transition:.15s }
  button:hover:not(:disabled) { border-color:var(--clay) }
  button.primary { background:var(--clay); border-color:var(--clay); color:#1b1207; font-weight:600 }
  button:disabled { opacity:.5; cursor:default }
  .out { border:1px solid var(--line); border-radius:10px; background:var(--card); overflow:hidden }
  .out header { display:flex; gap:12px; align-items:center; padding:11px 14px; border-bottom:1px solid var(--line);
                font-size:12.5px; color:var(--muted) }
  .pill { font-family:var(--mono); font-size:11.5px; padding:2px 8px; border-radius:5px; border:1px solid var(--line) }
  .ok { color:var(--green); border-color:#37502f } .bad { color:var(--amber); border-color:#4a3a1c }
  pre { margin:0; padding:14px; font-family:var(--mono); font-size:12px; line-height:1.55;
        white-space:pre-wrap; word-break:break-word; max-height:340px; overflow:auto; color:#d8d2c6 }
  code { font-family:var(--mono); color:var(--clay); font-size:.92em }
</style>
<main>
  <h1>▚ <span>treg</span> proxy demo</h1>
  <p class="sub">This app has no API key and no treg code. Click a button and see who answers.</p>

  <div class="mode" id="mode"><span class="dot"></span><span id="modeText">checking…</span></div>

  <div class="row">
    <button class="primary" data-t="openai">Call api.openai.com <small>(registered)</small></button>
    <button data-t="example">Call example.com <small>(not registered)</small></button>
  </div>

  <div class="out">
    <header>
      <span id="what">no call yet</span>
      <span style="margin-left:auto" id="meta"></span>
    </header>
    <pre id="body">Run this app two ways and compare:

  node server.js        → no key, so the vendor refuses
  treg node server.js   → the same code, credentialed by your team</pre>
  </div>
</main>
<script>
  const $ = (id) => document.getElementById(id);

  fetch("/mode").then(r => r.json()).then(m => {
    $("mode").className = "mode" + (m.viaTreg ? " on" : "");
    $("modeText").textContent = m.viaTreg
      ? "running under treg — registered hosts are credentialed by your team"
      : "running plain — every call goes out as-is, with no credentials";
  });

  document.querySelectorAll("button[data-t]").forEach(b => b.onclick = async () => {
    const buttons = document.querySelectorAll("button[data-t]");
    buttons.forEach(x => x.disabled = true);
    $("what").textContent = "calling…"; $("meta").textContent = ""; $("body").textContent = "";
    try {
      const r = await fetch("/call/" + b.dataset.t);
      const d = await r.json();
      $("what").innerHTML = '<span class="pill">' + d.url + "</span>";
      $("meta").innerHTML =
        '<span class="pill ' + (d.status >= 200 && d.status < 300 ? "ok" : "bad") + '">HTTP ' + d.status + "</span>" +
        ' <span class="pill">' + d.ms + " ms</span>" +
        ' <span class="pill">' + (d.viaTreg ? "through treg" : "direct") + "</span>";
      let text = d.body || "(empty)";
      try { text = JSON.stringify(JSON.parse(text), null, 2); } catch {}
      $("body").textContent = text.slice(0, 4000);
    } catch (e) {
      $("what").textContent = "failed"; $("body").textContent = String(e);
    } finally {
      buttons.forEach(x => x.disabled = false);
    }
  });
</script>`;

http
  .createServer(async (req, res) => {
    if (req.url === "/") {
      res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      return res.end(PAGE);
    }
    if (req.url === "/mode") {
      res.writeHead(200, { "content-type": "application/json" });
      return res.end(JSON.stringify({ viaTreg: Boolean(process.env.HTTPS_PROXY || process.env.https_proxy) }));
    }
    const match = req.url?.match(/^\/call\/(\w+)$/);
    if (match && TARGETS[match[1]]) {
      try {
        const out = await callTarget(match[1]);
        res.writeHead(200, { "content-type": "application/json" });
        return res.end(JSON.stringify(out));
      } catch (err) {
        res.writeHead(200, { "content-type": "application/json" });
        return res.end(JSON.stringify({ status: 0, body: String(err), url: TARGETS[match[1]].url, ms: 0 }));
      }
    }
    res.writeHead(404).end("not found");
  })
  .listen(PORT, () => {
    const proxy = process.env.HTTPS_PROXY || process.env.https_proxy;
    console.log(`\n  treg proxy demo → http://localhost:${PORT}`);
    console.log(proxy ? "  running under treg: registered hosts will be credentialed.\n"
                      : "  running plain: no credentials. Try `treg node server.js` to compare.\n");
  });
