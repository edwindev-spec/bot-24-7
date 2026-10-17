"""Official Luau discovery and bounded, file-backed subprocess execution."""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from .errors import MoonVeilError

ROOT = Path(__file__).resolve().parent.parent


def positive_timeout(value: float) -> float:
    if not math.isfinite(value) or value <= 0:
        raise MoonVeilError("timeout must be finite and greater than zero")
    return value


def _executable(value: Path) -> Path:
    candidate = value.expanduser().resolve()
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise MoonVeilError(f"not an executable file: {value}")
    return candidate


def find_luau(explicit: Optional[Path] = None) -> Path:
    if explicit is not None:
        return _executable(explicit)
    if os.environ.get("LUAU_PATH"):
        return _executable(Path(os.environ["LUAU_PATH"]))
    suffix = ".exe" if os.name == "nt" else ""
    installed = ROOT / ".tools" / "luau-0.722" / ("luau" + suffix)
    if installed.is_file():
        return _executable(installed)
    found = shutil.which("luau")
    if found:
        return _executable(Path(found))
    raise MoonVeilError("Luau not found; run python tools/install_luau.py, set LUAU_PATH, or pass --luau")


def find_compiler(luau: Path) -> Optional[Path]:
    if os.environ.get("LUAU_COMPILE_PATH"):
        return _executable(Path(os.environ["LUAU_COMPILE_PATH"]))
    sibling = luau.with_name("luau-compile.exe" if os.name == "nt" else "luau-compile")
    if sibling.is_file():
        return _executable(sibling)
    found = shutil.which("luau-compile")
    return _executable(Path(found)) if found else None


def run_bounded(command, *, cwd: Path, timeout: float, max_bytes: int = 16 * 1024 * 1024):
    positive_timeout(timeout)
    # Files bound Python memory even when the child produces excessive output.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)
        except OSError as exc:
            raise MoonVeilError(f"could not start {command[0]}: {exc}") from exc
        deadline = time.monotonic() + timeout
        try:
            while True:
                if os.fstat(stdout.fileno()).st_size + os.fstat(stderr.fileno()).st_size > max_bytes:
                    raise MoonVeilError(f"runtime output exceeded {max_bytes} bytes")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MoonVeilError(f"runtime exceeded the {timeout:g}s timeout")
                try:
                    process.wait(timeout=min(0.05, remaining))
                    break
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
        if os.fstat(stdout.fileno()).st_size + os.fstat(stderr.fileno()).st_size > max_bytes:
            raise MoonVeilError(f"runtime output exceeded {max_bytes} bytes")
        stdout.seek(0)
        stderr.seek(0)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        if process.returncode:
            raise MoonVeilError(f"runtime exited with code {process.returncode}: {err[-2000:] or out[-2000:]}")
        return out, err


def compile_source(source: str, luau: Path, timeout: float) -> Optional[bool]:
    compiler = find_compiler(luau)
    if compiler is None:
        return None
    with tempfile.TemporaryDirectory(prefix="moonveil-compile-") as directory:
        path = Path(directory) / "recovered.luau"
        path.write_text(source, encoding="utf-8")
        run_bounded([str(compiler), "--text", str(path)], cwd=path.parent, timeout=timeout)
    return True
