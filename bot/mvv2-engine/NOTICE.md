# Aviso de terceros: `.mvv2`

El paquete `moonveil/` se actualizó desde el ZIP de MoonVeil V2 Deobfuscator **0.3.0** proporcionado por el usuario; el ZIP tiene SHA-256 `8f69ae06ad640774b65dc9f0782bdd46762a349cd5ac351149cbb333fe6fb4ae`. Se distribuye bajo MIT y la licencia completa está en `LICENSE.txt`. La referencia metodológica declarada es `edwindev-spec/MoonVeil` (commit `a948c4849a692f0af40d554708cac28b610c5115`), cuyo perfil es 1.4.5.

La versión 0.3.0 añade emisión de Luau y traza simbólica. En `auto`, un layout no soportado por el perfil estático puede ejecutar el programa protegido en Luau con globals simbólicos; las APIs del juego/red, archivos, `require` y carga dinámica no se exponen como APIs reales. La traza genérica sigue siendo contención de recursos, **no sandbox de sistema operativo**. El bot limita CPU, memoria, tiempo, archivos, descriptores, core dumps y salida.

`behavior.py` y `trace_template.luau` son una adaptación local existente para una única variante callback-heavy, protegida por huella canónica exacta. Usa proxies inertes y callbacks sintéticos, no abre conexiones ni ejecuta contenido remoto, y devuelve un `.txt` de observaciones en lugar de código fuente.

Luau y `luau-compile` se compilan desde el proyecto oficial Luau 0.722, commit `32d52d1b2ceef46fc25d87094a2d7f201c3ea5b8`; la licencia MIT se conserva en `../mv-engine/LUAU_LICENSE.txt`. Los fixtures y pruebas del ZIP fuente no se redistribuyen en este repositorio.
