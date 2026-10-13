# Origen de los componentes

| Proyecto | Revisión inspeccionada | Uso |
| --- | --- | --- |
| [caomod2077/Deobfuscator-Luraph-V15](https://github.com/caomod2077/Deobfuscator-Luraph-V15) | `892b48ac200e9deda6cd6fd3142bf66abed393c6` | Motor v14/v15, reconstrucción del flujo, registros, constantes y emulación Roblox. Código MIT incorporado en `vendor/caomod2077`. |
| [KryptIT/luraph-v15-v14.x-deobfuscator](https://github.com/KryptIT/luraph-v15-v14.x-deobfuscator) | `ed79a86cc41187af0480e69f45a4838723fdb099` | Integración opcional mediante subprocess con una copia instalada por el usuario. No se redistribuye código: no incluye licencia. Su separación entre trazas y recuperación estructural orienta los informes. |
| [mehCake/luraph-deobfuscator-py](https://github.com/mehCake/luraph-deobfuscator-py) | `af863900d2f59b8db2b22b8248371e2f1bbdb98d` | Esta revisión solo tiene README y licencia GPLv2, sin implementación integrable. No se incorporan componentes. |

Se conserva íntegra la licencia del motor MIT en `vendor/caomod2077/LICENSE`.
El adaptador `worker.js` es nuevo. Los motores originales se conservan salvo
los cambios documentados aquí: `src/detect.js` corrige la restauración de la
cabecera para reconocer solo una cabecera inicial y no añadir saltos al EOF.
`core/obfuscators/luraph_v15/devirt.py` corrige la lectura de una lista variádica:
las escrituras explícitas prevalecen sobre sus argumentos originales. También
marca como pendiente la mutación de listas variádicas del prólogo que no se
conservan entre instrucciones, evitando un lifting aparentemente correcto.

El script de construcción usa [Luau](https://github.com/luau-lang/luau), MIT,
en la revisión `a62362a53ddc9c629b0e29378a84abb4534d8b64` (0.739). Se deja
escribible la tabla de métodos de vectores para la emulación de Vector3.
El código Luau descargado y su licencia quedan en `.build/luau`; no se
incluyen ejecutables ni archivos de muestra de terceros en este repositorio.
