# Motor `.mvv2`

Este paquete recupera el perfil **MoonVeil 2.1.0 table-dispatch** calibrado en el código adjunto. No reemplaza `.mv` (MoonVeil 1.4.5) y no acepta cualquier archivo que simplemente lleve el banner `2.1.0`; las variantes no reconocidas se rechazan.

El bot ejecuta `python -m moonveil decompile … --require-compile` en un subproceso con entorno reducido y límites de CPU, memoria, tiempo, tamaño de salida, descriptores y core dumps. La recuperación instrumenta el bootstrap/deserializador en el runtime oficial Luau y captura la VM antes del despacho del programa protegido. No ejecuta la lógica final de la VM, pero tampoco constituye aislamiento del sistema operativo.

No requiere dependencias Python adicionales. Reutiliza Luau 0.722 y `luau-compile` ya instalados por los Dockerfiles del bot. El formato se describe en [`docs/FORMAT.md`](docs/FORMAT.md); procedencia y licencias, en `UPSTREAM.md` y `LICENSE.txt`.

El fixture del ZIP original no se vuelve a distribuir en el repositorio del bot.
