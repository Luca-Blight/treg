---
title: Local proxy — catch the agent's own outgoing calls (`treg shell --proxy`)
status: shipped
sources:
  - src/treg/localproxy.py
related:
  - architecture/proxy-model.md
  - interface/shell.md
  - architecture/local-run.md
---

# The local proxy (`src/treg/localproxy.py`)

Everything else in treg works only when the agent is **told** to use it — `treg call`, `treg run`, the
`/call/` URL. The moment an agent writes its own script that talks to `api.stripe.com` directly, treg is
invisible and the script has no key. The local proxy closes that gap: `HTTPS_PROXY` plus a certificate
authority generated on the member's machine catch the call on the way out, and the **server** adds the
credential.

**Not `proxy.py`.** That is the server relay (`relay()`), the thing that injects. This is the local
catcher that feeds it — two ends of one call. The module is named for `localrun.py`, its neighbour in
"code that runs on the member's machine". Plan + build log: `docs/LOCAL-PROXY-PLAN.md`.

## What we took from oneCLI, and what we refused
Their design has two separable halves. We keep the capture mechanism (`HTTPS_PROXY` + own CA) and throw
away the injection half: their gateway holds `SECRET_ENCRYPTION_KEY` and decrypts secrets **on the
laptop**. That is the exact situation treg exists to prevent. A future "inject locally under `treg run`"
mode is deliberately out of scope — deciding it later costs nothing, getting it wrong now costs the
product's promise.

## The path a call takes
`handle_client` reads the request head, checks `Proxy-Authorization` (`authorized`, `compare_digest`),
then branches on `CONNECT`:

- **not on the allow-list → `_blind_tunnel`.** Bytes copied both ways, never decrypted, no certificate
  ever signed for that host. This is also what keeps us out of the agent's own `api.anthropic.com` /
  `api.openai.com` traffic.
- **on the allow-list → `_intercept`.** Answer `200 Connection established`, upgrade the socket with
  `StreamWriter.start_tls` using a leaf from `CertAuthority.context_for(host)`, read the plaintext
  request, and re-address it to `{base}/call/https://{host}{path}` (`_treg_url`) — the **URL-passthrough**
  shape, so the proxy never needs to know a tool's NAME. `_forward_headers` drops transport headers and
  the caller's `x-treg-*`, then adds our `X-Treg-Token` / `X-Treg-Org` / `X-Treg-Client`. The answer
  streams back inside the same TLS connection (`_write_response`), and keep-alive is honoured.

Because the call lands on the ordinary `/call/` path, the per-member tool list, project scope, deny
rules, daily caps, the audit record and OAuth refresh all apply with no second copy — see
[proxy-model](proxy-model.md). That is why this module is small and there is no second policy engine.

`_forward_plain` handles absolute-form `http://` (we set `HTTP_PROXY` too, so an http caller must not
break) and strips `Proxy-Authorization` before the upstream sees it.

## The certificate authority (`CertAuthority`, `ensure_ca`, `build_bundle`)
Generated **per machine** on first use: ECDSA P-256, 2 years, self-signed, `BasicConstraints(ca=True)`.
The private key file is created 0600 **before any bytes go in** — writing first and chmod-ing after
leaves a window where a private key is world-readable. It regenerates when the files are unreadable or
inside the last 30 days (`_RENEW_WITHIN_DAYS`), so an expiry never surfaces mid-session as an
unexplained TLS error; `treg shell start --renew-ca` forces it.

`build_bundle` writes **the system roots plus ours** (`certifi`, which ships with httpx, falling back to
OpenSSL's configured file). Appending is the point: `SSL_CERT_FILE` REPLACES the trust list, so a bundle
holding only our CA would leave the agent unable to verify the real internet.

Leaves are signed on demand with one reused key, cached per host as an `ssl.SSLContext`. Python's
`load_cert_chain` only reads from a file, so the leaf is written to a 0600 temp file that is deleted the
moment it is loaded.

**The system trust store is never touched.** Trust is scoped by environment variables, so only the
agent's process tree trusts us — not the browser, not the operating system. oneCLI does the same; there
is no `add-trusted-cert` anywhere in their repo.

## The environment (`proxy_env`)
`HTTPS_PROXY`/`HTTP_PROXY` (both letter cases — curl reads lowercase), `NO_PROXY` covering loopback and
the registry host, the bundle under `NODE_EXTRA_CA_CERTS` / `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` /
`CURL_CA_BUNDLE` / `GIT_SSL_CAINFO` / `DENO_CERT` / `AWS_CA_BUNDLE`, and **`NODE_USE_ENV_PROXY=1`** —
since Node 18 the built-in `fetch` silently ignores proxy variables, so without that flag every Node
agent walks straight past the proxy and the feature looks intermittently broken.

## The allow-list (`fetch_hosts`, `refresh_hosts_forever`)
Hosts come from `GET /tools`, which is already filtered to what **this member** may use — so the
allow-list inherits the per-member tool list and project scope for free. A failed fetch returns an empty
set (no answer must never mean "intercept everything"), and the refresher never applies an empty answer
over a working list, which would silently stop injecting for five minutes. `treg shell --proxy` seeds it
from the tools it already fetched for its shims, so starting the proxy costs no extra request.

## Errors an agent can act on (`treg_error_message`, `_explain_treg_error`)
A raw 404 from treg reads as "the vendor has no such endpoint", and an agent "fixes" it by rewriting a
perfectly good URL. So treg's own refusals are replaced with a message naming treg and the next action
(register the tool / ask an admin / `treg login` / the cap is used up).

This fires **only** on `X-Treg-Error`, the header `api.py`'s `_mark_treg_own_errors` puts on treg's own
`/call/` refusals — otherwise a genuine vendor 404 would be rewritten too. Against an older registry that
does not send it, replies pass through untouched. treg being unreachable answers `502` naming treg and
saying the vendor call was **not** made, so an agent does not blame a healthy vendor and retry forever.

## The non-negotiables (each has a test)
1. CA private key per machine, 0600, never shipped or shared.
2. Never touch the system trust store.
3. Allow-list only — everything else is a blind tunnel.
4. Bind `127.0.0.1` only (`LISTEN_HOST`), never `0.0.0.0`.
5. A random proxy token per session (`mint_token`) carried in the proxy URL, so another local process
   cannot spend the member's quota through us. `_CALLER_MUST_NOT_SET` likewise stops a caller naming its
   own identity.
6. **`trust_env=False`** on the proxy's own client (`treg_client`). httpx reads `HTTPS_PROXY` from its
   own environment, and inside a treg shell that points at THIS proxy — the first intercepted call would
   loop straight back into us.
7. Never log bodies, headers or the token — host and status only (`_log`).

## Lifecycle
`serve()` binds on the current loop; `start()` wraps it in a daemon thread for the synchronous CLI and
returns a `ProxyHandle` (`.env()`, `.stop()`). `stop()` cancels the in-flight handlers before stopping
the loop — a tunnel is a long-lived task, and killing the loop under one prints an `asyncio` complaint
the user cannot act on. Wiring lives in [shell](../interface/shell.md).

## Two front doors
Same engine, two ways in. **`treg shell start --proxy`** runs it inside a subshell: the token lives only
in that shell's environment and both end together. **`treg serve`** runs it as a background service for
people who want their own shell — `start` / `stop` / `status` / `env`, where `eval "$(treg serve env)"`
points a terminal at it and `--unset` reverses that (stopping the service cannot reach into a shell that
already has the variables).

A service must be findable by other terminals, so `write_state` records port, pid, token, registry and
captured hosts in `~/.treg/proxy/proxy.json` at mode **0600**, created before any bytes go in. That file
is the whole extra risk of `serve` versus `--proxy`: it holds this session's proxy token — never a vendor
key — but it is on disk. `running()` treats a state file whose pid is gone as *not running* and deletes
it, otherwise `status` would insist forever and `start` would refuse to replace a dead daemon.
`pid_alive` rejects a non-positive pid **before** calling `os.kill`, because `os.kill(0, …)` signals the
caller's whole process group — a truncated pid would read as alive and `stop` would signal the terminal.

The detached child is launched as `sys.executable -c "from treg.cli import main; main()" serve start
--foreground`, not whatever `treg` is on `PATH`: a Homebrew copy one version behind would be started
instead and would not have the command at all. Its output goes to `~/.treg/proxy/serve.log`, and the
parent prints the log's last line when the child fails to appear rather than telling the user to go
read a file.

## Packaging
Certificate generation needs `cryptography`, which is compiled, so it sits in a `[proxy]` extra rather
than the base install — `pip install tools-registry` must stay the light CLI (`httpx` + `questionary`).
`_require_cryptography` raises `ProxyDependencyError` with the exact install line. `[server]` already
includes it, so a self-hoster gets it free.

## Known limits (say these out loud)
Certificate **pinning** — a client that accepts only its own exact certificate refuses ours; it fails
alone and needs a never-intercept entry. **Remote MCP servers** are not covered (a hosted one calls from
someone else's machine; a local stdio one inherits our environment and is captured). Not HTTPS, not
covered: SSH, database wire protocols; WebSockets are tunnelled, not intercepted. Every intercepted call
adds a hop to treg and back, and **fails if treg is down** — the price of the key never landing. Large
uploads flow through us. Some vendor CLIs still need `treg run`: `gh` refuses before it makes any network
call, so there is nothing to intercept.
