"""Sandbox (§9, M5) — the escape sweep is release-blocking: containment must hold."""

from pathlib import Path

import pytest

from caucus import sandbox
from caucus.sandbox import (
    SeatbeltSandbox,
    discard,
    ephemeral_copy,
    get_sandbox,
    run_tests,
    scrub_env,
)

_SB = get_sandbox()
requires_sandbox = pytest.mark.skipif(_SB is None, reason="no sandbox backend on this host")


def _run(code: str, timeout: int = 8):
    src = Path(__import__("tempfile").mkdtemp(prefix="caucus-t-"))
    wd = ephemeral_copy(src)
    try:
        return _SB.run(wd, ["python3", "-c", code], timeout=timeout, mem_mb=512, cpu_s=6)
    finally:
        discard(wd)
        __import__("shutil").rmtree(src, ignore_errors=True)


# ---- fail-closed + plumbing ----------------------------------------------------------

def test_fail_closed_when_no_backend(monkeypatch):
    # §9: if isolation can't be guaranteed, get_sandbox() returns None → caller degrades to
    # pass-through. Never run unsandboxed.
    monkeypatch.setattr(sandbox.DockerSandbox, "available", staticmethod(lambda: False))
    monkeypatch.setattr(sandbox.SeatbeltSandbox, "available", staticmethod(lambda: False))
    assert get_sandbox() is None


def test_scrub_env_drops_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("CAUCUS_MODEL", "m")
    env = scrub_env(tmp_path)
    assert "OPENAI_API_KEY" not in env
    assert not any(k.startswith("CAUCUS_") for k in env)
    assert env["HOME"] == str(tmp_path)  # ~ repointed to the disposable copy


def test_ephemeral_copy_leaves_original_untouched(tmp_path):
    src = tmp_path / "ws"
    src.mkdir()
    (src / "a.txt").write_text("orig")
    copy = ephemeral_copy(src)
    (copy / "a.txt").write_text("changed-in-copy")
    assert (src / "a.txt").read_text() == "orig"
    discard(copy)
    assert not copy.exists()


# ---- escape sweep (release-blocking, §9) ---------------------------------------------

@requires_sandbox
def test_escape_network_egress_blocked():
    r = _run("import socket; socket.create_connection(('1.1.1.1',80),timeout=4); print('CONNECTED')")
    assert "CONNECTED" not in r.stdout and r.returncode != 0


@requires_sandbox
def test_escape_env_has_no_keys(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-reach-child")
    r = _run("import os; print('LEAK' if any('API_KEY' in k for k in os.environ) else 'CLEAN')")
    assert "CLEAN" in r.stdout


@requires_sandbox
def test_escape_host_home_write_denied():
    target = Path.home() / "caucus-escape-test.txt"
    _run(f"open(r'{target}','w').write('pwned'); print('WROTE')")
    assert not target.exists(), "candidate wrote outside its sandbox copy!"


@requires_sandbox
def test_escape_resource_bomb_killed():
    r = _run("x=0\nwhile True:\n x+=1", timeout=5)
    assert r.timed_out or r.returncode != 0


@requires_sandbox
def test_workspace_copy_is_writable():
    r = _run("open('candidate.txt','w').write('edit'); print('WROTE-COPY')")
    assert "WROTE-COPY" in r.stdout and r.returncode == 0


# ---- test-runner abstraction ---------------------------------------------------------

@requires_sandbox
def test_run_tests_reports_pass_and_fail(tmp_path):
    wd = ephemeral_copy(tmp_path)
    try:
        ok = run_tests(_SB, wd, ["python3", "-c", "print('ok')"], timeout=10)
        assert ok.passed and ok.returncode == 0
        bad = run_tests(_SB, wd, ["python3", "-c", "import sys; sys.exit(1)"], timeout=10)
        assert not bad.passed and bad.returncode == 1
    finally:
        discard(wd)


@requires_sandbox
def test_breakout_via_test_command_is_contained(tmp_path):
    # The test command itself is hostile — it runs inside the same sandbox, so network is denied.
    wd = ephemeral_copy(tmp_path)
    try:
        outcome = run_tests(
            _SB, wd,
            ["python3", "-c", "import socket; socket.create_connection(('1.1.1.1',80),timeout=4)"],
            timeout=10)
        assert not outcome.passed  # network blocked → the hostile test command fails
    finally:
        discard(wd)


@pytest.mark.skipif(not SeatbeltSandbox.available(), reason="seatbelt backend only on macOS")
def test_seatbelt_deny_default_runs_yet_contains(tmp_path):
    # The seatbelt profile is deny-by-default: the toolchain still runs and can write its copy, but
    # network egress, host-home reads, and writes outside the copy are all denied. (Forces seatbelt
    # explicitly — get_sandbox() may pick Docker on this host.)
    sb = SeatbeltSandbox()
    wd = ephemeral_copy(tmp_path)
    try:
        ran = sb.run(wd, ["python3", "-c", "open('c.txt','w').write('x'); print('RAN')"], timeout=12)
        assert "RAN" in ran.stdout and ran.returncode == 0          # toolchain runs + writes the copy
        net = sb.run(wd, ["python3", "-c",
                          "import socket; socket.create_connection(('1.1.1.1',80),timeout=3)"], timeout=12)
        assert net.returncode != 0                                  # network denied
        ssh = Path.home() / ".ssh"
        rd = sb.run(wd, ["python3", "-c",
                         f"import os;print('READ' if os.path.isdir({str(ssh)!r}) and "
                         f"os.access({str(ssh)!r},os.R_OK) and os.listdir({str(ssh)!r}) else 'NO')"], timeout=12)
        assert "READ" not in rd.stdout                              # host-home read denied
        out = Path("/tmp/caucus-seatbelt-escape-check.txt")
        out.unlink(missing_ok=True)
        sb.run(wd, ["python3", "-c", f"open({str(out)!r},'w').write('p')"], timeout=12)
        assert not out.exists()                                     # write outside the copy denied
    finally:
        discard(wd)
