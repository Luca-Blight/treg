"""The local proxy (`src/treg/localproxy.py`) — P0 (skeleton, blind tunnel) and P1 (certificates).

Everything here runs against loopback sockets; nothing reaches the internet and nothing needs a treg
server. Two properties are worth naming, because they are what makes the feature safe to install
before interception exists (P2):

- a connection to a host we do not intercept is copied **byte for byte** and no certificate is ever
  generated for it;
- the trust bundle we hand the agent is the system roots **plus** ours, never ours alone.

See docs/LOCAL-PROXY-PLAN.md §Testing.
"""

from __future__ import annotations

import asyncio
import base64
import os
import socket
import ssl
import stat
import threading
from datetime import datetime, timedelta, timezone

import pytest

from treg import localproxy as lp


# ---- helpers ------------------------------------------------------------------------------
class _EchoServer:
    """A plain TCP server that echoes back whatever it is sent, upper-cased. Stands in for "the real
    upstream" so a tunnel can be proved byte-faithful without the network."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        with conn:
            while True:
                try:
                    data = conn.recv(4096)
                except OSError:
                    return
                if not data:
                    return
                conn.sendall(data.upper())

    def close(self):
        self.sock.close()


def _proxy_connect(port: int, token: str, target: str) -> socket.socket:
    """Open a CONNECT tunnel through the proxy and return the socket, positioned after the reply."""
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    cred = base64.b64encode(f"treg:{token}".encode()).decode()
    s.sendall(
        f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
        f"Proxy-Authorization: Basic {cred}\r\n\r\n".encode()
    )
    return s


def _read_head(s: socket.socket) -> bytes:
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf


@pytest.fixture()
def proxy():
    """A running proxy on an operating-system-assigned port, torn down after the test."""
    handle = lp.start(lp.ProxyConfig(token=lp.mint_token(), port=0))
    yield handle
    handle.stop()


@pytest.fixture()
def ca(tmp_path):
    return lp.ensure_ca(tmp_path / "proxy")


# ---- P0 · listening, authentication, blind tunnel ----------------------------------------
def test_binds_loopback_only(proxy):
    """Rule 4: the proxy speaks for the member's quota, so it must never be reachable off-box."""
    host = proxy._server.sockets[0].getsockname()[0]
    assert host == "127.0.0.1"


def test_tunnel_is_byte_faithful(proxy):
    """A CONNECT tunnel copies bytes both ways without touching them."""
    echo = _EchoServer()
    try:
        s = _proxy_connect(proxy.port, proxy.token, f"127.0.0.1:{echo.port}")
        assert _read_head(s).startswith(b"HTTP/1.1 200 ")
        payload = b"hello \x00\xff binary bytes"
        s.sendall(payload)
        assert s.recv(4096) == payload.upper()
        s.close()
    finally:
        echo.close()


def test_tunnel_generates_no_certificate(proxy, ca):
    """The point of P0, and the standing promise for any non-registered host: we never decrypt it, so
    no leaf certificate is ever signed for it."""
    proxy.ca = ca
    echo = _EchoServer()
    try:
        s = _proxy_connect(proxy.port, proxy.token, f"127.0.0.1:{echo.port}")
        _read_head(s)
        s.sendall(b"ping")
        s.recv(4096)
        s.close()
    finally:
        echo.close()
    assert ca._contexts == {}


def test_missing_credentials_get_407(proxy):
    """Rule 5: without the session token the proxy answers 407 and does nothing else — another local
    process cannot quietly spend the member's quota through us."""
    s = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
    s.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com\r\n\r\n")
    head = _read_head(s)
    assert head.startswith(b"HTTP/1.1 407 ")
    assert b"Proxy-Authenticate: Basic" in head
    s.close()


def test_wrong_token_is_rejected(proxy):
    s = _proxy_connect(proxy.port, "not-the-token", "example.com:443")
    assert _read_head(s).startswith(b"HTTP/1.1 407 ")
    s.close()


def test_error_text_never_leaks_the_token(proxy):
    s = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
    s.sendall(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
    body = _read_head(s) + s.recv(4096)
    assert proxy.token.encode() not in body
    s.close()


def test_refuses_to_tunnel_to_itself(proxy):
    """A loop guard: pointing the proxy at its own address would eat a connection per attempt."""
    s = _proxy_connect(proxy.port, proxy.token, f"127.0.0.1:{proxy.port}")
    assert _read_head(s).startswith(b"HTTP/1.1 400 ")
    s.close()


def test_unreachable_upstream_is_a_readable_502(proxy):
    """A treg-side or network failure must not read like the vendor being down."""
    with socket.socket() as probe:                 # a port nobody is listening on
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
    s = _proxy_connect(proxy.port, proxy.token, f"127.0.0.1:{dead_port}")
    head = _read_head(s)
    assert head.startswith(b"HTTP/1.1 502 ")
    assert b"treg proxy" in head + s.recv(4096)
    s.close()


def test_malformed_request_is_a_400(proxy):
    s = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
    s.sendall(b"GARBAGE\r\n\r\n")
    assert _read_head(s).startswith(b"HTTP/1.1 400 ")
    s.close()


def test_relative_form_request_is_refused(proxy):
    """A request that is not CONNECT and not absolute-form did not come through a proxy setting."""
    cred = base64.b64encode(f"treg:{proxy.token}".encode()).decode()
    s = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
    s.sendall(f"GET /health HTTP/1.1\r\nHost: x\r\nProxy-Authorization: Basic {cred}\r\n\r\n".encode())
    assert _read_head(s).startswith(b"HTTP/1.1 400 ")
    s.close()


def test_plain_http_is_forwarded(proxy):
    """`HTTP_PROXY` is set alongside `HTTPS_PROXY`, so an http:// caller must keep working."""
    served = threading.Event()
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    seen: list[bytes] = []

    def _serve():
        conn, _ = srv.accept()
        with conn:
            seen.append(conn.recv(4096))
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        served.set()

    threading.Thread(target=_serve, daemon=True).start()
    cred = base64.b64encode(f"treg:{proxy.token}".encode()).decode()
    s = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
    s.sendall(
        f"GET http://127.0.0.1:{port}/thing?a=1 HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
        f"Proxy-Authorization: Basic {cred}\r\nConnection: close\r\n\r\n".encode()
    )
    reply = _read_head(s)
    s.close()
    srv.close()
    assert served.wait(timeout=5)
    assert reply.startswith(b"HTTP/1.1 200 ")
    # Origin-form request line, and our session token stripped before the upstream ever sees it.
    assert seen[0].startswith(b"GET /thing?a=1 HTTP/1.1\r\n")
    assert b"Proxy-Authorization" not in seen[0]


# ---- P0 · unit pieces ---------------------------------------------------------------------
def test_authorized_accepts_only_the_exact_token():
    ok = {"proxy-authorization": "Basic " + base64.b64encode(b"treg:abc").decode()}
    assert lp.authorized(ok, "abc")
    assert not lp.authorized(ok, "abcd")
    assert not lp.authorized({}, "abc")
    assert not lp.authorized({"proxy-authorization": "Bearer abc"}, "abc")
    assert not lp.authorized({"proxy-authorization": "Basic !!!not-base64"}, "abc")


def test_split_hostport_handles_ipv6_and_defaults():
    assert lp.split_hostport("api.stripe.com:443", 443) == ("api.stripe.com", 443)
    assert lp.split_hostport("api.stripe.com", 443) == ("api.stripe.com", 443)
    assert lp.split_hostport("[::1]:8443", 443) == ("::1", 8443)
    assert lp.split_hostport("[::1]", 443) == ("::1", 443)


def test_mint_token_is_unguessable():
    assert len({lp.mint_token() for _ in range(50)}) == 50
    assert len(lp.mint_token()) >= 24


# ---- P1 · the certificate authority --------------------------------------------------------
def test_ensure_ca_writes_a_private_key_only_the_owner_can_read(tmp_path):
    """Rule 1. A CA private key readable by other accounts on the machine would let them impersonate
    any site to this user."""
    ca = lp.ensure_ca(tmp_path / "proxy")
    assert stat.S_IMODE(os.stat(ca.key_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(ca.cert_path).st_mode) == 0o644
    assert stat.S_IMODE(os.stat(ca.dir).st_mode) == 0o700


def test_ca_is_a_two_year_signing_certificate(ca):
    from cryptography import x509

    basic = ca.cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    usage = ca.cert.extensions.get_extension_for_class(x509.KeyUsage).value
    assert basic.ca is True and basic.path_length == 0
    assert usage.key_cert_sign is True
    life = ca.expires - datetime.now(timezone.utc)
    assert timedelta(days=720) < life <= timedelta(days=lp.CA_DAYS)
    assert ca.cert.subject == ca.cert.issuer          # self-signed root


def test_ca_is_reused_across_starts(tmp_path):
    """The agent trusts the bundle for the whole session and beyond; regenerating on every start
    would invalidate it constantly."""
    first = lp.ensure_ca(tmp_path / "proxy")
    second = lp.ensure_ca(tmp_path / "proxy")
    assert first.cert.serial_number == second.cert.serial_number


def test_renew_replaces_the_authority(tmp_path):
    first = lp.ensure_ca(tmp_path / "proxy")
    renewed = lp.ensure_ca(tmp_path / "proxy", renew=True)
    assert renewed.cert.serial_number != first.cert.serial_number
    assert renewed.expires > first.cert.not_valid_before_utc


def test_an_expiring_ca_is_regenerated(tmp_path):
    """Inside the last 30 days we replace it, rather than let an expiry surface mid-session as an
    unexplained TLS error."""
    old = lp.ensure_ca(tmp_path / "proxy", days=10)
    fresh = lp.ensure_ca(tmp_path / "proxy")
    assert fresh.cert.serial_number != old.cert.serial_number
    assert fresh.expires - datetime.now(timezone.utc) > timedelta(days=700)


def test_a_corrupt_ca_does_not_brick_the_proxy(tmp_path):
    ca = lp.ensure_ca(tmp_path / "proxy")
    ca.key_path.write_text("not a key")
    again = lp.ensure_ca(tmp_path / "proxy")
    assert again.cert.serial_number != ca.cert.serial_number


def test_bundle_is_the_system_roots_plus_ours(ca):
    """Rule: `SSL_CERT_FILE` REPLACES the trust list. A bundle holding only our CA would leave the
    agent unable to verify the real internet."""
    bundle = ca.bundle_path.read_bytes()
    assert bundle.endswith(ca.cert_path.read_bytes())
    assert bundle.count(b"-----BEGIN CERTIFICATE-----") > 10   # the system roots are still there
    assert stat.S_IMODE(os.stat(ca.bundle_path).st_mode) == 0o644


def test_leaf_carries_the_host_in_its_subject_alternative_name(ca):
    from cryptography import x509

    cert_pem, key_pem = ca.leaf_pem("api.stripe.com")
    leaf = x509.load_pem_x509_certificate(cert_pem)
    names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert names.get_values_for_type(x509.DNSName) == ["api.stripe.com"]
    assert leaf.issuer == ca.cert.subject
    assert b"PRIVATE KEY" in key_pem


def test_leaf_for_an_ip_uses_an_ip_entry(ca):
    import ipaddress

    from cryptography import x509

    cert_pem, _ = ca.leaf_pem("127.0.0.1")
    leaf = x509.load_pem_x509_certificate(cert_pem)
    names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert names.get_values_for_type(x509.IPAddress) == [ipaddress.ip_address("127.0.0.1")]


def test_context_is_cached_per_host(ca):
    """A fresh signature per call would put a public-key operation in the hot path."""
    first = ca.context_for("api.stripe.com")
    assert ca.context_for("api.stripe.com") is first
    assert ca.context_for("api.openai.com") is not first


def test_no_leaf_key_is_left_on_disk(ca):
    """`load_cert_chain` only reads from a file, so the leaf touches disk for microseconds. Nothing
    may remain afterwards."""
    ca.context_for("api.stripe.com")
    leftovers = [p.name for p in ca.dir.iterdir() if p.name.startswith("treg-leaf-")]
    assert leftovers == []


def test_a_real_client_trusts_a_leaf_signed_by_our_ca(ca):
    """The proof that P1 works end to end: a TLS client that trusts only our bundle completes a
    handshake with a server presenting a leaf we signed for that hostname."""
    server_ctx = ca.context_for("api.stripe.com")
    client_ctx = ssl.create_default_context(cafile=str(ca.bundle_path))

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    result: list = []

    def _serve():
        conn, _ = listener.accept()
        try:
            with server_ctx.wrap_socket(conn, server_side=True) as tls:
                tls.sendall(b"hello")
        except ssl.SSLError as exc:                       # pragma: no cover - failure path
            result.append(exc)

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            with client_ctx.wrap_socket(raw, server_hostname="api.stripe.com") as tls:
                assert tls.recv(16) == b"hello"
    finally:
        thread.join(timeout=5)
        listener.close()
    assert result == []


def test_a_client_trusting_only_the_system_roots_rejects_our_leaf(ca):
    """The other half of the same promise: our certificates are trusted ONLY where we explicitly put
    the bundle. Rule 2 — the system trust store is never touched."""
    server_ctx = ca.context_for("api.stripe.com")
    client_ctx = ssl.create_default_context()             # system roots only

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def _serve():
        conn, _ = listener.accept()
        try:
            with server_ctx.wrap_socket(conn, server_side=True):
                pass
        except (ssl.SSLError, OSError):
            pass

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            with pytest.raises(ssl.SSLCertVerificationError):
                client_ctx.wrap_socket(raw, server_hostname="api.stripe.com")
    finally:
        thread.join(timeout=5)
        listener.close()


# ---- P1 · the environment we publish -------------------------------------------------------
def test_proxy_env_carries_the_flags_that_matter(ca):
    env = lp.proxy_env(18791, "tok en/+", ca.bundle_path, "treg.superdesign.dev")
    assert env["HTTPS_PROXY"] == "http://treg:tok%20en%2F%2B@127.0.0.1:18791"
    assert env["https_proxy"] == env["HTTPS_PROXY"]        # curl reads lowercase
    assert env["NODE_USE_ENV_PROXY"] == "1"                # Node's fetch ignores the proxy without it
    assert env["NODE_EXTRA_CA_CERTS"] == str(ca.bundle_path)
    for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO",
                "DENO_CERT", "AWS_CA_BUNDLE"):
        assert env[key] == str(ca.bundle_path)
    # Loopback and the registry itself must never come back through us.
    assert "127.0.0.1" in env["NO_PROXY"] and "treg.superdesign.dev" in env["NO_PROXY"]
    assert env["no_proxy"] == env["NO_PROXY"]


def test_handle_env_matches_the_running_proxy(ca):
    handle = lp.start(lp.ProxyConfig(token=lp.mint_token(), port=0, ca=ca))
    try:
        env = handle.env("treg.example")
        assert f"@127.0.0.1:{handle.port}" in env["HTTPS_PROXY"]
        assert env["SSL_CERT_FILE"] == str(ca.bundle_path)
    finally:
        handle.stop()


def test_proxy_dir_follows_treg_config(tmp_path, monkeypatch):
    """Tests and per-agent identities redirect the CLI with TREG_CONFIG; the CA must follow, or a test
    run writes into the developer's real ~/.treg."""
    monkeypatch.setenv("TREG_CONFIG", str(tmp_path / "cfg" / "config.json"))
    assert lp.proxy_dir() == tmp_path / "cfg" / "proxy"
    monkeypatch.delenv("TREG_CONFIG")
    assert lp.proxy_dir().name == "proxy"


# ---- lifecycle ------------------------------------------------------------------------------
def test_stop_is_idempotent():
    handle = lp.start(lp.ProxyConfig(token=lp.mint_token(), port=0))
    handle.stop()
    handle.stop()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", handle.port), timeout=2).close()


def test_a_broken_connection_does_not_take_the_proxy_down(proxy):
    """One misbehaving client must never end the session for the rest of the agent's calls."""
    s = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
    s.sendall(b"CONNECT ")      # hang up mid-head
    s.close()
    echo = _EchoServer()
    try:
        good = _proxy_connect(proxy.port, proxy.token, f"127.0.0.1:{echo.port}")
        assert _read_head(good).startswith(b"HTTP/1.1 200 ")
        good.close()
    finally:
        echo.close()


def test_an_oversized_head_is_refused(proxy):
    """A client that never finishes its head must not be able to make us buffer without bound."""
    cred = base64.b64encode(f"treg:{proxy.token}".encode()).decode()
    s = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
    s.sendall(f"CONNECT example.com:443 HTTP/1.1\r\nProxy-Authorization: Basic {cred}\r\n".encode())
    filler = b"X-Pad: " + b"a" * 4000 + b"\r\n"
    try:
        for _ in range(40):                                # ~160 KB, past the 64 KB head cap
            s.sendall(filler)
    except OSError:
        pass                                               # the proxy may have closed us first
    head = _read_head(s)
    assert head == b"" or head.startswith(b"HTTP/1.1 431 ")
    s.close()


@pytest.mark.asyncio
async def test_handle_client_survives_an_unexpected_error(monkeypatch):
    """The catch-all around a connection: a bug in one path returns, it does not kill the listener."""
    monkeypatch.setattr(lp, "_parse_head", lambda *_: (_ for _ in ()).throw(RuntimeError("boom")))
    reader = asyncio.StreamReader()
    reader.feed_data(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
    reader.feed_eof()

    class _W:
        def write(self, _data): pass
        async def drain(self): pass
        def close(self): self.closed = True
        def can_write_eof(self): return False

    writer = _W()
    await lp.handle_client(lp.ProxyConfig(token="t"), 18791, reader, writer)   # must not raise
    assert getattr(writer, "closed", False)
