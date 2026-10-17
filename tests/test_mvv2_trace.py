import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot" / "mvv2-engine"))

from moonveil.behavior import behavior_trace, is_known_trace_variant
from moonveil.errors import MoonVeilError
from moonveil.source import parse_wrapper, tokenize


def make_structural_wrapper():
    names = ["P", "q", "u"] + [f"slot_{index}" for index in range(3, 22)]
    fields = ",".join("function() return 1 end" for _ in names)
    name_list = ",".join(f'"{name}"' for name in names)
    return (
        "-- This script was generated using MoonVeil 2.1.0\n"
        f"return({{{fields}}}):p({{{name_list}}}, \"P\", ...)"
    )


class MoonVeilTraceGateTests(unittest.TestCase):
    def test_structural_parser_does_not_weaken_strict_profile(self):
        source = make_structural_wrapper()
        with self.assertRaisesRegex(MoonVeilError, "35 unique names"):
            parse_wrapper(source)
        wrapper = parse_wrapper(source, require_profile=False)
        self.assertEqual(wrapper.remapper, "p")
        self.assertEqual(wrapper.entry, "P")
        self.assertEqual(sum(token.kind == "string" for token in tokenize(wrapper.names_source)), 22)

    def test_unknown_22_slot_wrapper_cannot_activate_trace(self):
        source = make_structural_wrapper()
        wrapper = parse_wrapper(source, require_profile=False)
        self.assertFalse(is_known_trace_variant(source, wrapper))
        with self.assertRaisesRegex(MoonVeilError, "no behavior-trace profile"):
            behavior_trace(source, wrapper, luau=Path("/does/not/exist"))


if __name__ == "__main__":
    unittest.main()
