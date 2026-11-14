# AETHERYON ServiceDeck V1

Desktop (Windows) · Python + Flet 1.0.5 · control real de procesos locales sin Docker.

## Ejecutar
    pip install -r requirements.txt
    python main.py          (o run.bat)

Datos en `%LOCALAPPDATA%\AETHERYON\ServiceDeck` (override: variable `SERVICEDECK_HOME`):
`services.json` (registro), `runtime.json` (identidad de procesos vivos), `logs/<id>.out.log|.err.log`.

## Módulos
| Módulo | Archivo | Responsabilidad |
|---|---|---|
| Contratos | `interfaces.py` | dataclasses, estados, Protocols `RegistryAPI` / `EngineAPI` |
| ServiceRegistry | `serviceregistry.py` | ids inmutables `svc_xxxx`, alias únicos, validación, persistencia atómica |
| ProcessEngine | `process_engine.py` | `subprocess.Popen`, Job Objects, logs, monitor, reconcile |
| Configurator | `configurator.py` | modal alta/edición, baja con confirmación |
| Dashboard | `dashboard.py` | UI; solo usa las interfaces, nunca lanza comandos |

## Garantías
- Toggle ↔ servicio por **ID interno**; el alias es único y el nombre visible puede cambiar sin romper nada.
- El toggle refleja el estado efectivo: se actualiza solo con eventos del motor (STARTING → RUNNING solo si el proceso sobrevive 0.8 s; PID real).
- Detención: terminate al árbol completo → kill tras 5 s. Windows: un Job Object por ejecución con `KILL_ON_JOB_CLOSE` (si ServiceDeck muere, el SO mata el árbol). Linux/macOS (dev): grupo de procesos + `killpg`.
- Si el proceso principal termina, sus descendientes se eliminan (sin huérfanos).
- Cierre inesperado → ERROR (código ≠ 0); `autorestart` con backoff (máx. 5 en 60 s).
- Cerrar la ventana detiene todos los servicios gestionados.
- Al iniciar: `reconcile()` adopta solo procesos cuyo PID **y** hora de creación **y** marca `SERVICEDECK_SERVICE_ID` coinciden. Un PID viejo reutilizado por otro programa no se adopta ni se toca.
- Config bloqueada (editar/eliminar) mientras el servicio está activo, en UI **y** en el registro.
- Modo "Visible console": ventana propia; su salida no se captura en logs (se avisa en la consola).

## Intérprete / terminal
El `python` que ve ServiceDeck puede diferir del de tu terminal (venv, conda, otra instalación). En el modal:
- **Detect**: lista los Python encontrados (PATH, venv del proyecto, py launcher, conda…), con versión y qué imports del script les faltan; click en una fila para usarla.
- **Test**: prueba el Executable actual. **Launch via**: lanza a través de cmd / PowerShell / Bash para heredar el entorno de la terminal.
- La consola muestra `launch → <ruta>` en cada inicio: ahí se ve qué intérprete se usó.

## v1.3 — logs en vivo, Open URL, autostart
- **Logs en vivo (sin botón):** el motor sigue stdout/stderr de cada servicio y emite cada línea como evento; al seleccionar un servicio la consola pasa a su pestaña LOG, carga lo último y sigue en tiempo real. stderr en ámbar (rojo si es error/traceback), filtro de texto y **Follow** para fijar/soltar el auto-scroll.
- **Open ↗:** se detecta la primera URL http(s) que imprime el servicio (0.0.0.0 → 127.0.0.1, prioriza loopback); botón en la fila y en el Inspector.
- **Auto Start:** por servicio ("Al abrir ServiceDeck"); se inician escalonados, omitiendo los ya activos.

## Pruebas
    python tests/test_engine.py -v         # 10 pruebas con procesos reales
    python tests/ui_logic_smoke.py          # modal → guardar → toggle → PID → stop (página simulada)
