# Servicio del bot

Esta carpeta se despliega como contenedor Docker persistente. Configura `DISCORD_TOKEN` y `DISCORD_OWNER_ID` como secretos/variables en el proveedor. `PORT` lo proporciona el hosting. El servicio publica `/health` para health checks y el panel web. No ejecutes el bot como una Vercel Function: necesita una conexión continua con Discord Gateway.

`.help` lista los comandos. Los comandos de descarga/análisis están restringidos a `DISCORD_OWNER_ID`:

- `.deobf`: motor Prometheus; acepta adjunto `.lua`/`.txt` o URL directa.
- `.luraph`: procesa `.lua`, `.luau` o `.txt` (máx. 2 MiB) para Luraph v14.7–v15; si no se detecta versión, indícala, por ejemplo `.luraph 14.8`.
- `.ironbrew <URL>` o adjunta `.lua`/`.txt` (máx. 2 MiB): Ironbrew2, salida bytecode Lua 5.1 `resultado.luac`.
- `.ironbrew1 <URL>` o adjunta `.lua`/`.luau`/`.txt` (máx. 2 MiB): recupera IronBrew1 mediante ejecución instrumentada en Luau 0.739 y un entorno Roblox simulado. No es análisis puramente estático; si no se capturan protos puede entregar `resultado-ironbrew1-traza.txt`, una traza y no código fuente.
- `.lb <URL>` o adjunta `.lua`/`.txt` (máx. 2 MiB): LuaObfuscator Chaotic Evil mediante análisis estático; no ejecuta el Lua recibido, recuperación best-effort.
- `.iv <URL>` o adjunta `.lua`/`.txt` (máx. 2 MiB): intenta recuperar Ironveil V1 y devuelve Lua como `resultado.lua`.
- `.mv <URL>` o adjunta `.lua`/`.luau`/`.txt` (máx. 2 MiB): recuperación de MoonVeil V1.4.5, best-effort; no ejecuta el archivo ni lo sube a servicios externos.
- `.mvv2 <URL>` o adjunta `.lua`/`.luau`/`.txt` (máx. 2 MiB): recuperación estática para un perfil calibrado o traza simbólica para layouts compatibles; ciertas variantes entregan un informe, no fuente.
- `.get <URL>`: GET a URLs/APIs HTTP(S) públicas (máx. 2 MiB); comprueba respuestas DNS y redirecciones, bloquea IP privadas y no permite degradar HTTPS a HTTP. No omite controles de acceso.

`.get` distingue errores 401/403/429/204 y reintenta hasta tres veces solo en errores de conexión y respuestas 429/5xx, respetando `Retry-After` hasta 5 s. Los logs registran estado/latencia y cabeceras permitidas; se redactan query values, cookies y credenciales, y no se guarda el cuerpo.

## Despliegue y procedencia

Después de cambiar archivos, reconstruye y despliega la imagen Docker; reiniciar una imagen antigua no incorpora la actualización. El Dockerfile raíz compila Luau 0.739 y `luau-ast` desde el commit fijado, con una adaptación de Vector3 necesaria para el entorno simulado del motor IronBrew1; se incluye su licencia MIT. El motor Ironbrew2 se compila desde un commit fijado bajo GPL-3.0; los binarios Luau específicos de IronBrew1 no se guardan en el repositorio.

El motor `.ironbrew1` proviene de `larpcorrupt/luraph-v15-v14.x-deobfuscator`, commit `ed79a86cc41187af0480e69f45a4838723fdb099`. El usuario indicó que tiene autorización para reutilizar y redistribuirlo; upstream no publica una licencia explícita para esos archivos. Ver `ironbrew1-engine/UPSTREAM.md` y `ironbrew1-engine/IRONBREW1.md`.

En Docker, el proceso hijo `.ironbrew1` se ejecuta como UID/GID 65534 (`nobody`), sin variables de entorno del bot, con límites de tiempo, CPU, memoria, tamaño de archivo, procesos y descriptores; las URLs usan un resolver que rechaza cualquier respuesta DNS no pública. Esto reduce riesgos, pero no equivale a aislamiento de kernel/VM ni garantiza recuperación para todo input.

El motor Luraph conserva licencia y herramientas; `.lb` opera en modo estático sin Lupa y `.iv` deshabilita el helper basado en `eval`. Los motores externos usan límites de tiempo/recursos. La caché puede conservar resultados recuperados (no entradas) hasta 128 MiB/256 elementos; `/app/cache` puede ser efímero. Para persistirla, monta un volumen en `/data` y configura `LURAPH_CACHE_DIR=/data/luraph-cache`; para desactivar la caché en disco, deja esa variable vacía.
