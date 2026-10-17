# Aviso de terceros: `.mvv2`

El código de `moonveil/` procede del ZIP proporcionado por el usuario y se mantiene bajo MIT; la licencia completa está en `LICENSE.txt`. La implementación se guía por `edwindev-spec/MoonVeil` (commit `a948c4849a692f0af40d554708cac28b610c5115`), cuyo deobfuscator upstream cubre MoonVeil 1.4.5. Este paquete separado está orientado a un perfil calibrado de MoonVeil 2.1.0.

El runtime `luau` y `luau-compile` se compilan desde el proyecto oficial Luau 0.722, commit `32d52d1b2ceef46fc25d87094a2d7f201c3ea5b8`; su licencia MIT se conserva en `../mv-engine/LUAU_LICENSE.txt`.

La captura principal ejecuta etapas de bootstrap/deserialización derivadas de la entrada, pero intercepta el constructor antes de despachar la VM protegida. Para una única huella canónica adicional, una traza opcional ejecuta la entrada con globals reducidos, proxies inertes y HTTP interceptado; no obtiene ni ejecuta código remoto y solo produce observaciones, no fuente recuperada. El subproceso del bot tiene límites de recursos y un entorno reducido; esto no es un sandbox de sistema operativo.
