#!/usr/bin/env python3
"""Build a pinned Luau runtime and AST parser; no downloaded executables."""
import argparse
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "a62362a53ddc9c629b0e29378a84abb4534d8b64"  # Luau 0.739


def run(*args):
    subprocess.run(args, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs', type=int, default=2)
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error('--jobs must be positive')
    src = ROOT / '.build' / 'luau'
    if not src.exists():
        src.parent.mkdir(parents=True, exist_ok=True)
        run('git', 'clone', '--depth', '1', '--branch', '0.739',
            'https://github.com/luau-lang/luau.git', str(src))
    revision = subprocess.check_output(['git', '-C', str(src), 'rev-parse', 'HEAD'], text=True).strip()
    if revision != COMMIT:
        raise SystemExit('Unexpected Luau revision; remove .build/luau and retry')
    # The emulator installs Roblox Vector3 methods on the vector metatable.
    # Leave only that table writable; fail if the pinned source shape changes.
    vector = src / 'VM' / 'src' / 'lveclib.cpp'
    source = vector.read_text()
    old = '    lua_setreadonly(L, -1, true);\n    lua_pop(L, 1); // pop the metatable'
    new = '    // Fusion: enable emulated Roblox Vector3 members.\n    lua_pop(L, 1); // pop the metatable'
    if new not in source:
        if source.count(old) != 1:
            raise SystemExit('Unexpected vector metatable layout')
        vector.write_text(source.replace(old, new))
    build = ROOT / '.build' / 'runtime'
    run('cmake', '-S', str(src), '-B', str(build), '-DCMAKE_BUILD_TYPE=Release',
        '-DLUAU_BUILD_TESTS=OFF')
    run('cmake', '--build', str(build), '--config', 'Release', '--target',
        'Luau.Repl.CLI', 'Luau.Ast.CLI', '--parallel', str(args.jobs))
    dest = ROOT / 'vendor' / 'caomod2077' / 'bin'
    dest.mkdir(parents=True, exist_ok=True)
    for name in ('luau', 'luau-ast'):
        suffix = '.exe' if __import__('os').name == 'nt' else ''
        candidates = [build / (name + suffix), build / 'Release' / (name + suffix)]
        binary = next((p for p in candidates if p.is_file()), None)
        if binary is None:
            raise SystemExit(f'Missing built executable: {name}')
        shutil.copy2(binary, dest / binary.name)
    print(f'Runtime ready: {dest}')


if __name__ == '__main__':
    main()
