# Servicio del bot

Esta carpeta se despliega como contenedor Docker persistente. Configura `DISCORD_TOKEN` y `DISCORD_OWNER_ID` como secretos/variables en el proveedor. `PORT` lo proporciona el hosting. El servicio publica `/health` para los health checks y el panel web.

No ejecutes el bot como una Vercel Function: necesita una conexión continua con Discord Gateway.

`.help` lista todos los comandos y sus usos. Los comandos restringidos a `DISCORD_OWNER_ID` son: `.luraph` procesa un adjunto `.lua`, `.luau` o `.txt` (hasta 2 MiB) para Luraph v14.7–v15; si no hay cabecera, se puede indicar la versión, por ejemplo `.luraph 14.8` con el adjunto. `.deobf` procesa adjuntos `.lua`/`.txt` con Prometheus. `.get <URL>` devuelve `raw.txt` desde una URL HTTP(S) pública (hasta 2 MiB), sigue hasta cinco redirecciones y valida cada destino.

Tras cambiar archivos o añadir el motor, reconstruye y despliega la imagen Docker; reiniciar un contenedor anterior no incorpora la actualización. El motor fuente se incluye con su licencia y se compila durante el build.

La caché de disco conserva únicamente resultados recuperados verificados (hasta 128 MiB y 256 entradas), no los archivos de entrada. El valor predeterminado `/app/cache` puede ser efímero. Para persistirla, monta un volumen del hosting en `/data` y configura `LURAPH_CACHE_DIR=/data/luraph-cache`; para desactivar la caché de disco, deja la variable vacía. Requiere configurar en el hosting las variables Discord habituales.
