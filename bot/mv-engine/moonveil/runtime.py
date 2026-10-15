"""Portable discovery and validation of the official Luau tools."""

from __future__ import annotations

import math
import os
import shutil
from pathlib import Path

from .errors import MoonVeilError


def validate_timeout(value: float) -> float:
    if not math.isfinite(value) or value <= 0:
        raise MoonVeilError("timeout must be a finite number greater than zero")
    return value


def _executable(path: Path) -> Path | None:
    candidate = path.expanduser().resolve()
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return candidate
    return None


def _configured(path: Path, label: str) -> Path:
    result = _executable(path)
    if result is None:
        raise MoonVeilError(f"{label} is not an executable file: {path}")
    return result


def _names(name: str) -> tuple[str, str]:
    return (name + ".exe", name) if os.name == "nt" else (name, name + ".exe")


def _discover(name: str, project_root: Path) -> Path | None:
    for filename in _names(name):
        candidate = _executable(project_root / ".tools" / "luau-0.722" / filename)
        if candidate:
            return candidate
    for filename in _names(name):
        found = shutil.which(filename)
        if found:
            return Path(found).resolve()
    return None


def find_luau(explicit: Path | None, project_root: Path) -> Path:
    if explicit is not None:
        return _configured(explicit, "Luau runtime (--luau)")
    if os.environ.get("LUAU_PATH"):
        return _configured(Path(os.environ["LUAU_PATH"]), "LUAU_PATH")
    result = _discover("luau", project_root)
    if result:
        return result
    raise MoonVeilError(
        "Luau runtime not found; pass --luau, set LUAU_PATH, or install official Luau"
    )


def find_compiler(
    luau_path: Path | None = None, project_root: Path | None = None
) -> Path | None:
    if os.environ.get("LUAU_COMPILE_PATH"):
        return _configured(Path(os.environ["LUAU_COMPILE_PATH"]), "LUAU_COMPILE_PATH")
    if luau_path is None and os.environ.get("LUAU_PATH"):
        luau_path = Path(os.environ["LUAU_PATH"]).expanduser().resolve()
    if luau_path is not None:
        for filename in _names("luau-compile"):
            candidate = _executable(luau_path.with_name(filename))
            if candidate:
                return candidate
    root = project_root or Path(__file__).resolve().parent.parent
    return _discover("luau-compile", root)
