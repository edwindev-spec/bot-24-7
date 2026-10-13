# Servicio del bot

Esta carpeta se despliega como contenedor Docker persistente. Configura `DISCORD_TOKEN` y `DISCORD_OWNER_ID` como secretos/variables en el proveedor. `PORT` lo proporciona el hosting. El servicio publica `/health` para los health checks y el panel web.

No ejecutes el bot como una Vercel Function: necesita una conexión continua con Discord Gateway.

Comandos restringidos a `DISCORD_OWNER_ID`: `.luraph` procesa un adjunto `.lua`, `.luau` o `.txt` (hasta 2 MiB) para Luraph v14.7–v15; si no hay cabecera, se puede indicar la versión, por ejemplo `.luraph 14.8` con el adjunto. `.deobf` procesa adjuntos `.lua`/`.txt` con Prometheus. `.get <URL>` devuelve `raw.txt` desde una URL HTTP(S) pública (hasta 2 MiB), sigue hasta cinco redirecciones y valida cada destino.

Tras cambiar archivos o añadir el motor, reconstruye y despliega la imagen Docker; reiniciar un contenedor anterior no incorpora la actualización. La primera preparación de Luraph puede tardar varios minutos y requiere Git, CMake y compilador C++17.
