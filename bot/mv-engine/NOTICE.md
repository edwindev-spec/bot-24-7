# Aviso de terceros: MoonVeil

El motor proviene de [`UnstableGrape/Moonveil-1.4.5-Deobfuscator`](https://github.com/UnstableGrape/Moonveil-1.4.5-Deobfuscator), commit `93c2d8169bf1fe0959c00b34fffd7889dd1ce2cf`, bajo MIT. El código fuente y `LICENSE.txt` se distribuyen juntos.

El runtime y el compilador provienen del proyecto oficial [`luau-lang/luau`](https://github.com/luau-lang/luau), versión `0.722`, commit `32d52d1b2ceef46fc25d87094a2d7f201c3ea5b8`, también bajo MIT. El contenedor los compila durante la construcción; `LUAU_LICENSE.txt` conserva su licencia.

El handler usa `moonveil decompile --no-verify`. Esto omite la comparación de comportamiento que ejecutaría el programa protegido. La recuperación sí ejecuta etapas instrumentadas del bootstrap del formato dentro del CLI Luau. El subproceso tiene límites de CPU, memoria, tiempo, archivos y descriptores, y no recibe el token de Discord ni otras variables del entorno del bot.
