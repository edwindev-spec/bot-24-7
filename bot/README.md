# Servicio del bot

Esta carpeta se despliega como contenedor Docker persistente. Configura `DISCORD_TOKEN` y `DISCORD_OWNER_ID` como secretos/variables en el proveedor. `PORT` lo proporciona el hosting. El servicio publica `/health` para los health checks y el panel web.

No ejecutes el bot como una Vercel Function: necesita una conexión continua con Discord Gateway.

`.help` lista todos los comandos y sus usos. Los comandos de análisis están restringidos a `DISCORD_OWNER_ID`:

- `.deobf`: conserva el motor Prometheus ya integrado y acepta un adjunto `.lua`/`.txt` o URL directa.
- `.ironbrew <URL>` o adjunta `.lua`/`.txt` (hasta 2 MiB): devuelve bytecode Lua 5.1 como `resultado.luac`.
- `.lb <URL>` o adjunta `.lua`/`.txt` (hasta 2 MiB): prueba LuaObfuscator Chaotic Evil mediante análisis estático solamente; no ejecuta el Lua recibido. La recuperación es best-effort.
- `.iv <URL>` o adjunta `.lua`/`.txt` (hasta 2 MiB): intenta recuperar Ironveil V1 y devuelve código Lua como `resultado.lua`.
- `.mv <URL>` o adjunta `.lua`/`.luau`/`.txt` (hasta 2 MiB): recuperación estática best-effort de MoonVeil V1.4.5; no ejecuta el archivo ni lo sube a servicios externos.
- `.luraph`: procesa `.lua`, `.luau` o `.txt` (hasta 2 MiB) para Luraph v14.7–v15; si no se detecta la versión, indica una con el adjunto, por ejemplo `.luraph 14.8`.
- `.get <URL>`: hace GET a URLs y APIs HTTP(S) públicas (hasta 2 MiB); formatea JSON válido como `raw.json` (con vista previa si es pequeño) y otros contenidos como `raw.txt`. Comprueba todos los IP devueltos por DNS y cada destino de hasta cinco redirecciones, bloquea direcciones privadas y no permite degradar HTTPS a HTTP.

En errores de API, `.get` distingue `401` (autenticación), `403` (acceso bloqueado), `429` (límite, incluyendo `Retry-After`) y `204` (sin contenido). No intenta omitir controles de acceso del servidor.

Ante errores transitorios de conexión y respuestas `429`/`5xx`, realiza hasta tres intentos totales y respeta `Retry-After` si es de 5 segundos o menos; nunca reintenta `403` ni rota cabeceras para evitar bloqueos.

Los logs del servicio registran cada intento `.get` (`get_request`/`get_response`), status HTTP, latencia y cabeceras auditables. Los valores de query, cookies, credenciales y valores de cabeceras desconocidas se redactan/omiten; no se guardan cuerpos. Configura `LOG_LEVEL` para ajustar el nivel de logs (por defecto `INFO`).

Tras cambiar archivos o añadir el motor, reconstruye y despliega la imagen Docker; reiniciar un contenedor anterior no incorpora la actualización. El motor Luraph se incluye con su licencia y se compila durante el build. Ironbrew se obtiene en un commit fijado y se compila como .NET 8 en la etapa Docker; la imagen incluye su licencia GPL-3.0. `.lb` se distribuye con permiso confirmado por el usuario; el upstream no declara licencia y el modo instalado es estático, sin Lupa. `.iv` se incluye bajo MIT; se deshabilitó el helper upstream basado en `eval`. `.mv` se incluye bajo MIT y usa únicamente el análisis estático del perfil MoonVeil 1.4.5. Los procesos externos tienen timeout y límites de recursos.

La caché de disco conserva únicamente resultados recuperados verificados (hasta 128 MiB y 256 entradas), no los archivos de entrada. El valor predeterminado `/app/cache` puede ser efímero. Para persistirla, monta un volumen del hosting en `/data` y configura `LURAPH_CACHE_DIR=/data/luraph-cache`; para desactivar la caché de disco, deja la variable vacía. Requiere configurar en el hosting las variables Discord habituales.
