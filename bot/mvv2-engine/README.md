# Motor `.mvv2`

El paquete separado `moonveil/` está actualizado a la versión **0.3.0** del ZIP proporcionado por el usuario. No sustituye `.mv` (MoonVeil 1.4.5) ni garantiza compatibilidad con cualquier archivo que lleve el banner MoonVeil 2.1.0.

## Comportamiento en el bot

El bot invoca `python -m moonveil decompile --mode auto --require-compile`. El motor intenta primero el perfil VM estático conocido, que captura el grafo antes de despachar el programa protegido. Para un layout distinto, el modo `auto` intenta la recuperación de operaciones mediante un entorno simbólico y escribe el Luau observado como `resultado.lua`. Ese resultado solo describe las operaciones que alcanzó el camino ejecutado; no recupera control de flujo o cálculos que no expongan interacciones con el entorno, y compilarlo no demuestra equivalencia.

Una variante callback-heavy adicional se mantiene detrás de una huella canónica exacta. Esa ruta usa la plantilla local `moonveil/trace_template.luau`, proxies inertes para juego/UI/HTTP y callbacks sintéticos; produce `resultado-mvv2-traza.txt`, no código Lua. La respuesta no descarga ni ejecuta contenido remoto. Otros wrappers no reciben este tratamiento especial.

## Seguridad y límites

El modo simbólico ejecuta el programa protegido dentro de Luau, pero sustituye los globals de juego, HTTP, archivos y carga dinámica por marcadores simbólicos; las funciones `io`, `os.execute`, `require` y `loadstring` reales no se exponen al programa. La lista estándar permitida se limita a bibliotecas de cálculo/datos y un subconjunto de `os` sin `execute`. Hay límite de operaciones, timeout, memoria, tamaño de salida, CPU, archivos, descriptores y core dumps.

Esto es contención de recursos y un entorno de globals reducido, **no aislamiento del sistema operativo** ni una garantía frente a una vulnerabilidad del runtime. No se abren conexiones para procesar el archivo. El comando sigue restringido al propietario configurado del bot.

El bot reutiliza los binarios oficiales `luau` y `luau-compile` 0.722 de los Dockerfiles existentes; no requiere dependencias Python nuevas ni el instalador del ZIP. Los fixtures y las pruebas del archivo de origen no se redistribuyen dentro del repositorio del bot. Consulta `CHANGELOG.md`, `docs/FORMAT.md`, `UPSTREAM.md` y `NOTICE.md`.
