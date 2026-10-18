# Ejecución del motor `.ironbrew1`

El plugin ejecuta el programa instrumentado en el runtime oficial de Luau y un entorno Roblox simulado para capturar closures/protos y levantarlos a código Luau. No es análisis puramente estático. El programa no se ejecuta dentro de Roblox ni se envía a un servicio externo; el hijo recibe una lista reducida de variables de entorno, sin el token Discord.

En los Dockerfiles del proyecto, el bot crea un proceso independiente y lo cambia a UID/GID 65534 (`nobody`) antes de ejecutar el CLI. Solo el directorio temporal de trabajo se transfiere a ese usuario. El proceso tiene límites de 90 s de CPU, 768 MiB de memoria virtual, 32 MiB por archivo, 64 procesos y 32 descriptores; se termina el grupo de procesos tras acabar o exceder el tiempo. Las URL se descargan con `PublicOnlyResolver`, que rechaza cualquier dirección DNS no global, y no se siguen redirecciones.

El comando acepta hasta 2 MiB y solo responde al `DISCORD_OWNER_ID`. Estos controles reducen riesgos de consumo de recursos y acceso accidental a los archivos del bot, pero no equivalen a aislamiento de kernel/VM ni constituyen una garantía frente a vulnerabilidades del runtime o del parser. La recuperación es best-effort; una salida de traza se etiqueta como informe, no como fuente.
