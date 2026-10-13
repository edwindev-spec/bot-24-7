# Actualización Luraph con caché de disco

Extrae este ZIP en la raíz de bot-24-7 y reemplaza sus archivos, conservando bot/luraph-fusion/ y bot/result_cache.py. Sube los cambios y vuelve a desplegar reconstruyendo Docker. Railway: Root Directory vacío. Render: puedes usar bot. Conserva DISCORD_TOKEN y DISCORD_OWNER_ID.

La imagen activa LURAPH_CACHE_DIR=/app/cache. Para conservar resultados entre reconstrucciones/recreaciones del contenedor, monta un volumen persistente del hosting en /data y configura LURAPH_CACHE_DIR=/data/luraph-cache. Sin volumen, la caché depende del disco efímero. Puedes desactivar la caché de disco dejando esa variable vacía; permanece la caché en memoria.

Comando: .luraph con un archivo .lua/.luau/.txt (máximo 2 MiB), devuelve resultado.lua. El motor optimizado se incluye como fuente y compila sus herramientas durante el build. Si sobrescribiste LURAPH_RECOVER_DIR en el hosting, configura /app/luraph-fusion. Se mantienen .deobf, .get, el formato StyLua y los avisos de recuperación parcial.

Mejoras:
- Resultados previamente verificados se guardan en SQLite: hasta 128 MiB de datos, 256 entradas. No se guardan entradas obfuscadas. Se comprueban tamaño/SHA256, se invalidan si cambia el motor/binario/bot, y se descartan datos corruptos.
- Archivos repetidos se entregan aun mientras otro archivo nuevo se desvirtualiza.
- La lectura/escritura de disco no bloquea el bucle de Discord.
- Las rondas de ejecución se despiertan al terminar/salir datos, evitando esperas fijas de 100 ms y manteniendo los límites de tiempo.

Mediciones locales: archivo real v15, primera recuperación 2,083 s; cinco entregas del mismo resultado desde disco con instancias nuevas del bot: mediana 1,856 ms. No incluye la red Discord. Rondas cortas: mediana 100,647 ms antes frente a 12,694 ms después. No se ha demostrado una gran mejora en la primera recuperación de archivos nuevos; sigue dependiendo del archivo y del servidor.

Validación: suite completa de 60 pruebas y 28 pruebas del bot tras la última corrección (incluye una prueba nueva), recuperación real y Lua devuelto ejecutado. No se inició sesión real en Discord.
