"""Narrow, sandboxed behavior trace for one unsupported MoonVeil 2.1.0 sample."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from .errors import MoonVeilError
from .runtime import find_luau, run_bounded
from .source import Wrapper, canonical, tokenize

TRACE_MAGIC = "MOONVEIL_BEHAVIOR_TRACE_V1"
TRACE_CANONICAL_SHA256 = "0857b81ed099300b06152cf85c9a152a6ba534333cd0422e62240eed22271953"
TRACE_TEMPLATE = Path(__file__).with_name("trace_template.luau")
TRACE_REQUIRED_NAMES = frozenset({"P", "q", "u"})


def _wrapper_names(wrapper: Wrapper) -> list[str]:
    return [token.text[1:-1] for token in tokenize(wrapper.names_source) if token.kind == "string"]


def is_known_trace_variant(source: str, wrapper: Wrapper) -> bool:
    """Allow tracing only this canonicalized source and its known 22-slot layout."""
    try:
        digest = hashlib.sha256(canonical(source).encode("utf-8")).hexdigest()
        names = _wrapper_names(wrapper)
    except (MoonVeilError, UnicodeError):
        return False
    return (
        digest == TRACE_CANONICAL_SHA256
        and len(names) == 22
        and len(set(names)) == 22
        and TRACE_REQUIRED_NAMES.issubset(names)
        and wrapper.remapper == "p"
        and wrapper.entry == "P"
    )


def behavior_trace(source: str, wrapper: Wrapper, *, luau: Path | None = None, timeout: float = 20.0) -> str:
    """Run the exact known sample with inert game/UI/network proxies and return observations."""
    if not is_known_trace_variant(source, wrapper):
        raise MoonVeilError("no behavior-trace profile is enabled for this input")
    if not TRACE_TEMPLATE.is_file():
        raise MoonVeilError("the behavior-trace template is missing")

    names = _wrapper_names(wrapper)
    names_lua = "{" + ",".join(json.dumps(name) for name in names) + "}"
    replacements = {
        "__TABLE__": wrapper.table_source,
        "__NAMES__": names_lua,
        "__REMAPPER__": json.dumps(wrapper.remapper),
        "__ENTRY__": json.dumps(wrapper.entry),
    }
    template = TRACE_TEMPLATE.read_text(encoding="utf-8")
    # The canonical fingerprint prevents marker injection from arbitrary inputs;
    # still reject a marker collision rather than silently changing the harness.
    for marker, value in replacements.items():
        if marker not in template or marker in value:
            raise MoonVeilError("invalid behavior-trace template or wrapper marker")
        template = template.replace(marker, value, 1)

    luau_path = find_luau(luau)
    with tempfile.TemporaryDirectory(prefix="moonveil-trace-") as directory:
        harness = Path(directory) / "trace.luau"
        harness.write_text(template, encoding="utf-8")
        stdout, _stderr = run_bounded(
            [str(luau_path), str(harness)],
            cwd=harness.parent,
            timeout=timeout,
            max_bytes=2 * 1024 * 1024,
        )

    events = [line for line in stdout.splitlines() if line.startswith(("@@EXEC", "@@REQ|", "@@EV|", "@@FAIL|", "@@HARNESS|"))]
    if not any(line.startswith("@@EXEC") for line in events):
        raise MoonVeilError("behavior trace did not reach the protected entry point")
    if any(line.startswith(("@@FAIL|", "@@HARNESS|error")) for line in events):
        raise MoonVeilError("behavior trace failed while initializing the sample")

    report = [
        TRACE_MAGIC,
        "MoonVeil 2.1.0 — traza de comportamiento de una variante conocida",
        "Este archivo NO es el código fuente original ni una prueba de equivalencia.",
        "La muestra se ejecutó en Luau con entorno restringido y APIs de juego/UI simuladas.",
        "Las llamadas HTTP se interceptaron localmente; no se abrió ninguna conexión.",
        "loadstring devolvió un proxy inerte; el contenido remoto no se descargó ni ejecutó.",
        "Al final se probaron callbacks sintéticos Toggle(true), Slider(80) y Toggle(false).",
        "Las filas DECODE-*, CALL, SET y SIM son observaciones de esa simulación.",
        "",
        "Eventos observados:",
        *events,
        "",
        "Límite: la traza cubre esta huella y el camino ejecutado por los valores simulados;",
        "no recupera ramas que no se ejecutaron ni demuestra que el resultado sea equivalente.",
    ]
    return "\n".join(report) + "\n"
