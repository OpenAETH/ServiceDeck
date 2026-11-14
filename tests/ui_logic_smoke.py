import sys, os, time, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from unittest.mock import MagicMock
import flet as ft, psutil
from serviceregistry import ServiceRegistry
from process_engine import ProcessEngine
from dashboard import Dashboard

d = tempfile.mkdtemp(); work = tempfile.mkdtemp(); open(work+"/app.py","w").write("import subprocess,sys,time\nsubprocess.Popen([sys.executable,\"-c\",\"import time;time.sleep(600)\"])\nprint(\"hello\",flush=True);time.sleep(600)\n")
reg = ServiceRegistry(d + "/services.json"); eng = ProcessEngine(d); reg.set_lock_guard(eng.is_active)
import asyncio
page = MagicMock(); page.window = MagicMock(); page.width = 1280; page.height = 720
page.run_task.side_effect = lambda h, *a, **k: asyncio.run(h(*a, **k))
dash = Dashboard(page, reg, eng); root = dash.build()
shown = []; page.show_dialog.side_effect = shown.append
dash.configurator.open()                       # construye el modal completo
dlg = shown[-1]; print("modal ok:", type(dlg).__name__)
# simular completar formulario vía el árbol de controles
def walk(c):
    yield c
    for a in ("content","controls","actions","title"):
        v = getattr(c, a, None)
        if v is None: continue
        for x in (v if isinstance(v, list) else [v]):
            if isinstance(x, ft.Control): yield from walk(x)
tfs = {c.label: c for c in walk(dlg) if isinstance(c, (ft.TextField, ft.Dropdown)) and getattr(c,"label",None)}
tfs["Service Name"].value = "Mi Servicio"; tfs["Service Name"].on_change(None)
print("alias auto:", tfs["UI Alias / Binding ID"].value)
tfs["Executable (ruta completa recomendada)"].value = sys.executable; tfs["Arguments"].value = "app.py"; tfs["Working Directory"].value = work
save = [c for c in walk(dlg) if isinstance(c, ft.Container) and isinstance(c.content, ft.Text) and "Save" in (c.content.value or "")][0]
save.on_click(None)
cfg = reg.list()[0]; print("saved:", cfg.alias, cfg.id)
# toggle real
sw = [c for c in walk(dash.list_view) if isinstance(c, ft.Switch)][0]
sw.on_change(MagicMock(control=sw)); 
for _ in range(50):
    st = eng.status(cfg.id)
    if st.state.value == "RUNNING": break
    time.sleep(0.1)
print("state:", st.state.value, "pid:", st.pid, "alive:", psutil.pid_exists(st.pid))
time.sleep(0.3); dash.refresh()
sw = [c for c in walk(dash.list_view) if isinstance(c, ft.Switch)][0]; print("switch value after confirm:", sw.value)
kids = psutil.Process(st.pid).children(recursive=True); print("children:", len(kids))
print("log:", eng.read_log(cfg.id).splitlines()[-1])
sw.on_change(MagicMock(control=sw)); time.sleep(1.5)
print("after stop:", eng.status(cfg.id).state.value, "pid alive:", psutil.pid_exists(st.pid), "kids alive:", any(k.is_running() and k.status()!='zombie' for k in kids))
dash._select(cfg.id); print("inspector ok")
eng.shutdown()

# --- v1.3: la primera línea de salida no se pierde (URL detectada durante STARTING no limpia la vista)
import dashboard as _dm
from interfaces import EngineEvent, ServiceStatus, ServiceState
d2 = _dm.Dashboard(page, reg, eng); d2.build(); d2.selected = cfg.id
ST = lambda st, m="": EngineEvent("state", cfg.id, m, "info", ServiceStatus(cfg.id, st))
d2._on_event(ST(ServiceState.STARTING, "Iniciando…"))
d2._on_event(EngineEvent("output", cfg.id, "primera linea", "info", stream="out"))
d2._on_event(ST(ServiceState.STARTING, "URL detectada"))          # sigue STARTING: NO debe limpiar
assert ("out", "primera linea") in list(d2._buf), "se perdió la primera línea"
d2._on_event(ST(ServiceState.RUNNING)); d2._on_event(ST(ServiceState.STOPPED)); d2._on_event(ST(ServiceState.STARTING))
assert not list(d2._buf), "nueva ejecución debe limpiar la vista"
print("live-view regression ok")

# --- v1.4: consola de eventos (dedupe, nivel, copiado) y flush por el loop de la UI
d3 = _dm.Dashboard(page, reg, eng); d3.build()
for _ in range(3):
    d3.log("puerto ocupado", "warn", sid=cfg.id, alias="mi_servicio")
d3.log("todo bien", "ok"); d3.log("falló", "error", sid=cfg.id, alias="mi_servicio")
assert page.run_task.called                                   # los hilos agendan en el loop de la UI
assert len(d3._entries) >= 3 and d3._entries[-3]["count"] == 3, [ (e["text"], e["count"]) for e in d3._entries]
txt = d3._console_text(); assert "WARN  mi_servicio: puerto ocupado (×3)" in txt and "ERROR mi_servicio: falló" in txt, txt
d3._cycle_level(); d3._cycle_level()                           # nivel ERROR
assert [e["text"] for e in d3._entries if e["row"].visible] == ["falló"]
assert d3._console_text().strip().endswith("falló") and "todo bien" not in d3._console_text()
d3.selected = cfg.id; d3._load_buffer(cfg.id); d3._set_tab("log", update=False); d3._rerender(update=False)
assert "\n".join(l for _, l in d3._buf) == d3._console_text()   # copiar en LOG = todo el buffer filtrado
print("events/copy ok")
eng.shutdown()
