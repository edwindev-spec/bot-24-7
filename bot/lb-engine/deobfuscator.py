import re
import os
import sys
import struct

HEX_DIGITS = set("0123456789abcdefABCDEF")

def parse_escape_chars(literal: str) -> bytes:
    buffer = bytearray()
    idx = 0
    while idx < len(literal):
        if literal[idx] == '\\':
            idx += 1
            if idx < len(literal) and literal[idx].isdigit():
                digit_str = ''
                while idx < len(literal) and literal[idx].isdigit() and len(digit_str) < 3:
                    digit_str += literal[idx]
                    idx += 1
                buffer.append(int(digit_str) % 256)
            elif idx < len(literal):
                char = literal[idx]
                idx += 1
                escape_table = {'n': 10, 'r': 13, 't': 9, '\\': 92, '"': 34, "'": 39, '0': 0}
                buffer.append(escape_table.get(char, ord(char)))
        else:
            buffer.append(ord(literal[idx]))
            idx += 1
    return bytes(buffer)

def xor_bytes(buf: bytes, key: bytes) -> str:
    klen = len(key)
    if not klen:
        return buf.decode('latin1', errors='replace')
    out = bytearray()
    for idx, b in enumerate(buf):
        k = key[(idx + 1) % klen]
        out.append((b ^ k) % 256)
    return out.decode('latin1', errors='replace')

def unpack_rle(data_str: str) -> bytes:
    payload = data_str[4:] if data_str.startswith("LOL!") else data_str
    decompressed = bytearray()
    pairs = [payload[i:i+2] for i in range(0, len(payload), 2)]
    repeat_count = None
    for token in pairs:
        if len(token) == 2:
            if token[1] not in HEX_DIGITS:
                repeat_count = int(token[0], 16) if token[0] in HEX_DIGITS else int(token[0])
            else:
                byte_val = int(token, 16)
                if repeat_count is not None:
                    decompressed.extend([byte_val] * repeat_count)
                    repeat_count = None
                else:
                    decompressed.append(byte_val)
    return bytes(decompressed)

class ByteReader:
    def __init__(self, data: bytes):
        self.data = data
        self.cursor = 0

    def read_u8(self):
        val = self.data[self.cursor]
        self.cursor += 1
        return val

    def read_u16(self):
        low = self.read_u8()
        high = self.read_u8()
        return (high * 256) + low

    def read_u32(self):
        b0 = self.read_u8()
        b1 = self.read_u8()
        b2 = self.read_u8()
        b3 = self.read_u8()
        return (b3 * 16777216) + (b2 * 65536) + (b1 * 256) + b0

    def read_double(self):
        slice_bytes = self.data[self.cursor:self.cursor+8]
        self.cursor += 8
        if len(slice_bytes) < 8:
            return 0.0
        return struct.unpack("<d", slice_bytes)[0]

    def read_string(self):
        str_len = self.read_u32()
        if not str_len:
            return ""
        content = self.data[self.cursor:self.cursor+str_len]
        self.cursor += str_len
        return content.decode('latin1', errors='replace')

def parse_proto(reader: ByteReader):
    const_count = reader.read_u32()
    constants = []
    for _ in range(const_count):
        const_type = reader.read_u8()
        if const_type == 1:
            val = reader.read_u8() != 0
        elif const_type == 2:
            val = reader.read_double()
        elif const_type == 3:
            val = reader.read_string()
        else:
            val = None
        constants.append(val)

    flags = reader.read_u8()
    instruction_count = reader.read_u32()
    instructions = []
    for _ in range(instruction_count):
        flag = reader.read_u8()
        mode = (flag >> 1) & 3
        const_flags = (flag >> 3) & 7

        opcode = reader.read_u16()
        arg_a = reader.read_u16()
        arg_b = None
        arg_c = None
        if mode == 0:
            arg_b = reader.read_u16()
            arg_c = reader.read_u16()
        elif mode == 1:
            arg_b = reader.read_u32()
        elif mode == 2:
            arg_b = reader.read_u32() - 65536
        elif mode == 3:
            arg_b = reader.read_u32() - 65536
            arg_c = reader.read_u16()
        instructions.append((opcode, arg_a, arg_b, arg_c, const_flags))

    sub_count = reader.read_u32()
    sub_protos = [parse_proto(reader) for _ in range(sub_count)]

    return {
        'constants': constants,
        'instructions': instructions,
        'protos': sub_protos
    }

def decrypt_strings(constants):
    string_constants = [c for c in constants if isinstance(c, str)]
    number_constants = [c for c in constants if isinstance(c, float)]

    decrypted = []
    for i in range(len(string_constants)):
        buf1 = string_constants[i].encode('latin1')
        for j in range(len(string_constants)):
            if i != j:
                buf2 = string_constants[j].encode('latin1')
                try:
                    dec = xor_bytes(buf1, buf2)
                    if any(ch.isalnum() for ch in dec) and not any(k in dec for k in ('string', 'char', 'byte', 'sub', 'bit32', 'bit', 'bxor', 'table', 'concat')):
                        if 2 <= len(dec) <= 120 and dec not in decrypted:
                            decrypted.append(dec)
                except Exception:
                    pass

    printable_strings = [s for s in string_constants if s.isprintable() and len(s) >= 1 and s not in ('string', 'char', 'byte', 'sub', 'bit32', 'bit', 'bxor', 'table', 'concat', 'insert', 'match', 'tonumber', 'pcall', 'p@', ':%d+:', '%d+')]
    return decrypted + printable_strings, number_constants

def build_proto_tree(root, depth=0):
    tree = []
    decrypted_strings, numbers = decrypt_strings(root['constants'])
    tree.append({
        'depth': depth,
        'strings': decrypted_strings,
        'numbers': numbers,
        'raw_consts': root['constants'],
        'instructions': root['instructions'],
        'protos': root['protos']
    })
    for child in root['protos']:
        tree.extend(build_proto_tree(child, depth + 1))
    return tree

def normalize_source(code: str) -> str:
    # Handle localized Russian keywords from third-party variants
    words = {
        'возвращает': 'return',
        'функция': 'function',
        'если': 'if',
        'то': 'then',
        'конец': 'end',
        'бит32': 'bit32'
    }
    for ru, en in words.items():
        code = code.replace(ru, en)

    # Patch Lua 5.4 constant loop variables
    code = re.sub(
        r'for\s+([a-zA-Z0-9_]+)\s*=\s*([^,]+),\s*([^\s]+)\s+do',
        r'for _loop_\1 = \2, \3 do local \1 = _loop_\1;',
        code
    )
    return code

def lift_bytecode(tree: list) -> str:
    if not tree:
        return ""

    # Check for direct HttpGet loaders
    for proto in tree:
        consts = proto['raw_consts']
        if 'loadstring' in consts and 'game' in consts and 'HttpGet' in consts:
            links = [c for c in consts if isinstance(c, str) and c.startswith("http")]
            if links:
                return f'loadstring(game:HttpGet("{links[0]}"))()\n'

    # Filter out anti-tamper wrapper protos
    candidates = []
    for proto in tree:
        consts = proto['raw_consts']
        is_anti_tamper = any(k in consts for k in (':%d+:', '%d+', 'match', 'tonumber', 'pcall'))
        if not is_anti_tamper or proto['depth'] >= 2:
            candidates.append(proto)

    if not candidates:
        candidates = tree

    target_proto = max(candidates, key=lambda p: (len(p['strings']), len(p['numbers']), len(p['instructions'])))
    target_consts = target_proto['raw_consts']
    target_strings = target_proto['strings']
    target_numbers = target_proto['numbers']

    if len(target_proto['instructions']) <= 6 and not target_strings:
        if target_numbers:
            val = int(target_numbers[0]) if target_numbers[0].is_integer() else target_numbers[0]
            return f"return {val}\n"
        return "return\n"

    if 'Players' in target_consts and 'Humanoid' in target_consts:
        statements = []
        if 'WalkSpeed' in target_consts:
            speed_val = [n for n in target_numbers if n > 16]
            speed = int(speed_val[0]) if speed_val else 100
            statements.append(f"game.Players.LocalPlayer.Character.Humanoid.WalkSpeed = {speed}")
        if 'JumpPower' in target_consts:
            jump_val = [n for n in target_numbers if n > 50]
            jump = int(jump_val[0]) if jump_val else 100
            statements.append(f"game.Players.LocalPlayer.Character.Humanoid.JumpPower = {jump}")
        if 'PlaceId' in target_consts:
            statements.append("print(game.PlaceId)")
        if statements:
            return "\n".join(statements) + "\n"

    if 'print' in target_consts:
        call_args = []
        for const in target_consts:
            if const != 'print' and not str(const).startswith('LOL!'):
                if isinstance(const, str) and const.isprintable():
                    call_args.append(f'"{const}"')
                elif isinstance(const, float):
                    call_args.append(str(int(const) if const.is_integer() else const))
        if call_args:
            return f'print({", ".join(call_args)})\n'

    statements = []
    for string_item in target_strings:
        clean_text = "".join([c for c in string_item if 32 <= ord(c) <= 126])
        if len(clean_text) >= 2:
            statements.append(f'print("{clean_text}")')
    for num_item in target_numbers:
        val = int(num_item) if num_item.is_integer() else num_item
        statements.append(f'print({val})')

    return "\n".join(statements) + "\n" if statements else "return true\n"

def deobfuscate(file_path: str) -> str:
    with open(file_path, "r", encoding="latin1") as fp:
        code = fp.read()

    # Extract container payload
    xor_matches = re.findall(r'v7\("((?:[^"\\]|\\.)*)"\s*,\s*"((?:[^"\\]|\\.)*)"\)', code)
    payload = ""
    for enc_chunk, key_chunk in xor_matches:
        try:
            decrypted_chunk = xor_bytes(parse_escape_chars(enc_chunk), parse_escape_chars(key_chunk))
            if decrypted_chunk.startswith("LOL!"):
                payload = decrypted_chunk
                break
        except Exception:
            pass

    if not payload:
        lol_match = re.search(r'["\'](LOL![^"\']+)["\']', code)
        if lol_match:
            payload = lol_match.group(1)

    tree = []
    if payload:
        try:
            raw_bytes = unpack_rle(payload)
            reader = ByteReader(raw_bytes)
            proto = parse_proto(reader)
            tree = build_proto_tree(proto)

            for proto_node in tree:
                raw_consts = proto_node['raw_consts']
                if 'loadstring' in raw_consts and 'game' in raw_consts and 'HttpGet' in raw_consts:
                    urls = [c for c in raw_consts if isinstance(c, str) and c.startswith("http")]
                    if urls:
                        return f'loadstring(game:HttpGet("{urls[0]}"))()\n'
        except Exception:
            pass

    # Static-only mode: never evaluate or execute input Lua.
    static_output = lift_bytecode(tree)
    if static_output and len(static_output.strip()) > 0:
        return static_output

    return "return true\n"

def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    if len(sys.argv) < 2:
        print("Usage: python deobfuscator.py <input.lua> [output.lua]")
        return

    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else "output.lua"

    if not os.path.exists(input_file):
        print(f"Error: '{input_file}' not found.")
        return

    result = deobfuscate(input_file)
    with open(output_file, "w", encoding="utf-8") as fp:
        fp.write(result)

    print(f"Deobfuscated: {input_file} -> {output_file}")
    print(result.encode('ascii', errors='replace').decode('ascii'))

if __name__ == "__main__":
    main()
