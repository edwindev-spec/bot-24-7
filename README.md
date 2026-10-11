# Prometheus Recover — web en Vercel + bot 24/7

Este proyecto separa la web estática (Vercel) del bot de Discord (servicio persistente). Vercel no mantiene una conexión Discord Gateway abierta de forma permanente, así que no despliegues el proceso del bot como una función de Vercel.

## 1. Desplegar el bot

Recomendado: Railway con Docker. También puedes desplegar la misma carpeta Docker en Render u otro proveedor de servicios persistentes.

1. Sube este proyecto a un repositorio privado de GitHub.
2. En Railway crea un proyecto desde ese repositorio.
3. Configura **Root Directory** como `/bot`, para que Railway detecte `bot/Dockerfile`.
4. Añade las variables de entorno `DISCORD_TOKEN` y `DISCORD_OWNER_ID`.
5. Despliega y espera a que los logs indiquen que el bot se ha conectado a Discord.
6. Genera un dominio público para el servicio. La ruta `https://TU-DOMINIO/health` devolverá su estado.

En Render, crea un Web Service desde el repositorio, usa `bot` como Root Directory y Docker como runtime. Configura las mismas variables. Para disponibilidad continua el servicio necesita un plan que no se suspenda; revisa el coste del proveedor antes de desplegar.

### Variables obligatorias

- `DISCORD_TOKEN`: token del bot de Discord. Guárdalo solo como secreto del proveedor; nunca lo subas a GitHub ni lo pongas en la web.
- `DISCORD_OWNER_ID`: tu ID numérico de usuario de Discord. El comando `.deobf` solo responde a ese ID.
- `PORT`: lo establece automáticamente el proveedor; localmente el bot usa `8080`.

Activa **Message Content Intent** en Discord Developer Portal. Invita el bot al servidor con permisos para ver mensajes, leer el contenido y enviar mensajes/archivos.

## 2. Desplegar la web en Vercel

1. Importa el mismo repositorio en Vercel.
2. Establece **Root Directory** en `web`.
3. Selecciona **Other** como framework/preset y deja el proyecto como sitio estático, sin comando de build.
4. Despliega.
5. Abre tu web y pega la URL pública del servicio del bot (sin `/health`); pulsa **Guardar URL** y **Comprobar estado**.

La web guarda la URL en el navegador local, no en el servidor. El token de Discord nunca pasa por Vercel.

## 3. Usar el bot

Envía en Discord un mensaje `.deobf` y adjunta un único archivo `.lua` o `.txt` de hasta 2 MiB. El bot devuelve `resultado.lua` cuando el análisis termina. Se mantiene la restricción del notebook: solo responde al `DISCORD_OWNER_ID` configurado.

## Seguridad y funcionamiento

- No compartas el token del bot. Si ya se publicó por error, regénéralo en Discord Developer Portal.
- La ruta `/health` revela únicamente estado de conexión, nombre de cuenta del bot y tiempo de proceso; no devuelve secretos.
- El motor se obtiene del commit fijado `98e1d89faeddbe8d478a9b72df58d13d43a2b765` de `edwin-dev67/coderabit`, igual que en tu notebook.
- La caché de resultados y las carpetas temporales viven en memoria/disco efímero del proceso; no se guardan entre reinicios.
- Vercel aloja la web, pero el bot solo estará 24/7 mientras el proveedor del servicio de bot mantenga la instancia encendida.
