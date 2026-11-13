"""Dashboard: interfaz compacta de una sola pantalla (sin scroll de página).

Solo habla con EngineAPI/RegistryAPI/Configurator; nunca lanza comandos.
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Optional

import flet as ft

import theme as T
from configurator import Configurator
from interfaces import ACTIVE_STATES, EngineEvent, ServiceState

NARROW = 900          # px: por debajo se apila en columna (con scroll)
ELL = ft.TextOverflow.ELLIPSIS


def _chip(text, color=T.CYAN, bg="#102735"):
    return ft.Container(ft.Text(text, size=10, color=color, font_family=T.MONO, max_lines=1),
                        bgcolor=bg, border_radius=4, padding=ft.Padding.symmetric(vertical=2, horizontal=6))


class Dashboard:
    def __init__(self, page: ft.Page, registry, engine):
        self.page, self.registry, self.engine = page, registry, engine
        self.selected: Optional[str] = None
        self.tab = "events"                       # events | log
        self._lock = threading.RLock()
        self._log_cache = ""
        self.configurator = Configurator(page, registry, engine, self._configured)

        self.list_view = ft.ListView(spacing=6, expand=True, padding=ft.Padding.all(10))
        self.inspector = ft.Container(padding=ft.Padding.symmetric(vertical=8, horizontal=12))
        self.events = ft.ListView(spacing=0, auto_scroll=True, expand=True, padding=ft.Padding.all(8))
        self.logtext = ft.Text("", size=11, font_family=T.MONO, color=T.TEXT, selectable=True)
        self.logview = ft.Column([self.logtext], scroll=ft.ScrollMode.AUTO, auto_scroll=True, expand=True, visible=False)
        self.tab_events = ft.Container()
        self.tab_log = ft.Container()
        self.stat = {k: ft.Text("0", size=15, weight=ft.FontWeight.W_700) for k in ("total", "running", "stopped", "errors")}
        self.stat["running"].color, self.stat["errors"].color = T.GREEN, T.RED
        self.body = ft.Container(expand=True)
        engine.add_listener(self._on_event)

    # ---------------------------------------------------------------- layout
    def build(self) -> ft.Control:
        def stat(lbl, key):
            return ft.Container(ft.Row([T.label(lbl, 9, spacing=1), self.stat[key]], spacing=7,
                                       vertical_alignment=ft.CrossAxisAlignment.CENTER),
                                bgcolor=T.PANEL, border=ft.Border.all(1, T.LINE), border_radius=7,
                                padding=ft.Padding.symmetric(vertical=5, horizontal=10))

        topbar = ft.Container(ft.Row([
            ft.Row([ft.Text("AETHERYON", size=15, weight=ft.FontWeight.W_800, color=T.TEXT),
                    ft.Text("ServiceDeck", size=15, weight=ft.FontWeight.W_800, color=T.CYAN),
                    T.label("V1.0 · LOCAL PROCESS CONTROL", 9, spacing=1.2)], spacing=7,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Row([stat("REGISTERED", "total"), stat("RUNNING", "running"),
                    stat("STOPPED", "stopped"), stat("ERRORS", "errors")], spacing=8),
            ft.Row([ft.Text("● ENGINE READY", size=10, color=T.GREEN),
                    T.button("■ Stop All", self._stop_all, "danger", small=True),
                    T.button("＋ Add Service", lambda _: self.configurator.open(), "primary", small=True)], spacing=8,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER)],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN, wrap=True, run_spacing=6),
            padding=ft.Padding.symmetric(vertical=8, horizontal=14), bgcolor="#0d1521",
            border=ft.Border.only(bottom=ft.BorderSide(1, T.LINE)))

        self.registry_panel = T.panel(self.list_view, self._head("Service Registry", "CONFIGURED PROCESSES"), fill=True)
        self.registry_panel.expand = True
        self.inspector_panel = T.panel(self.inspector, self._head("Service Inspector", "SELECTED INSTANCE"))
        self.console_panel = T.panel(
            ft.Container(ft.Stack([self.events, self.logview], expand=True), bgcolor=T.LOG_BG, expand=True),
            ft.Row([self._tabs(), T.button("Clear", self._clear_console, small=True)],
                   alignment=ft.MainAxisAlignment.SPACE_BETWEEN, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            fill=True)
        self.console_panel.expand = True

        footer = ft.Container(ft.Row([T.label("AETHERYON SYSTEMS · SERVICEDECK", 9, spacing=1),
                                      T.label("LOCAL INSTANCE · LIVE ENGINE", 9, spacing=1)],
                                     alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                              padding=ft.Padding.symmetric(vertical=5, horizontal=14),
                              border=ft.Border.only(top=ft.BorderSide(1, T.LINE)))
        self._apply_layout()
        self.page.on_resize = lambda e: self._apply_layout(update=True)
        self._set_tab("events", update=False)
        self.refresh()
        self.log("ServiceDeck initialized.", "info")
        self.log("Registry loaded.", "ok")
        threading.Thread(target=self._tail_loop, daemon=True).start()
        return ft.Column([topbar, self.body, footer], spacing=0, expand=True)

    def _head(self, title, sub):
        return ft.Row([ft.Text(title, size=13, weight=ft.FontWeight.W_700, color=T.TEXT),
                       T.label(sub, 9, spacing=1.2)], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER)

    def _tabs(self):
        self.tab_events = ft.Container(on_click=lambda _: self._set_tab("events"), ink=True, border_radius=5,
                                       padding=ft.Padding.symmetric(vertical=3, horizontal=8))
        self.tab_log = ft.Container(on_click=lambda _: self._set_tab("log"), ink=True, border_radius=5,
                                    padding=ft.Padding.symmetric(vertical=3, horizontal=8))
        return ft.Row([self.tab_events, self.tab_log], spacing=4)

    def _apply_layout(self, update=False):
        width = self.page.width or 1280
        narrow = width < NARROW
        right = ft.Column([self.inspector_panel, self.console_panel], spacing=10, expand=not narrow)
        if narrow:                                    # móvil/ventana chica: apila con scroll
            self.registry_panel.expand = False
            self.list_view.expand = False
            self.list_view.height = 320
            self.console_panel.expand = False
            self.console_panel.height = 300
            self.body.content = ft.Column([self.registry_panel, right], scroll=ft.ScrollMode.AUTO, spacing=10)
        else:                                         # escritorio: una sola vista, sin scroll
            self.registry_panel.expand = 5
            self.list_view.expand = True
            self.list_view.height = None
            self.console_panel.expand = True
            self.console_panel.height = None
            right.expand = 4
            self.body.content = ft.Row([self.registry_panel, right], spacing=10,
                                       vertical_alignment=ft.CrossAxisAlignment.STRETCH)
        self.body.padding = ft.Padding.all(10)
        if update:
            self.page.update()

    # ---------------------------------------------------------------- render
    def refresh(self) -> None:
        with self._lock:
            services = self.registry.list()
            statuses = {s.id: self.engine.status(s.id) for s in services}
            vis = [s for s in services if s.visible]
            self.list_view.controls = [self._row(s, statuses[s.id]) for s in vis] or \
                [ft.Container(T.label("Sin servicios. Usá “＋ Add Service”.", 13), padding=30, alignment=ft.Alignment.CENTER)]
            count = lambda st: sum(1 for x in statuses.values() if x.state == st)
            self.stat["total"].value = str(len(services))
            self.stat["running"].value = str(count(ServiceState.RUNNING))
            self.stat["stopped"].value = str(count(ServiceState.STOPPED))
            self.stat["errors"].value = str(count(ServiceState.ERROR))
            if self.selected and not self.registry.get(self.selected):
                self.selected = None
            self._render_inspector()
            self._render_tabs()
            try:
                self.page.update()
            except Exception:
                pass

    def _row(self, cfg, st) -> ft.Control:
        busy = st.state in (ServiceState.STARTING, ServiceState.STOPPING)
        color = T.STATE_COLOR[st.state.value]
        sw = ft.Switch(value=st.state in (ServiceState.RUNNING, ServiceState.STARTING), disabled=busy,
                       active_color=T.GREEN, inactive_track_color="#344258", inactive_thumb_color="#ffffff",
                       tooltip=f"Toggle {cfg.name} [{cfg.alias}]", key=f"toggle_{cfg.alias}", scale=0.8,
                       on_change=lambda e, i=cfg.id: self._toggle(i, e.control))
        head = ft.Row([
            ft.Container(width=8, height=8, border_radius=4, bgcolor=color, tooltip=st.state.value),
            ft.Text(cfg.name, size=13, weight=ft.FontWeight.W_700, color=T.TEXT, max_lines=1, overflow=ELL),
            _chip(cfg.alias),
            ft.Container(expand=True),
            ft.Text(st.state.value, size=10, weight=ft.FontWeight.W_700, color=color),
            T.label(f"PID {st.pid or '—'}", 10, mono=True),
            T.label(cfg.mode.upper()[:4], 10, mono=True)], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER)
        cmd = ft.Text(f"{cfg.executable} {cfg.args}".strip(), size=10.5, color=T.MUTED, font_family=T.MONO,
                      max_lines=1, overflow=ELL)
        main = ft.Column([head, cmd], spacing=3, expand=True)
        actions = ft.Row([sw, T.icon_button("↻", "Restart", lambda _, i=cfg.id: self._restart(i), disabled=busy),
                          T.icon_button("⚙", "Configure", lambda _, i=cfg.id: self.configurator.open(i))],
                         spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER)
        return ft.Container(ft.Row([main, actions], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=8),
                            padding=ft.Padding.symmetric(vertical=7, horizontal=11), bgcolor=T.ROW_BG, border_radius=8,
                            border=ft.Border.all(1, T.CYAN if self.selected == cfg.id else T.LINE),
                            on_click=lambda _, i=cfg.id: self._select(i))

    def _render_inspector(self) -> None:
        cfg = self.registry.get(self.selected) if self.selected else None
        if not cfg:
            self.inspector.content = T.label("Seleccioná un servicio para ver su configuración.", 12)
            return
        st = self.engine.status(cfg.id)
        active = st.state in ACTIVE_STATES

        def kv(k, v, color=T.TEXT, w=118):
            return ft.Container(ft.Column([T.label(k.upper(), 9, spacing=0.8),
                                           ft.Text(str(v), size=11, color=color, max_lines=1, overflow=ELL,
                                                   selectable=True, tooltip=str(v))], spacing=1), width=w)

        def wide(k, v, color=T.TEXT):
            return ft.Row([T.label(k.upper(), 9, spacing=0.8),
                           ft.Text(str(v), size=11, color=color, max_lines=1, overflow=ELL, selectable=True,
                                   expand=True, tooltip=str(v), font_family=T.MONO)], spacing=8)
        uptime = f"{int(time.time() - st.started_at)}s" if st.started_at and st.state == ServiceState.RUNNING else "—"
        self.inspector.content = ft.Column([
            ft.Row([ft.Text(cfg.name, size=13, weight=ft.FontWeight.W_700, color=T.TEXT, max_lines=1, overflow=ELL),
                    _chip(cfg.alias), ft.Container(expand=True), _chip(cfg.id, T.MUTED, "#0d1624")], spacing=8),
            ft.Row([kv("State", st.state.value, T.STATE_COLOR[st.state.value]), kv("PID", st.pid or "—"),
                    kv("Uptime", uptime), kv("Exit", "—" if st.exit_code is None else st.exit_code),
                    kv("Restart", "ON" if cfg.autorestart else "OFF", w=70)], spacing=6, wrap=True, run_spacing=4),
            wide("Exec", f"{cfg.executable} {cfg.args}".strip()), wide("Dir", cfg.cwd),
            *([wide("Msg", st.message, T.RED if st.state == ServiceState.ERROR else T.MUTED)] if st.message else []),
            ft.Row([T.button("Edit", lambda _: self.configurator.open(cfg.id), small=True, disabled=active),
                    T.button("Logs", lambda _: self._set_tab("log"), small=True),
                    T.button("Delete", lambda _: self.configurator.confirm_delete(cfg.id), "danger", small=True,
                             disabled=active)], spacing=6)], spacing=5, tight=True)

    def _select(self, sid: str) -> None:
        self.selected = sid
        self._log_cache = ""
        self.refresh()
        self._pull_log()

    # ---------------------------------------------------------------- consola / tabs
    def _render_tabs(self):
        cfg = self.registry.get(self.selected) if self.selected else None
        for box, name, text in ((self.tab_events, "events", "EVENTS"),
                                (self.tab_log, "log", f"LOG · {cfg.alias}" if cfg else "LOG")):
            on = self.tab == name
            box.content = ft.Text(text, size=10, weight=ft.FontWeight.W_700, color=T.CYAN if on else T.MUTED,
                                  max_lines=1, overflow=ELL)
            box.bgcolor = "#102735" if on else None

    def _set_tab(self, name: str, update: bool = True) -> None:
        self.tab = name
        self.events.visible = name == "events"
        self.logview.visible = name == "log"
        self._render_tabs()
        if name == "log":
            self._pull_log(force=True)
        if update:
            try:
                self.page.update()
            except Exception:
                pass

    def _pull_log(self, force: bool = False) -> None:
        if self.tab != "log":
            return
        text = self.engine.read_log(self.selected, tail=400) if self.selected else "(seleccioná un servicio)"
        if force or text != self._log_cache:
            self._log_cache = text
            self.logtext.value = text
            try:
                self.logtext.update()
            except Exception:
                pass

    def _tail_loop(self) -> None:                     # refresco en vivo del log y uptime
        while True:
            time.sleep(1.5)
            try:
                self._pull_log()
            except Exception:
                pass

    def log(self, message: str, level: str = "info") -> None:
        color = {"ok": T.GREEN, "info": T.CYAN, "warn": T.AMBER, "error": T.RED}.get(level, T.MUTED)
        self.events.controls.append(ft.Text(f"[{time.strftime('%H:%M:%S')}] {message}", size=11, color=color,
                                            font_family=T.MONO, selectable=True))
        del self.events.controls[:-200]
        try:
            self.events.update()
        except Exception:
            pass

    def _clear_console(self, _=None) -> None:
        if self.tab == "events":
            self.events.controls.clear()
            self.events.update()

    # ---------------------------------------------------------------- acciones
    def _toggle(self, sid: str, control: ft.Switch) -> None:
        st = self.engine.status(sid)
        # el switch refleja SIEMPRE el estado efectivo: se revierte hasta que el motor confirme
        control.value = st.state in (ServiceState.RUNNING, ServiceState.STARTING)
        try:
            control.update()
        except Exception:
            pass
        cfg = self.registry.get(sid)
        if not cfg:
            return
        if st.state == ServiceState.RUNNING:
            self._bg(self.engine.stop, sid)
        elif st.state in (ServiceState.STOPPED, ServiceState.ERROR):
            self._bg(self.engine.start, cfg)

    def _restart(self, sid: str) -> None:
        cfg = self.registry.get(sid)
        if cfg and self.engine.status(sid).state not in (ServiceState.STARTING, ServiceState.STOPPING):
            self._bg(self.engine.restart, cfg)

    def _stop_all(self, _=None) -> None:
        if not any(self.engine.is_active(s.id) for s in self.registry.list()):
            return self.log("Stop All: no hay servicios activos", "info")
        self.log("Stop All requested", "warn")
        self._bg(self.engine.stop_all)

    @staticmethod
    def _bg(fn, *a) -> None:
        threading.Thread(target=fn, args=a, daemon=True).start()

    def _configured(self, sid: Optional[str]) -> None:
        if sid:
            self.selected = sid
            cfg = self.registry.get(sid)
            self.log(f"Configuration saved: {cfg.name} [{cfg.alias}]" if cfg else "Saved", "ok")
        else:
            self.log("Service removed", "warn")
        self.refresh()

    # ---------------------------------------------------------------- engine → UI
    def _on_event(self, ev: EngineEvent) -> None:
        self.log(ev.message if ev.kind == "log" else f"{self._alias(ev.service_id)}: {ev.message}", ev.level)
        if ev.kind == "state":
            self.refresh()
            if ev.service_id == self.selected:
                self._pull_log(force=True)

    def _alias(self, sid) -> str:
        cfg = self.registry.get(sid) if sid else None
        return cfg.alias if cfg else str(sid)

    async def on_window_event(self, e: ft.WindowEvent) -> None:
        if e.type == ft.WindowEventType.CLOSE:
            self.log("Closing: stopping managed services…", "warn")
            await asyncio.to_thread(self.engine.shutdown)
            await self.page.window.destroy()
