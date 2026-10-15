# Aviso de terceros: MoonVeil

El paquete proviene de [`edwindev-spec/MoonVeil`](https://github.com/edwindev-spec/MoonVeil), versión `0.13.0`, commit `a948c4849a692f0af40d554708cac28b610c5115`, bajo MIT. Esta versión deriva de `UnstableGrape/Moonveil-1.4.5-Deobfuscator` en el commit `93c2d8169bf1fe0959c00b34fffd7889dd1ce2cf`; se conserva la procedencia en `UPSTREAM.md` y la licencia en `LICENSE.txt`.

El runtime y el compilador Luau se construyen desde el proyecto oficial `luau-lang/luau`, versión `0.722`, commit `32d52d1b2ceef46fc25d87094a2d7f201c3ea5b8`, bajo MIT; la licencia se conserva en `LUAU_LICENSE.txt`.

La integración local mantiene todos los modos de recuperación y ejecuta como máximo dos probes independientes en paralelo. El handler usa `moonveil decompile --no-verify`: omite la comparación de trazas del programa protegido, pero las etapas instrumentadas de recuperación sí ejecutan partes del bootstrap en Luau. El proceso recibe un entorno reducido y límites de CPU, memoria, tiempo, tamaño de archivos, descriptores y core dumps. Esto no constituye aislamiento del sistema operativo.
