"""Discord: .luraph (Luraph Fusion) and .deobf (optional Prometheus)."""
import asyncio
import hashlib
import io
import ipaddress
import json
import signal
import sys
import shutil
import os
import tempfile
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import aiohttp
import discord
from aiohttp import web
from discord.ext import commands

DEOBF_DIR = Path(os.environ.get(
    "DEOBF_RECOVER_DIR", str(Path(__file__).resolve().parent / "prometheus-recover")
)).resolve()
LURAPH_DIR = Path(os.environ.get(
    "LURAPH_RECOVER_DIR", str(Path(__file__).resolve().parent if
    (Path(__file__).resolve().parent / "luraph" / "cli.py").is_file() else
    Path(__file__).resolve().parent / "luraph-fusion")
)).resolve()
PARTIAL_HEADER = b"-- Recuperacion parcial: algunos bloques no pudieron recuperarse.\n"
LURAPH_VERSIONS = ("14.7", "14.8", "14.9", "15")
MAX_INPUT = 2 * 1024 * 1024
BUSY = asyncio.Lock()


class ProcessingError(Exception):
    pass


class ResultCache:
    """Keep a bounded set of successful results for this bot session."""

    def __init__(self, max_bytes=128 * 1024 * 1024, max_entries=128):
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.entries = OrderedDict()
        self.size = 0

    def get(self, key):
        value = self.entries.get(key)
        if value is not None:
            self.entries.move_to_end(key)
        return value

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
                return result.read_bytes()

    async def close(self):
        self.closed = True
        await self._stop()


class LuraphDeobfuscator(Deobfuscator):
    """Run Fusion without Discord credentials in its environment."""

    def __init__(self, repo=None):
        super().__init__(repo or LURAPH_DIR)
        paths = [str(Path(sys.executable).parent), os.defpath]
        if self.node:
            paths.insert(0, str(Path(self.node).parent))
        self.environment = {"PATH": os.pathsep.join(paths), "LANG": "C.UTF-8"}

    async def prepare(self):
        suffix = ".exe" if os.name == "nt" else ""
        if sys.version_info < (3, 10) or not self.node:
            raise ProcessingError("Luraph necesita Python 3.10+ y Node.js 18+.")
        if not (self.repo / "luraph" / "cli.py").is_file():
            raise ProcessingError("No encuentro Luraph. Ejecuta prepare_engine.py o configura LURAPH_RECOVER_DIR.")
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


class DeobfBot(commands.Bot):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.engine = Deobfuscator()
        self.luraph_engine = LuraphDeobfuscator()
        self.engine_errors = {}

    async def setup_hook(self):
        for name, engine in (("deobf", self.engine), ("luraph", self.luraph_engine)):
            try:
                await engine.prepare()
            except (ProcessingError, OSError) as error:
                self.engine_errors[name] = str(error)
        if len(self.engine_errors) == 2:
            raise ProcessingError("No hay un motor preparado. " + self.engine_errors["luraph"])

    async def close(self):
        try:
            await super().close()
        finally:
            await self.engine.close()
            await self.luraph_engine.close()


def create_bot(owner_id):
    cache = ResultCache()
    luraph_cache = ResultCache()
    intents = discord.Intents.default()
    intents.message_content = True
    bot = DeobfBot(
        command_prefix=".", intents=intents, help_command=None,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    @bot.event
    async def on_ready():
        print(f"Recover conectado como {bot.user}. Envía .luraph con un archivo .lua, .luau o .txt; .deobf usa Prometheus.")

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
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ValueError
            if parsed.username or parsed.password:
                raise ValueError
            hostname = parsed.hostname.rstrip(".").lower()
            if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".localhost") or hostname.endswith(".local"):
                raise ValueError
            try:
                address = ipaddress.ip_address(hostname)
            except ValueError:
                address = None
            if address is not None and (not address.is_global):
                raise ValueError
        except (ValueError, UnicodeError):
            await ctx.send("URL no válida. Usa una URL pública que empiece por `https://` o `http://`.")
            return

        timeout = aiohttp.ClientTimeout(total=25, connect=8, sock_read=15)
        request_headers = {
            "User-Agent": "PrometheusRecoverBot/1.0 (+Discord URL fetcher)",
            "Accept": "text/plain, application/json, application/octet-stream, text/html;q=0.8, */*;q=0.5",
        }
        current_url = url
        try:
            async with aiohttp.ClientSession(timeout=timeout, trust_env=False, headers=request_headers) as session:
                # Follow a small number of redirects manually so each destination is validated.
                for _ in range(6):
                    parsed = urlsplit(current_url)
                    hostname = (parsed.hostname or "").rstrip(".").lower()
                    if parsed.scheme not in ("http", "https") or not hostname or parsed.username or parsed.password:
                        await ctx.send("La URL o una redirección apunta a una dirección no permitida.")
                        return
                    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith((".localhost", ".local")):
                        await ctx.send("La URL o una redirección apunta a una dirección no permitida.")
                        return
                    try:
                        address = ipaddress.ip_address(hostname)
                    except ValueError:
                        address = None
                    if address is not None and not address.is_global:
                        await ctx.send("La URL o una redirección apunta a una dirección no pública.")
                        return

                    async with session.get(current_url, allow_redirects=False) as response:
                        if response.status in (301, 302, 303, 307, 308):
                            location = response.headers.get("Location")
                            if not location:
                                await ctx.send("El servidor devolvió una redirección sin destino.")
                                return
                            current_url = urljoin(current_url, location)
                            continue
                        if response.status != 200:
                            await ctx.send(f"La URL respondió con HTTP {response.status}; no se creó el archivo.")
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
                        break
                else:
                    await ctx.send("La URL tiene demasiadas redirecciones (máximo 5).")
                    return
        except asyncio.TimeoutError:
            await ctx.send("La descarga superó el tiempo límite de 25 segundos.")
            return
        except (aiohttp.ClientError, OSError):
            await ctx.send("No pude descargar esa URL. Comprueba que sea pública y accesible.")
            return

        # Some protected endpoints redirect to an explanatory HTML page instead of returning the script.
        preview = raw_data[:8192].decode("utf-8-sig", errors="replace").lower()
        if "yisus-hub.vercel.app/protection" in current_url.lower() or (
            "protección del endpoint" in preview and "solicitud navegador" in preview
        ):
            await ctx.send("El endpoint redirigió a su página de protección en lugar de devolver el contenido del script. `.get` ya sigue redirecciones públicas, pero el servidor debe permitir el acceso directo al endpoint para obtener el loader.")
            return

        # Save as UTF-8 text even if the source contains malformed byte sequences.
        text_data = raw_data.decode("utf-8-sig", errors="replace").encode("utf-8")
        upload_limit = ctx.guild.filesize_limit if ctx.guild else 10 * 1024 * 1024
        if len(text_data) > upload_limit:
            await ctx.send("El archivo resultante supera el límite de archivos de este servidor de Discord.")
            return
        downloadable = discord.File(io.BytesIO(text_data), filename="raw.txt")
        try:
            await ctx.send(f"Contenido descargado ({len(text_data)} bytes):", file=downloadable)
        finally:
            downloadable.close()

    async def process_attachment(ctx, url, engine, cache, command, version=""):
        if ctx.author.id != owner_id:
            return
        if command in bot.engine_errors:
            await ctx.send(bot.engine_errors[command])
            return
        if version and version not in LURAPH_VERSIONS:
            await ctx.send("Versión no válida: usa 14.7, 14.8, 14.9 o 15.")
            return
        extensions = (".lua", ".luau", ".txt") if command == "luraph" else (".lua", ".txt")
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
        if BUSY.locked():
            await ctx.send("Estoy procesando otro archivo. Inténtalo cuando termine.")
            return
        async with BUSY:
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
                cached = cache.get(key)
                if cached is not None:
                    # Cache hit: avoid creating a temp directory and writing the result to disk.
                    result_data = cached
                else:
                    with tempfile.TemporaryDirectory(prefix="deobf-") as folder:
                        source = Path(folder) / "entrada.lua"
                        source.write_bytes(input_data)
                        if command == "luraph":
                            result_data = await engine.run(source, engine=version)
                        else:
                            result_data = await engine.run(source)
                    if len(result_data) <= cache.max_bytes:
                        cache.put(key, result_data)
                upload_limit = ctx.guild.filesize_limit if ctx.guild else 10 * 1024 * 1024
                if len(result_data) > upload_limit:
                    raise ProcessingError("El resultado supera el límite de archivos de Discord.")
                progress.cancel()
                await asyncio.gather(progress, return_exceptions=True)
                elapsed = time.perf_counter() - started
                detail = " (desde caché)" if cached is not None else ""
                if command == "luraph" and result_data.startswith(PARTIAL_HEADER):
                    detail += " — recuperación parcial: algunos bloques no pudieron recuperarse"
                downloadable = discord.File(io.BytesIO(result_data), filename="resultado.lua")
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