"""Discord: .luraph (Luraph Fusion) and .deobf (optional Prometheus)."""
import asyncio
import hashlib
import io
import ipaddress
import json
import logging
import re
import random
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
    return await ctx.send("Procesando…")


async def clear_progress(task):
    task.cancel()
    results = await asyncio.gather(task, return_exceptions=True)
    if not results or isinstance(results[0], BaseException):
        return
    message = results[0]
    delete = getattr(message, "delete", None)
    if callable(delete):
        try:
            await delete()
        except discord.HTTPException:
            pass


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


class Ironbrew1Deobfuscator:
    """Run the pinned IronBrew1 tracer/lifter in a resource-limited child."""

    def __init__(self):
        default = Path(__file__).resolve().parent / "ironbrew1-engine"
        self.root = Path(os.environ.get("IRONBREW1_ENGINE_DIR", str(default))).resolve()
        self.deobf = self.root / "deobf"
        self.cli = self.deobf / "deob.py"
        self.bin = self.deobf / "bin"
        self.luau = self.bin / "luau"
        self.ast = self.bin / "luau-ast"
        current_uid = os.geteuid() if hasattr(os, "geteuid") else 65534
        current_gid = os.getegid() if hasattr(os, "getegid") else 65534
        configured_uid = os.environ.get("IRONBREW1_UID")
        configured_gid = os.environ.get("IRONBREW1_GID")
        self.worker_uid = int(configured_uid) if configured_uid else (65534 if current_uid == 0 else current_uid)
        self.worker_gid = int(configured_gid) if configured_gid else (65534 if current_uid == 0 else current_gid)
        self.process = None
        self.lock = asyncio.Lock()
        self.closed = False
        self.environment = {
            "PATH": os.pathsep.join([str(self.bin), os.defpath]),
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
        }

    async def prepare(self):
        if os.name != "posix":
            raise ProcessingError("El motor `.ironbrew1` requiere el contenedor Linux del proyecto.")
        if self.worker_uid == 0 or self.worker_gid == 0:
            raise ProcessingError("El motor `.ironbrew1` no permite ejecutarse con privilegios de root.")
        if os.geteuid() != 0 and (self.worker_uid != os.geteuid() or self.worker_gid != os.getegid()):
            raise ProcessingError("No puedo cambiar al usuario sin privilegios configurado para `.ironbrew1`.")
        if (not self.cli.is_file()
                or any(not path.is_file() or not os.access(path, os.X_OK) for path in (self.luau, self.ast))):
            raise ProcessingError("No encuentro IronBrew1 o sus binarios Linux Luau/luau-ast; reconstruye el contenedor.")
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(self.cli), "--help", cwd=str(self.deobf), env=self.environment,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=True,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=12)
            if process.returncode or b"ironbrew1" not in output.lower():
                raise ProcessingError("No pude iniciar el CLI aislado de `.ironbrew1`.")
        except asyncio.TimeoutError:
            raise ProcessingError("El CLI `.ironbrew1` no respondió al iniciar.") from None
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
        await process.wait()

    async def run(self, source, timeout=90):
        async with self.lock:
            if self.closed:
                raise ProcessingError("Reinicia el bot antes de procesar otro archivo.")
            if source.stat().st_size > MAX_INPUT:
                raise ProcessingError("El archivo `.ironbrew1` debe pesar como máximo 2 MiB.")
            with tempfile.TemporaryDirectory(prefix="ironbrew1-") as folder:
                workdir = Path(folder)
                isolated_source = workdir / "entrada.lua"
                result = workdir / "resultado.lua"
                isolated_source.write_bytes(source.read_bytes())
                if self.worker_uid != os.geteuid():
                    os.chown(workdir, self.worker_uid, self.worker_gid)
                    os.chown(isolated_source, self.worker_uid, self.worker_gid)

                def limit_child_resources():
                    import resource
                    resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout + 2))
                    resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024, 32 * 1024 * 1024))
                    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
                    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
                    memory_limit = 768 * 1024 * 1024
                    resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))
                    resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
                    if self.worker_uid != os.geteuid():
                        os.setgroups([])
                        os.setgid(self.worker_gid)
                        os.setuid(self.worker_uid)

                with tempfile.TemporaryFile() as log:
                    process = await asyncio.create_subprocess_exec(
                        sys.executable, str(self.cli), str(isolated_source),
                        "--output", str(result), "--obfuscator", "ironbrew1",
                        "--timeout", "65", "--budget", "25", "--no-pypy",
                        cwd=str(workdir), env=self.environment,
                        stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=log,
                        start_new_session=True, preexec_fn=limit_child_resources,
                    )
                    self.process = process
                    try:
                        await asyncio.wait_for(process.wait(), timeout=timeout + 4)
                    except asyncio.TimeoutError:
                        raise ProcessingError("El archivo `.ironbrew1` superó el tiempo permitido.") from None
                    finally:
                        await self._stop()
                    if process.returncode != 0:
                        raise ProcessingError("IronBrew1 no pudo recuperar este archivo Luau compatible.")
                    if not result.is_file() or result.stat().st_size == 0:
                        raise ProcessingError("El motor `.ironbrew1` no produjo una salida.")
                    if result.stat().st_size > 32 * 1024 * 1024:
                        raise ProcessingError("El resultado `.ironbrew1` supera el límite de 32 MiB.")
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
                "--output", str(result.resolve()), "--require-compile", "--mode", "auto", "--trace-unsupported",
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
        self.ironbrew1_engine = Ironbrew1Deobfuscator()
        self.lb_engine = ChaoticEvilDeobfuscator()
        self.iv_engine = IronveilDeobfuscator()
        self.mv_engine = MoonVeilDeobfuscator()
        self.mvv2_engine = MoonVeilV2Deobfuscator()
        self.engine_errors = {}

    async def setup_hook(self):
        for name, engine in (("deobf", self.engine), ("luraph", self.luraph_engine),
                             ("ironbrew", self.ironbrew_engine), ("ironbrew1", self.ironbrew1_engine),
                             ("lb", self.lb_engine),
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
            await self.ironbrew1_engine.close()
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
    ironbrew1_cache = ResultCache()
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
        print(f"Recover conectado como {bot.user}. Comandos: .help, .get, .deobf, .luraph, .ironbrew, .ironbrew1, .lb, .iv, .mv y .mvv2.")

    @bot.command(name="help")
    async def show_help(ctx):
        """List the available bot commands and their basic usage."""
        await ctx.send(
            "**Comandos disponibles**\n"
            "`.help` — Muestra esta lista.\n"
            "`.get <URL>` — Hace GET a URLs/APIs HTTP(S) públicas (máx. 2 MiB); entrega JSON legible y conserva nombres seguros del endpoint, validando DNS y redirecciones.\n"
            "`.deobf` — Recupera código Prometheus desde un adjunto `.lua`/`.txt` o una URL directa.\n"
            "`.lb <URL>` o adjunta `.lua`/`.txt` (máx. 2 MiB) — prueba recuperación estática para LuaObfuscator Chaotic Evil; best-effort, sin ejecutar el script.\n"
            "`.luraph` — Recupera Luraph v14.7–v15 desde un adjunto `.lua`/`.luau`/`.txt` o una URL directa.\n"
            "`.ironbrew <URL>` o adjunta `.lua`/`.txt` (máx. 2 MiB) — desofusca Ironbrew2 y devuelve bytecode Lua 5.1 (`.luac`).\n"
            "`.ironbrew1 <URL>` o adjunta `.lua`/`.luau`/`.txt` (máx. 2 MiB) — ejecuta IronBrew1 instrumentado en Luau simulado; puede devolver una traza, no fuente recuperada.\n"
            "`.iv <URL>` o adjunta `.lua`/`.txt` (máx. 2 MiB) — prueba recuperación de Ironveil V1 y devuelve Lua como `resultado.lua`.\n"
            "`.mv <URL>` o adjunta `.lua`/`.luau`/`.txt` (máx. 2 MiB) — recupera MoonVeil V1.4.5 con el motor 0.13.0 y decodificación instrumentada; no ejecuta el script protegido ni contacta servicios externos.\n"
            "`.mvv2 <URL>` o adjunta `.lua`/`.luau`/`.txt` (máx. 2 MiB) — motor 0.3.0: usa el perfil estático y prueba traza simbólica para otros layouts; una variante conocida con callbacks devuelve un informe `.txt`, no código fuente.\n"
            "Si el archivo Luraph no tiene cabecera, adjúntalo y escribe `.luraph 14.8` (también admite 14.7, 14.9 y 15).\n"
            "Los comandos `.get`, `.deobf`, `.luraph`, `.ironbrew`, `.ironbrew1`, `.lb`, `.iv`, `.mv` y `.mvv2` están restringidos al propietario configurado del bot."
        )

    @bot.command(name="get")
    async def get_raw(ctx, url: str = ""):
        """Fetch a public HTTP(S) text URL and return its content as a downloadable file."""
        if ctx.author.id != owner_id:
            return

        url = url.strip().strip("<>")
        if not url:
            await ctx.send("Uso: `.get <URL>`")
            return

        try:
            parsed = validate_public_http_url(url)
        except (ValueError, UnicodeError):
            await ctx.send(
                "URL no válida. Usa una URL pública que empiece por `https://` o `http://`."
            )
            return

        # ── Configuración centralizada ───────────────────────────────────────────
        TOTAL_DEADLINE = 35.0
        MAX_REDIRECTS = 5
        CONNECT_TIMEOUT = 8.0
        SOCK_READ_TIMEOUT = 10.0
        ATTEMPT_TIMEOUT = 12.0
        CHUNK_SIZE = 64 * 1024
        PREVIEW_BYTES = 12288

        REQUEST_HEADERS = {
            "User-Agent": "PrometheusRecoverBot/1.0 (+Discord URL fetcher)",
            "Accept": (
                "application/json, application/problem+json, text/plain;q=0.9, "
                "application/octet-stream;q=0.8, text/html;q=0.5, */*;q=0.2"
            ),
        }

        loop = asyncio.get_running_loop()
        deadline = loop.time() + TOTAL_DEADLINE
        current_url = url
        https_required = parsed.scheme == "https"
        redirect_count = 0
        final_status: int | None = None
        final_headers: dict | None = None
        raw_data: bytes | None = None
        content_type: str | None = None

        # ── Helpers internos ─────────────────────────────────────────────────────
        def remaining_time() -> float:
            return max(0.0, deadline - loop.time())

        def make_timeout() -> aiohttp.ClientTimeout:
            rem = remaining_time()
            return aiohttp.ClientTimeout(
                total=min(ATTEMPT_TIMEOUT, rem),
                connect=min(CONNECT_TIMEOUT, rem),
                sock_read=min(SOCK_READ_TIMEOUT, rem),
            )

        def is_protection_page(url: str, body_preview: str) -> bool:
            url_l = url.lower()
            body_l = body_preview.lower()
            return (
                "yisus-hub.vercel.app/protection" in url_l
                or ("protección del endpoint" in body_l and "solicitud navegador" in body_l)
                or "this endpoint is protected" in body_l
                or ("protected by" in body_l and "script protection" in body_l)
                or ("access denied" in body_l and "cloudflare" in body_l)
                or ("just a moment" in body_l and "cloudflare" in body_l)
                or "checking your browser" in body_l
                or "enable javascript and cookies" in body_l
            )

        def safe_filename(name: str, is_json: bool) -> str:
            name = name.replace("\\", "/").rsplit("/", 1)[-1]
            name = re.sub(r"[\x00-\x1f\x7f]", "", name)
            name = re.sub(r"[^\w.() -]", "_", name, flags=re.UNICODE).strip(" .")[:120]
            return name if name not in ("", ".", "..") else ("raw.json" if is_json else "raw.txt")

        def extract_filename(headers: dict, url: str, is_json: bool) -> str:
            # 1. Content-Disposition
            cd = headers.get("Content-Disposition") or headers.get("content-disposition")
            if cd:
                # filename*=UTF-8''... o filename="..."
                import re
                match = re.search(
                    r"filename\*=UTF-8''([^;]+)|filename=\"([^\"]+)\"|filename=([^;]+)",
                    cd,
                    re.IGNORECASE,
                )
                if match:
                    name = next(g for g in match.groups() if g)
                    name = name.strip().strip('"')
                    if name:
                        return safe_filename(name, is_json)

            # 2. Path de la URL
            from urllib.parse import urlparse, unquote
            path = unquote(urlparse(url).path)
            if path and path not in ("/", ""):
                base = path.rsplit("/", 1)[-1]
                if base and "." in base:
                    return safe_filename(base, is_json)

            return "raw.json" if is_json else "raw.txt"

        def build_error_message(status: int, headers: dict, body_sample: bytes) -> str:
            detail = None
            try:
                data = json.loads(body_sample)
                if isinstance(data, dict):
                    for key in ("message", "detail", "error_description", "error", "title", "msg"):
                        val = data.get(key)
                        if isinstance(val, str) and val.strip():
                            detail = " ".join(val.split())[:160]
                            break
            except (ValueError, TypeError, RecursionError):
                pass

            if status == 401:
                msg = (
                    "HTTP 401: la API requiere autenticación. "
                    "`.get` solo envía solicitudes GET públicas y no añade tokens."
                )
            elif status == 403:
                msg = (
                    "HTTP 403: el servidor bloqueó o no autorizó la solicitud. "
                    "`.get` no puede saltarse ese control."
                )
            elif status == 429:
                retry_after = headers.get("Retry-After")
                suffix = f" Reintenta después de: {retry_after[:80]}." if retry_after else ""
                msg = f"HTTP 429: la API limitó las solicitudes.{suffix} No se volverá a intentar automáticamente."
            else:
                retry_after = headers.get("Retry-After")
                suffix = (
                    f" El servidor indica reintentar después de: {retry_after[:80]}."
                    if retry_after
                    else ""
                )
                msg = f"La URL respondió con HTTP {status}; no se creó el archivo.{suffix}"

            if detail and status not in (401, 403, 429):
                msg += f"\nDetalle de la API: {discord.utils.escape_markdown(detail)}"
            return msg

        # ── Fetch principal ──────────────────────────────────────────────────────
        try:
            connector = aiohttp.TCPConnector(
                resolver=PublicOnlyResolver(),
                use_dns_cache=True,
                ttl_dns_cache=60,
                limit=8,
                limit_per_host=3,
                enable_cleanup_closed=True,
                force_close=False,
            )

            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=ATTEMPT_TIMEOUT),
                trust_env=False,
                headers=REQUEST_HEADERS,
                connector=connector,
                cookie_jar=aiohttp.DummyCookieJar(),
            ) as session:

                while True:
                    # Seguridad: revalidar SIEMPRE
                    try:
                        parsed = validate_public_http_url(
                            current_url, require_https=https_required
                        )
                    except (ValueError, UnicodeError):
                        await ctx.send(
                            "La URL o una redirección apunta a una dirección no permitida."
                        )
                        return

                    https_required = https_required or parsed.scheme == "https"

                    if remaining_time() <= 0:
                        raise asyncio.TimeoutError

                    success = False
                    for attempt in range(MAX_FETCH_ATTEMPTS):
                        if remaining_time() <= 0:
                            raise asyncio.TimeoutError

                        safe_url = redact_url_for_log(current_url)
                        started = time.perf_counter()

                        HTTP_LOGGER.info(
                            "get_request method=GET attempt=%d/%d url=%s headers=%s",
                            attempt + 1, MAX_FETCH_ATTEMPTS, safe_url,
                            json.dumps(
                                redact_http_headers_for_log(REQUEST_HEADERS),
                                ensure_ascii=False, sort_keys=True,
                            ),
                        )

                        try:
                            async with session.get(
                                current_url,
                                allow_redirects=False,
                                timeout=make_timeout(),
                            ) as resp:
                                elapsed_ms = int((time.perf_counter() - started) * 1000)
                                final_status = resp.status
                                final_headers = dict(resp.headers)
                                actual_request_headers = getattr(
                                    getattr(resp, "request_info", None), "headers", REQUEST_HEADERS
                                )
                                content_type = (
                                    resp.headers.get("Content-Type")
                                    or resp.headers.get("content-type")
                                    or ""
                                )

                                HTTP_LOGGER.info(
                                    "get_response status=%d attempt=%d/%d url=%s elapsed_ms=%d "
                                    "request_headers=%s response_headers=%s",
                                    resp.status, attempt + 1, MAX_FETCH_ATTEMPTS,
                                    safe_url, elapsed_ms,
                                    json.dumps(
                                        redact_http_headers_for_log(actual_request_headers, current_url),
                                        ensure_ascii=False, sort_keys=True,
                                    ),
                                    json.dumps(
                                        redact_http_headers_for_log(resp.headers, current_url),
                                        ensure_ascii=False, sort_keys=True,
                                    ),
                                )

                                # Redirect
                                if resp.status in (301, 302, 303, 307, 308):
                                    location = resp.headers.get("Location")
                                    if not location:
                                        await ctx.send(
                                            "El servidor devolvió una redirección sin destino."
                                        )
                                        return
                                    redirect_count += 1
                                    if redirect_count > MAX_REDIRECTS:
                                        await ctx.send(
                                            f"Demasiadas redirecciones (máximo {MAX_REDIRECTS})."
                                        )
                                        return
                                    current_url = urljoin(current_url, location)
                                    success = True
                                    break

                                if resp.status == 204:
                                    await ctx.send(
                                        "HTTP 204 No Content — la solicitud fue correcta "
                                        "pero no hay contenido para descargar."
                                    )
                                    return

                                # Reintentos por status
                                if (
                                    resp.status in RETRYABLE_HTTP_STATUSES
                                    and attempt < MAX_FETCH_ATTEMPTS - 1
                                ):
                                    delay = retry_wait_seconds(
                                        resp.headers.get("Retry-After"), attempt
                                    )
                                    if delay is not None and loop.time() + delay < deadline:
                                        HTTP_LOGGER.warning(
                                            "get_retry reason=http_status status=%d "
                                            "attempt=%d wait=%.2f url=%s",
                                            resp.status, attempt + 1, delay, safe_url,
                                        )
                                        await asyncio.sleep(delay)
                                        continue

                                # Error definitivo
                                if not (200 <= resp.status < 300):
                                    sample = await resp.content.read(512)
                                    msg = build_error_message(
                                        resp.status, resp.headers, sample
                                    )
                                    await ctx.send(msg)
                                    return

                                # Límite de tamaño
                                if (
                                    resp.content_length is not None
                                    and resp.content_length > MAX_INPUT
                                ):
                                    await ctx.send("El contenido supera el límite de 2 MiB.")
                                    return

                                chunks: list[bytes] = []
                                total = 0
                                async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                                    total += len(chunk)
                                    if total > MAX_INPUT:
                                        await ctx.send(
                                            "El contenido supera el límite de 2 MiB."
                                        )
                                        return
                                    chunks.append(chunk)

                                raw_data = b"".join(chunks)

                                HTTP_LOGGER.info(
                                    "get_body_complete status=%d bytes=%d url=%s",
                                    resp.status, len(raw_data), safe_url,
                                )
                                success = True
                                break

                        except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as exc:
                            HTTP_LOGGER.warning(
                                "get_connection_error attempt=%d/%d url=%s error=%s",
                                attempt + 1, MAX_FETCH_ATTEMPTS, safe_url, type(exc).__name__,
                            )
                            if attempt >= MAX_FETCH_ATTEMPTS - 1:
                                raise

                            delay = min(
                                0.35 * (2 ** attempt) + random.uniform(0.05, 0.35), 2.8
                            )
                            if loop.time() + delay >= deadline:
                                raise asyncio.TimeoutError
                            await asyncio.sleep(delay)

                    if not success:
                        break

                    # Si fue redirect → seguir el while
                    if final_status in (301, 302, 303, 307, 308):
                        continue
                    break

        except asyncio.TimeoutError:
            await ctx.send(
                f"La descarga superó el tiempo límite total de {int(TOTAL_DEADLINE)} segundos "
                "(incluyendo reintentos)."
            )
            return
        except (aiohttp.ClientError, OSError):
            await ctx.send(
                f"No pude descargar esa URL tras hasta {MAX_FETCH_ATTEMPTS} intentos. "
                "Comprueba que sea pública y accesible."
            )
            return

        if raw_data is None:
            await ctx.send("No se obtuvo contenido de la URL.")
            return

        # ── Post-procesado ───────────────────────────────────────────────────────
        preview = raw_data[:PREVIEW_BYTES].decode("utf-8-sig", errors="replace")

        if is_protection_page(current_url, preview):
            await ctx.send(
                "El endpoint redirigió a su página de protección en lugar de devolver "
                "el contenido. `.get` sigue redirecciones públicas, pero el servidor "
                "debe permitir el acceso directo al endpoint."
            )
            return

        # Decodificar
        text_bytes = raw_data.decode("utf-8-sig", errors="replace").encode("utf-8")

        upload_limit = ctx.guild.filesize_limit if ctx.guild else 10 * 1024 * 1024
        output_data = text_bytes
        is_json = False
        json_preview: str | None = None
        json_compact = False

        # Intentar JSON
        try:
            parsed_json = json.loads(text_bytes)
            pretty = (
                json.dumps(parsed_json, ensure_ascii=False, indent=2, allow_nan=False)
                + "\n"
            ).encode("utf-8")
            compact = json.dumps(
                parsed_json, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")

            is_json = True
            if len(pretty) <= upload_limit:
                output_data = pretty
                json_preview = pretty.decode("utf-8").rstrip()
            elif len(compact) <= upload_limit:
                output_data = compact
                json_compact = True
            else:
                await ctx.send(
                    "La respuesta JSON formateada supera el límite de archivos de este servidor."
                )
                return
        except (ValueError, TypeError, RecursionError):
            pass

        if len(output_data) > upload_limit:
            await ctx.send(
                "El archivo resultante supera el límite de archivos de este servidor."
            )
            return

        filename = extract_filename(final_headers or {}, current_url, is_json)

        # Aviso si parece HTML inesperado
        html_warning = ""
        if not is_json and (
            content_type.startswith("text/html")
            or preview.lstrip().lower().startswith(("<!doctype", "<html", "<head"))
        ):
            html_warning = (
                "\n⚠️ El contenido parece ser HTML (posible página de error/protección)."
            )

        # ── Mensaje final ────────────────────────────────────────────────────────
        size = len(output_data)
        redirect_info = (
            f" • {redirect_count} redirección{'es' if redirect_count != 1 else ''}"
            if redirect_count
            else ""
        )
        status_info = f" • HTTP {final_status}" if final_status else ""
        type_info = f" • {content_type.split(';')[0].strip()}" if content_type else ""

        meta = f"({size} bytes{redirect_info}{status_info}{type_info})"

        downloadable = discord.File(io.BytesIO(output_data), filename=filename)

        try:
            if is_json:
                if json_compact:
                    msg = f"JSON descargado como `{filename}` {meta} (compacto por límite).{html_warning}"
                elif json_preview and len(json_preview) <= 900:
                    safe = json_preview.replace("```", "``\u200b`")
                    msg = f"JSON formateado {meta}:{html_warning}\n```json\n{safe}\n```"
                else:
                    msg = f"JSON formateado y descargado como `{filename}` {meta}.{html_warning}"
            else:
                msg = f"Contenido descargado como `{filename}` {meta}.{html_warning}"

            await ctx.send(msg, file=downloadable)
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
        extensions = (".lua", ".luau", ".txt") if command in {"luraph", "ironbrew1", "mv", "mvv2"} else (".lua", ".txt")
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
                validate_public_http_url(url)
            except (ValueError, UnicodeError):
                await ctx.send("URL no válida. Usa una URL pública que empiece por `https://` o `http://`.")
                return
        started = time.perf_counter()
        progress = asyncio.create_task(delayed_progress(ctx))
        try:
            if url:
                timeout = aiohttp.ClientTimeout(total=25, connect=8, sock_read=15)
                connector = aiohttp.TCPConnector(resolver=PublicOnlyResolver())
                async with aiohttp.ClientSession(
                    timeout=timeout, trust_env=False, connector=connector
                ) as session:
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
            elapsed = time.perf_counter() - started
            detail = " (desde caché)" if cached is not None else ""
            if command == "ironbrew1":
                detail += " — ejecución instrumentada en Luau simulado"
            if command == "luraph" and result_data.startswith(PARTIAL_HEADER):
                detail += " — recuperación parcial: algunos bloques no pudieron recuperarse"
            is_behavior_trace = command == "mvv2" and result_data.startswith(b"MOONVEIL_BEHAVIOR_TRACE_V1\n")
            is_ironbrew1_trace = (
                command == "ironbrew1"
                and b"-- source: " in result_data[:2048]
                and b"-- run status:" in result_data[:2048]
            )
            if is_behavior_trace:
                detail += " — análisis de comportamiento, no es fuente recuperada"
            if is_ironbrew1_trace:
                detail += " — traza de comportamiento, no es fuente recuperada"
            output_name = (
                "resultado.luac" if command == "ironbrew" else
                "resultado-mvv2-traza.txt" if is_behavior_trace else
                "resultado-ironbrew1-traza.txt" if is_ironbrew1_trace else
                "resultado.lua"
            )
            downloadable = discord.File(io.BytesIO(result_data), filename=output_name)
            try:
                await ctx.send(
                    f"{'Traza de comportamiento' if is_behavior_trace or is_ironbrew1_trace else 'Resultado'} en {elapsed:.2f} s{detail}:",
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
            await clear_progress(progress)

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

    @bot.command(name="ironbrew1")
    async def ironbrew1(ctx, url: str = ""):
        await process_attachment(ctx, url, bot.ironbrew1_engine, ironbrew1_cache, "ironbrew1")

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
