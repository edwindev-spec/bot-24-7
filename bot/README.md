# Servicio del bot

Esta carpeta se despliega como contenedor Docker persistente. Configura `DISCORD_TOKEN` y `DISCORD_OWNER_ID` como secretos/variables en el proveedor. `PORT` lo proporciona el hosting. El servicio publica `/health` para los health checks y el panel web.

No ejecutes el bot como una Vercel Function: necesita una conexión continua con Discord Gateway.

`.help` lista todos los comandos y sus usos. Los comandos de análisis están restringidos a `DISCORD_OWNER_ID`:

- `.deobf`: conserva el motor Prometheus ya integrado y acepta un adjunto `.lua`/`.txt` o URL directa.
- `.ironbrew <URL>` o adjunta `.lua`/`.txt` (hasta 2 MiB): devuelve bytecode Lua 5.1 como `resultado.luac`.
- `.lb <URL>` o adjunta `.lua`/`.txt` (hasta 2 MiB): prueba LuaObfuscator Chaotic Evil mediante análisis estático solamente; no ejecuta el Lua recibido. La recuperación es best-effort.
- `.luraph`: procesa `.lua`, `.luau` o `.txt` (hasta 2 MiB) para Luraph v14.7–v15; si no se detecta la versión, indica una con el adjunto, por ejemplo `.luraph 14.8`.
- `.get <URL>`: devuelve `raw.txt` desde una URL HTTP(S) pública (hasta 2 MiB), validando hasta cinco redirecciones.

Tras cambiar archivos o añadir el motor, reconstruye y despliega la imagen Docker; reiniciar un contenedor anterior no incorpora la actualización. El motor Luraph se incluye con su licencia y se compila durante el build. Ironbrew se obtiene en un commit fijado y se compila como .NET 8 en la etapa Docker; la imagen incluye su licencia GPL-3.0. `.lb` se distribuye con permiso confirmado por el usuario; el upstream no declara licencia. Su código se mantiene estático, sin Lupa, y se ejecuta en un proceso con límites de CPU, memoria, archivos y tiempo.

La caché de disco conserva únicamente resultados recuperados verificados (hasta 128 MiB y 256 entradas), no los archivos de entrada. El valor predeterminado `/app/cache` puede ser efímero. Para persistirla, monta un volumen del hosting en `/data` y configura `LURAPH_CACHE_DIR=/data/luraph-cache`; para desactivar la caché de disco, deja la variable vacía. Requiere configurar en el hosting las variables Discord habituales.
