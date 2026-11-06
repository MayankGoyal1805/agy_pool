"""On-demand quota lookup (`agy -p /usage`). Never polled automatically."""

from __future__ import annotations

import concurrent.futures
import datetime
import subprocess
from dataclasses import dataclass, field
from typing import Optional

from .paths import Paths, find_agy_binary
from .store import Account


@dataclass
class Limit:
    group: str  # e.g. "Gemini Models"
    name: str  # raw limit name, e.g. "Five hour limit"
    pct: Optional[int]  # % remaining
    reset: Optional[datetime.datetime]


@dataclass
class UsageResult:
    account: Account
    limits: list[Limit] = field(default_factory=list)
    error: Optional[str] = None


def parse_reset(text: Optional[str]) -> Optional[datetime.datetime]:
    if not text:
        return None
    clean = text.strip().replace(" UTC", "+00:00").replace("Z", "+00:00")
    if "T" not in clean and " " in clean:
        clean = clean.replace(" ", "T", 1)
    try:
        dt = datetime.datetime.fromisoformat(clean)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)


def parse(raw: str) -> list[Limit]:
    """Parse tab-separated `/usage` lines: group, limit, pct%, [reset]."""
    out = []
    for line in raw.splitlines():
        parts = [p.strip() for p in line.split("\t") if p.strip()]
        if len(parts) < 3:
            continue
        try:
            pct = int(parts[2].rstrip("%"))
        except ValueError:
            continue
        out.append(Limit(parts[0], parts[1], pct, parse_reset(parts[3] if len(parts) > 3 else None)))
    return out


def fetch(paths: Paths, account: Account, timeout: float = 40.0) -> UsageResult:
    if not account.has_token:
        return UsageResult(account, error="no token")
    cmd = [find_agy_binary(), f"--app_data_dir={paths.app_dir_arg(account.dir)}", "-p", "/usage"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return UsageResult(account, error=f"timed out after {timeout:.0f}s")
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout).strip().splitlines()
        return UsageResult(account, error=(msg[-1] if msg else f"exit {proc.returncode}")[:160])
    limits = parse(proc.stdout)
    if not limits:
        return UsageResult(account, error="could not parse /usage output")
    return UsageResult(account, limits)


def fetch_many(paths: Paths, accounts: list[Account]) -> list[UsageResult]:
    if not accounts:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(accounts), 8)) as ex:
        return list(ex.map(lambda a: fetch(paths, a), accounts))
