"""Release maintenance runs explicitly before serving, never inside an app lifespan."""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _env(tmp_path: Path, *, single_user: bool = False) -> tuple[dict[str, str], Path, Path]:
    database = tmp_path / "upgrade.db"
    token_file = tmp_path / "local-token"
    env = os.environ.copy()
    env.update({
        "TREG_DATABASE_URL": f"sqlite+aiosqlite:///{database}",
        "TREG_PUBLIC_URL": "http://localhost:18790",
        "TREG_SINGLE_USER": "true" if single_user else "false",
        "TREG_SINGLE_USER_TOKEN_FILE": str(token_file),
        "TREG_CLAUDE_CONNECTOR_ENABLED": "false",
    })
    return env, database, token_file


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args], cwd=ROOT, env=env, text=True,
        capture_output=True, timeout=90, check=False,
    )


def _upgrade(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return _run(["-m", "treg", "upgrade"], env)


def _seed_connection(env: dict[str, str]) -> None:
    script = textwrap.dedent(
        """
        import asyncio

        from treg import oauth_providers
        from treg.db import init_db, session_maker
        from treg.models import Org, Secret, Tool

        async def seed():
            await init_db()
            provider = oauth_providers.get("google-analytics")
            assert provider is not None
            async with session_maker() as db:
                org = Org(name="Upgrade Test", slug="upgrade-test")
                db.add(org)
                await db.flush()
                secret = Secret(
                    org_id=org.id,
                    name="google-analytics",
                    owner="owner@example.test",
                    kind="oauth",
                    value="unused-encrypted-placeholder",
                    provider="google-analytics",
                )
                db.add(secret)
                await db.flush()
                db.add(Tool(
                    org_id=org.id,
                    name="google-analytics",
                    owner="owner@example.test",
                    base_url=provider.base_url,
                    host="analyticsdata.googleapis.com",
                    bindings=[{"secret_id": secret.id}],
                ))
                await db.commit()

        asyncio.run(seed())
        """
    )
    result = _run(["-c", script], env)
    assert result.returncode == 0, result.stderr


def _companion_count(database: Path) -> int:
    with sqlite3.connect(database) as db:
        return db.execute(
            "SELECT COUNT(*) FROM tool WHERE name = 'google-analytics-admin'"
        ).fetchone()[0]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_real_serve_path_can_query_database_after_pre_serve_maintenance(tmp_path):
    env, _, token_file = _env(tmp_path, single_user=True)
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env.update({"PORT": str(port), "TREG_PUBLIC_URL": base_url})
    stdout_path = tmp_path / "server.stdout"
    stderr_path = tmp_path / "server.stderr"
    ready = False
    request_status = None
    request_detail = "request was not attempted"

    with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
        process = subprocess.Popen(
            [sys.executable, "-m", "treg"],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=stdout,
            stderr=stderr,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                try:
                    with urllib.request.urlopen(f"{base_url}/meta", timeout=1) as response:
                        ready = response.status == 200
                except (OSError, urllib.error.URLError):
                    pass
                if ready and token_file.exists():
                    break
                time.sleep(0.1)

            if ready and token_file.exists():
                request = urllib.request.Request(
                    f"{base_url}/tools",
                    headers={"X-Treg-Token": token_file.read_text().strip()},
                )
                try:
                    with urllib.request.urlopen(request, timeout=10) as response:
                        request_status = response.status
                        request_detail = response.read().decode(errors="replace")
                except urllib.error.HTTPError as exc:
                    request_status = exc.code
                    request_detail = exc.read().decode(errors="replace")
                except (OSError, urllib.error.URLError) as exc:
                    request_detail = repr(exc)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)

    server_stderr = stderr_path.read_text()
    assert ready, f"server never became ready\nserver stderr:\n{server_stderr}"
    assert token_file.exists(), f"single-user token was not written\nserver stderr:\n{server_stderr}"
    assert request_status == 200, (
        f"GET /tools returned {request_status}: {request_detail}\n"
        f"server stderr:\n{server_stderr}"
    )


def test_upgrade_on_a_fresh_database_creates_schema_without_provisioning_a_user(tmp_path):
    env, database, token_file = _env(tmp_path, single_user=True)

    result = _upgrade(env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "treg upgrade complete"
    with sqlite3.connect(database) as db:
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
        users = db.execute('SELECT COUNT(*) FROM "user"').fetchone()[0]
    assert {"org", "secret", "tool"} <= tables
    assert users == 0
    assert not token_file.exists(), "hosted upgrade must never provision the local user"


def test_upgrade_backfills_companions_and_is_idempotent(tmp_path):
    env, database, _ = _env(tmp_path)
    initial = _upgrade(env)
    assert initial.returncode == 0, initial.stderr
    _seed_connection(env)
    assert _companion_count(database) == 0

    first = _upgrade(env)
    assert first.returncode == 0, first.stderr
    assert _companion_count(database) == 1

    second = _upgrade(env)
    assert second.returncode == 0, second.stderr
    assert _companion_count(database) == 1

