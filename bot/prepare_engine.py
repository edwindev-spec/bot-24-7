from pathlib import Path

repo = Path("/app/prometheus-recover")
validate_file = repo / "src" / "validate.js"
validate_text = validate_file.read_text(encoding="utf-8")
old_validation = r"""function validate(code, executable) {
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
new_validation = r"""const compilerCache = new Map();
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
