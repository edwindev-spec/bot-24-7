"""Bot de Discord: .deobf (URL o adjunto .lua/.txt) y .get (descarga de URL pública).

Requiere: discord.py 2.x, aiohttp, Node 22+ y el compilador Lua 5.1 (luac5.1).
"""
import asyncio
import hashlib
import io
import ipaddress
import os
import re
import shutil
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
MAX_INPUT = 2 * 1024 * 1024
MAX_RESULT = 32 * 1024 * 1024
DEFAULT_UPLOAD_LIMIT = 10 * 1024 * 1024
MAX_REDIRECTS = 5
REDIRECT_CODES = (301, 302, 303, 307, 308)
FETCH_TIMEOUT = aiohttp.ClientTimeout(total=25, connect=8, sock_read=15)
REQUEST_HEADERS = {
    "User-Agent": "PrometheusRecoverBot/1.0 (+Discord URL fetcher)",
    "Accept": "text/plain, application/json, application/octet-stream, text/html;q=0.8, */*;q=0.5",
}
PROTECTION_MARKER = "yisus-hub.vercel.app/protection"
INVALID_URL = "URL no válida. Usa una URL pública que empiece por `https://` o `http://`."
DOWNLOAD_FAILED = "No pude descargar esa URL. Comprueba que sea pública y accesible."
SERVICE_STARTED = time.time()


class ProcessingError(Exception):
    """Error con un mensaje ya listo para mostrar en Discord."""


# ---------------------------------------------------------------- utilidades

def check_public_url(url: str) -> str:
    """Devuelve el host normalizado si la URL es http(s) pública; si no, lanza ValueError."""
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(url)
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith((".localhost", ".local")):
        raise ValueError(url)
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return hostname  # Es un nombre de dominio, no una IP literal.
    if not address.is_global:
        raise ValueError(url)
    return hostname


def to_utf8(data: bytes) -> bytes:
    """Convierte a UTF-8 sin BOM, sustituyendo secuencias inválidas."""
    return data.decode("utf-8-sig", errors="replace").encode("utf-8")


def upload_limit(ctx) -> int:
    return ctx.guild.filesize_limit if ctx.guild else DEFAULT_UPLOAD_LIMIT


async def send_file(ctx, message: str, data: bytes, filename: str):
    file = discord.File(io.BytesIO(data), filename=filename)
    try:
        await ctx.send(message, file=file)
    finally:
        file.close()


async def read_limited(response) -> bytes:
    chunks, total = [], 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > MAX_INPUT:
            raise ProcessingError("El contenido supera el límite de 2 MiB.")
        chunks.append(chunk)
    return b"".join(chunks)


async def fetch_text(url: str, follow_redirects: bool):
    """Descarga una URL pública. Devuelve (url_final, bytes).

    Si follow_redirects es True, cada redirección se valida antes de seguirla.
    """
    current = url
    try:
        async with aiohttp.ClientSession(timeout=FETCH_TIMEOUT, trust_env=False,
                                         headers=REQUEST_HEADERS) as session:
            for _ in range(MAX_REDIRECTS + 1):
                try:
                    check_public_url(current)
                except ValueError:
                    raise ProcessingError(
                        "La URL o una redirección apunta a una dirección no permitida.") from None

                async with session.get(current, allow_redirects=False) as response:
                    if response.status in REDIRECT_CODES:
                        if not follow_redirects:
                            raise ProcessingError("La URL redirige. Usa el enlace directo al archivo raw.")
                        location = response.headers.get("Location")
                        if not location:
                            raise ProcessingError("El servidor devolvió una redirección sin destino.")
                        current = urljoin(current, location)
                        continue
                    if response.status != 200:
                        raise ProcessingError(f"La URL respondió con HTTP {response.status}.")
                    return current, await read_limited(response)
    except asyncio.TimeoutError:
        raise ProcessingError("La descarga superó el tiempo límite de 25 segundos.") from None
    except (aiohttp.ClientError, OSError):
        raise ProcessingError(DOWNLOAD_FAILED) from None
    raise ProcessingError(f"La URL tiene demasiadas redirecciones (máximo {MAX_REDIRECTS}).")


async def delayed_progress(ctx):
    await asyncio.sleep(1)
    await ctx.send("Procesando…")


# ---------------------------------------------------------------- caché

class ResultCache:
    """Conjunto acotado de resultados correctos para esta sesión del bot."""

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


# ---------------------------------------------------------------- motor

class Deobfuscator:
    """Ejecuta el CLI de Prometheus Recover y devuelve solo el código Lua resultante."""

    def __init__(self, repo=None):
        self.repo = Path(repo or DEOBF_DIR).resolve()
        self.node = shutil.which("node")
        self.luac = shutil.which("luac5.1") or shutil.which("luac51") or shutil.which("luac-5.1")
        self.process = None
        self.lock = asyncio.Lock()
        self.closed = False
        # Se conserva el PATH real: el motor puede necesitar encontrar binarios del sistema.
        self.environment = {"PATH": os.environ.get("PATH", os.defpath), "LANG": "C.UTF-8"}

    async def _quick_check(self, *command):
        """Ejecuta un comando corto y devuelve su salida, o None si falla."""
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env=self.environment)
        output = None
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 10)
        except asyncio.TimeoutError:
            pass
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()
        if output is None or process.returncode != 0:
            return None
        return output.strip()

    async def prepare(self):
        if not self.node or not self.luac:
            raise ProcessingError("Ejecuta la primera celda: faltan Node o luac5.1.")
        if not (self.repo / "bin" / "recover.js").is_file():
            raise ProcessingError("Ejecuta la primera celda para descargar el motor.")
        node_out = await self._quick_check(self.node, "--version") or b""
        luac_out = await self._quick_check(self.luac, "-v") or b""
        match = re.match(rb"v?(\d+)", node_out)
        if not match or int(match.group(1)) < 22 or not luac_out.startswith(b"Lua 5.1"):
            raise ProcessingError("Se necesitan Node 22+ y Lua 5.1. Ejecuta la primera celda.")

    async def _stop(self):
        process, self.process = self.process, None
        if process is not None:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()

    @staticmethod
    def _log_failure(log, code):
        """Vuelca al log de consola las últimas líneas del motor para diagnosticar fallos."""
        log.seek(0)
        tail = log.read()[-2000:].decode(errors="replace")
        print(f"[deobf] recover.js terminó con código {code}:\n{tail}", flush=True)

    async def run(self, source: Path, timeout=75) -> bytes:
        async with self.lock:
            if self.closed:
                raise ProcessingError("Reinicia el bot antes de procesar otro archivo.")
            destination = source.parent / "result"
            with tempfile.TemporaryFile() as log:
                process = await asyncio.create_subprocess_exec(
                    self.node, "--max-old-space-size=2048", str(self.repo / "bin" / "recover.js"),
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
                    self._log_failure(log, process.returncode)
                    raise ProcessingError("El motor no pudo analizar este archivo Lua/Prometheus.")

                name = "recovered.lua" if process.returncode == 0 else "partial.lua"
                result = destination / name
                if not result.is_file():
                    self._log_failure(log, process.returncode)
                    raise ProcessingError("El motor terminó sin generar resultado.")
                if result.stat().st_size > MAX_RESULT:
                    raise ProcessingError("El resultado supera el límite de 32 MiB.")
                return result.read_bytes()

    async def close(self):
        self.closed = True
        await self._stop()


# ---------------------------------------------------------------- bot

class DeobfBot(commands.Bot):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.engine = Deobfuscator()
        self.busy = asyncio.Lock()
        self.cache = ResultCache()

    async def setup_hook(self):
        await self.engine.prepare()

    async def close(self):
        try:
            await super().close()
        finally:
            await self.engine.close()


def create_bot(owner_id: int) -> DeobfBot:
    intents = discord.Intents.default()
    intents.message_content = True
    bot = DeobfBot(
        command_prefix=".", intents=intents, help_command=None,
        allowed_mentions=discord.AllowedMentions.none(),
    )

    def is_owner(ctx) -> bool:
        return ctx.author.id == owner_id

    @bot.event
    async def on_ready():
        print(f"Prometheus Recover conectado como {bot.user}. Envía .deobf con un archivo .lua o .txt.",
              flush=True)

    @bot.command(name="get")
    async def get_raw(ctx, url: str = ""):
        """Descarga una URL pública de texto y la devuelve como raw.txt."""
        if not is_owner(ctx):
            return
        url = url.strip().strip("<>")
        if not url:
            await ctx.send("Uso: `.get <URL>`")
            return
        try:
            check_public_url(url)
        except ValueError:
            await ctx.send(INVALID_URL)
            return

        try:
            final_url, raw = await fetch_text(url, follow_redirects=True)
        except ProcessingError as error:
            await ctx.send(str(error))
            return

        # Algunos endpoints protegidos redirigen a una página explicativa en vez del script.
        preview = raw[:8192].decode("utf-8-sig", errors="replace").lower()
        if PROTECTION_MARKER in final_url.lower() or (
            "protección del endpoint" in preview and "solicitud navegador" in preview
        ):
            await ctx.send(
                "El endpoint redirigió a su página de protección en lugar de devolver el contenido "
                "del script. `.get` ya sigue redirecciones públicas, pero el servidor debe permitir "
                "el acceso directo al endpoint para obtener el loader."
            )
            return

        text = to_utf8(raw)
        if len(text) > upload_limit(ctx):
            await ctx.send("El archivo resultante supera el límite de archivos de este servidor de Discord.")
            return
        await send_file(ctx, f"Contenido descargado ({len(text)} bytes):", text, "raw.txt")

    @bot.command(name="deobf")
    async def deobf(ctx, url: str = ""):
        """Recupera código Lua ofuscado con Prometheus desde una URL o un adjunto."""
        if not is_owner(ctx):
            return
        url = url.strip().strip("<>")
        attachments = ctx.message.attachments

        if url and attachments:
            await ctx.send("Usa una URL o adjunta un archivo, no ambos a la vez.")
            return
        if url:
            try:
                check_public_url(url)
            except ValueError:
                await ctx.send(INVALID_URL)
                return
        else:
            if len(attachments) != 1 or not attachments[0].filename.lower().endswith((".lua", ".txt")):
                await ctx.send("Uso: `.deobf <URL>` o adjunta un único archivo `.lua` o `.txt`.")
                return
            if attachments[0].size > MAX_INPUT:
                await ctx.send("El archivo debe pesar como máximo 2 MiB.")
                return

        if bot.busy.locked():
            await ctx.send("Estoy procesando otro archivo. Inténtalo cuando termine.")
            return

        async with bot.busy:
            started = time.perf_counter()
            progress = asyncio.create_task(delayed_progress(ctx))
            try:
                if url:
                    _, input_data = await fetch_text(url, follow_redirects=False)
                else:
                    input_data = await asyncio.wait_for(attachments[0].read(), timeout=30)
                if len(input_data) > MAX_INPUT:
                    raise ProcessingError("El archivo debe pesar como máximo 2 MiB.")

                key = hashlib.sha256(input_data).digest()
                result_data = bot.cache.get(key)
                cached = result_data is not None
                if not cached:
                    with tempfile.TemporaryDirectory(prefix="deobf-") as folder:
                        source = Path(folder) / "entrada.lua"
                        source.write_bytes(input_data)
                        result_data = await bot.engine.run(source)
                    bot.cache.put(key, result_data)

                if len(result_data) > upload_limit(ctx):
                    raise ProcessingError("El resultado supera el límite de archivos de Discord.")

                progress.cancel()
                elapsed = time.perf_counter() - started
                detail = " (desde caché)" if cached else ""
                await send_file(ctx, f"Resultado en {elapsed:.2f} s{detail}:",
                                result_data, "resultado.lua")
            except ProcessingError as error:
                await ctx.send(str(error))
            except asyncio.TimeoutError:
                await ctx.send("La descarga tardó demasiado. Inténtalo de nuevo.")
            except Exception as error:
                print(f"[deobf] error inesperado: {error!r}", flush=True)
                await ctx.send("No pude completar la operación. Revisa el archivo y los permisos del bot.")
            finally:
                progress.cancel()
                await asyncio.gather(progress, return_exceptions=True)

    return bot


# ---------------------------------------------------------------- endpoint de estado

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
    await web.TCPSite(runner, "0.0.0.0", port).start()
    print(f"Endpoint de estado disponible en el puerto {port}.", flush=True)
    return runner


# ---------------------------------------------------------------- arranque

async def main():
    token = os.environ.get("DISCORD_TOKEN", "")
    owner = os.environ.get("DISCORD_OWNER_ID", "")
    if not token or not owner.isdecimal():
        raise RuntimeError("Configura DISCORD_TOKEN y DISCORD_OWNER_ID como variables de entorno.")
    if not (DEOBF_DIR / "bin" / "recover.js").is_file():
        raise RuntimeError("No se encuentra la herramienta. Revisa DEOBF_RECOVER_DIR.")

    bot = create_bot(int(owner))
    runner = await start_health_server(bot)
    try:
        async with bot:
            await bot.start(token)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
