"""Discord: .luraph (Luraph Fusion) and .deobf (optional Prometheus)."""
import asyncio
import hashlib
import io
import ipaddress
import json
import logging
import re
import signal
import socket
import sys
import shutil
import os
import tempfile
import time
from collections import OrderedDict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import aiohttp
import discord
from aiohttp import web
from discord.ext import commands

from result_cache import ResultStore, engine_namespace

DEOBF_DIR = Path(os.environ.get(
    "DEOBF_RECOVER_DIR", str(Path(__file__).resolve().parent / "prometheus-recover")
)).resolve()
def find_luraph_directory():
    """Recheck installed engines when preparation happened after startup."""
    here = Path(__file__).resolve().parent
    configured = os.environ.get("LURAPH_RECOVER_DIR")
    if configured:
        directory = Path(configured).expanduser()
        return (directory if directory.is_absolute() else here / directory).resolve()
    candidates = [here, here / "luraph-fusion", here / "luraph",
                  Path.cwd(), Path.cwd() / "luraph-fusion", Path.cwd() / "luraph"]
    for candidate in candidates:
        if (candidate / "luraph" / "cli.py").is_file():
            return candidate.resolve()
    return here / "luraph-fusion"


LURAPH_DIR = find_luraph_directory()
PARTIAL_HEADER = b"-- Recuperacion parcial: algunos bloques no pudieron recuperarse.\n"
LURAPH_VERSIONS = ("14.7", "14.8", "14.9", "15")
MAX_INPUT = 2 * 1024 * 1024
BUSY = asyncio.Lock()
MAX_FETCH_ATTEMPTS = 3
RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}
MAX_AUTOMATIC_RETRY_WAIT = 5.0
HTTP_LOGGER = logging.getLogger("bot.http")
_LOG_SENSITIVE_NAME = re.compile(
    r"(?:auth|token|api[-_]?key|access[-_]?key|secret|credential|password|"
    r"signature|session|jwt|nonce|csrf|bypass)", re.IGNORECASE,
)
_LOG_SECRET_PATH_MARKER = re.compile(
    r"^(?:token|key|secret|auth|session|signature|sig|password|credential)$",
    re.IGNORECASE,
)
_LOG_LONG_TOKEN = re.compile(r"^[A-Za-z0-9._~-]{24,}$")
_AUDITABLE_HEADER_VALUES = {
    "accept", "accept-encoding", "cache-control", "content-disposition",
    "content-encoding", "content-length", "content-type", "date", "origin",
    "retry-after", "server", "user-agent", "x-correlation-id", "x-request-id",
    "cf-ray", "x-vercel-id",
}


def _safe_log_text(value, limit=512):
    text = "".join(char if char.isprintable() else " " for char in str(value))
    return text[:limit]


def redact_url_for_log(url):
    """Hide query values and token-like path components before writing URLs to logs."""
    try:
        parsed = urlsplit(str(url))
        path_parts = []
        redact_next = False
        for segment in parsed.path.split("/"):
            if redact_next and not segment:
                safe_segment = segment
            elif redact_next or _LOG_LONG_TOKEN.fullmatch(segment):
                safe_segment = "[REDACTED]"
                redact_next = False
            else:
                safe_segment = segment
            path_parts.append(safe_segment)
            if _LOG_SECRET_PATH_MARKER.fullmatch(segment):
                redact_next = True
        safe_query = []
        for key, _value in parse_qsl(parsed.query, keep_blank_values=True):
            safe_key = "[REDACTED_KEY]" if _LOG_SENSITIVE_NAME.search(key) else key
            safe_query.append((safe_key, "[REDACTED]"))
        safe_netloc = parsed.netloc.rsplit("@", 1)[-1]
        safe = urlunsplit((parsed.scheme, safe_netloc, "/".join(path_parts),
                           urlencode(safe_query), ""))
        return _safe_log_text(safe, 1024)
    except (TypeError, ValueError, UnicodeError):
        return "[invalid-url]"


def redact_http_headers_for_log(headers, base_url=""):
    """Keep safe header values, redact known secrets, and omit unknown values."""
    safe = []
    for index, (name, value) in enumerate(headers.items()):
        if index >= 100:
            safe.append({"name": "[TRUNCATED]", "value": "header-count-limit"})
            break
        header_name = _safe_log_text(name, 128)
        normalized = str(name).lower()
        if _LOG_SENSITIVE_NAME.search(normalized):
            header_value = "[REDACTED]"
        elif normalized in {"location", "referer"}:
            header_value = redact_url_for_log(urljoin(base_url, str(value)))
        elif normalized in _AUDITABLE_HEADER_VALUES:
            header_value = _safe_log_text(value)
        else:
            header_value = "[OMITTED]"
        safe.append({"name": header_name, "value": header_value})
    return safe


def configure_logging():
    level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
    if not isinstance(level, int):
        level = logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    HTTP_LOGGER.setLevel(level)
    logging.getLogger("discord").setLevel(logging.WARNING)


def retry_wait_seconds(retry_after, attempt):
    """Return a bounded Retry-After/backoff delay, or None if server asks us to wait longer."""
    if retry_after:
        try:
            delay = max(0.0, float(retry_after))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                delay = max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                delay = None
        if delay is not None:
            return delay if delay <= MAX_AUTOMATIC_RETRY_WAIT else None
    return min(0.5 * (2 ** attempt), 2.0)


def validate_public_http_url(url, require_https=False):
    """Validate URL syntax and reject literal non-public IP addresses."""
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Only HTTP(S) URLs are accepted")
    if require_https and parsed.scheme != "https":
        raise ValueError("HTTPS redirects may not downgrade to HTTP")
    if parsed.username or parsed.password:
        raise ValueError("Credentials in URLs are not accepted")
    # Accessing .port forces urllib to reject malformed/out-of-range ports early.
    _ = parsed.port
    hostname = parsed.hostname.rstrip(".").lower()
    if (hostname in {"localhost", "localhost.localdomain"}
            or hostname.endswith((".localhost", ".local"))):
        raise ValueError("Local hostnames are not accepted")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not is_public_address(address):
        raise ValueError("Non-public IP addresses are not accepted")
    return parsed


def is_public_address(address):
    """Check routability, including IPv4 addresses embedded in IPv6 forms."""
    if isinstance(address, ipaddress.IPv6Address):
        embedded = address.ipv4_mapped or address.sixtofour
        if embedded is not None and not embedded.is_global:
            return False
        teredo = address.teredo
        if teredo is not None and any(not item.is_global for item in teredo):
            return False
        # The well-known NAT64 prefix preserves the IPv4 address in its low 32 bits.
        if address in ipaddress.IPv6Network("64:ff9b::/96"):
            translated = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
            if not translated.is_global:
                return False
    return address.is_global


class PublicOnlyResolver(aiohttp.abc.AbstractResolver):
    """Resolve a hostname only when every returned address is globally routable."""

    def __init__(self, resolver=None):
        self.resolver = resolver or aiohttp.resolver.DefaultResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        records = await self.resolver.resolve(host, port, family)
        if not records:
            raise OSError("DNS returned no addresses")
        for record in records:
            try:
                address = ipaddress.ip_address(record["host"])
            except (KeyError, ValueError, TypeError) as error:
                raise OSError("DNS returned an invalid address") from error
            if not is_public_address(address):
                raise OSError("DNS resolved to a non-public address")
        return records

    async def close(self):
        await self.resolver.close()


class ProcessingError(Exception):
    pass


class ResultCache:
    """Keep a bounded set of successful results for this bot session."""

    def __init__(self, max_bytes=128 * 1024 * 1024, max_entries=128, store=None):
        self.store = store
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.entries = OrderedDict()
        self.size = 0

    def get(self, key):
        value = self.entries.get(key)
        if value is not None:
            self.entries.move_to_end(key)
        return value

    async def async_get(self, key):
        value = self.get(key)
        if value is None and self.store is not None:
            value = await asyncio.to_thread(self.store.get, key)
            if value is not None:
                self.put(key, value)
        return value

    async def async_put(self, key, value):
        self.put(key, value)
        if self.store is not None:
            await asyncio.to_thread(self.store.put, key, value)

    def put(self, key, value):
        if len(value) > self.max_bytes:
            return
        old = self.entries.pop(key, None)
        if old is not None:
            self.size -= len(old)
        self.entries[key] = value
        self.size += len(value)
        while self.size > self.max_bytes or len(self.entries) > self.max_entries:
            _, removed = self.entries.popitem(last=False)
            self.size -= len(removed)


async def delayed_progress(ctx):
    await asyncio.sleep(1)
    await ctx.send("Procesando…")


class Deobfuscator:
    """Run the pinned recovery CLI and return only the Lua source."""

    def __init__(self, repo=None):
        self.repo = Path(repo or DEOBF_DIR).resolve()
        self.node = shutil.which("node")
        self.luac = shutil.which("luac5.1") or shutil.which("luac51") or shutil.which("luac-5.1")
        self.process = None
        self.lock = asyncio.Lock()
        self.closed = False
        self.environment = {"PATH": os.defpath, "LANG": "C.UTF-8"}

    async def prepare(self):
        if not self.node or not self.luac:
            raise ProcessingError("Ejecuta la primera celda: faltan Node o luac5.1.")
        if not (self.repo / "bin/recover.js").is_file():
            raise ProcessingError("Ejecuta la primera celda para descargar el motor.")
        for command, valid in [
            ([self.node, "--version"], lambda v: int(v.strip().lstrip(b"v").split(b".")[0]) >= 22),
            ([self.luac, "-v"], lambda v: v.startswith(b"Lua 5.1")),
        ]:
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, env=self.environment)
            try:
                version, _ = await asyncio.wait_for(process.communicate(), 10)
                if process.returncode or not valid(version):
                    raise ProcessingError("Se necesitan Node 22+ y Lua 5.1. Ejecuta la primera celda.")
            finally:
                if process.returncode is None:
                    process.kill()
                await process.wait()

    async def _stop(self):
        process, self.process = self.process, None
        if process is not None:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()

    async def run(self, source, timeout=75):
        async with self.lock:
            if self.closed:
                raise ProcessingError("Reinicia el bot antes de procesar otro archivo.")
            # source.parent ya es único para este trabajo: no crear otra carpeta temporal.
            with tempfile.TemporaryFile() as log:
                destination = source.parent / "result"
                process = await asyncio.create_subprocess_exec(
                    self.node, "--max-old-space-size=2048", str(self.repo / "bin/recover.js"),
                    str(source.resolve()), "-o", str(destination), "--luac", self.luac,
                    "--timeout", "30000",
                    cwd=str(source.parent), env=self.environment, stdout=log, stderr=log,
                )
                self.process = process
                try:
                    if self.closed:
                        raise ProcessingError("El bot se está cerrando.")
                    await asyncio.wait_for(process.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    raise ProcessingError("El archivo superó el tiempo permitido.") from None
                finally:
                    await self._stop()
                if process.returncode not in (0, 2):
                    raise ProcessingError("El motor no pudo analizar este archivo Lua/Prometheus.")
                filename = "recovered.lua" if process.returncode == 0 else "partial.lua"
                result = destination / filename
                if result.stat().st_size > 32 * 1024 * 1024:
                    raise ProcessingError("El resultado supera el límite de 32 MiB.")
                result_data = result.read_bytes()
                # No presentar una recuperación incompleta como si fuera código íntegro.
                if filename == "partial.lua":
                    warning = (
                        "-- ADVERTENCIA: Prometheus Recover solo pudo recuperar una parte del código.\n"
                        "-- Revisa este resultado antes de usarlo; puede faltar lógica o haber fragmentos inválidos.\n\n"
                    ).encode("utf-8")
                    result_data = warning + result_data
                # StyLua es opcional: si está instalado, aplicar formato Lua legible.
                # Nunca reemplazar el resultado recuperado si el formateador falla.
                stylua = shutil.which("stylua")
                if stylua and filename == "recovered.lua":
                    formatted = source.parent / "formatted.lua"
                    formatted.write_bytes(result_data)
                    try:
                        format_process = await asyncio.create_subprocess_exec(
                            stylua, "--syntax", "Lua51", str(formatted),
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            cwd=str(source.parent), env=self.environment,
                        )
                        await asyncio.wait_for(format_process.communicate(), timeout=15)
                        if format_process.returncode == 0 and formatted.is_file():
                            result_data = formatted.read_bytes()
                    except (asyncio.TimeoutError, OSError):
                        pass
                return result_data

    async def close(self):
        self.closed = True
        await self._stop()


class LuraphDeobfuscator(Deobfuscator):
    """Run Fusion without Discord credentials in its environment."""

    def __init__(self, repo=None):
        self.repo_override = Path(repo).resolve() if repo is not None else None
        super().__init__(self.repo_override or find_luraph_directory())
        paths = [str(Path(sys.executable).parent), os.defpath]
        if self.node:
            paths.insert(0, str(Path(self.node).parent))
        self.environment = {"PATH": os.pathsep.join(paths), "LANG": "C.UTF-8"}

    async def prepare(self):
        self.repo = self.repo_override or find_luraph_directory()
        self.node = shutil.which("node")
        if self.node:
            self.environment["PATH"] = os.pathsep.join(
                [str(Path(self.node).parent), str(Path(sys.executable).parent), os.defpath])
        suffix = ".exe" if os.name == "nt" else ""
        if sys.version_info < (3, 10) or not self.node:
            raise ProcessingError("Luraph necesita Python 3.10+ y Node.js 18+.")
        if not (self.repo / "luraph" / "cli.py").is_file():
            raise ProcessingError(f"No encuentro el motor Luraph en {self.repo}.")
        for tool in ("luau", "luau-ast"):
            binary = self.repo / "vendor" / "caomod2077" / "bin" / (tool + suffix)
            if not binary.is_file() or (os.name != "nt" and not os.access(binary, os.X_OK)):
                raise ProcessingError("Faltan las herramientas Luau. Ejecuta python prepare_engine.py.")
        process = await asyncio.create_subprocess_exec(
            self.node, "--version", stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env=self.environment)
        try:
            version, _ = await asyncio.wait_for(process.communicate(), 10)
            if process.returncode or int(version.strip().lstrip(b"v").split(b".")[0]) < 18:
                raise ProcessingError("Luraph necesita Node.js 18+.")
        except (ValueError, asyncio.TimeoutError):
            raise ProcessingError("No pude verificar Node.js para Luraph.") from None
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()

    async def ensure_ready(self):
        """Prepare an absent engine on first use, without retaining startup errors."""
        try:
            await self.prepare()
            return
        except ProcessingError as initial_error:
            if not self.node or sys.version_info < (3, 10):
                raise
            setup = Path(__file__).resolve().parent / "prepare_engine.py"
            if not setup.is_file():
                raise ProcessingError(
                    f"{initial_error} Guarda prepare_engine.py junto a bot.py y vuelve a usar .luraph.") from None
        async with self.lock:
            if self.closed:
                raise ProcessingError("Reinicia el bot antes de procesar otro archivo.")
            environment = dict(self.environment, LURAPH_RECOVER_DIR=str(self.repo))
            with tempfile.TemporaryFile() as log:
                process = await asyncio.create_subprocess_exec(
                    sys.executable, str(setup), "--skip-prometheus", "--skip-dependencies", "--jobs", "2",
                    cwd=str(setup.parent), env=environment, stdout=log, stderr=log,
                    start_new_session=os.name != "nt")
                self.process = process
                try:
                    if self.closed:
                        raise ProcessingError("El bot se está cerrando.")
                    await asyncio.wait_for(process.wait(), 1200)
                except asyncio.TimeoutError:
                    raise ProcessingError("La preparación de Luraph superó los 20 minutos.") from None
                finally:
                    await self._stop()
                if process.returncode:
                    log.seek(0, 2)
                    size = log.tell()
                    log.seek(max(0, size - 4000))
                    # Keep setup diagnostics in the host log, without bot credentials.
                    print(log.read().decode("utf8", errors="replace"), file=sys.stderr)
                    raise ProcessingError(
                        "No pude preparar Luraph. Ejecuta python prepare_engine.py --skip-prometheus "
                        "en el servidor y revisa el error; necesita Git, CMake y un compilador C++17.")
            await self.prepare()

    async def _stop(self):
        process, self.process = self.process, None
        if process is not None:
            if os.name != "nt":
                if process.returncode is None:
                    # Fusion owns a separate worker process group. Let its
                    # KeyboardInterrupt handler stop that group before killing
                    # this CLI session, including on bot shutdown/cancellation.
                    try:
                        os.killpg(process.pid, signal.SIGINT)
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(process.wait(), 5)
                    except asyncio.TimeoutError:
                        pass
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif process.returncode is None:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/F", "/T", "/PID", str(process.pid),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await killer.wait()
            await process.wait()

    async def run(self, source, timeout=300, engine=""):
        async with self.lock:
            if self.closed:
                raise ProcessingError("Reinicia el bot antes de procesar otro archivo.")
            if engine and engine not in LURAPH_VERSIONS:
                raise ProcessingError("Versión no válida: usa 14.7, 14.8, 14.9 o 15.")
            result = source.parent / "resultado.lua"
            command = [sys.executable, "-m", "luraph", str(source.resolve()),
                       "-o", str(result.resolve()), "--allow-partial", "--wall-timeout", "240"]
            if engine:
                command.extend(["--engine", engine])
            with tempfile.TemporaryFile() as log:
                process = await asyncio.create_subprocess_exec(
                    *command, cwd=str(self.repo), env=self.environment,
                    stdout=log, stderr=log, start_new_session=os.name != "nt")
                self.process = process
                try:
                    if self.closed:
                        raise ProcessingError("El bot se está cerrando.")
                    await asyncio.wait_for(process.wait(), timeout)
                except asyncio.TimeoutError:
                    raise ProcessingError("El archivo Luraph superó el tiempo permitido.") from None
                finally:
                    await self._stop()
                if process.returncode != 0 or not result.is_file():
                    raise ProcessingError("No pude desvirtualizar el archivo. Comprueba que sea Luraph v14.7–v15; sin cabecera, indica la versión: `.luraph 14.8` con el adjunto.")
                try:
                    report = json.loads(Path(str(result) + ".json").read_text(encoding="utf8"))
                    selected = report.get("selected") or {}
                    if selected.get("kind") not in ("lifted", "partial"):
                        raise ValueError("recovery mode")
                    if result.stat().st_size > 32 * 1024 * 1024:
                        raise ProcessingError("El resultado supera el límite de 32 MiB.")
                    data = result.read_bytes()
                    if not data or hashlib.sha256(data).hexdigest() != selected.get("sha256"):
                        raise ValueError("output evidence mismatch")
                except (OSError, ValueError, TypeError, AttributeError):
                    raise ProcessingError("No pude verificar el resultado del motor Luraph.") from None
                return PARTIAL_HEADER + data if selected["kind"] == "partial" else data


class IronbrewDeobfuscator:
    """Run the pinned Ironbrew bytecode recovery adapter in a separate process."""

    def __init__(self):
        default = Path(__file__).resolve().parent / "ironbrew" / "IronbrewAdapter"
        self.executable = Path(os.environ.get("IRONBREW_DEOBF_PATH", str(default))).resolve()
        self.process = None
        self.lock = asyncio.Lock()
        self.closed = False
        self.environment = {"PATH": os.defpath, "LANG": "C.UTF-8"}

    async def prepare(self):
        if not self.executable.is_file() or (os.name != "nt" and not os.access(self.executable, os.X_OK)):
            raise ProcessingError("No encuentro el motor Ironbrew. Reconstruye y despliega la imagen Docker.")
        process = await asyncio.create_subprocess_exec(
            str(self.executable), "--version", stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env=self.environment,
            start_new_session=os.name != "nt")
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=10)
            if process.returncode or b"IronbrewDeobfuscator adapter" not in output:
                raise ProcessingError("No pude iniciar el motor Ironbrew.")
        except asyncio.TimeoutError:
            raise ProcessingError("El motor Ironbrew no respondió al iniciar.") from None
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()

    async def _stop(self):
        process, self.process = self.process, None
        if process is None:
            return
        if process.returncode is not None:
            await process.wait()
            return
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(process.pid),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await killer.wait()
        await process.wait()

    async def run(self, source, timeout=90):
        async with self.lock:
            if self.closed:
                raise ProcessingError("Reinicia el bot antes de procesar otro archivo.")
            result = source.parent / "resultado.luac"
            try:
                result.unlink()
            except FileNotFoundError:
                pass
            with tempfile.TemporaryFile() as log:
                process = await asyncio.create_subprocess_exec(
                    str(self.executable), str(source.resolve()), str(result.resolve()),
                    cwd=str(source.parent), env=self.environment, stdout=log, stderr=log,
                    start_new_session=os.name != "nt")
                self.process = process
                try:
                    await asyncio.wait_for(process.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    raise ProcessingError("El archivo Ironbrew superó el tiempo permitido.") from None
                finally:
                    await self._stop()
                if process.returncode != 0:
                    raise ProcessingError("Ironbrew no pudo analizar este archivo; verifica que sea Ironbrew2 válido.")
                if not result.is_file() or result.stat().st_size == 0:
                    raise ProcessingError("Ironbrew no produjo bytecode para este archivo.")
                if result.stat().st_size > 32 * 1024 * 1024:
                    raise ProcessingError("El bytecode resultante supera el límite de 32 MiB.")
                return result.read_bytes()

    async def close(self):
        self.closed = True
        await self._stop()


class ChaoticEvilDeobfuscator:
    """Run the authorized LuaObfuscatorChaoticEvil parser in static-only mode."""

    def __init__(self):
        default = Path(__file__).resolve().parent / "lb-engine" / "deobfuscator.py"
        self.script = Path(os.environ.get("LB_DEOBF_PATH", str(default))).resolve()
        self.python = sys.executable
        self.process = None
        self.lock = asyncio.Lock()
        self.closed = False
        self.environment = {"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}

    async def prepare(self):
        if not self.script.is_file():
            raise ProcessingError("No encuentro el motor `.lb`. Reconstruye y despliega la imagen Docker.")
        process = await asyncio.create_subprocess_exec(
            self.python, str(self.script), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env=self.environment,
            start_new_session=os.name != "nt")
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=10)
            if process.returncode or b"Usage: python deobfuscator.py" not in output:
                raise ProcessingError("No pude iniciar el motor `.lb` estático.")
        except asyncio.TimeoutError:
            raise ProcessingError("El motor `.lb` no respondió al iniciar.") from None
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()

    async def _stop(self):
        process, self.process = self.process, None
        if process is None:
            return
        if process.returncode is not None:
            await process.wait()
            return
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(process.pid),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await killer.wait()
        await process.wait()

    async def run(self, source, timeout=60):
        async with self.lock:
            if self.closed:
                raise ProcessingError("Reinicia el bot antes de procesar otro archivo.")
            result = source.parent / "resultado.lua"
            try:
                result.unlink()
            except FileNotFoundError:
                pass

            def limit_child_resources():
                import resource
                resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout + 2))
                resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024, 32 * 1024 * 1024))
                resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
                memory_limit = 768 * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))

            with tempfile.TemporaryFile() as log:
                process = await asyncio.create_subprocess_exec(
                    self.python, str(self.script), str(source.resolve()), str(result.resolve()),
                    cwd=str(source.parent), env=self.environment, stdout=log, stderr=log,
                    start_new_session=os.name != "nt",
                    preexec_fn=limit_child_resources if os.name != "nt" else None,
                )
                self.process = process
                try:
                    await asyncio.wait_for(process.wait(), timeout=timeout + 3)
                except asyncio.TimeoutError:
                    raise ProcessingError("El archivo `.lb` superó el tiempo permitido.") from None
                finally:
                    await self._stop()
                if process.returncode != 0:
                    raise ProcessingError("El motor `.lb` no pudo procesar este archivo Lua.")
                if not result.is_file() or result.stat().st_size == 0:
                    raise ProcessingError("El motor `.lb` no produjo una salida para este archivo.")
                if result.stat().st_size > 32 * 1024 * 1024:
                    raise ProcessingError("El resultado `.lb` supera el límite de 32 MiB.")
                return result.read_bytes()

    async def close(self):
        self.closed = True
        await self._stop()


class IronveilDeobfuscator:
    """Run the pinned Ironveil V1 parser; input Lua is parsed, never executed."""

    def __init__(self):
        default = Path(__file__).resolve().parent / "iv-engine" / "deobfuscator" / "index.js"
        self.script = Path(os.environ.get("IRONVEIL_DEOBF_PATH", str(default))).resolve()
        self.node = shutil.which("node")
        self.process = None
        self.lock = asyncio.Lock()
        self.closed = False
        self.environment = {
            "PATH": os.pathsep.join([str(Path(self.node).parent), os.defpath]) if self.node else os.defpath,
            "LANG": "C.UTF-8",
        }

    async def prepare(self):
        if not self.node or not self.script.is_file():
            raise ProcessingError("No encuentro Node.js o el motor `.iv`. Reconstruye y despliega la imagen Docker.")
        required = (self.script.parent / "utils" / "parse.js",
                    self.script.parent / "Beautiful" / "beautifier.js")
        if any(not path.is_file() for path in required):
            raise ProcessingError("La instalación del motor `.iv` está incompleta.")
        process = await asyncio.create_subprocess_exec(
            self.node, "--version", stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env=self.environment,
            start_new_session=os.name != "nt")
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=10)
            try:
                major = int(output.strip().lstrip(b"v").split(b".", 1)[0])
            except (ValueError, IndexError):
                major = 0
            if process.returncode or major < 18:
                raise ProcessingError("El motor `.iv` requiere Node.js 18 o superior.")
        except asyncio.TimeoutError:
            raise ProcessingError("Node.js no respondió al iniciar el motor `.iv`.") from None
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()

    async def _stop(self):
        process, self.process = self.process, None
        if process is None:
            return
        if process.returncode is not None:
            await process.wait()
            return
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(process.pid),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await killer.wait()
        await process.wait()

    async def run(self, source, timeout=60):
        async with self.lock:
            if self.closed:
                raise ProcessingError("Reinicia el bot antes de procesar otro archivo.")
            result = source.parent / "resultado.lua"
            try:
                result.unlink()
            except FileNotFoundError:
                pass

            def limit_child_resources():
                import resource
                resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout + 2))
                resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024, 32 * 1024 * 1024))
                resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))

            with tempfile.TemporaryFile() as log:
                process = await asyncio.create_subprocess_exec(
                    self.node, "--max-old-space-size=512", str(self.script),
                    str(source.resolve()), str(result.resolve()),
                    cwd=str(source.parent), env=self.environment, stdout=log, stderr=log,
                    start_new_session=os.name != "nt",
                    preexec_fn=limit_child_resources if os.name != "nt" else None,
                )
                self.process = process
                try:
                    await asyncio.wait_for(process.wait(), timeout=timeout + 3)
                except asyncio.TimeoutError:
                    raise ProcessingError("El archivo `.iv` superó el tiempo permitido.") from None
                finally:
                    await self._stop()
                if process.returncode != 0:
                    raise ProcessingError("Ironveil no pudo procesar este archivo Lua.")
                if not result.is_file() or result.stat().st_size == 0:
                    raise ProcessingError("Ironveil no produjo una salida para este archivo.")
                if result.stat().st_size > 32 * 1024 * 1024:
                    raise ProcessingError("El resultado `.iv` supera el límite de 32 MiB.")
                return result.read_bytes()

    async def close(self):
        self.closed = True
        await self._stop()


class MoonVeilDeobfuscator:
    """Run MoonVeil v1.4.5 decode steps with protected-program verification disabled."""

    def __init__(self):
        default = Path(__file__).resolve().parent / "mv-engine"
        self.root = Path(os.environ.get("MV_ENGINE_DIR", str(default))).resolve()
        self.cli = self.root / "moonveil" / "cli.py"
        self.luau_path = Path(
            os.environ.get("LUAU_PATH", "/usr/local/bin/luau")
        ).expanduser().resolve()
        self.process = None
        self.lock = asyncio.Lock()
        self.closed = False
        self.environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(self.root),
            "LUAU_PATH": str(self.luau_path),
        }

    async def prepare(self):
        compiler = self.luau_path.with_name("luau-compile")
        if (
            not self.cli.is_file()
            or not self.luau_path.is_file()
            or not compiler.is_file()
        ):
            raise ProcessingError(
                "No encuentro MoonVeil o el runtime/compilador Luau. "
                "Reconstruye y despliega la imagen Docker."
            )
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "moonveil", "--help",
            cwd=str(self.root), env=self.environment,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=os.name != "nt",
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=15)
            if process.returncode or b"usage: moonveil" not in output.lower():
                raise ProcessingError("No pude iniciar el motor MoonVeil `.mv`.")
        except asyncio.TimeoutError:
            raise ProcessingError("El motor `.mv` no respondió al iniciar.") from None
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()

    async def _stop(self):
        process, self.process = self.process, None
        if process is None:
            return
        if process.returncode is not None:
            await process.wait()
            return
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(process.pid),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        await process.wait()

    async def run(self, source, timeout=90):
        async with self.lock:
            if self.closed:
                raise ProcessingError("Reinicia el bot antes de procesar otro archivo.")
            output_base = source.parent / "moonveil-output"
            result = Path(str(output_base) + ".deobf.lua")
            try:
                result.unlink()
            except FileNotFoundError:
                pass

            def limit_child_resources():
                import resource
                resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout + 2))
                resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
                resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024, 32 * 1024 * 1024))
                resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
                memory_limit = 768 * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))

            command = [
                sys.executable, "-m", "moonveil", "decompile", str(source.resolve()),
                "--output", str(result.resolve()), "--no-verify",
                "--timeout", str(timeout),
            ]
            with tempfile.TemporaryFile() as log:
                process = await asyncio.create_subprocess_exec(
                    *command, cwd=str(self.root), env=self.environment, stdout=log, stderr=log,
                    start_new_session=os.name != "nt",
                    preexec_fn=limit_child_resources if os.name != "nt" else None,
                )
                self.process = process
                try:
                    await asyncio.wait_for(process.wait(), timeout=timeout + 3)
                except asyncio.TimeoutError:
                    raise ProcessingError("El archivo `.mv` superó el tiempo permitido.") from None
                finally:
                    await self._stop()
                if process.returncode != 0:
                    log.seek(0, os.SEEK_END)
                    log_size = log.tell()
                    log.seek(max(0, log_size - 8192))
                    diagnostic = log.read(8192).decode("utf-8", errors="replace").lower()
                    if "no base64 payload found" in diagnostic:
                        message = (
                            "`.mv` no encontró el payload Base64 esperado. El archivo puede usar otro "
                            "formato de empaquetado o no ser una muestra MoonVeil V1.4.5 compatible."
                        )
                        reason = "payload_not_found"
                    elif "luraph v14.x loader detected" in diagnostic:
                        message = "El archivo parece protegido con Luraph, no MoonVeil; prueba `.luraph`."
                        reason = "luraph_detected"
                    elif "incomplete vm spec" in diagnostic:
                        message = (
                            "MoonVeil detectó una VM, pero el perfil V1.4.5 no pudo completar sus datos. "
                            "Puede ser otra versión o una variante no compatible."
                        )
                        reason = "incomplete_vm_profile"
                    elif "unknown payload transform" in diagnostic or "zstandard" in diagnostic or "zstd" in diagnostic:
                        message = "`.mv` reconoció un empaquetado que no pudo descomprimir; puede ser una variante no compatible."
                        reason = "payload_decode_failed"
                    elif "memoryerror" in diagnostic:
                        message = "`.mv` agotó el límite de memoria procesando el archivo."
                        reason = "memory_limit"
                    else:
                        message = (
                            f"MoonVeil terminó con error (código {process.returncode}). "
                            "El formato puede no ser compatible con V1.4.5 o el analizador encontró un caso no soportado."
                        )
                        reason = "analyzer_error"
                    logging.getLogger("bot.mv").warning(
                        "mv_failed returncode=%s reason=%s", process.returncode, reason,
                    )
                    raise ProcessingError(message)
                if not result.is_file() or result.stat().st_size == 0:
                    raise ProcessingError("MoonVeil no produjo una salida para este archivo.")
                if result.stat().st_size > 32 * 1024 * 1024:
                    raise ProcessingError("El resultado `.mv` supera el límite de 32 MiB.")
                return result.read_bytes()

    async def close(self):
        self.closed = True
        await self._stop()


class MoonVeilV2Deobfuscator:
    """Run the narrow MoonVeil 2.1.0 profile in a bounded subprocess."""

    def __init__(self):
        default = Path(__file__).resolve().parent / "mvv2-engine"
        self.root = Path(os.environ.get("MVV2_ENGINE_DIR", str(default))).resolve()
        self.cli = self.root / "moonveil" / "cli.py"
        self.luau_path = Path(
            os.environ.get("LUAU_PATH", "/usr/local/bin/luau")
        ).expanduser().resolve()
        self.process = None
        self.lock = asyncio.Lock()
        self.closed = False
        self.environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(self.root),
            "LUAU_PATH": str(self.luau_path),
        }

    async def prepare(self):
        compiler = self.luau_path.with_name("luau-compile")
        if (
            not self.cli.is_file()
            or not self.luau_path.is_file()
            or not os.access(self.luau_path, os.X_OK)
            or not compiler.is_file()
            or not os.access(compiler, os.X_OK)
        ):
            raise ProcessingError(
                "No encuentro el motor `.mvv2` o Luau/luau-compile. "
                "Reconstruye y despliega la imagen Docker."
            )
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "moonveil", "--help",
            cwd=str(self.root), env=self.environment,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=os.name != "nt",
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=15)
            help_output = output.lower()
            if process.returncode or b"usage:" not in help_output or b"decompile" not in help_output:
                raise ProcessingError("No pude iniciar el motor `.mvv2`.")
        except asyncio.TimeoutError:
            raise ProcessingError("El motor `.mvv2` no respondió al iniciar.") from None
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()

    async def _stop(self):
        process, self.process = self.process, None
        if process is None:
            return
        if process.returncode is not None:
            await process.wait()
            return
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(process.pid),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        await process.wait()

    async def run(self, source, timeout=60):
        async with self.lock:
            if self.closed:
                raise ProcessingError("Reinicia el bot antes de procesar otro archivo.")
            result = source.parent / "moonveil-v2-output.deobf.lua"
            result.unlink(missing_ok=True)

            def limit_child_resources():
                import resource
                resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout + 2))
                resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
                resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024, 32 * 1024 * 1024))
                resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
                memory_limit = 768 * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))

            command = [
                sys.executable, "-m", "moonveil", "decompile", str(source.resolve()),
                "--output", str(result.resolve()), "--require-compile",
                "--timeout", str(timeout),
            ]
            with tempfile.TemporaryFile() as log:
                process = await asyncio.create_subprocess_exec(
                    *command, cwd=str(self.root), env=self.environment,
                    stdout=log, stderr=log, start_new_session=os.name != "nt",
                    preexec_fn=limit_child_resources if os.name != "nt" else None,
                )
                self.process = process
                try:
                    await asyncio.wait_for(process.wait(), timeout=timeout + 3)
                except asyncio.TimeoutError:
                    raise ProcessingError("El archivo `.mvv2` superó el tiempo permitido.") from None
                finally:
                    await self._stop()
                if process.returncode != 0:
                    log.seek(0, os.SEEK_END)
                    log_size = log.tell()
                    log.seek(max(0, log_size - 8192))
                    diagnostic = log.read(8192).decode("utf-8", errors="replace").lower()
                    if any(marker in diagnostic for marker in (
                        "unsupported moonveil version", "unsupported 2.1.0 profile",
                        "unsupported prototype layout", "unknown handler",
                    )):
                        message = (
                            "`.mvv2` solo admite el perfil MoonVeil 2.1.0 calibrado; "
                            "esta variante no coincide o aún no está soportada."
                        )
                        reason = "unsupported_profile"
                    elif "memoryerror" in diagnostic:
                        message = "`.mvv2` agotó el límite de memoria procesando el archivo."
                        reason = "memory_limit"
                    elif "timeout" in diagnostic or "exceeded" in diagnostic:
                        message = "El archivo `.mvv2` excedió el tiempo permitido."
                        reason = "timeout"
                    else:
                        message = (
                            f"El motor `.mvv2` terminó con error (código {process.returncode}); "
                            "el formato puede no coincidir con el perfil compatible."
                        )
                        reason = "analyzer_error"
                    logging.getLogger("bot.mvv2").warning(
                        "mvv2_failed returncode=%s reason=%s", process.returncode, reason,
                    )
                    raise ProcessingError(message)
                if not result.is_file() or result.stat().st_size == 0:
                    raise ProcessingError("El motor `.mvv2` no produjo una salida.")
                if result.stat().st_size > 32 * 1024 * 1024:
                    raise ProcessingError("El resultado `.mvv2` supera el límite de 32 MiB.")
                return result.read_bytes()

    async def close(self):
        self.closed = True
        await self._stop()


class DeobfBot(commands.Bot):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.engine = Deobfuscator()
        self.luraph_engine = LuraphDeobfuscator()
        self.ironbrew_engine = IronbrewDeobfuscator()
        self.lb_engine = ChaoticEvilDeobfuscator()
        self.iv_engine = IronveilDeobfuscator()
        self.mv_engine = MoonVeilDeobfuscator()
        self.mvv2_engine = MoonVeilV2Deobfuscator()
        self.engine_errors = {}

    async def setup_hook(self):
        for name, engine in (("deobf", self.engine), ("luraph", self.luraph_engine),
                             ("ironbrew", self.ironbrew_engine), ("lb", self.lb_engine),
                             ("iv", self.iv_engine), ("mv", self.mv_engine),
                             ("mvv2", self.mvv2_engine)):
            try:
                await engine.prepare()
            except (ProcessingError, OSError) as error:
                self.engine_errors[name] = str(error)
        if self.engine_errors:
            print("Motores no disponibles al iniciar: " + ", ".join(sorted(self.engine_errors)))

    async def close(self):
        try:
            await super().close()
        finally:
            await self.engine.close()
            await self.luraph_engine.close()
            await self.ironbrew_engine.close()
            await self.lb_engine.close()
            await self.iv_engine.close()
            await self.mv_engine.close()
            await self.mvv2_engine.close()


def create_bot(owner_id):
    cache = ResultCache()
    cache_directory = os.environ.get("LURAPH_CACHE_DIR")
    store = None
    if cache_directory:
        try:
            store = ResultStore(cache_directory, engine_namespace(find_luraph_directory(), __file__))
        except OSError:
            # Cache storage is optional; recovery still works without it.
            store = None
    luraph_cache = ResultCache(store=store)
    ironbrew_cache = ResultCache()
    lb_cache = ResultCache()
    iv_cache = ResultCache()
    mv_cache = ResultCache()
    mvv2_cache = ResultCache()
    intents = discord.Intents.default()
    intents.message_content = True
    bot = DeobfBot(
        command_prefix=".", intents=intents, help_command=None,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    @bot.event
    async def on_ready():
        print(f"Recover conectado como {bot.user}. Comandos: .help, .get, .deobf, .luraph, .ironbrew, .lb, .iv, .mv y .mvv2.")

    @bot.command(name="help")
    async def show_help(ctx):
        """List the available bot commands and their basic usage."""
        await ctx.send(
            "**Comandos disponibles**\n"
            "`.help` — Muestra esta lista.\n"
            "`.get <URL>` — Hace GET a URLs/APIs HTTP(S) públicas (máx. 2 MiB); entrega JSON legible como `raw.json` y texto como `raw.txt`, validando DNS y redirecciones.\n"
            "`.deobf` — Recupera código Prometheus desde un adjunto `.lua`/`.txt` o una URL directa.\n"
            "`.lb <URL>` o adjunta `.lua`/`.txt` (máx. 2 MiB) — prueba recuperación estática para LuaObfuscator Chaotic Evil; best-effort, sin ejecutar el script.\n"
            "`.luraph` — Recupera Luraph v14.7–v15 desde un adjunto `.lua`/`.luau`/`.txt` o una URL directa.\n"
            "`.ironbrew <URL>` o adjunta `.lua`/`.txt` (máx. 2 MiB) — desofusca Ironbrew2 y devuelve bytecode Lua 5.1 (`.luac`).\n"
            "`.iv <URL>` o adjunta `.lua`/`.txt` (máx. 2 MiB) — prueba recuperación de Ironveil V1 y devuelve Lua como `resultado.lua`.\n"
            "`.mv <URL>` o adjunta `.lua`/`.luau`/`.txt` (máx. 2 MiB) — recupera MoonVeil V1.4.5 con el motor 0.13.0 y decodificación instrumentada en Luau; omite la verificación del programa protegido.\n"
            "`.mvv2 <URL>` o adjunta `.lua`/`.luau`/`.txt` (máx. 2 MiB) — prueba recuperación del perfil calibrado MoonVeil 2.1.0; las variantes no reconocidas se rechazan.\n"
            "Si el archivo Luraph no tiene cabecera, adjúntalo y escribe `.luraph 14.8` (también admite 14.7, 14.9 y 15).\n"
            "Los comandos `.get`, `.deobf`, `.luraph`, `.ironbrew`, `.lb`, `.iv`, `.mv` y `.mvv2` están restringidos al propietario configurado del bot."
        )

    @bot.command(name="get")
    async def get_raw(ctx, url: str = ""):
        """Fetch a public HTTP(S) text URL and return its content as a .txt file."""
        if ctx.author.id != owner_id:
            return
        url = url.strip().strip("<>")
        if not url:
            await ctx.send("Uso: `.get <URL>`")
            return
        try:
            parsed = validate_public_http_url(url)
        except (ValueError, UnicodeError):
            await ctx.send("URL no válida. Usa una URL pública que empiece por `https://` o `http://`.")
            return

        timeout = aiohttp.ClientTimeout(total=12, connect=8, sock_read=10)
        request_headers = {
            "User-Agent": "PrometheusRecoverBot/1.0 (+Discord URL fetcher)",
            "Accept": "application/json, application/problem+json, text/plain;q=0.9, application/octet-stream;q=0.8, text/html;q=0.5, */*;q=0.2",
        }
        current_url = url
        https_required = parsed.scheme == "https"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 35
        try:
            connector = aiohttp.TCPConnector(
                resolver=PublicOnlyResolver(), use_dns_cache=True, ttl_dns_cache=60,
            )
            async with aiohttp.ClientSession(
                timeout=timeout, trust_env=False, headers=request_headers,
                connector=connector, cookie_jar=aiohttp.DummyCookieJar(),
            ) as session:
                # Follow redirects manually; retry only transient failures, never HTTP 403.
                for _ in range(6):
                    try:
                        parsed = validate_public_http_url(current_url, require_https=https_required)
                    except (ValueError, UnicodeError):
                        await ctx.send("La URL o una redirección apunta a una dirección no permitida.")
                        return
                    https_required = https_required or parsed.scheme == "https"

                    redirected = False
                    for attempt in range(MAX_FETCH_ATTEMPTS):
                        remaining = deadline - loop.time()
                        if remaining <= 0:
                            raise asyncio.TimeoutError
                        attempt_timeout = aiohttp.ClientTimeout(
                            total=min(12, remaining), connect=min(8, remaining),
                            sock_read=min(10, remaining),
                        )
                        retry_delay = None
                        request_started = time.perf_counter()
                        safe_url = redact_url_for_log(current_url)
                        HTTP_LOGGER.info(
                            "get_request method=GET attempt=%d/%d url=%s headers=%s",
                            attempt + 1, MAX_FETCH_ATTEMPTS, safe_url,
                            json.dumps(redact_http_headers_for_log(request_headers), ensure_ascii=False, sort_keys=True),
                        )
                        try:
                            async with session.get(
                                current_url, allow_redirects=False, timeout=attempt_timeout,
                            ) as response:
                                actual_request_headers = getattr(
                                    getattr(response, "request_info", None), "headers", request_headers,
                                )
                                HTTP_LOGGER.info(
                                    "get_response status=%d attempt=%d/%d url=%s headers_elapsed_ms=%d "
                                    "request_headers=%s response_headers=%s",
                                    response.status, attempt + 1, MAX_FETCH_ATTEMPTS, safe_url,
                                    int((time.perf_counter() - request_started) * 1000),
                                    json.dumps(redact_http_headers_for_log(actual_request_headers, current_url),
                                               ensure_ascii=False, sort_keys=True),
                                    json.dumps(redact_http_headers_for_log(response.headers, current_url),
                                               ensure_ascii=False, sort_keys=True),
                                )
                                if response.status in RETRYABLE_HTTP_STATUSES and attempt < MAX_FETCH_ATTEMPTS - 1:
                                    retry_delay = retry_wait_seconds(
                                        response.headers.get("Retry-After"), attempt,
                                    )
                                    if retry_delay is not None and loop.time() + retry_delay < deadline:
                                        # Do not read an arbitrary retry body; closing it releases the socket safely.
                                        HTTP_LOGGER.warning(
                                            "get_retry reason=http_status status=%d attempt=%d/%d "
                                            "wait_seconds=%.2f url=%s",
                                            response.status, attempt + 1, MAX_FETCH_ATTEMPTS,
                                            retry_delay, safe_url,
                                        )
                                    else:
                                        retry_delay = None
                                if retry_delay is None:
                                    if response.status in (301, 302, 303, 307, 308):
                                        location = response.headers.get("Location")
                                        if not location:
                                            await ctx.send("El servidor devolvió una redirección sin destino.")
                                            return
                                        current_url = urljoin(current_url, location)
                                        redirected = True
                                    elif response.status == 204:
                                        await ctx.send("La API respondió HTTP 204 No Content; la solicitud fue correcta, pero no hay contenido para descargar.")
                                        return
                                    elif not 200 <= response.status < 300:
                                        error_body = await response.content.read(512)
                                        detail = None
                                        try:
                                            error_json = json.loads(error_body)
                                            if isinstance(error_json, dict):
                                                for key in ("message", "detail", "error_description", "error", "title"):
                                                    candidate = error_json.get(key)
                                                    if isinstance(candidate, str) and candidate.strip():
                                                        detail = " ".join(candidate.split())[:160]
                                                        break
                                        except (ValueError, TypeError, RecursionError):
                                            pass
                                        if response.status == 401:
                                            message = "HTTP 401: la API requiere autenticación; `.get` solo envía solicitudes GET públicas y no añade tokens."
                                        elif response.status == 403:
                                            message = "HTTP 403: el servidor bloqueó o no autorizó la solicitud. `.get` no puede saltarse ese control; el dueño de la API debe permitir el acceso desde el bot o indicar un método oficial."
                                        elif response.status == 429:
                                            retry_after = response.headers.get("Retry-After")
                                            suffix = f" Reintenta después de: {retry_after[:80]}." if retry_after else ""
                                            message = f"HTTP 429: la API limitó las solicitudes.{suffix} No se volverá a intentar automáticamente."
                                        else:
                                            retry_after = response.headers.get("Retry-After")
                                            suffix = f" El servidor indica reintentar después de: {retry_after[:80]}." if retry_after else ""
                                            message = f"La URL respondió con HTTP {response.status}; no se creó el archivo.{suffix}"
                                        if detail and response.status not in (401, 403, 429):
                                            safe_detail = discord.utils.escape_markdown(detail)
                                            message += f" Detalle de la API: {safe_detail}"
                                        await ctx.send(message)
                                        return
                                    else:
                                        if response.content_length is not None and response.content_length > MAX_INPUT:
                                            await ctx.send("El contenido supera el límite de 2 MiB.")
                                            return
                                        chunks = []
                                        total = 0
                                        async for chunk in response.content.iter_chunked(64 * 1024):
                                            total += len(chunk)
                                            if total > MAX_INPUT:
                                                await ctx.send("El contenido supera el límite de 2 MiB.")
                                                return
                                            chunks.append(chunk)
                                        raw_data = b"".join(chunks)
                                        HTTP_LOGGER.info(
                                            "get_body_complete status=%d attempt=%d/%d url=%s "
                                            "bytes=%d elapsed_ms=%d",
                                            response.status, attempt + 1, MAX_FETCH_ATTEMPTS,
                                            safe_url, len(raw_data),
                                            int((time.perf_counter() - request_started) * 1000),
                                        )
                        except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as error:
                            HTTP_LOGGER.warning(
                                "get_connection_error attempt=%d/%d url=%s error_type=%s",
                                attempt + 1, MAX_FETCH_ATTEMPTS, safe_url, type(error).__name__,
                            )
                            if attempt >= MAX_FETCH_ATTEMPTS - 1:
                                raise
                            retry_delay = min(0.5 * (2 ** attempt), 2.0)
                            if loop.time() + retry_delay >= deadline:
                                raise asyncio.TimeoutError
                            HTTP_LOGGER.warning(
                                "get_retry reason=connection_error attempt=%d/%d "
                                "wait_seconds=%.2f url=%s",
                                attempt + 1, MAX_FETCH_ATTEMPTS, retry_delay, safe_url,
                            )
                        if retry_delay is not None:
                            await asyncio.sleep(retry_delay)
                            continue
                        break
                    if redirected:
                        continue
                    break
                else:
                    await ctx.send("La URL tiene demasiadas redirecciones (máximo 5).")
                    return
        except asyncio.TimeoutError:
            await ctx.send("La descarga superó el tiempo límite total de 35 segundos, incluidos los reintentos.")
            return
        except (aiohttp.ClientError, OSError):
            await ctx.send(f"No pude descargar esa URL tras hasta {MAX_FETCH_ATTEMPTS} intentos. Comprueba que sea pública y accesible.")
            return

        # Some protected endpoints redirect to an explanatory HTML page instead of returning the script.
        preview = raw_data[:8192].decode("utf-8-sig", errors="replace").lower()
        if "yisus-hub.vercel.app/protection" in current_url.lower() or (
            "protección del endpoint" in preview and "solicitud navegador" in preview
        ):
            await ctx.send("El endpoint redirigió a su página de protección en lugar de devolver el contenido del script. `.get` ya sigue redirecciones públicas, pero el servidor debe permitir el acceso directo al endpoint para obtener el loader.")
            return

        # Keep text readable and pretty-print valid JSON API responses.
        text_data = raw_data.decode("utf-8-sig", errors="replace").encode("utf-8")
        upload_limit = ctx.guild.filesize_limit if ctx.guild else 10 * 1024 * 1024
        output_data = text_data
        filename = "raw.txt"
        json_preview = None
        json_compact = False
        try:
            json_value = json.loads(text_data)
            pretty_json = (json.dumps(json_value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
            compact_json = json.dumps(json_value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, RecursionError):
            pretty_json = compact_json = None
        if pretty_json is not None:
            filename = "raw.json"
            if len(pretty_json) <= upload_limit:
                output_data = pretty_json
                json_preview = pretty_json.decode("utf-8").rstrip()
            elif len(compact_json) <= upload_limit:
                output_data = compact_json
                json_compact = True
            else:
                await ctx.send("La respuesta JSON formateada supera el límite de archivos de este servidor de Discord.")
                return
        if len(output_data) > upload_limit:
            await ctx.send("El archivo resultante supera el límite de archivos de este servidor de Discord.")
            return
        downloadable = discord.File(io.BytesIO(output_data), filename=filename)
        if filename == "raw.json":
            if json_compact:
                message = f"JSON descargado como `raw.json` ({len(output_data)} bytes); se dejó compacto para respetar el límite de archivos."
            elif json_preview is not None and len(json_preview) <= 900:
                safe_preview = json_preview.replace("```", "``\u200b`")
                message = f"Respuesta JSON formateada ({len(output_data)} bytes):\n```json\n{safe_preview}\n```"
            else:
                message = f"Respuesta JSON formateada y descargada como `raw.json` ({len(output_data)} bytes)."
        else:
            message = f"Contenido descargado ({len(output_data)} bytes):"
        try:
            await ctx.send(message, file=downloadable)
        finally:
            downloadable.close()

    async def process_attachment(ctx, url, engine, cache, command, version=""):
        if ctx.author.id != owner_id:
            return
        if command != "luraph" and command in bot.engine_errors:
            await ctx.send(bot.engine_errors[command])
            return
        if version and version not in LURAPH_VERSIONS:
            await ctx.send("Versión no válida: usa 14.7, 14.8, 14.9 o 15.")
            return
        extensions = (".lua", ".luau", ".txt") if command in {"luraph", "mv", "mvv2"} else (".lua", ".txt")
        url = url.strip().strip("<>")
        attachments = ctx.message.attachments
        if url and attachments:
            await ctx.send("Usa una URL o adjunta un archivo, no ambos a la vez.")
            return
        if not url and (len(attachments) != 1 or not attachments[0].filename.lower().endswith(extensions)):
            await ctx.send(f"Uso: `.{command} <URL>` o adjunta un único archivo {', '.join(extensions)}.")
            return
        if not url and attachments[0].size > MAX_INPUT:
            await ctx.send("El archivo debe pesar como máximo 2 MiB.")
            return
        if url:
            try:
                parsed = urlsplit(url)
                if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError
                hostname = parsed.hostname.rstrip(".").lower()
                if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith((".localhost", ".local")):
                    raise ValueError
                try:
                    address = ipaddress.ip_address(hostname)
                except ValueError:
                    address = None
                if address is not None and not address.is_global:
                    raise ValueError
            except (ValueError, UnicodeError):
                await ctx.send("URL no válida. Usa una URL pública que empiece por `https://` o `http://`.")
                return
        started = time.perf_counter()
        progress = asyncio.create_task(delayed_progress(ctx))
        try:
            if url:
                timeout = aiohttp.ClientTimeout(total=25, connect=8, sock_read=15)
                async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
                    async with session.get(url, allow_redirects=False) as response:
                        if 300 <= response.status < 400:
                            raise ProcessingError("La URL redirige. Usa el enlace directo al archivo raw.")
                        if response.status != 200:
                            raise ProcessingError(f"La URL respondió con HTTP {response.status}.")
                        chunks = []
                        total = 0
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            total += len(chunk)
                            if total > MAX_INPUT:
                                raise ProcessingError("El archivo debe pesar como máximo 2 MiB.")
                            chunks.append(chunk)
                        input_data = b"".join(chunks)
            else:
                attachment = attachments[0]
                input_data = await asyncio.wait_for(attachment.read(), timeout=30)
            if len(input_data) > MAX_INPUT:
                raise ProcessingError("El archivo debe pesar como máximo 2 MiB.")
            key = hashlib.sha256(version.encode() + b"\0" + input_data).digest()
            cached = await cache.async_get(key)
            if cached is not None:
                # Cache hit: avoid creating a temp directory and writing the result to disk.
                result_data = cached
            else:
                if BUSY.locked():
                    await ctx.send("Estoy procesando otro archivo. Inténtalo cuando termine.")
                    return
                async with BUSY:
                    if command == "luraph":
                        if command in bot.engine_errors:
                            await ctx.send("Preparando el motor Luraph… La primera compilación puede tardar varios minutos.")
                        await engine.ensure_ready()
                        bot.engine_errors.pop("luraph", None)
                    with tempfile.TemporaryDirectory(prefix="deobf-") as folder:
                        source = Path(folder) / "entrada.lua"
                        source.write_bytes(input_data)
                        if command == "luraph":
                            result_data = await engine.run(source, engine=version)
                        else:
                            result_data = await engine.run(source)
                    if len(result_data) <= cache.max_bytes:
                        await cache.async_put(key, result_data)
            upload_limit = ctx.guild.filesize_limit if ctx.guild else 10 * 1024 * 1024
            if len(result_data) > upload_limit:
                raise ProcessingError("El resultado supera el límite de archivos de Discord.")
            progress.cancel()
            await asyncio.gather(progress, return_exceptions=True)
            elapsed = time.perf_counter() - started
            detail = " (desde caché)" if cached is not None else ""
            if command == "luraph" and result_data.startswith(PARTIAL_HEADER):
                detail += " — recuperación parcial: algunos bloques no pudieron recuperarse"
            output_name = "resultado.luac" if command == "ironbrew" else "resultado.lua"
            downloadable = discord.File(io.BytesIO(result_data), filename=output_name)
            try:
                await ctx.send(
                    f"Resultado en {elapsed:.2f} s{detail}:",
                    file=downloadable,
                )
            finally:
                downloadable.close()
        except ProcessingError as error:
            await ctx.send(str(error))
        except asyncio.TimeoutError:
            await ctx.send("La descarga tardó demasiado. Inténtalo de nuevo.")
        except (aiohttp.ClientError, OSError):
            await ctx.send("No pude descargar esa URL. Comprueba que sea pública y accesible.")
        except Exception:
            await ctx.send("No pude completar la operación. Revisa el archivo y los permisos del bot.")
        finally:
            progress.cancel()
            await asyncio.gather(progress, return_exceptions=True)

    @bot.command(name="deobf")
    async def deobf(ctx, url: str = ""):
        await process_attachment(ctx, url, bot.engine, cache, "deobf")

    @bot.command(name="luraph")
    async def luraph(ctx, url: str = "", version: str = ""):
        if url in LURAPH_VERSIONS and not version:
            version, url = url, ""
        await process_attachment(ctx, url, bot.luraph_engine, luraph_cache, "luraph", version)

    @bot.command(name="ironbrew")
    async def ironbrew(ctx, url: str = ""):
        await process_attachment(ctx, url, bot.ironbrew_engine, ironbrew_cache, "ironbrew")

    @bot.command(name="lb")
    async def lb(ctx, url: str = ""):
        await process_attachment(ctx, url, bot.lb_engine, lb_cache, "lb")

    @bot.command(name="iv")
    async def iv(ctx, url: str = ""):
        await process_attachment(ctx, url, bot.iv_engine, iv_cache, "iv")

    @bot.command(name="mv")
    async def mv(ctx, url: str = ""):
        await process_attachment(ctx, url, bot.mv_engine, mv_cache, "mv")

    @bot.command(name="mvv2")
    async def mvv2(ctx, url: str = ""):
        await process_attachment(ctx, url, bot.mvv2_engine, mvv2_cache, "mvv2")

    return bot


async def health_handler(request):
    bot = request.app["discord_bot"]
    connected = bot.is_ready()
    payload = {
        "status": "online" if connected else "starting",
        "discord_connected": connected,
        "bot_user": str(bot.user) if bot.user else None,
        "uptime_seconds": max(0, int(time.time() - SERVICE_STARTED)),
    }
    return web.json_response(payload, status=200 if connected else 503,
                             headers={"Cache-Control": "no-store"})


@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        response = web.Response(status=204)
    else:
        response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Cache-Control"] = "no-store"
    return response


async def start_health_server(bot):
    app = web.Application(middlewares=[cors_middleware])
    app["discord_bot"] = bot
    app.router.add_get("/health", health_handler)
    app.router.add_get("/", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Endpoint de estado disponible en el puerto {port}.", flush=True)
    return runner


SERVICE_STARTED = time.time()


async def main():
    configure_logging()
    token = os.environ.get("DISCORD_TOKEN", "")
    owner = os.environ.get("DISCORD_OWNER_ID", "")
    if not token or not owner.isdecimal():
        raise RuntimeError("Configura DISCORD_TOKEN y DISCORD_OWNER_ID como variables de entorno.")
    bot = create_bot(int(owner))
    runner = await start_health_server(bot)
    try:
        async with bot:
            await bot.start(token)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
