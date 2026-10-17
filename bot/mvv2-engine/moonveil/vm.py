"""Normalize the calibrated handlers and emit Luau without a VM dispatcher."""

from __future__ import annotations

import copy

from .errors import MoonVeilError

_FIELDS = {1, 3, 4, 8, 9, 10, 12, 13, 16, 17, 20, 27, 29}


def _integer(value, label, minimum=0, maximum=1000000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise MoonVeilError(f"invalid {label}: {value!r}")
    return value


def _value(atom):
    kind = atom["type"]
    if kind == "nil":
        return None
    if kind == "bytes":
        return bytes.fromhex(atom["hex"])
    if kind in {"number", "boolean"}:
        return atom["value"]
    if kind == "table":
        return ("table", atom["id"])
    raise MoonVeilError(f"unsupported graph atom: {kind}")


def _array(table, label):
    if set(table) != set(range(1, len(table) + 1)):
        raise MoonVeilError(f"{label} contains non-array keys or instruction gaps")
    return [table[key] for key in range(1, len(table) + 1)]


def normalize(graph):
    tables = {}
    for table in graph["tables"]:
        entries = {}
        for entry in table["entries"]:
            key = _value(entry["key"])
            _integer(key, "table key")
            if key in entries:
                raise MoonVeilError("duplicate decoded table key")
            entries[key] = _value(entry["value"])
        tables[table["id"]] = entries

    def dereference(value):
        if not isinstance(value, tuple) or value[0] != "table" or value[1] not in tables:
            raise MoonVeilError("invalid prototype table reference")
        return tables[value[1]]

    root = dereference(_value(graph["root"]))
    if set(root) != {11, 14, 15, 19, 23, 25, 28, 30}:
        raise MoonVeilError("unsupported prototype layout")
    stack = _integer(root[28], "stack size", maximum=4096)
    params = _integer(root[23], "parameter count", maximum=stack)
    for field in (11, 15, 30):
        _integer(root[field], f"prototype field {field}")
    if root[11] != 0 or dereference(root[14]) or dereference(root[19]):
        raise MoonVeilError("this profile has no closure/upvalue handlers; nested prototypes are unsupported")
    raw = [copy.deepcopy(dereference(value)) for value in _array(dereference(root[25]), "instructions")]
    if not raw or len(raw) > 100000:
        raise MoonVeilError("invalid instruction count")
    if any(set(ins) != _FIELDS for ins in raw):
        raise MoonVeilError("unsupported instruction layout")
    for fields in raw:
        for field, value in fields.items():
            if field != 4:
                _integer(value, f"instruction field {field}")
    handlers = {int(key): value for key, value in graph["handlers"].items()}
    instructions = []
    strings = []

    def reg(value):
        return _integer(value, "register", maximum=stack)

    def count(value):
        return _integer(value, "argument/result count", maximum=stack + 2)

    for offset, fields in enumerate(raw):
        pc = offset + 1
        kind = handlers.get(fields[20])
        if kind is None:
            raise MoonVeilError(f"unknown handler {fields[20]!r} at PC {pc}")
        record = {"pc": pc, "op": kind}
        if kind in {"xor_index", "xor_reverse"}:
            key = fields[4]
            if not isinstance(key, bytes) or not key:
                raise MoonVeilError(f"invalid string key at PC {pc}")
            distance = fields[12] - (42 if kind == "xor_index" else 12)
            _integer(distance, "forward string target", minimum=1, maximum=len(raw) - pc)
            target = raw[offset + distance]
            encrypted = target[4]
            if not isinstance(encrypted, bytes):
                raise MoonVeilError(f"string target at PC {pc + distance} is not a byte string")
            target[4] = bytes(
                byte ^ key[index % len(key) if kind == "xor_index" else -1 - index % len(key)]
                ^ (index % 256 if kind == "xor_index" else 0)
                for index, byte in enumerate(encrypted)
            )
            # The VM replaces the decoder instruction itself with a NOP.
            record.update(op="nop", decoded_target=pc + distance, decoder=kind)
        elif kind in {"loadk", "getglobal"}:
            record.update(a=reg(fields[12] - (19 if kind == "loadk" else 36)), value=fields[4])
            if kind == "getglobal" and not isinstance(fields[4], bytes):
                raise MoonVeilError(f"non-string global name at PC {pc}")
        elif kind == "self":
            a, b = reg(fields[12] - 45), reg(fields[8] - 63)
            reg(a + 1)
            if not isinstance(fields[4], bytes):
                raise MoonVeilError(f"non-string method name at PC {pc}")
            record.update(a=a, b=b, value=fields[4])
        elif kind in {"call", "call_return"}:
            a = reg(fields[12] - (59 if kind == "call" else 38))
            b = count(fields[8] - (41 if kind == "call" else 5))
            c = count(fields[9] - (4 if kind == "call" else 60))
            if b:
                reg(a + b - 1)
            if c > 1:
                reg(a + c - 2)
            record.update(a=a, b=b, c=c)
            if kind == "call_return":
                start, n = reg(fields[13] - 7), count(fields[3] - 4)
                if n > 1:
                    reg(start + n - 2)
                record.update(return_a=start, return_b=n)
        elif kind != "nop":
            raise MoonVeilError(f"unsupported semantic operation {kind}")
        if isinstance(record.get("value"), bytes):
            strings.append({"pc": pc, "hex": record["value"].hex()})
        instructions.append(record)
    if instructions[-1]["op"] != "call_return":
        raise MoonVeilError("prototype has no supported terminating return")
    return {"schema": "moonveil-2.1.0-ir-v1", "stack_size": stack, "parameter_count": params,
            "instructions": instructions, "strings": strings}


def lua_literal(value):
    if isinstance(value, bytes):
        parts = []
        for byte in value:
            if byte in (34, 92):
                parts.append("\\" + chr(byte))
            elif 32 <= byte < 127:
                parts.append(chr(byte))
            else:
                # Fixed three-digit escapes cannot merge with following digits.
                parts.append(f"\\{byte:03d}")
        return '"' + "".join(parts) + '"'
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "true" if value else "false"
    if type(value) in (int, float):
        return repr(value)
    raise MoonVeilError("unsupported Luau constant")


def _global(value):
    return "environment[" + lua_literal(value) + "]"


def _loader_source(ir):
    """Recognize a fully checked, straight-line nested call expression.

    Both open-result calls remain in final argument position, preserving all
    returns (including nil). No URL, register number or global name is assumed.
    """
    ops = [ins for ins in ir["instructions"] if ins["op"] != "nop"]
    if ir["parameter_count"] or len(ops) != 7:
        return None
    first, obj, constant, method, inner, outer, final = ops
    if [ins["op"] for ins in ops] != ["getglobal", "getglobal", "loadk", "self", "call", "call", "call_return"]:
        return None
    a, b = first["a"], obj["a"]
    if len({a, b, constant["a"]}) != 3 or b != a + 1 or method["a"] != b or method["b"] != b or constant["a"] != b + 2:
        return None
    if (inner["a"], inner["b"], inner["c"]) != (b, 3, 0) or (outer["a"], outer["b"], outer["c"]) != (a, 0, 2):
        return None
    if (final["a"], final["b"], final["c"], final["return_b"]) != (a, 1, 1, 1):
        return None
    # Named locals preserve global-read order and evaluate the object only once.
    # Calling through a local also preserves arbitrary method keys and receiver.
    return "\n".join([
        "-- Recovered from MoonVeil 2.1.0; original names and comments are unavailable.",
        "local environment = getfenv()",
        f"local recovered_function = {_global(first['value'])}",
        f"local receiver = {_global(obj['value'])}",
        f"local method = receiver[{lua_literal(method['value'])}]",
        f"recovered_function(method(receiver, {lua_literal(constant['value'])}))()",
        "return", "",
    ])


def emit(ir):
    compact = _loader_source(ir)
    if compact:
        return compact, "nested-calls"
    lines = [
        "-- Recovered from MoonVeil 2.1.0; registers retain VM numbering.",
        "local environment = getfenv()",
        "local pack, unpack = table.pack, table.unpack",
        "local r = {}",
        "local top = -1",
    ]
    if ir["parameter_count"]:
        targets = ", ".join(f"r[{index}]" for index in range(1, ir["parameter_count"] + 1))
        lines.append(targets + " = ...")
    for ins in ir["instructions"]:
        op = ins["op"]
        if op == "nop":
            continue
        a = ins["a"]
        lines.append(f"-- PC {ins['pc']}: {op}")
        if op in {"loadk", "getglobal"}:
            expr = lua_literal(ins["value"]) if op == "loadk" else _global(ins["value"])
            lines.append(f"r[{a}] = {expr}")
        elif op == "self":
            # The second write uses the saved receiver, even when A == B.
            lines.append(f"r[{a + 1}] = r[{ins['b']}]")
            lines.append(f"r[{a}] = r[{a + 1}][{lua_literal(ins['value'])}]")
        else:
            end = "top" if ins["b"] == 0 else str(a + ins["b"] - 1)
            call = f"r[{a}](unpack(r, {a + 1}, {end}))"
            c = ins["c"]
            if c == 1:
                lines.append(call)
            else:
                lines.extend(["do", f"    local values = pack({call})"])
                limit = "values.n" if c == 0 else str(c - 1)
                lines.append(f"    for index = 1, {limit} do r[{a} + index - 1] = values[index] end")
                if c == 0:
                    lines.append(f"    top = {a} + values.n - 1")
                lines.append("end")
            if op == "call_return":
                start, n = ins["return_a"], ins["return_b"]
                end = "top" if n == 0 else str(start + n - 2)
                lines.append("return" if n == 1 else f"return unpack(r, {start}, {end})")
                break
    return "\n".join(lines) + "\n", "registers"


def json_ir(ir):
    """Keep every byte string losslessly in the JSON artifacts."""
    result = copy.deepcopy(ir)
    for ins in result["instructions"]:
        if isinstance(ins.get("value"), bytes):
            value = ins["value"]
            ins["value"] = {"hex": value.hex()}
            try:
                ins["value"]["text"] = value.decode("utf-8")
            except UnicodeDecodeError:
                pass
    return result


def disassemble(ir):
    lines = ["; MoonVeil 2.1.0 normalized instructions (PC is 1-based)"]
    for ins in ir["instructions"]:
        operands = " ".join(f"{key}={lua_literal(value) if key == 'value' else value}" for key, value in ins.items() if key not in {"pc", "op"})
        lines.append(f"{ins['pc']:04d} {ins['op'].upper():12} {operands}".rstrip())
    return "\n".join(lines) + "\n"
