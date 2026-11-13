import sys, os, time, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from unittest.mock import MagicMock
import flet as ft, psutil
from serviceregistry import ServiceRegistry
from process_engine import ProcessEngine
from dashboard import Dashboard

d = tempfile.mkdtemp(); work = tempfile.mkdtemp(); open(work+"/app.py","w").write("import subprocess,sys,time\nsubprocess.Popen([sys.executable,\"-c\",\"import time;time.sleep(600)\"])\nprint(\"hello\",flush=True);time.sleep(600)\n")
reg = ServiceRegistry(d + "/services.json"); eng = ProcessEngine(d); reg.set_lock_guard(eng.is_active)
page = MagicMock(); page.window = MagicMock(); page.width = 1280; page.height = 720
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
