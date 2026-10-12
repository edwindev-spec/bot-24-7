"""Parchea Prometheus Recover para el bot. Es idempotente: se puede ejecutar varias veces."""
from pathlib import Path

REPO = Path("/app/prometheus-recover")
VALIDATE = REPO / "src" / "validate.js"
CLI = REPO / "bin" / "recover.js"
MARKER = "const compilerCache = new Map();"

# Las cadenas son raw: el código JS debe conservar sus barras simples (\. y \b).
OLD_VALIDATION = r"""function validate(code, executable) {
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

NEW_VALIDATION = r"""const compilerCache = new Map();
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

# Líneas del CLI que escriben report.json y strings.json, que el bot no usa.
UNWANTED_CLI_FRAGMENTS = (
    "path.join(destination, 'report.json')",
    "path.join(destination, 'strings.json')",
)


def patch_validate():
    text = VALIDATE.read_text(encoding="utf-8")
    if MARKER in text:
        print("validate.js ya estaba parcheado.")
        return
    if text.count(OLD_VALIDATION) != 1:
        raise RuntimeError(
            "No coincide el bloque esperado de validate.js (o aparece más de una vez). "
            "Se aborta para no aplicar un parche incorrecto."
        )
    VALIDATE.write_text(text.replace(OLD_VALIDATION, NEW_VALIDATION, 1), encoding="utf-8")
    print("validate.js parcheado.")


def patch_cli():
    lines = CLI.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [line for line in lines if not any(f in line for f in UNWANTED_CLI_FRAGMENTS)]
    removed = len(lines) - len(kept)
    if removed:
        CLI.write_text("".join(kept), encoding="utf-8")
    print(f"recover.js: {removed} línea(s) eliminadas.")


if __name__ == "__main__":
    patch_validate()
    patch_cli()
    print("Motor Prometheus preparado.")
