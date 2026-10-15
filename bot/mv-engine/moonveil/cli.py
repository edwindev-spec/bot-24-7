from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import traceback
from pathlib import Path

from . import __version__
from .runtime import find_compiler, validate_timeout
from .storage import write_bytes, write_text
from .core import (
    MoonVeilError,
    analyze,
    decode_payload,
    find_luau,
    graph_summary,
    instrument,
    normalize_prototypes,
    parse_dump,
    read_source,
    vm_disassembly,
    write_json,
)

from .v145_pipeline import V145RecoveryResult, recover_v145


def _timeout(value: str) -> float:
    try:
        return validate_timeout(float(value))
    except (ValueError, MoonVeilError) as exc:
        raise argparse.ArgumentTypeError("timeout must be a finite number greater than zero") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="moonveil",
        description=(
            "Recover complete MoonVeil v1.4.5 Lua/Luau into readable or "
            "structured, runnable Luau with reconstructed control flow."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--debug",
        action="store_true",
        help="show full tracebacks for unexpected errors (also MOONVEIL_DEBUG=1)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze_parser = subparsers.add_parser(
        "analyze", help="identify the v1.4.5 wrapper family and layers"
    )
    analyze_parser.add_argument(
        "input", type=Path, nargs="+", help="one or more files to inspect"
    )
    analyze_parser.add_argument("--json", action="store_true")

    doctor_parser = subparsers.add_parser(
        "doctor", help="check Python, the Luau runtime and the Luau compiler"
    )
    doctor_parser.add_argument("--luau", type=Path)
    doctor_parser.add_argument("--json", action="store_true")

    extract_parser = subparsers.add_parser(
        "extract", help="Base64-decode a directly embedded serialized payload"
    )
    extract_parser.add_argument("input", type=Path)
    extract_parser.add_argument("-o", "--output", type=Path, required=True)

    instrument_parser = subparsers.add_parser(
        "instrument", help="write a VM-blocking, prototype-dumping Luau copy"
    )
    instrument_parser.add_argument("input", type=Path)
    instrument_parser.add_argument("-o", "--output", type=Path, required=True)

    dump_parser = subparsers.add_parser(
        "dump", help="recover v1.4.5 graphs, semantic IR, and protocols"
    )
    dump_parser.add_argument("input", type=Path)
    dump_parser.add_argument("-o", "--output-dir", type=Path, required=True)
    dump_parser.add_argument("--luau", type=Path)
    dump_parser.add_argument("--timeout", type=_timeout, default=30.0)

    decompile_parser = subparsers.add_parser(
        "decompile",
        help="recover a complete v1.4.5 input into runnable Luau",
    )
    decompile_parser.add_argument("input", type=Path)
    decompile_parser.add_argument("-o", "--output", type=Path, required=True)
    decompile_parser.add_argument(
        "--artifacts",
        type=Path,
        help="optional directory for graphs, semantic IR, and protocols",
    )
    decompile_parser.add_argument("--luau", type=Path)
    decompile_parser.add_argument("--timeout", type=_timeout, default=60.0)
    decompile_parser.add_argument(
        "--no-verify",
        action="store_true",
        help="skip sandboxed original-versus-recovered trace comparison",
    )

    import_parser = subparsers.add_parser(
        "import-protocol",
        help="convert a captured v1.4.5 table protocol into artifacts",
    )
    import_parser.add_argument("input", type=Path)
    import_parser.add_argument("-o", "--output-dir", type=Path, required=True)

    return parser


def _print_analysis(result: object, as_json: bool) -> None:
    data = result.as_dict()  # type: ignore[attr-defined]
    if as_json:
        print(json.dumps(data, indent=2))
        return
    print(f"file:       {data['path']}")
    print(f"sha256:     {data['sha256']}")
    print(f"version:    {data['version'] or 'unknown'}")
    print(f"supported:  {'yes' if data['supported'] else 'no'}")
    payload = data["payload_bytes"]
    print(f"payload:    {payload if payload is not None else 'indirect/unavailable'}")
    print("layers:")
    for layer in data["layers"]:
        print(f"  - {layer}")
    for note in data["notes"]:
        print(f"note: {note}")


def _write_graph_artifacts(graph: dict, output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "prototype_graph.json", graph)
    write_text(
        output_dir / "moonveil.raw.asm", graph_summary(graph), encoding="utf-8"
    )
    normalized = normalize_prototypes(graph)
    write_json(output_dir / "prototypes.json", normalized)
    write_json(output_dir / "constants.json", normalized["constants"])
    write_text(
        output_dir / "moonveil.asm", vm_disassembly(normalized), encoding="utf-8"
    )
    return normalized


def _write_v145_recovery_artifacts(
    result: V145RecoveryResult,
    output_dir: Path,
    *,
    input_path: Path,
    output_path: Path | None,
    luau: Path,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    if result.graph is not None:
        write_json(output_dir / "prototype_graph.json", result.graph)
    if result.metadata is not None:
        write_json(output_dir / "prototypes.json", result.metadata)
    if result.semantic_ir is not None:
        write_json(output_dir / "semantic_ir.json", result.semantic_ir)
    for mode, decoded in result.decoded_modes.items():
        write_json(output_dir / f"decoded_{mode}.json", decoded)
    for mode, protocol in result.protocols.items():
        write_text(
            output_dir / f"decode_{mode}_protocol.txt", protocol, encoding="utf-8"
        )
    write_json(
        output_dir / "decompile_manifest.json",
        {
            "input": analyze(input_path).as_dict(),
            "output": str(output_path) if output_path is not None else None,
            "luau": str(luau),
            "profile": result.profile,
            "prototype_count": result.prototype_count,
            "instruction_count": result.instruction_count,
            "schema": result.schema,
            "layout": result.layout,
            "verification": result.verification,
            "notes": result.notes,
        },
    )


def _require_v145(input_path: Path) -> None:
    result = analyze(input_path)
    if result.version != "1.4.5":
        detected = result.version or "no MoonVeil version banner"
        raise MoonVeilError(
            "this public build supports only MoonVeil v1.4.5; "
            f"detected {detected}"
        )


def _recover_generic(
    args: argparse.Namespace,
    input_path: Path,
    *,
    verify: bool,
) -> tuple[V145RecoveryResult, Path]:
    project_root = Path(__file__).resolve().parent.parent
    luau = find_luau(args.luau, project_root)
    result = recover_v145(
        input_path,
        luau_path=luau,
        timeout=args.timeout,
        verify=verify,
    )
    return result, luau


def _decompile_generic(args: argparse.Namespace, input_path: Path) -> int:
    result, luau = _recover_generic(
        args,
        input_path,
        verify=not args.no_verify,
    )
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    write_text(output, result.recovered_source)
    if args.artifacts:
        _write_v145_recovery_artifacts(
            result,
            args.artifacts.resolve(),
            input_path=input_path,
            output_path=output,
            luau=luau,
        )
    print(
        f"decompiled MoonVeil v1.4.5 using {result.profile} "
        f"({result.prototype_count} prototypes, "
        f"{result.instruction_count} instruction slots) into {output}"
    )
    if result.verification.get("equivalent") is True:
        print(
            "sandbox verification matched "
            f"{result.verification['original_event_count']} observable events"
        )
    elif result.verification.get("compiled") is True:
        print("generated Luau passed compiler validation")
    for note in result.notes:
        print(f"note: {note}")
    return 0


def _validate_destinations(args: argparse.Namespace, input_path: Path) -> None:
    """Reject aliases of the source and overlapping output/artifact paths."""
    destinations: list[Path] = []
    if args.command in {"extract", "instrument", "decompile"}:
        destinations.append(args.output.resolve())
    directory = None
    if args.command in {"dump", "import-protocol"}:
        directory = args.output_dir.resolve()
    elif args.command == "decompile" and args.artifacts:
        directory = args.artifacts.resolve()
    if directory is not None:
        names = {
            "prototype_graph.json", "prototypes.json", "semantic_ir.json",
            "decompile_manifest.json", "constants.json", "moonveil.raw.asm", "moonveil.asm",
        }
        if args.command == "dump":
            names.add("reconstructed.luau")
        for mode in ("proxy", "symbolic_true", "numeric", "zero", "table", "false", "nil"):
            names.add(f"decoded_{mode}.json")
            names.add(f"decode_{mode}_protocol.txt")
        destinations.extend((directory / name).resolve() for name in sorted(names))
    seen: list[Path] = []
    for destination in destinations:
        if destination == input_path or (
            destination.exists() and input_path.exists() and destination.samefile(input_path)
        ):
            raise MoonVeilError("output or artifact path would overwrite the input file")
        if any(destination == other or (
            destination.exists() and other.exists() and destination.samefile(other)
        ) for other in seen):
            raise MoonVeilError("output path overlaps an artifact path")
        seen.append(destination)


def _debug_enabled(args: argparse.Namespace) -> bool:
    flag = os.environ.get("MOONVEIL_DEBUG", "").strip().lower()
    return bool(getattr(args, "debug", False)) or flag not in {"", "0", "false", "no"}


def _tool_version(path: Path, timeout: float = 10.0) -> str | None:
    """Best-effort ``--version`` probe; never raises."""
    try:
        completed = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (completed.stdout or completed.stderr or "").strip()
    return text.splitlines()[0] if text else None


def _doctor_report(luau_arg: Path | None) -> dict:
    project_root = Path(__file__).resolve().parent.parent
    report: dict = {
        "moonveil": __version__,
        "python": platform.python_version(),
        "python_ok": sys.version_info >= (3, 10),
        "platform": f"{platform.system()} {platform.machine()}".strip(),
        "luau": None,
        "luau_version": None,
        "compiler": None,
        "compiler_version": None,
        "problems": [],
    }
    if not report["python_ok"]:
        report["problems"].append("Python 3.10 or later is required")
    luau: Path | None = None
    try:
        luau = find_luau(luau_arg, project_root)
        report["luau"] = str(luau)
        report["luau_version"] = _tool_version(luau)
    except MoonVeilError as exc:
        report["problems"].append(str(exc))
    try:
        compiler = find_compiler(luau, project_root)
    except MoonVeilError as exc:
        compiler = None
        report["problems"].append(str(exc))
    if compiler is not None:
        report["compiler"] = str(compiler)
        report["compiler_version"] = _tool_version(compiler)
    elif luau is not None:
        report["problems"].append(
            "luau-compile not found: output will not be syntax-checked "
            "(optional; run tools/install_luau.py)"
        )
    report["ready"] = bool(report["python_ok"] and report["luau"])
    return report


def _doctor(args: argparse.Namespace) -> int:
    report = _doctor_report(args.luau)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"moonveil:  {report['moonveil']}")
        print(f"python:    {report['python']} ({'ok' if report['python_ok'] else 'too old'})")
        print(f"platform:  {report['platform']}")
        print(f"luau:      {report['luau'] or 'NOT FOUND'}"
              + (f" [{report['luau_version']}]" if report["luau_version"] else ""))
        print(f"compiler:  {report['compiler'] or 'not found (optional)'}"
              + (f" [{report['compiler_version']}]" if report["compiler_version"] else ""))
        for problem in report["problems"]:
            print(f"problem: {problem}")
        print("status:    " + ("ready" if report["ready"] else "not ready"))
    return 0 if report["ready"] else 2


def _analyze_many(args: argparse.Namespace) -> int:
    paths = [path.resolve() for path in args.input]
    if len(paths) == 1:
        _print_analysis(analyze(paths[0]), args.json)
        return 0
    failures = 0
    records: list[dict] = []
    for path in paths:
        try:
            result = analyze(path)
        except (MoonVeilError, OSError) as exc:
            failures += 1
            records.append({"path": str(path), "error": str(exc)})
            print(f"error: {path}: {exc}", file=sys.stderr)
            continue
        records.append(result.as_dict())
        if not args.json:
            _print_analysis(result, False)
            print()
    if args.json:
        print(json.dumps(records, indent=2))
    return 2 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            return _doctor(args)
        if args.command == "analyze":
            return _analyze_many(args)
        input_path: Path = args.input.resolve()
        _validate_destinations(args, input_path)
        if args.command == "extract":
            payload = decode_payload(read_source(input_path))
            output = args.output.resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            write_bytes(output, payload)
            print(f"wrote {len(payload)} bytes to {output}")
        elif args.command == "instrument":
            _require_v145(input_path)
            output = args.output.resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            write_text(output, instrument(read_source(input_path)))
            print(f"wrote VM-blocking instrumented source to {output}")
        elif args.command == "dump":
            _require_v145(input_path)
            result, luau = _recover_generic(args, input_path, verify=False)
            output_dir = args.output_dir.resolve()
            output_dir.mkdir(parents=True, exist_ok=True)
            recovered_path = output_dir / "reconstructed.luau"
            write_text(recovered_path, result.recovered_source)
            _write_v145_recovery_artifacts(
                result,
                output_dir,
                input_path=input_path,
                output_path=recovered_path,
                luau=luau,
            )
            print(
                f"decoded MoonVeil v1.4.5 using {result.profile} "
                f"into {output_dir}"
            )
        elif args.command == "decompile":
            _require_v145(input_path)
            return _decompile_generic(args, input_path)
        elif args.command == "import-protocol":
            graph = parse_dump(read_source(input_path))
            output_dir = args.output_dir.resolve()
            normalized = _write_graph_artifacts(graph, output_dir)
            print(
                f"imported {len(graph['tables'])} tables, "
                f"{len(normalized['prototypes'])} prototypes, and "
                f"{len(normalized['constants'])} constants into {output_dir}"
            )
        return 0
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return 130
    except (MoonVeilError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"error: {exc}", file=sys.stderr)
        if _debug_enabled(args):
            traceback.print_exc()
        return 2
    except RecursionError:
        print("error: the input is nested too deeply to process", file=sys.stderr)
        if _debug_enabled(args):
            traceback.print_exc()
        return 2
    except Exception as exc:  # noqa: BLE001 - last-resort CLI boundary
        if _debug_enabled(args):
            raise
        print(
            f"error: unexpected {type(exc).__name__}: {exc}\n"
            "re-run with --debug for the full traceback and please report it",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())