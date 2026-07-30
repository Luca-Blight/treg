"""Shell mode (`treg shell`) — the MVP / Phase 1 transparent CLI interception.

These exercise the client-side pieces without a live server or a real vendor CLI: the tool→shim
selection, the shim contract (clean PATH + verbatim args + exit-code passthrough → no recursion), the
session wiring (shim dir first on PATH, REALPATH kept clean, teardown), and the stop/guard paths.
See docs/CLI-SHELL-MODE-PLAN.md §7.
"""

from __future__ import annotations

import argparse
import os
import signal
import stat
import subprocess

import pytest

from treg import cli, shell


# ---- selection + routing ------------------------------------------------------------------
def test_plan_shims_filters_and_sorts():
    tools = [
        {"name": "stripe-tool", "cli": {"bin": "stripe", "enabled": True}},
        {"name": "gh", "cli": {"bin": "gh", "enabled": True}},
        {"name": "off", "cli": {"bin": "flyctl", "enabled": False}},   # local runs not enabled → skip
        {"name": "nobin", "cli": {"enabled": True}},                    # no bin → skip
        {"name": "nocli"},                                               # no cli profile → skip
        {"name": "escape", "cli": {"bin": "../evil", "enabled": True}},  # not a plain filename → skip
        {"name": "dup", "cli": {"bin": "gh", "enabled": True}},          # same bin as gh → first wins
    ]
    entries, warnings = shell.plan_shims(tools)
    assert entries == [("gh", "gh", "local"), ("stripe", "stripe-tool", "local")]
    assert warnings == []


def test_plan_shims_routes_server_for_runnable_tools():
    tools = [
        {"name": "stripe-tool", "cli": {"bin": "stripe", "enabled": True}, "server_runnable": True},
        {"name": "gh", "cli": {"bin": "gh", "enabled": True}, "server_runnable": False},
    ]
    # by bin (stripe) → server; gh requested but NOT server-runnable → falls back to local + a warning
    entries, warnings = shell.plan_shims(tools, frozenset({"stripe", "gh"}))
    assert entries == [("gh", "gh", "local"), ("stripe", "stripe-tool", "server")]
    assert any("gh" in w and "server" in w for w in warnings)


def test_plan_shims_matches_server_for_by_tool_name():
    tools = [{"name": "stripe-tool", "cli": {"bin": "stripe", "enabled": True}, "server_runnable": True}]
    entries, _ = shell.plan_shims(tools, frozenset({"stripe-tool"}))  # by tool name, not bin
    assert entries == [("stripe", "stripe-tool", "server")]


# ---- the shim contract --------------------------------------------------------------------
def test_shim_script_is_well_formed():
    s = shell.shim_script("stripe-tool", "/usr/local/bin/treg")
    assert s.startswith("#!/bin/sh\n")
    # runs on the CLEAN PATH so `treg run` finds the real bin, never this shim (loop avoidance)
    assert 'exec env PATH="$TREG_SHELL_REALPATH"' in s
    # the literal `--` fences treg's parsing from the user's args, which _run_local then strips
    assert "run stripe-tool -- \"$@\"" in s


def test_shim_script_server_route_adds_the_tier_flag():
    s = shell.shim_script("stripe-tool", "/usr/local/bin/treg", route="server")
    # the tier flag goes BEFORE the tool name (treg's own rule)
    assert "run --server stripe-tool -- \"$@\"" in s


def test_shim_execs_treg_run_with_clean_path_and_verbatim_args(tmp_path):
    """The heart of it: the shim must call `treg run <tool> -- <args>` verbatim, on the clean PATH,
    and hand back the CLI's exit code. Uses a fake `treg` that records its argv + PATH, then exits 7."""
    argv_out, path_out = tmp_path / "argv.out", tmp_path / "path.out"
    fake_treg = tmp_path / "faketreg"
    fake_treg.write_text(
        "#!/bin/sh\n"
        f'printf "%s" "$PATH" > {path_out}\n'
        f': > {argv_out}\n'
        f'for a in "$@"; do printf "%s\\n" "$a" >> {argv_out}; done\n'
        "exit 7\n"
    )
    fake_treg.chmod(0o755)

    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shell.write_shims(str(shim_dir), [("stripe", "stripe-tool", "local")], str(fake_treg))
    assert stat.S_IMODE((shim_dir / "stripe").stat().st_mode) == 0o755

    env = dict(os.environ, TREG_SHELL_REALPATH="/clean/only", PATH="/usr/bin:/bin")
    r = subprocess.run([str(shim_dir / "stripe"), "balance", "--live"], env=env, capture_output=True)

    assert r.returncode == 7  # the real CLI's exit code passes through
    assert path_out.read_text() == "/clean/only"  # ran on the clean PATH → no shim recursion
    assert argv_out.read_text().splitlines() == ["run", "stripe-tool", "--", "balance", "--live"]


def test_server_route_shim_execs_treg_run_server(tmp_path):
    """A server-routed shim must call `treg run --server <tool>` through a real shell."""
    argv_out = tmp_path / "argv.out"
    fake_treg = tmp_path / "faketreg"
    fake_treg.write_text(
        "#!/bin/sh\n"
        f': > {argv_out}\n'
        f'for a in "$@"; do printf "%s\\n" "$a" >> {argv_out}; done\n'
    )
    fake_treg.chmod(0o755)
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shell.write_shims(str(shim_dir), [("stripe", "stripe-tool", "server")], str(fake_treg))
    env = dict(os.environ, TREG_SHELL_REALPATH="/usr/bin:/bin")
    r = subprocess.run([str(shim_dir / "stripe"), "balance"], env=env, capture_output=True)
    assert r.returncode == 0
    assert argv_out.read_text().splitlines() == ["run", "--server", "stripe-tool", "--", "balance"]


def test_shim_script_bypasses_completion_to_the_real_bin():
    s = shell.shim_script("gh", "/usr/local/bin/treg", real_bin="/opt/homebrew/bin/gh")
    # cobra completion (__complete*) execs the real bin directly — never treg
    assert 'case "$1" in __complete*) exec /opt/homebrew/bin/gh "$@" ;; esac' in s
    # a normal invocation still routes through treg run
    assert "run gh -- \"$@\"" in s


def test_completion_call_bypasses_treg_real_bin_runs(tmp_path):
    """Through a real shell: `gh __complete …` execs the real bin directly (no treg → no audit/cap),
    while `gh <normal>` still routes through treg run."""
    real_log, treg_log = tmp_path / "real.out", tmp_path / "treg.out"
    real_bin = tmp_path / "realgh"
    real_bin.write_text(f'#!/bin/sh\nprintf "REAL %s\\n" "$*" >> {real_log}\n')
    real_bin.chmod(0o755)
    fake_treg = tmp_path / "faketreg"
    fake_treg.write_text(f'#!/bin/sh\nprintf "TREG %s\\n" "$*" >> {treg_log}\n')
    fake_treg.chmod(0o755)

    shim = tmp_path / "bin" / "gh"
    shim.parent.mkdir()
    shim.write_text(shell.shim_script("gh", str(fake_treg), real_bin=str(real_bin)))
    shim.chmod(0o755)
    env = dict(os.environ, TREG_SHELL_REALPATH="/usr/bin:/bin")

    subprocess.run([str(shim), "__complete", "sta"], env=env)   # completion → real bin
    subprocess.run([str(shim), "repo", "list"], env=env)        # normal → treg run
    assert real_log.read_text() == "REAL __complete sta\n"      # completion never hit treg
    assert treg_log.read_text() == "TREG run gh -- repo list\n"  # normal did


def test_run_subshell_ttl_closes_the_session():
    """The TTL hard cap fires: a subshell that would sleep 30s is terminated by the 1s timer."""
    import time
    t0 = time.monotonic()
    rc = shell._run_subshell(["/bin/sh", "-c", "sleep 30"], dict(os.environ), ttl_seconds=1)
    assert time.monotonic() - t0 < 10  # the timer closed it; we did not wait the full 30s
    assert rc != 0                      # terminated, not a clean exit


def test_registered_bin_resolves_shim_in_a_real_shell(tmp_path):
    """Through a real /bin/sh: a registered CLI name resolves to our shim (first on PATH) while an
    unregistered command is untouched — the 'is this registered?' test done for free by name resolution."""
    argv_out = tmp_path / "argv.out"
    fake_treg = tmp_path / "faketreg"
    fake_treg.write_text(
        "#!/bin/sh\n"
        f': > {argv_out}\n'
        f'for a in "$@"; do printf "%s\\n" "$a" >> {argv_out}; done\n'
    )
    fake_treg.chmod(0o755)
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shell.write_shims(str(shim_dir), [("stripe", "stripe-tool", "local")], str(fake_treg))

    realpath = "/usr/bin:/bin"
    env = dict(os.environ, PATH=f"{shim_dir}{os.pathsep}{realpath}", TREG_SHELL_REALPATH=realpath)
    # `stripe` → our shim; `true` (not registered, no shim) resolves to the system binary normally
    r = subprocess.run(["/bin/sh", "-c", "stripe deploy --prod && true"], env=env, capture_output=True)
    assert r.returncode == 0
    assert argv_out.read_text().splitlines() == ["run", "stripe-tool", "--", "deploy", "--prod"]


# ---- session base dir ---------------------------------------------------------------------
def test_session_base_dir_prefers_private_per_user(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert shell.session_base_dir() == str(tmp_path)
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    assert shell.session_base_dir() == str(tmp_path)


# ---- start_session wiring (no real subshell) ----------------------------------------------
def test_start_session_wires_path_and_tears_down(tmp_path, monkeypatch):
    captured: dict = {}

    def fake_run(argv, env, ttl_seconds=None):
        captured["argv"] = argv
        captured["env"] = dict(env)
        captured["ttl_seconds"] = ttl_seconds
        # the session dir + the shim must exist WHILE the shell runs
        captured["dir_exists_during"] = os.path.isdir(env[shell.ENV_DIR])
        captured["shim_exists_during"] = os.path.exists(os.path.join(env[shell.ENV_DIR], "bin", "stripe"))
        return 3

    monkeypatch.setattr(shell, "_run_subshell", fake_run)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

    rc = shell.start_session([("stripe", "stripe", "local")], "/usr/local/bin/treg", ttl_minutes=30)

    assert rc == 3  # the subshell's exit code is returned
    env = captured["env"]
    shim_dir = os.path.join(env[shell.ENV_DIR], "bin")
    assert env["PATH"].startswith(shim_dir + os.pathsep)       # our shims resolve first
    assert env[shell.ENV_REALPATH] == "/usr/bin:/bin"           # REALPATH is clean (no shim dir)
    assert shim_dir not in env[shell.ENV_REALPATH]
    assert env[shell.ENV_ACTIVE] == "1"
    assert env[shell.ENV_PID] == str(os.getpid())
    assert captured["ttl_seconds"] == 30 * 60  # --ttl minutes → seconds for the hard cap
    assert captured["dir_exists_during"]
    assert captured["shim_exists_during"]  # a shim was written before the shell launched
    # teardown ran on exit
    assert not os.path.exists(env[shell.ENV_DIR])


# ---- stop / guards ------------------------------------------------------------------------
def test_stop_outside_a_session_exits(monkeypatch):
    monkeypatch.delenv("TREG_SHELL", raising=False)
    with pytest.raises(SystemExit):
        shell.stop_session()


def test_stop_signals_the_controller(monkeypatch):
    sent: dict = {}
    monkeypatch.setenv("TREG_SHELL", "1")
    monkeypatch.setenv("TREG_SHELL_PID", "4242")
    monkeypatch.setattr(shell.os, "kill", lambda pid, sig: sent.update(pid=pid, sig=sig))
    shell.stop_session()
    assert sent == {"pid": 4242, "sig": signal.SIGTERM}


# ---- cmd_shell_start guards ---------------------------------------------------------------
def test_cmd_shell_start_refuses_when_nested(monkeypatch):
    monkeypatch.setenv("TREG_SHELL", "1")
    with pytest.raises(SystemExit):
        cli.cmd_shell_start(object(), {"token": "t", "base_url": "http://x"})


def test_cmd_shell_start_needs_login(monkeypatch):
    monkeypatch.delenv("TREG_SHELL", raising=False)
    with pytest.raises(SystemExit):
        cli.cmd_shell_start(object(), {"base_url": "http://x"})


def test_cmd_shell_start_no_runnable_clis_exits(monkeypatch):
    monkeypatch.delenv("TREG_SHELL", raising=False)

    class _Resp:
        status_code = 200
        def json(self): return [{"name": "api-only", "cli": None}]

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, path): return _Resp()

    monkeypatch.setattr(cli, "_client", lambda cfg: _C())
    args = cli.build_parser().parse_args(["shell", "start"])  # server_for=None, ttl=None
    with pytest.raises(SystemExit):
        cli.cmd_shell_start(args, {"token": "t", "base_url": "http://x"})


# ---- parser -------------------------------------------------------------------------------
def test_parser_dispatches_shell():
    p = cli.build_parser()
    assert p.parse_args(["shell", "start"]).fn is cli.cmd_shell_start
    assert p.parse_args(["shell", "stop"]).fn is cli.cmd_shell_stop


# ---- --proxy wiring (P4 of docs/LOCAL-PROXY-PLAN.md) ---------------------------------------
def test_start_session_publishes_the_proxy_env_and_stops_it(tmp_path, monkeypatch):
    """The seam `--proxy` uses: extra variables go into the subshell, and the proxy is stopped on the
    way out — a listener left running after the shell closed would keep answering for a session that
    no longer exists."""
    captured: dict = {}
    stopped: list = []

    def fake_run(argv, env, ttl_seconds=None):
        captured["env"] = dict(env)
        return 0

    monkeypatch.setattr(shell, "_run_subshell", fake_run)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

    shell.start_session(
        [("stripe", "stripe", "local")], "/usr/local/bin/treg",
        extra_env={"HTTPS_PROXY": "http://treg:tok@127.0.0.1:18791", "SSL_CERT_FILE": "/x/ca-bundle.pem"},
        on_close=lambda: stopped.append(True),
        captured_hosts=["api.stripe.com"],
    )
    assert captured["env"]["HTTPS_PROXY"] == "http://treg:tok@127.0.0.1:18791"
    assert captured["env"]["SSL_CERT_FILE"] == "/x/ca-bundle.pem"
    assert stopped == [True]


def test_extra_env_cannot_break_the_shims(tmp_path, monkeypatch):
    """Applied AFTER our own variables: an add-on that set PATH or a TREG_SHELL* marker would stop
    name resolution finding the shims, or make the session look like a nested one."""
    captured: dict = {}
    monkeypatch.setattr(shell, "_run_subshell",
                        lambda argv, env, ttl_seconds=None: captured.update(env=dict(env)) or 0)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

    shell.start_session([("stripe", "stripe", "local")], "/usr/local/bin/treg",
                        extra_env={"PATH": "/evil", shell.ENV_REALPATH: "/evil", shell.ENV_PID: "1"})
    env = captured["env"]
    assert env["PATH"].startswith(os.path.join(env[shell.ENV_DIR], "bin"))
    assert env[shell.ENV_REALPATH] == "/usr/bin:/bin"
    assert env[shell.ENV_PID] == str(os.getpid())


def test_the_banner_names_the_captured_hosts(capsys):
    """A member must never discover interception by accident: the hosts are listed, and everything
    else is named as untouched."""
    shell._print_banner([("stripe", "stripe", "local")], None, ["api.stripe.com", "api.intercom.io"])
    err = capsys.readouterr().err
    assert "Also captured (2 hosts)" in err
    assert "api.stripe.com" in err and "api.intercom.io" in err
    assert "Every other address goes straight out" in err


def test_no_proxy_no_extra_banner(capsys):
    shell._print_banner([("stripe", "stripe", "local")], None)
    assert "Also captured" not in capsys.readouterr().err


def test_cmd_shell_start_seeds_the_allow_list_from_the_tool_listing(monkeypatch, tmp_path):
    """`--proxy` reuses the tools already fetched for the shims — every registered host, including
    ones with no CLI, and no second request to the registry."""
    from treg import localproxy as lpx

    tools = [
        {"name": "stripe", "host": "api.stripe.com", "cli": {"bin": "stripe", "enabled": True}},
        {"name": "intercom", "host": "API.INTERCOM.IO"},          # no CLI: still captured by the proxy
        {"name": "odd", "cli": {"bin": "odd", "enabled": True}},   # no host: nothing to capture
    ]
    seen: dict = {}

    class _Handle:
        def env(self, treg_host):
            seen["treg_host"] = treg_host
            return {"HTTPS_PROXY": "http://x"}

        def stop(self):
            seen["stopped"] = True

    monkeypatch.setattr(lpx, "ensure_ca", lambda renew=False: seen.setdefault("ca", object()))
    monkeypatch.setattr(lpx, "start", lambda cfg: seen.setdefault("cfg", cfg) and None or _Handle())

    args = argparse.Namespace(proxy=True, proxy_port=None, renew_ca=False)
    env, stop, hosts = cli._start_local_proxy(args, {"base_url": "https://treg.example", "token": "tok",
                                                     "active_org": "acme"}, tools)
    assert hosts == ["api.intercom.io", "api.stripe.com"]     # lowercased, sorted, host-less tool skipped
    assert seen["cfg"].hosts == frozenset({"api.stripe.com", "api.intercom.io"})
    assert seen["cfg"].treg_token == "tok" and seen["cfg"].org == "acme"
    assert seen["cfg"].base_url == "https://treg.example"
    assert seen["treg_host"] == "treg.example"                # the registry never comes back through us
    assert env["HTTPS_PROXY"] == "http://x"
    stop()
    assert seen["stopped"]


def test_cmd_shell_start_explains_a_missing_certificate_library(monkeypatch):
    """The light CLI has no `cryptography`. Saying exactly what to install beats a traceback."""
    from treg import localproxy as lpx

    def _boom(renew=False):
        raise lpx.ProxyDependencyError('install it with: pip install "tools-registry[proxy]"')

    monkeypatch.setattr(lpx, "ensure_ca", _boom)
    args = argparse.Namespace(proxy=True, proxy_port=None, renew_ca=False)
    with pytest.raises(SystemExit) as exc:
        cli._start_local_proxy(args, {"base_url": "https://x", "token": "t"}, [])
    assert "tools-registry[proxy]" in str(exc.value)


def test_a_busy_port_is_a_readable_error(monkeypatch):
    from treg import localproxy as lpx

    monkeypatch.setattr(lpx, "ensure_ca", lambda renew=False: object())
    monkeypatch.setattr(lpx, "start", lambda cfg: (_ for _ in ()).throw(OSError("address in use")))
    args = argparse.Namespace(proxy=True, proxy_port=18791, renew_ca=False)
    with pytest.raises(SystemExit) as exc:
        cli._start_local_proxy(args, {"base_url": "https://x", "token": "t"}, [])
    assert "--proxy-port" in str(exc.value) and "18791" in str(exc.value)


# ---- treg serve (the daemon front door) -----------------------------------------------------
def test_serve_env_prints_shell_lines_and_the_way_back(tmp_path):
    """A daemon nobody can reach is useless, and typing ten exports by hand is worse. `--unset` is the
    way out, because `treg serve stop` cannot reach into a shell that already has the variables."""
    env = {"HTTPS_PROXY": "http://treg:a b@127.0.0.1:18791", "SSL_CERT_FILE": "/x/ca.pem"}
    lines = cli._serve_export_lines(env)
    assert "export SSL_CERT_FILE=/x/ca.pem" in lines
    assert "export HTTPS_PROXY='http://treg:a b@127.0.0.1:18791'" in lines   # quoted: the token is opaque
    assert cli._serve_export_lines(env, unset=True) == "unset HTTPS_PROXY\nunset SSL_CERT_FILE"


def test_serve_start_refuses_a_second_one(monkeypatch, capsys):
    from treg import localproxy as lpx

    monkeypatch.setattr(lpx, "running", lambda: {"port": 18791, "pid": 4242})
    with pytest.raises(SystemExit) as exc:
        cli.cmd_serve_start(argparse.Namespace(port=None, renew_ca=False, foreground=False),
                            {"token": "t", "base_url": "http://x"})
    assert "already running" in str(exc.value) and "treg serve stop" in str(exc.value)


def test_serve_start_needs_login(monkeypatch):
    with pytest.raises(SystemExit) as exc:
        cli.cmd_serve_start(argparse.Namespace(port=None, renew_ca=False, foreground=False),
                            {"base_url": "http://x"})
    assert "treg login" in str(exc.value)


def test_serve_status_says_what_is_captured(monkeypatch, capsys):
    from treg import localproxy as lpx

    monkeypatch.setattr(lpx, "running", lambda: {
        "port": 18791, "pid": 4242, "base_url": "https://treg.example", "org": "acme",
        "hosts": ["api.stripe.com", "api.vercel.com"]})
    cli.cmd_serve_status(argparse.Namespace(), {})
    out = capsys.readouterr().out
    assert "127.0.0.1:18791" in out and "acme" in out
    assert "api.stripe.com" in out and "api.vercel.com" in out
    assert "everything else goes straight out" in out


def test_serve_status_and_env_when_nothing_runs(monkeypatch, capsys):
    from treg import localproxy as lpx

    monkeypatch.setattr(lpx, "running", lambda: None)
    cli.cmd_serve_status(argparse.Namespace(), {})
    assert "not running" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exc:
        cli.cmd_serve_env(argparse.Namespace(unset=False), {})
    assert "treg serve start" in str(exc.value)


def test_unset_works_after_the_proxy_is_gone(monkeypatch, capsys):
    """--unset must NOT need a running proxy: it is what you reach for right after `stop`. Requiring
    one leaves the shell wedged — its variables point at a dead port, every call fails, and the one
    command that fixes it refuses. Found in live testing."""
    from treg import localproxy as lpx

    monkeypatch.setattr(lpx, "running", lambda: None)
    cli.cmd_serve_env(argparse.Namespace(unset=True), {})
    out = capsys.readouterr().out
    assert "unset HTTPS_PROXY" in out and "unset SSL_CERT_FILE" in out
    assert "unset NODE_USE_ENV_PROXY" in out and "export" not in out


def test_serve_stop_reports_when_nothing_runs(monkeypatch):
    from treg import localproxy as lpx

    monkeypatch.setattr(lpx, "running", lambda: None)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_serve_stop(argparse.Namespace(), {})
    assert "no proxy is running" in str(exc.value)
