# Actualización .luraph para bot-24-7

1. Extrae este ZIP en la raíz de tu repositorio bot-24-7 y reemplaza los archivos incluidos, conservando la carpeta bot/.
2. Guarda los cambios y vuelve a desplegar el servicio en Railway o Render. Reiniciar el contenedor anterior no instala la actualización: se necesita reconstruir la imagen.
3. Mantén DISCORD_TOKEN y DISCORD_OWNER_ID como variables del proveedor. No incluyas el token en archivos.
4. Espera a que termine la compilación del motor durante el build y a que el bot conecte a Discord.
5. Envía .luraph con un adjunto .lua, .luau o .txt (máximo 2 MiB). Devuelve resultado.lua. Si falta la cabecera, usa .luraph 14.8, 14.9, 14.7 o 15 según el archivo.

El Dockerfile raíz sirve para Railway con Root Directory vacío; bot/Dockerfile sirve para despliegues con Root Directory bot. Ambos incluyen Git, CMake y C++17 y preparan Luraph en /app/luraph-fusion antes de arrancar. Conservan el motor Prometheus y los comandos .deobf y .get.

La versión del bot detecta instalaciones posteriores al arranque y vuelve a preparar el motor si falta. Las recuperaciones parciales se entregan con aviso; no se garantiza recuperar cualquier archivo Luraph.

Verificación: bot y preparación idénticos a la versión que pasó 24 pruebas y una instalación independiente con recuperación ejecutable. La conexión real a Discord no se probó. El estado de validación Docker se incluye al final de este archivo.

Docker verificado: imagen raíz construida y prueba dentro del contenedor aprobada; el resultado Lua recuperado imprimió 42 y Prometheus quedó preparado. La prueba añadió únicamente el certificado público del proxy del sandbox en una copia temporal; ese certificado no se incluye en esta actualización. bot/Dockerfile se comprobó por inspección de configuración, sin construir una segunda imagen. No se inició sesión en Discord.
