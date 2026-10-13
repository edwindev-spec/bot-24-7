"""Prepare Luraph Fusion; retain the optional Prometheus patches."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

ENGINE_REVISION = "abf3d61fbcd4025e2423fda47f19dc491d13cc2e"
ENGINE_REPOSITORY = "https://github.com/EdwinVM-luraph/luraph.git"


def luraph_directory():
    here = Path(__file__).resolve().parent
    configured = os.environ.get("LURAPH_RECOVER_DIR")
    if configured:
        directory = Path(configured).expanduser()
        return (directory if directory.is_absolute() else here / directory).resolve()
    for candidate in (here, here / "luraph-fusion", here / "luraph",
                      Path.cwd(), Path.cwd() / "luraph-fusion", Path.cwd() / "luraph"):
        if (candidate / "luraph" / "cli.py").is_file():
            return candidate.resolve()
    return here / "luraph-fusion"


PROMETHEUS_OLD_VALIDATION = r"""function validate(code, executable) {
  const candidates = executable ? [executable] : ['luac5.1', 'luac51', 'luac'];
  let compiler;
  for (const candidate of candidates) {
    const version = spawnSync(candidate, ['-v'], { encoding: 'utf8', timeout: 3000 });
    if (!version.error && version.status === 0 && /Lua 5\.1\b/.test(version.stdout + version.stderr)) {
      compiler = candidate;
      break;
    }
  }
  if (!compiler) return { status: 'unavailable', reason: 'Lua 5.1 compiler required; set --luac or LUAC51.' };
"""

PROMETHEUS_NEW_VALIDATION = r"""const compilerCache = new Map();
function validate(code, executable) {
  const cacheKey = executable || '';
  let compiler = compilerCache.get(cacheKey);
  if (!compiler) {
    const candidates = executable ? [executable] : ['luac5.1', 'luac51', 'luac'];
    for (const candidate of candidates) {
      const version = spawnSync(candidate, ['-v'], { encoding: 'utf8', timeout: 3000 });
      if (!version.error && version.status === 0 && /Lua 5\.1\b/.test(version.stdout + version.stderr)) {
        compiler = candidate;
        break;
      }
    }
    if (compiler) compilerCache.set(cacheKey, compiler);
  }
  if (!compiler) return { status: 'unavailable', reason: 'Lua 5.1 compiler required; set --luac or LUAC51.' };
"""


def prepare_prometheus(repo):
    validate_file = repo / "src" / "validate.js"
    validate_text = validate_file.read_text(encoding="utf-8")
    old_validation = PROMETHEUS_OLD_VALIDATION
    new_validation = PROMETHEUS_NEW_VALIDATION
    if "const compilerCache = new Map();" not in validate_text:
        if old_validation not in validate_text:
            raise RuntimeError("No coincide el bloque esperado de validate.js; se aborta para evitar un parche incorrecto.")
        validate_file.write_text(validate_text.replace(old_validation, new_validation, 1), encoding="utf-8")

    cli = repo / "bin" / "recover.js"
    cli_text = cli.read_text(encoding="utf-8")
    cli_text = "".join(
        line for line in cli_text.splitlines(keepends=True)
        if "path.join(destination, 'report.json')" not in line
        and "path.join(destination, 'strings.json')" not in line
    )
    cli.write_text(cli_text, encoding="utf-8")
    print("Motor Prometheus preparado.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--skip-prometheus", action="store_true")
    parser.add_argument("--skip-dependencies", action="store_true", help="el bot ya tiene sus dependencias instaladas")
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs debe ser positivo")
    if sys.version_info < (3, 10):
        parser.error("Se necesita Python 3.10+")
    for command in ("node", "git", "cmake"):
        if not shutil.which(command):
            parser.error(f"Falta {command}; instálalo antes de preparar el motor.")
    repo = luraph_directory()
    if not (repo / "luraph" / "cli.py").is_file():
        if repo.exists() and (not repo.is_dir() or any(repo.iterdir())):
            parser.error(f"{repo} existe pero no contiene el motor. Configura LURAPH_RECOVER_DIR.")
        subprocess.run(["git", "clone", "--no-checkout", ENGINE_REPOSITORY, str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "checkout", "--detach", ENGINE_REVISION], check=True)
    requirements = Path(__file__).resolve().parent / "requirements-bot.txt"
    dependencies = ["-r", str(requirements)] if requirements.is_file() else ["discord.py>=2.4,<3", "aiohttp>=3.10,<4"]
    if not args.skip_dependencies:
        subprocess.run([sys.executable, "-m", "pip", "install", *dependencies], check=True)
    subprocess.run([sys.executable, str(repo / "scripts" / "build_runtime.py"), "--jobs", str(args.jobs)], check=True)
    prometheus = Path(os.environ.get("DEOBF_RECOVER_DIR", str(Path(__file__).resolve().parent / "prometheus-recover"))).resolve()
    if not args.skip_prometheus and (prometheus / "src" / "validate.js").is_file():
        prepare_prometheus(prometheus)
    print(f"Motor Luraph preparado en {repo}. Inicia: python bot.py")


if __name__ == "__main__":
    main()
