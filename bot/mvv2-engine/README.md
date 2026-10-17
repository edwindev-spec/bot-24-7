# Motor `.mvv2`

Este paquete recupera el perfil **MoonVeil 2.1.0 table-dispatch** calibrado en el código adjunto. No reemplaza `.mv` (MoonVeil 1.4.5) y no acepta cualquier archivo que simplemente lleve el banner `2.1.0`; las variantes no reconocidas se rechazan. Para una única variante adicional calibrada, `.mvv2` puede producir un informe `.txt` de comportamiento simulado, no código fuente.

El bot ejecuta `python -m moonveil decompile … --require-compile --trace-unsupported` en un subproceso con entorno reducido y límites de CPU, memoria, tiempo, tamaño de salida, descriptores y core dumps. La recuperación principal instrumenta el bootstrap/deserializador en el runtime oficial Luau y captura la VM antes del despacho del programa protegido. El fallback solo se habilita para una huella canónica exacta; ejecuta esa muestra con globals reducidos y proxies inertes para juego/UI/red. No abre conexiones ni carga el código remoto, y el informe indica claramente que no es fuente recuperada ni demuestra equivalencia. Ninguno de estos controles constituye aislamiento del sistema operativo.

No requiere dependencias Python adicionales. Reutiliza Luau 0.722 y `luau-compile` ya instalados por los Dockerfiles del bot. El formato se describe en [`docs/FORMAT.md`](docs/FORMAT.md); procedencia y licencias, en `UPSTREAM.md` y `LICENSE.txt`.

El fixture del ZIP original no se vuelve a distribuir en el repositorio del bot.
