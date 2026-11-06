"""Reading and safely copying agy OAuth token files."""

from __future__ import annotations

import datetime
import json
import os
import tempfile
from pathlib import Path
from typing import Optional


def token_email(path: Path) -> Optional[str]:
    """Email stored in a token file, or None if missing/unreadable."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data.get("id_token_email") or data.get("email") or None


def atomic_copy(src: Path, dst: Path) -> None:
    """Copy src over dst atomically (temp file in dst's dir + fsync + rename), mode 0600."""
    data = Path(src).read_bytes()
    atomic_write(dst, data)


def atomic_write(dst: Path, data: bytes) -> None:
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{dst.name}.", dir=dst.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def backup(src: Path, backups_dir: Path, tag: str) -> Optional[Path]:
    """Save a timestamped copy of src into backups_dir. Returns the backup path."""
    src = Path(src)
    if not src.is_file():
        return None
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    safe_tag = "".join(c if c.isalnum() or c in "@._-" else "_" for c in tag)
    dst = Path(backups_dir) / f"{stamp}-{safe_tag}"
    atomic_copy(src, dst)
    return dst


def same_content(a: Path, b: Path) -> bool:
    try:
        return Path(a).read_bytes() == Path(b).read_bytes()
    except OSError:
        return False
