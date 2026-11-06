"""Finding and stopping agy processes that belong to a managed instance.

Processes are matched by the AGY_POOL_INSTANCE env marker that our launchers set,
so anything not started through a launcher (e.g. the VS Code extension) is never touched.
"""

from __future__ import annotations

import os
import re
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

MARKER_ENV = "AGY_POOL_INSTANCE"
PROC = Path(os.environ.get("AGY_POOL_PROC", "/proc"))


@dataclass
class Proc:
    pid: int
    cmdline: str
    exe: str
    started_ago: Optional[float]  # seconds

    @property
    def kind(self) -> str:
        c = f" {self.cmdline} "
        if " acp " in c:
            return "ACP session (T3 / editor)"
        if " remote-control " in c:
            return "remote-control daemon"
        if " -p " in c or " --print " in c or " --prompt " in c:
            return "print run"
        return "interactive CLI"


def _read(path: Path) -> Optional[bytes]:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _is_agy(pid_dir: Path) -> Optional[str]:
    try:
        exe = os.readlink(pid_dir / "exe")
    except OSError:
        return None
    # After a self-update the running binary shows up as e.g. "agy.1793876763971783100.old (deleted)".
    name = Path(exe.replace(" (deleted)", "")).name
    return exe if name == "agy" or re.fullmatch(r"agy\.\d+\.old", name) else None


def _uptime_and_hz() -> tuple[Optional[float], int]:
    raw = _read(PROC / "uptime")
    up = float(raw.split()[0]) if raw else None
    return up, os.sysconf("SC_CLK_TCK")


def _age(pid_dir: Path, uptime: Optional[float], hz: int) -> Optional[float]:
    raw = _read(pid_dir / "stat")
    if not raw or uptime is None:
        return None
    try:
        fields = raw.rsplit(b")", 1)[1].split()
        return max(0.0, uptime - int(fields[19]) / hz)
    except (IndexError, ValueError):
        return None


def _env(pid_dir: Path) -> dict[str, str]:
    raw = _read(pid_dir / "environ") or b""
    out = {}
    for item in raw.split(b"\0"):
        k, sep, v = item.partition(b"=")
        if sep:
            out[k.decode(errors="replace")] = v.decode(errors="replace")
    return out


def _has_open_under(pid_dir: Path, directory: Path) -> bool:
    prefix = str(directory.resolve()) + os.sep
    try:
        fds = list((pid_dir / "fd").iterdir())
    except OSError:
        return False
    for fd in fds:
        try:
            target = os.readlink(fd)
        except OSError:
            continue
        if target.startswith(prefix):
            return True
    return False


def _ancestry_has(pid: int, needle: str) -> bool:
    seen = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        cmd = (_read(PROC / str(pid) / "cmdline") or b"").replace(b"\0", b" ").decode(errors="replace")
        if needle in cmd:
            return True
        stat = _read(PROC / str(pid) / "stat")
        if not stat:
            return False
        try:
            pid = int(stat.rsplit(b")", 1)[1].split()[1])
        except (IndexError, ValueError):
            return False
    return False


def scan(instance: str, instance_dir: Path) -> tuple[list[Proc], list[Proc]]:
    """Return (marked, unmarked) agy processes for an instance.

    marked:   agy processes started via this instance's launcher -> safe to stop.
    unmarked: other agy processes with files open in the instance dir, excluding the
              VS Code extension -> reported only, never stopped.
    """
    uptime, hz = _uptime_and_hz()
    marked, unmarked = [], []
    me = os.getpid()
    for d in PROC.iterdir():
        if not d.name.isdigit() or int(d.name) == me:
            continue
        exe = _is_agy(d)
        if not exe:
            continue
        cmd = (_read(d / "cmdline") or b"").replace(b"\0", b" ").decode(errors="replace").strip()
        proc = Proc(int(d.name), cmd, exe, _age(d, uptime, hz))
        if _env(d).get(MARKER_ENV) == instance:
            marked.append(proc)
        elif _env(d).get(MARKER_ENV) is None and _has_open_under(d, instance_dir):
            if not _ancestry_has(proc.pid, ".vscode-server") and not _ancestry_has(proc.pid, ".vscode/"):
                unmarked.append(proc)
    return marked, unmarked


def stop(procs: list[Proc], timeout: float = 8.0) -> list[int]:
    """SIGTERM, wait up to timeout, then SIGKILL. Returns pids that had to be killed."""
    alive = set()
    for p in procs:
        try:
            os.kill(p.pid, signal.SIGTERM)
            alive.add(p.pid)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + timeout
    while alive and time.monotonic() < deadline:
        for pid in list(alive):
            if not _pid_alive(pid):
                alive.discard(pid)
        time.sleep(0.2)
    killed = []
    for pid in alive:
        try:
            os.kill(pid, signal.SIGKILL)
            killed.append(pid)
        except ProcessLookupError:
            pass
    return killed


def _pid_alive(pid: int) -> bool:
    stat = _read(PROC / str(pid) / "stat")
    if not stat:
        return False
    try:
        return stat.rsplit(b")", 1)[1].split()[0] != b"Z"
    except IndexError:
        return False
