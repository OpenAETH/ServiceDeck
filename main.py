"""AETHERYON ServiceDeck — punto de entrada. Cablea los cuatro módulos."""
import atexit
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import flet as ft

import theme as T
from dashboard import Dashboard
from process_engine import ProcessEngine
from serviceregistry import ServiceRegistry


def data_dir() -> str:
    if os.environ.get("SERVICEDECK_HOME"):
        return os.environ["SERVICEDECK_HOME"]
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "AETHERYON", "ServiceDeck")


def build_core():
    d = data_dir()
    os.makedirs(d, exist_ok=True)
    registry = ServiceRegistry(os.path.join(d, "services.json"))
    engine = ProcessEngine(d)
    registry.set_lock_guard(engine.is_active)      # no editar/borrar mientras corre
    engine.reconcile(registry.list())              # estado real, sin fiarse de PIDs viejos
    atexit.register(engine.shutdown)
    return registry, engine


def autostart(registry, engine):
    """Inicia (escalonado) los servicios marcados 'Auto Start' que no estén ya activos."""
    for cfg in registry.list():
        if cfg.autostart and not engine.is_active(cfg.id):
            engine.start(cfg)
            time.sleep(0.3)


def main(page: ft.Page):
    registry, engine = build_core()
    page.title = "AETHERYON ServiceDeck"
    page.bgcolor = T.BG
    page.padding = 0
    page.theme_mode = ft.ThemeMode.DARK
    page.theme = ft.Theme(font_family="Inter")
    page.window.width, page.window.height = 1180, 720
    page.window.min_width, page.window.min_height = 380, 520
    dash = Dashboard(page, registry, engine)
    page.window.prevent_close = True
    page.window.on_event = dash.on_window_event
    page.add(dash.build())
    threading.Thread(target=autostart, args=(registry, engine), daemon=True).start()


if __name__ == "__main__":
    ft.run(main)
