# Aviso de MoonVeil Deobfuscator

- Proyecto fuente: `binxgtl/luau-vmp-deobf`.
- Commit integrado: `2ef18387b69e94ce71e42d2b2c2456329f208f99`.
- Licencia upstream: MIT; el texto íntegro se distribuye junto al código en `LICENSE`.
- Integración utilizada: comando estático `luauvmp deobf` con `profiles/moonveil-1.4.5.json`.
- El bot no ejecuta el Lua de entrada, no invoca el runtime Lune y no llama a `lua.expert` ni a otros servicios de decompilación externos.
- El contenedor aplica límites de tiempo, CPU, memoria, tamaño de archivos y descriptores al proceso.

El resultado depende de que el archivo sea compatible con el perfil MoonVeil 1.4.5; otros formatos o variantes pueden producir errores o una recuperación incompleta.
