# Procedencia y licencias

- El motor Python `moonveil/` procede de la versión **0.3.0** del archivo `moonveil-v2-deobfuscator-0.3.0(1).zip` proporcionado por el usuario. SHA-256 del archivo recibido: `8f69ae06ad640774b65dc9f0782bdd46762a349cd5ac351149cbb333fe6fb4ae`. Se conserva su licencia MIT completa en `LICENSE.txt` y su changelog en `CHANGELOG.md`.
- La implementación declara como referencia metodológica [edwindev-spec/MoonVeil](https://github.com/edwindev-spec/MoonVeil), commit `a948c4849a692f0af40d554708cac28b610c5115`, bajo MIT; ese proyecto de referencia trata MoonVeil 1.4.5, no el perfil 2.1.0 de este motor.
- `analysis.py`, `emit.py` y `trace.py` de 0.3.0 se mantienen junto a los módulos del paquete incluidos en el archivo. No se copia `tools/install_luau.py`: el contenedor ya compila Luau desde la versión oficial fijada por el proyecto.
- No se incorporan al repositorio del bot los fixtures, muestras ni pruebas del ZIP. La carpeta `tests/fixtures` del archivo original queda fuera del paquete publicado.
- `behavior.py` y `trace_template.luau` son una adaptación local preexistente del bot para una sola variante callback-heavy. La ruta está bloqueada por la huella canónica `0857b81ed099300b06152cf85c9a152a6ba534333cd0422e62240eed22271953`; su informe no es fuente recuperada.
- Los ejecutables `luau` y `luau-compile` se compilan desde el proyecto oficial Luau 0.722, commit `32d52d1b2ceef46fc25d87094a2d7f201c3ea5b8`. Su licencia MIT se conserva en `../mv-engine/LUAU_LICENSE.txt`; los binarios no se vendorizan en este directorio.
