# Procedencia de IronBrew1

El motor en `deobf/` proviene de `larpcorrupt/luraph-v15-v14.x-deobfuscator`, commit `ed79a86cc41187af0480e69f45a4838723fdb099`, subdirectorio `Deobfuscator/deobf/` y plugin `obfuscators/ironbrew1/`. La integración se limita al plugin IronBrew1 y sus módulos compartidos; no incluye otros plugins upstream, muestras de entrada ni ejecutables Windows.

La persona que mantiene este proyecto indicó que cuenta con autorización para reutilizar y redistribuir este código para esta integración. El repositorio upstream no declara una licencia explícita para estos archivos; esta nota no pretende crear una licencia ni conceder derechos adicionales.

Cambios locales deliberados: `obfuscators/__init__.py` registra exclusivamente IronBrew1; la cabecera promocional de salida del módulo `obfuscators/base.py` se reemplazó por una atribución neutral. El runtime y la herramienta AST de Luau se compilan desde el commit fijado en el Dockerfile y no se incluyen como binarios en el repositorio.

La licencia MIT de Luau 0.739 se conserva en `LUAU_LICENSE.txt` y también se copia a `/app/licenses/LUAU-0.739-MIT.txt` en la imagen.
