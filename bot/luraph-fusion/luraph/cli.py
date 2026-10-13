"""Repository CLI; engines run in disposable child process groups."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / 'vendor' / 'caomod2077'
HEADER = re.compile(r'This file was protected using Luraph Obfuscator v(\d+)(?:\.(\d+))?')
VERSIONS = ('14.7', '14.8', '14.9', '15')
# These indicate known unresolved paths, not proof of semantic completeness.
UNRESOLVED = re.compile(r'error\(["\'](?:devirt:|Luraph runtime function)|\(nil\)\(|unlifted|unresolved|recursive function', re.I)
RANK = {'lifted': 4, 'partial': 3, 'unverified': 2, 'trace': 1}


def detect(source):
    match = HEADER.search(source[:500])
    if match:
        major, minor = match.groups()
        version = '15' if major == '15' else f'{major}.{minor}'
        return {'engine': version if version in VERSIONS else None,
                'version': version, 'confidence': 1.0, 'reason': 'version header'}
    head = source.lstrip()[:4000]
    if re.match(r'return\s+setmetatable\s*\(\s*\{', head) and (
            re.search(r'\[\d+\]\s*=\s*(?:bit32|buffer|string|table|math)\.', head)
            or 'LPH' in source[:200000]):
        return {'engine': '15', 'version': None, 'confidence': 0.8, 'reason': 'v15 VM shape'}
    if (re.match(r'return\s*\(\s*function\s*\(', head) and 'loadstring' in head) or (
            re.match(r'return\s*\(\s*\{', head) and re.search(r'bit32|loadstring', source[:100000])):
        return {'engine': None, 'version': '14.x', 'confidence': 0.8,
                'reason': 'v14 shape; specify --engine 14.7, 14.8 or 14.9'}
    return {'engine': None, 'version': None, 'confidence': 0.0, 'reason': 'unknown input'}


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('must be a finite positive number')
    return number


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return number


def run_process(command, timeout, env=None):
    """Kill the entire engine tree on timeout or Ctrl-C on POSIX."""
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=env, start_new_session=os.name != 'nt')
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        if os.name == 'nt':
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(process.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.communicate()
        raise
    return process.returncode, stdout, stderr


def validate_output(output, kind, parser, timeout):
    """Parse the candidate independently of the recovery engine."""
    if not output.is_file() or not output.stat().st_size:
        return {'kind': 'invalid', 'error': 'empty or missing output'}
    code, stdout, stderr = run_process([str(parser), str(output)], min(timeout, 30))
    if code:
        return {'kind': 'invalid', 'error': stderr.decode('utf8', errors='replace')[-2000:]}
    ast = json.loads(stdout.decode('latin1'))
    body = ast['root'].get('body', [])
    if not body:
        return {'kind': 'invalid', 'error': 'output contains no executable statements'}
    text = output.read_text(encoding='utf8', errors='replace')
    unresolved = len(UNRESOLVED.findall(text))
    if kind == 'lifted' and unresolved:
        kind = 'partial'
    return {'kind': kind, 'syntax_valid': True, 'unresolved_markers': unresolved,
            'top_level_statements': len(body), 'sha256': hashlib.sha256(output.read_bytes()).hexdigest()}


def run_backend(name, input_file, engine, args, work):
    started = time.monotonic()
    output = work / 'candidate.luau'
    metadata = work / 'metadata.json'
    env = dict(os.environ, PYTHON_BIN=sys.executable)
    # Each child selects its engine without state inherited from previous inputs.
    env.pop('DEOB_ENGINE', None)
    if name == 'caomod2077':
        node = shutil.which('node')
        if not node:
            raise RuntimeError('Node.js 18+ is required')
        config = {'input': str(input_file), 'engine': engine, 'work': str(work),
                  'output': str(output), 'metadata': str(metadata), 'timeout': args.timeout,
                  'budget': args.budget, 'maxRuns': args.max_runs, 'rounds': args.rounds,
                  'traceOnly': args.trace_only}
        config_path = work / 'config.json'
        config_path.write_text(json.dumps(config), encoding='utf8')
        command = [node, str(VENDOR / 'worker.js'), str(config_path)]
    else:
        if not args.kryptit_root:
            raise RuntimeError('KryptIT requires --kryptit-root pointing to your local checkout')
        directory = args.kryptit_root.resolve() / 'Deobfuscator' / 'deobf'
        entry = directory / ('deob.py' if engine == '15' else 'cli.py')
        if not entry.is_file():
            raise RuntimeError(f'Missing external engine: {entry}')
        # Pass a disposable copy; external backends may write caches beside input.
        isolated_input = work / 'input.lua'
        shutil.copy2(input_file, isolated_input)
        command = [sys.executable, str(entry), str(isolated_input), '-o', str(output),
                   '--timeout', str(args.timeout), '--budget', str(args.budget),
                   '--max-runs', str(args.max_runs), '--devirt-rounds', str(args.rounds)]
        command += ['--obfuscator', 'luraph_v15'] if engine == '15' else ['--engine', engine]
        if args.trace_only:
            command.append('--no-devirt')
    code, stdout, stderr = run_process(command, args.wall_timeout, env)
    log = (stdout + stderr).decode('utf8', errors='replace')
    result = {'backend': name, 'seconds': round(time.monotonic() - started, 3),
              'exit_code': code, 'diagnostics': log[-6000:]}
    if code:
        return dict(result, kind='failed'), output
    kind = ('trace' if args.trace_only else 'unverified')
    if name == 'caomod2077':
        kind = json.loads(metadata.read_text())['kind']
    result.update(validate_output(output, kind, args.parser, args.wall_timeout))
    return result, output


def eligible(candidate, args):
    kind = candidate['kind']
    return kind == 'lifted' or (kind in ('partial', 'unverified') and args.allow_partial) or (
        kind == 'trace' and (args.allow_trace or args.trace_only))


def choose(candidates, args):
    available = [c for c in candidates if eligible(c, args)]
    # Prefer structural recovery; fewer unresolved markers wins within a kind.
    return max(available, key=lambda c: (RANK[c['kind']], -c.get('unresolved_markers', 0)), default=None)


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def recover(input_file, output, args):
    source = input_file.read_bytes()
    detection = detect(source.decode('latin1'))
    engine = args.engine or detection['engine']
    if not engine:
        raise RuntimeError(detection['reason'] + '; choose a supported --engine explicitly')
    report = {'schema_version': 1, 'input': str(input_file),
              'input_sha256': hashlib.sha256(source).hexdigest(),
              'detection': detection, 'engine': engine, 'candidates': [], 'selected': None,
              'limitations': 'Syntax and unresolved markers do not prove semantic equivalence or full branch coverage.'}
    backends = ['caomod2077', 'kryptit'] if args.backend == 'all' else [args.backend]
    with tempfile.TemporaryDirectory(prefix='luraph-fusion-') as temp:
        paths = {}
        for name in backends:
            work = Path(temp) / name
            work.mkdir()
            print(f'[*] {input_file.name}: {name}, engine {engine}', file=sys.stderr)
            try:
                candidate, recovered = run_backend(name, input_file, engine, args, work)
                paths[name] = recovered
            except (OSError, RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
                candidate = {'backend': name, 'kind': 'failed', 'error': str(exc)}
            report['candidates'].append(candidate)
        best = choose(report['candidates'], args)
        if best:
            data = paths[best['backend']].read_bytes()
            atomic_write(output, data)
            report['selected'] = {'backend': best['backend'], 'kind': best['kind'],
                                  'output': str(output), 'sha256': best['sha256']}
        # Always keep the evidence, including rejected traces and engine failures.
        atomic_write(Path(str(output) + '.json'), (json.dumps(report, indent=2) + '\n').encode())
    if not best:
        raise RuntimeError('No acceptable recovery; see ' + str(output) + '.json')
    print(f'[+] {best["kind"]}: {output}', file=sys.stderr)


def collect(paths):
    files = []
    for value in paths:
        path = value.resolve()
        if path.is_dir():
            files.extend(sorted(p.resolve() for p in path.iterdir()
                                if p.is_file() and p.suffix.lower() in ('.lua', '.luau', '.txt')))
        elif path.is_file():
            files.append(path)
        else:
            raise RuntimeError(f'Input does not exist: {path}')
    return list(dict.fromkeys(files))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Luraph v14/v15 recovery with JSON evidence')
    parser.add_argument('inputs', nargs='+', type=Path)
    parser.add_argument('-o', '--output', type=Path)
    parser.add_argument('--detect', action='store_true')
    parser.add_argument('--engine', choices=VERSIONS)
    parser.add_argument('--backend', choices=('caomod2077', 'kryptit', 'all'), default='caomod2077')
    parser.add_argument('--kryptit-root', type=Path)
    parser.add_argument('--trace-only', action='store_true')
    parser.add_argument('--allow-trace', action='store_true', help='accept an observed behaviour trace')
    parser.add_argument('--allow-partial', action='store_true', help='accept partial or unverified recovery')
    parser.add_argument('--timeout', type=positive, default=90, help='per runtime pass, seconds')
    parser.add_argument('--wall-timeout', type=positive, default=300, help='total per engine, seconds')
    parser.add_argument('--budget', type=positive, default=30)
    parser.add_argument('--max-runs', type=positive_int, default=12)
    parser.add_argument('--rounds', type=positive_int, default=200)
    suffix = '.exe' if os.name == 'nt' else ''
    parser.add_argument('--parser', type=Path, default=VENDOR / 'bin' / ('luau-ast' + suffix))
    args = parser.parse_args(argv)
    if sys.version_info < (3, 10):
        parser.error('Python 3.10+ is required')
    try:
        files = collect(args.inputs)
        if not files:
            raise RuntimeError('No input files found')
        if args.detect:
            for file in files:
                print(json.dumps({'input': str(file), **detect(file.read_bytes().decode('latin1'))}))
            return 0
        if not args.parser.is_file():
            raise RuntimeError('AST parser missing; run python scripts/build_runtime.py')
        # Explicit output is a directory for batch jobs, a file for one input.
        targets = [args.output.resolve() if args.output and len(files) == 1 else
                   ((args.output.resolve() if args.output else file.parent / 'output') /
                    (file.stem + '.deobf.luau')) for file in files]
        destinations = targets + [Path(str(p) + '.json') for p in targets]
        if len(set(destinations)) != len(destinations):
            raise RuntimeError('Output filename collision; process those inputs separately')
        if set(destinations) & set(files):
            raise RuntimeError('An output or report would overwrite an input file')
        failed = 0
        for file, output in zip(files, targets):
            try:
                recover(file, output, args)
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
                print(f'[!] {file.name}: {exc}', file=sys.stderr)
                failed += 1
        return 1 if failed else 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f'[!] {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
