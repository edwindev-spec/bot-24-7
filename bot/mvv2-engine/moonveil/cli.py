"""Command-line interface for analysis, decoded graphs and source recovery."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from . import __version__
from .behavior import behavior_trace, is_known_trace_variant
from .decoder import decode
from .errors import MoonVeilError
from .runtime import compile_source, find_luau, positive_timeout
from .source import parse_wrapper
from .storage import check_destinations, read_source, write_json, write_text
from .vm import disassemble, emit, json_ir, normalize


def _timeout(value):
    try:
        return positive_timeout(float(value))
    except (ValueError, MoonVeilError) as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _parser():
    parser = argparse.ArgumentParser(description="Recover Luau from the supported MoonVeil 2.1.0 table-dispatch family.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    analysis = commands.add_parser("analyze", help="identify the supported profile without executing Lua")
    analysis.add_argument("input", type=Path)
    analysis.add_argument("--json", action="store_true")
    for name in ("dump", "decompile"):
        command = commands.add_parser(name)
        command.add_argument("input", type=Path)
        command.add_argument("-o", "--output", type=Path, required=True)
        command.add_argument("--luau", type=Path)
        command.add_argument("--timeout", type=_timeout, default=30.0)
        if name == "decompile":
            command.add_argument("--artifacts", type=Path)
            command.add_argument("--require-compile", action="store_true", help="fail if luau-compile is unavailable")
            command.add_argument("--trace-unsupported", action="store_true", help="return a sandboxed behavior trace for the one explicitly calibrated fallback sample")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        raw, source = read_source(args.input)
        digest = hashlib.sha256(raw).hexdigest()
        if args.command == "analyze":
            report = {"input": str(args.input), "size": len(raw), "sha256": digest, "supported": False}
            try:
                wrapper = parse_wrapper(source)
                report.update(supported=True, version="2.1.0", profile="table-dispatch-210", roles=wrapper.roles)
            except MoonVeilError as exc:
                report["reason"] = str(exc)
            print(json.dumps(report, indent=2) if args.json else (
                f"MoonVeil 2.1.0: supported table-dispatch profile ({len(raw)} bytes)" if report["supported"] else report["reason"]
            ))
            return 0 if report["supported"] else 2
        artifacts = getattr(args, "artifacts", None)
        destinations = [args.output]
        if artifacts:
            destinations += [artifacts / name for name in ("decoded.json", "normalized.json", "instructions.txt", "manifest.json")]
        check_destinations(args.input, destinations)
        try:
            wrapper = parse_wrapper(source)
        except MoonVeilError as profile_error:
            if args.command != "decompile" or not args.trace_unsupported:
                raise
            try:
                structural = parse_wrapper(source, require_profile=False)
            except MoonVeilError:
                raise profile_error
            if not is_known_trace_variant(source, structural):
                raise profile_error
            report = behavior_trace(source, structural, luau=args.luau, timeout=args.timeout)
            write_text(args.output, report)
            print(f"Behavior trace: {args.output} (not recovered source)")
            return 0
        luau = find_luau(args.luau)
        graph = decode(wrapper, luau, args.timeout)
        if args.command == "dump":
            write_json(args.output, graph)
            print(f"Decoded graph: {args.output} ({len(graph['tables'])} tables)")
            return 0
        ir = normalize(graph)
        recovered, profile = emit(ir)
        compiled = compile_source(recovered, luau, args.timeout)
        if args.require_compile and compiled is None:
            raise MoonVeilError("luau-compile is required but was not found; set LUAU_COMPILE_PATH")
        manifest = {
            "tool_version": __version__, "moonveil_version": "2.1.0", "profile": "table-dispatch-210",
            "source_sha256": digest, "output_sha256": hashlib.sha256(recovered.encode()).hexdigest(),
            "instruction_count": len(ir["instructions"]),
            "semantic_instruction_count": sum(ins["op"] != "nop" for ins in ir["instructions"]),
            "emission": profile, "compiled": compiled, "equivalent": None,
            "protected_program_executed": False,
            "notes": ["The loader and deserializer ran in official Luau; the protected constructor was replaced before dispatch.",
                      "Behavioral equivalence is not established by compilation alone."]}
        write_text(args.output, recovered)
        if artifacts:
            write_json(artifacts / "decoded.json", graph)
            write_json(artifacts / "normalized.json", json_ir(ir))
            write_text(artifacts / "instructions.txt", disassemble(ir))
            write_json(artifacts / "manifest.json", manifest)
        verification = "compiled with official Luau" if compiled else "compiler unavailable; syntax was not checked"
        print(f"Recovered {len(ir['instructions'])} instructions: {args.output} ({verification})")
        return 0
    except (MoonVeilError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
