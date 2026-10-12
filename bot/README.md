# Servicio del bot

Esta carpeta se despliega como contenedor Docker persistente. Configura `DISCORD_TOKEN` y `DISCORD_OWNER_ID` como secretos/variables en el proveedor. `PORT` lo proporciona el hosting. El servicio publica `/health` para los health checks y el panel web.

No ejecutes el bot como una Vercel Function: necesita una conexión continua con Discord Gateway.

Comandos: `.deobf` procesa un adjunto Lua/TXT; `.get <URL>` descarga una URL pública HTTP(S) y devuelve `raw.txt` (máximo 2 MiB). `.get` sigue hasta cinco redirecciones, validando cada destino, y no descarga direcciones privadas ni páginas protegidas que no expongan directamente el contenido. Ambos comandos están restringidos a `DISCORD_OWNER_ID`.
