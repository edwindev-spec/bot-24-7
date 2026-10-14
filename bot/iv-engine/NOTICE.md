# Ironveil Deobfuscator V1

Código tomado de `prostone55/Ironveil-Deobfuscator-V1`, revisión `64b27cfd0cb80919ccefdc8a2e61d6f7d742fb5a`. El upstream declara licencia MIT; el texto se incluye en `LICENSE` y la guía upstream en `README.upstream.md`.

La función upstream `evalLuaNum` evaluaba expresiones mediante JavaScript `eval`, aunque el flujo CLI principal no la necesita. En esta copia se reemplazó por una función que devuelve `null`. El comando `.iv` procesa de forma estática el archivo recibido, sin ejecutar el Lua. Node se ejecuta en un proceso separado, con `--max-old-space-size=512`, límites de CPU, tamaño máximo de archivo y descriptores, entorno reducido y timeout. La recuperación es específica de Ironveil V1 y best-effort.
