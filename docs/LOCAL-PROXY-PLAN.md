# Local proxy — catch the agent's calls without the agent knowing (`treg serve`)

**Status:** planned · **Depends on:** nothing new server-side · **v1 scope:** capture locally, inject on
the server.

## The problem this solves

treg works today only when the agent is told to use it — `treg call …`, or the `/call/` URL. The moment
an agent writes its own script that calls `api.stripe.com` directly, treg is invisible and the script
has no key.

A local proxy closes that gap: the agent makes an ordinary call, we catch it on the way out, and the
**server** adds the credential. Nothing about the agent changes.

## What we take from oneCLI, and what we do not

Their design is two separable halves. We want one of them.

| Half | oneCLI | treg |
|---|---|---|
| Catching the request | `HTTPS_PROXY` + its own certificate authority | **same** |
| Applying the credential | locally, decrypting real keys on the user's machine | **on the server** — keys never land |

Their gateway holds `SECRET_ENCRYPTION_KEY` and decrypts secrets on the laptop (`crypto.rs`,
`connect.rs`). That is the exact situation treg exists to prevent, so we keep the capture mechanism and
throw away the injection half.

**v1 is server-injection only.** A future "inject locally under `treg-run`" mode is possible — the
grant path, the run-proof gate and the owned-vs-shared rule already exist — but it is deliberately out
of scope. Deciding it later costs nothing; getting it wrong now costs the product's main promise.

## How it works

```
agent → api.stripe.com/v1/charges
   ↓  HTTPS_PROXY=127.0.0.1:<port>
local proxy   — is this host a registered tool?
   ├─ no  → blind tunnel, bytes untouched, we never see inside
   └─ yes → terminate TLS with a leaf cert we sign, read the request, re-address it
              ↓
        treg server  POST /call/https://api.stripe.com/v1/charges   (X-Treg-Token)
              ↓  server injects the credential — the existing relay
        Stripe → response streams all the way back to the agent
```

The proxy carries the member's **treg token** and nothing else. No vendor key ever reaches the machine.

Because the call lands on the normal `/call/` path, everything already built applies unchanged: the
per-member tool ACL, project scope, deny rules, daily caps, the audit record, OAuth auto-refresh. There
is no second policy engine to write — the reason oneCLI needed 24k lines of Rust and we do not.

## Hard constraint: the CLI must stay light

`pip install tools-registry` is the **CLI only** (`httpx` + `questionary`). Certificate generation needs
`cryptography`, which is a compiled dependency and lives in the `[server]` extra today. It must not
enter the base install.

**Decision: a third extra, `[proxy]` → `cryptography` only.** The proxy itself uses `asyncio` + `ssl`
from the standard library and `httpx` (already base). `treg serve` without the extra exits with a clear
install hint. `[server]` already includes `cryptography`, so a self-hoster gets it free.

## Components

| Piece | Where | Notes |
|---|---|---|
| Proxy server | `src/treg/localproxy.py` (new) | named for `localrun.py`; **not** `proxy.py`, which is the server relay |
| CA + leaf certs | same module | ECDSA P-256, leaf cached per host in memory |
| CLI command | `cmd_serve` in `cli.py` | `treg serve [--port] [--stop] [--status] [--print-env]` |
| Shell wiring | `shell.py` | `treg shell` starts the proxy and sets the env in the subshell |
| State | `~/.treg/proxy/` | `ca-key.pem` 0600 · `ca-cert.pem` 0644 · `ca-bundle.pem` 0644 · `proxy.json` |

### The environment we set

```
HTTPS_PROXY / HTTP_PROXY = http://treg:<local-token>@127.0.0.1:<port>
NODE_USE_ENV_PROXY       = 1      # Node's built-in fetch IGNORES HTTPS_PROXY without this
NODE_EXTRA_CA_CERTS      = ~/.treg/proxy/ca-bundle.pem
SSL_CERT_FILE            = "
REQUESTS_CA_BUNDLE       = "
CURL_CA_BUNDLE           = "
GIT_SSL_CAINFO           = "
DENO_CERT                = "
AWS_CA_BUNDLE            = "
NO_PROXY                 = localhost,127.0.0.1,<treg host>
```

`NODE_USE_ENV_PROXY` is the one that will otherwise cost a day of debugging: since Node 18 the built-in
`fetch` silently ignores proxy env vars and goes straight out. oneCLI carries the same flag in their
integration docs.

The bundle is **system CAs + ours**, so the agent still trusts the real internet. Never replace the
system roots; append to them.

## The non-negotiables

1. **The CA private key is generated per machine.** Never shipped, never shared, never committed. A
   single CA distributed to all users would let anyone impersonate any site to every user.
2. **Never touch the system trust store in v1.** Env-var scoping means only the agent's process tree
   trusts us — not the browser, not the OS. oneCLI does the same; there is no `add-trusted-cert`
   anywhere in their repo.
3. **Allow-list only.** Intercept a host only when it is a registered tool. Everything else is a blind
   tunnel we cannot read. This is also what stops us breaking (and reading) the agent's own calls to
   `api.anthropic.com` / `api.openai.com`.
4. **Bind `127.0.0.1` only** — never `0.0.0.0`.
5. **Proxy auth.** A random token minted per session, carried in the proxy URL, so another local process
   cannot quietly spend the member's quota through us.
6. **`trust_env=False` on the proxy's own httpx client.** Otherwise it reads `HTTPS_PROXY` from its own
   environment and talks to itself — an infinite loop on the first request.
7. **Never log bodies, headers or the token.** Host and status only.

## Phases — each independently testable

**P0 · Skeleton (no interception).** Listen, accept `CONNECT`, blind-tunnel everything both ways.
*Proof:* `curl -x http://127.0.0.1:PORT https://example.com` works exactly as without the proxy.

**P1 · Certificates.** Generate + persist the CA, build the bundle, generate and cache leaf certs.
`treg serve --print-env`.
*Proof:* a unit test signs a leaf for `example.com` and validates it against the CA; `curl
--cacert ca-bundle.pem` through the proxy trusts an intercepted host.

**P2 · Intercept and forward.** For allow-listed hosts: terminate TLS, read the request, re-address to
`{base}/call/https://{host}{path}`, add `X-Treg-Token` / `X-Treg-Org` / `X-Treg-Client`, stream the
response back.
*Proof:* a registered tool answers correctly with **no key anywhere on the machine**.

**P3 · Allow-list + errors.** Fetch hosts from `GET /tools` at start, refresh on a timer. Map treg's
failures to something readable: a 404 from `_resolve_call` means "no tool registered for this host or
path"; a 403 means "you do not have access"; treg unreachable must not look like the vendor being down.

**P4 · Wiring UX.** `treg shell` starts the proxy and exports the env for the subshell — it already runs
a controlled subshell, so this is the natural home. Standalone `treg serve` for people who want it in
their own shell.

**P5 · Docs.** `docs/context/architecture/local-proxy.md` fragment (new subsystem, so it needs one),
plus `/llms.txt` and the dashboard's agent instructions.

## Testing

- **Unit, no network:** CA + leaf generation, allow-list matching, URL rewrite, header filtering,
  `NO_PROXY` parsing.
- **Integration:** run the proxy against the existing ASGI test app; drive it with `httpx` configured to
  use the proxy and trust the bundle. Assert the request that lands on `/call/` is byte-faithful.
- **Loop guard:** an explicit test that the proxy's own client ignores `HTTPS_PROXY`.
- **Blind tunnel:** assert a non-registered host is never decrypted (no leaf generated for it).

## Known limits — say these out loud in the docs

- **Certificate pinning.** A tool that accepts only its own exact certificate will refuse. Needs a
  per-host "never intercept" list.
- **Remote MCP servers are not covered.** A local (stdio) MCP server inherits our env and is captured; a
  hosted one makes its calls on someone else's machine.
- **Not HTTPS, not covered:** SSH, database wire protocols. WebSockets should be tunnelled, not
  intercepted — the relay is request/response.
- **Latency + a hard dependency.** Every intercepted call goes to treg and back; if treg is down those
  calls fail. This is the price of the key never landing.
- **Large uploads flow through us.** Watch request timeouts on Render.
- **Some vendor CLIs still need `treg run`.** `gh` refuses before it makes any network call, so there is
  nothing to intercept. The proxy widens coverage; it does not replace `treg run`.

## Deliberately not in v1

Local injection under `treg-run` · system trust store install · Windows · WebSocket interception ·
per-agent proxy tokens (one per session is enough) · request/response inspection beyond routing.

## Open decisions

1. **Command shape** — `treg serve` as its own daemon, or only inside `treg shell`? (I suggest both:
   `serve` is the engine, `shell` is the convenient front door.)
2. **CA lifetime** — oneCLI uses 10 years. Shorter is safer but expires; suggest 2 years plus
   `treg serve --renew-ca`.
3. **Default port.**
4. **Ship `[proxy]` as an extra** (recommended) or fold `cryptography` into base (rejected: breaks the
   light CLI).
