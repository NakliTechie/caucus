"""The sandbox — the load-bearing containment for v1.1 action selection.

Model-proposed code **will** be hostile-by-accident or hostile-by-injection, so candidate code
and the repo's test command run only inside an ephemeral, isolated copy of the workspace with:
no network, no host mutation, no access to the user's keys/env, and CPU/memory/wall-clock caps.

Backends, in order of preference, selected by what the host can actually guarantee:

* **seatbelt** (macOS ``sandbox-exec``) — an SBPL profile denies network and host-home
  read/write; only the workspace copy + temp are writable; the credential store is denied.
* **docker** (Linux/macOS, if a daemon is reachable) — ``--network none``, ephemeral, capped.
* otherwise → **no sandbox**: ``get_sandbox()`` returns ``None`` and action selection must
  **fail closed** (degrade to pass-through with a notice). Never run unsandboxed.

Every backend additionally: scrubs the environment (no ``*_API_KEY`` / ``CAUCUS_*`` reaches the
child), points ``HOME`` at the disposable copy, and enforces rlimits +
a wall-clock timeout, killing the whole process group.
"""

from __future__ import annotations

import os
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .log import get_logger

log = get_logger("sandbox")

# Environment variables the child may keep. Everything else — especially keys — is dropped.
_ENV_ALLOW = {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR"}


@dataclass
class SandboxResult:
    ok: bool                 # ran to completion within caps (returncode is meaningful)
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    reason: str = ""         # "", "timeout", "sandbox-error"


def scrub_env(workdir: Path) -> dict:
    """A minimal env: no keys, no CAUCUS_*; HOME/TMPDIR point inside the disposable copy."""
    env = {k: v for k, v in os.environ.items() if k in _ENV_ALLOW}
    env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    env["HOME"] = str(workdir)            # candidate's ~ is the sandbox, not the real home
    env["TMPDIR"] = str(workdir / ".tmp")
    (workdir / ".tmp").mkdir(exist_ok=True)
    return env


def _preexec(mem_mb: int, cpu_s: int):
    def limits() -> None:
        os.setsid()  # new process group so we can kill the whole tree
        wanted = [
            (resource.RLIMIT_CPU, (cpu_s, cpu_s)),
            (resource.RLIMIT_AS, (mem_mb * 1024 * 1024,) * 2),
            (resource.RLIMIT_FSIZE, (64 * 1024 * 1024,) * 2),
            (resource.RLIMIT_NOFILE, (256, 256)),
        ]
        for res, want in wanted:
            try:
                resource.setrlimit(res, want)
            except (ValueError, OSError):
                # Requested soft limit exceeds the hard cap → clamp to the hard limit so SOME cap
                # still applies (never run with no limit). The wall-clock timeout is the backstop.
                try:
                    _, hard = resource.getrlimit(res)
                    resource.setrlimit(res, (hard, hard))
                except (ValueError, OSError):
                    pass
    return limits


def _run(argv: list[str], *, workdir: Path, env: dict, timeout: int,
         mem_mb: int, cpu_s: int) -> SandboxResult:
    try:
        proc = subprocess.Popen(
            argv, cwd=str(workdir), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, preexec_fn=_preexec(mem_mb, cpu_s),
        )
    except Exception as exc:  # pragma: no cover - platform dependent
        return SandboxResult(False, -1, "", f"sandbox spawn failed: {exc}", False, "sandbox-error")
    try:
        out, err = proc.communicate(timeout=timeout)
        return SandboxResult(True, proc.returncode, out, err, False)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        out, err = proc.communicate()
        return SandboxResult(False, -9, out or "", err or "", True, "timeout")


class SeatbeltSandbox:
    """macOS sandbox-exec (seatbelt). Denies network + host-home access via an SBPL profile."""

    backend = "seatbelt"

    @staticmethod
    def available() -> bool:
        return sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").exists()

    def _profile(self, workdir: Path) -> str:
        home = str(Path.home())
        wd = str(workdir.resolve())
        # DENY BY DEFAULT, but allow reads broadly so the toolchain (dyld/python/test runner) loads
        # without enumerating every macOS-version-specific path. The risks that actually matter are
        # denied: NETWORK (no allow → denied), the user's HOME (keys/creds/data — read-denied), and
        # WRITES anywhere but the disposable copy. Exec is allowed but neutered — nothing it spawns
        # has network, the keys, or a way to persist outside the copy.
        return (
            "(version 1)\n"
            "(deny default)\n"
            "(allow process-fork)\n"
            "(allow process-exec)\n"
            "(allow signal (target self))\n"
            "(allow sysctl-read)\n"
            "(allow mach*)\n"
            "(allow ipc-posix-shm*)\n"
            "(allow file-read*)\n"                         # reads broad so the toolchain runs…
            f'(deny file-read* (subpath "{home}"))\n'      # …except the user's home (keys/creds/data)
            "(allow file-write*\n"
            '  (literal "/dev/null") (literal "/dev/zero") (literal "/dev/dtracehelper")\n'
            '  (literal "/dev/tty") (literal "/dev/random") (literal "/dev/urandom")\n'
            f'  (subpath "{wd}"))\n'                        # writes ONLY to the disposable copy
        )

    def run(self, workdir: Path, command: list[str], *, timeout: int = 30,
            mem_mb: int = 512, cpu_s: int = 20) -> SandboxResult:
        env = scrub_env(workdir)
        argv = ["/usr/bin/sandbox-exec", "-p", self._profile(workdir), *command]
        return _run(argv, workdir=workdir, env=env, timeout=timeout, mem_mb=mem_mb, cpu_s=cpu_s)


class DockerSandbox:
    """Container backend: --network none, ephemeral, capped. Used when a daemon is reachable."""

    backend = "docker"
    _IMAGE = os.environ.get("CAUCUS_SANDBOX_IMAGE", "python:3.12-slim")

    @staticmethod
    def available() -> bool:
        if not shutil.which("docker"):
            return False
        try:
            return subprocess.run(["docker", "info"], capture_output=True, timeout=5).returncode == 0
        except Exception:
            return False

    def run(self, workdir: Path, command: list[str], *, timeout: int = 30,
            mem_mb: int = 512, cpu_s: int = 20) -> SandboxResult:
        # Run as the host (non-root) user so writes to the bind-mounted copy keep its ownership,
        # drop every Linux capability, and forbid privilege escalation — the container executes
        # model-generated code, so it gets the least privilege the host can grant.
        uid = os.getuid() if hasattr(os, "getuid") else 0
        gid = os.getgid() if hasattr(os, "getgid") else 0
        argv = [
            "docker", "run", "--rm", "--network", "none",
            "--user", f"{uid}:{gid}",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            f"--memory={mem_mb}m", "--cpus=1", "--pids-limit=128",
            "--read-only", "--tmpfs", "/tmp",
            "-v", f"{workdir.resolve()}:/work", "-w", "/work",
            "-e", "HOME=/work", self._IMAGE, *command,
        ]
        # env is not forwarded (no -e for host vars) → keys never enter the container. The docker
        # *client* runs with a minimal hardcoded PATH (enough to find the docker binary), not the
        # host PATH.
        return _run(argv, workdir=workdir,
                    env={"PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
                    timeout=timeout, mem_mb=mem_mb, cpu_s=cpu_s)


def get_sandbox() -> Optional[object]:
    """Return the strongest available sandbox, or None (caller must fail closed)."""
    for backend in (DockerSandbox, SeatbeltSandbox):
        if backend.available():
            return backend()
    return None


# ---- ephemeral workspace copy --------------------------------------------------------

def ephemeral_copy(src: Path) -> Path:
    """Copy a workspace into a throwaway temp dir; the original is never touched."""
    dst = Path(tempfile.mkdtemp(prefix="caucus-sbx-"))
    work = dst / "work"
    # symlinks=True copies links AS links (does NOT follow them, which would pull external file
    # content into the copy). Then drop any link that escapes the copy, so the sandboxed test can't
    # reach host files (a planted `cfg -> /etc/passwd` or `-> ~/.ssh`) through it.
    shutil.copytree(src, work, symlinks=True,
                    ignore=shutil.ignore_patterns(".git", "node_modules", ".venv", "__pycache__"))
    root = work.resolve()
    for p in work.rglob("*"):
        if p.is_symlink():
            try:
                target = (p.parent / os.readlink(p)).resolve()
            except OSError:
                p.unlink(missing_ok=True)
                continue
            if target != root and root not in target.parents:
                p.unlink(missing_ok=True)
    return work


def discard(workdir: Path) -> None:
    parent = workdir.parent if workdir.name == "work" else workdir
    shutil.rmtree(parent, ignore_errors=True)


# ---- test-runner abstraction ---------------------------------------------------------

@dataclass
class TestOutcome:
    passed: bool
    returncode: int
    output: str
    timed_out: bool


def run_tests(sandbox, workdir: Path, test_command: list[str], *, timeout: int = 60,
              mem_mb: int = 1024, cpu_s: int = 50) -> TestOutcome:
    """Run a repo's test command *inside* the sandbox, under the same caps."""
    res = sandbox.run(workdir, test_command, timeout=timeout, mem_mb=mem_mb, cpu_s=cpu_s)
    output = (res.stdout + ("\n" + res.stderr if res.stderr else ""))[-8000:]
    return TestOutcome(passed=(res.ok and res.returncode == 0), returncode=res.returncode,
                       output=output, timed_out=res.timed_out)
