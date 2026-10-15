# Luraph y Prometheus Recover — web en Vercel + bot 24/7

Este proyecto separa la web estática (Vercel) del bot de Discord (servicio persistente). Vercel no mantiene una conexión Discord Gateway abierta de forma permanente, así que no despliegues el proceso del bot como una función de Vercel.

## 1. Desplegar el bot

Recomendado: Railway con Docker. También puedes desplegar la misma carpeta Docker en Render u otro proveedor de servicios persistentes.

1. Conecta el repositorio de GitHub.
2. En Railway crea un proyecto desde ese repositorio.
3. Deja **Root Directory** vacío (la raíz del repositorio). El `Dockerfile` y `railway.toml` de la raíz configuran automáticamente el servicio; no uses el builder Railpack.
4. Añade las variables de entorno `DISCORD_TOKEN` y `DISCORD_OWNER_ID`.
5. Despliega y espera a que los logs indiquen que el bot se ha conectado a Discord.
6. Genera un dominio público para el servicio. La ruta `https://TU-DOMINIO/health` devolverá su estado.

En Render, crea un Web Service desde el repositorio, usa `bot` como Root Directory y Docker como runtime. Configura las mismas variables. Para disponibilidad continua el servicio necesita un plan que no se suspenda; revisa el coste del proveedor antes de desplegar.

### Variables obligatorias

- `DISCORD_TOKEN`: token del bot de Discord. Guárdalo solo como secreto del proveedor; nunca lo subas a GitHub ni lo pongas en la web.
- `DISCORD_OWNER_ID`: tu ID numérico de usuario de Discord. `.get`, `.deobf`, `.luraph`, `.ironbrew`, `.lb`, `.iv` y `.mv` solo responden a ese ID.
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

Envía `.help` en Discord para ver la lista de comandos y sus usos.

- `.deobf`: conserva el motor Prometheus ya integrado; acepta adjunto `.lua`/`.txt` o URL directa.
- `.ironbrew <URL>` o adjunta `.lua`/`.txt` (hasta 2 MiB): devuelve bytecode Lua 5.1 como `resultado.luac`.
- `.lb <URL>` o adjunta `.lua`/`.txt` (hasta 2 MiB): prueba recuperación **estática** para LuaObfuscator Chaotic Evil; devuelve Lua de texto. No ejecuta el script y los resultados son best-effort.
- `.iv <URL>` o adjunta `.lua`/`.txt` (hasta 2 MiB): intenta recuperar Ironveil V1 y devuelve Lua de texto en `resultado.lua`.
- `.mv <URL>` o adjunta `.lua`/`.luau`/`.txt` (hasta 2 MiB): recupera MoonVeil V1.4.5 con el motor solicitado; usa el host oficial Luau para las etapas instrumentadas de decodificación y omite la comparación de comportamiento que ejecutaría el programa protegido.
- `.luraph`: procesa adjuntos `.lua`, `.luau` o `.txt` (hasta 2 MiB) para Luraph v14.7–v15. Si no detecta la versión, indica una con el adjunto, por ejemplo `.luraph 14.8`.
- `.get <URL>`: hace GET a URLs y APIs HTTP(S) públicas (máx. 2 MiB); devuelve JSON válido formateado en `raw.json` —con vista previa si es pequeño— y otros datos como `raw.txt`. Valida los IP de DNS y cada destino de hasta cinco redirecciones, bloquea redes privadas y evita downgrades HTTPS.
- Ante respuestas `401`, `403`, `429` o `204`, `.get` explica autenticación, acceso bloqueado, límite de solicitudes o ausencia de contenido; también muestra detalles JSON de otros errores cuando están disponibles.
- Reintenta como máximo tres intentos totales ante errores transitorios de conexión y respuestas `429`/`5xx`, respetando `Retry-After` hasta 5 s. No reintenta `403` ni cambia cabeceras para eludir bloqueos.
- La auditoría de `.get` aparece en los logs estándar del servicio (`get_request`, `get_response` y reintentos): registra método, URL saneada, cabeceras permitidas, status y latencia. Se ocultan valores de consulta, credenciales, cookies y cabeceras no reconocidas; nunca se registra el cuerpo de la respuesta. `LOG_LEVEL` permite ajustar el nivel (por defecto `INFO`).

`.get`, `.deobf`, `.luraph`, `.ironbrew`, `.lb`, `.iv` y `.mv` solo responden al `DISCORD_OWNER_ID` configurado.

Después de actualizar los archivos, **reconstruye y despliega la imagen Docker** en Railway/Render. El motor Luraph va incluido como fuente licenciada y sus herramientas se compilan durante la construcción. Luau oficial 0.722 también se compila desde el commit fijado para `.mv`; reiniciar un contenedor antiguo no incorpora estas actualizaciones.

### Caché Luraph

El bot conserva hasta 256 resultados verificados (máximo 128 MiB) en SQLite. La caché almacena **código recuperado** —no el archivo de entrada—, por lo que desactívala si no quieres conservar esas salidas. Por defecto usa `/app/cache`, que puede perderse al recrear el contenedor. Para conservarla, monta un volumen persistente en `/data` y configura `LURAPH_CACHE_DIR=/data/luraph-cache` en Railway/Render. Para desactivar la caché de disco, deja `LURAPH_CACHE_DIR` vacío; la caché temporal en memoria continúa activa.

## Seguridad y funcionamiento

- No compartas el token del bot. Si ya se publicó por error, regénéralo en Discord Developer Portal.
- La ruta `/health` revela únicamente estado de conexión, nombre de cuenta del bot y tiempo de proceso; no devuelve secretos.
- El motor Prometheus se obtiene del commit fijado `98e1d89faeddbe8d478a9b72df58d13d43a2b765`; el motor Luraph Fusion incluido se fija en `0d2becf3eb4122784b187214a20397703b5a634f` y mantiene sus avisos/licencias de terceros.
- Ironbrew Deobfuscator se compila desde `Lethal-Luka/IronbrewDeobfuscator` en el commit fijado `43dfbd8fcff52c1f0b9d1ad32ae45142c03683d4`, bajo GPL-3.0; su licencia se incluye en la imagen Docker.
- `.lb` integra `lovesyko/LuaObfuscatorChaoticEvilDeobfuscator` desde `7a3a80ce5ee1f9909bcdf85148b82d1a4c03208d`, con permiso del usuario confirmado en esta conversación. El upstream no declara licencia; la copia vendorizada elimina la ejecución dinámica/Lupa y corre solo análisis estático, aislado con límites de recursos. Ver `bot/lb-engine/NOTICE.md`.
- `.iv` integra Ironveil Deobfuscator V1 desde `prostone55/Ironveil-Deobfuscator-V1` en el commit `64b27cfd0cb80919ccefdc8a2e61d6f7d742fb5a`, con licencia MIT incluida. El parser trabaja sobre el texto/bytecode; se deshabilitó el helper upstream que usaba `eval` y el proceso tiene límites de memoria/CPU/tiempo.
- `.mv` integra `edwindev-spec/MoonVeil` versión `0.13.0` desde `a948c4849a692f0af40d554708cac28b610c5115`, bajo MIT, derivado del upstream UnstableGrape; también usa Luau oficial `0.722` desde `32d52d1b2ceef46fc25d87094a2d7f201c3ea5b8`, bajo MIT. El bot omite la verificación del programa protegido y ejecuta el motor en un proceso limitado por tiempo, CPU, memoria, tamaño de archivos y descriptores. El decoder instrumentado ejecuta etapas de bootstrap del formato, por lo que no es análisis puramente estático ni un aislamiento de sistema operativo. Ver `bot/mv-engine/NOTICE.md`.
- La caché de resultados se limita a 128 MiB y 256 entradas; su persistencia depende de que el proveedor tenga un volumen montado en la ruta configurada.
- Vercel aloja la web, pero el bot solo estará 24/7 mientras el proveedor del servicio de bot mantenga la instancia encendida.
