"""Dashboard: interfaz y controles. Solo habla con EngineAPI/RegistryAPI/Configurator."""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Optional

import flet as ft

import theme as T
from configurator import Configurator
from interfaces import ACTIVE_STATES, EngineEvent, ServiceState


class Dashboard:
    def __init__(self, page: ft.Page, registry, engine):
        self.page, self.registry, self.engine = page, registry, engine
        self.selected: Optional[str] = None
        self._lock = threading.RLock()
        self.configurator = Configurator(page, registry, engine, self._configured)

        self.list_col = ft.Column(spacing=10)
        self.inspector = ft.Container(padding=16)
        self.console = ft.ListView(spacing=0, auto_scroll=True, height=210, padding=12)
        self.stat = {k: ft.Text("0", size=25, weight=ft.FontWeight.W_700) for k in ("total", "running", "stopped", "errors")}
        self.stat["running"].color, self.stat["errors"].color = T.GREEN, T.RED
        self.registry_count = T.label("0 SERVICES", 10, spacing=1)
        engine.add_listener(self._on_event)

    # ---------------------------------------------------------------- layout
    def build(self) -> ft.Control:
        header = ft.Container(
            ft.Row([ft.Column([ft.Row([ft.Text("AETHERYON", size=17, weight=ft.FontWeight.W_800, color=T.TEXT),
                                        ft.Text("ServiceDeck", size=17, weight=ft.FontWeight.W_800, color=T.CYAN)], spacing=6),
                               T.label("LOCAL PROCESS CONTROL / V1.0", 10, spacing=1.5)], spacing=2),
                    ft.Row([ft.Text("● ENGINE READY", size=11, color=T.GREEN), T.label("WINDOWS", 10, spacing=1.5)], spacing=15)],
                   alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            height=70, padding=ft.Padding.symmetric(horizontal=28), bgcolor="#0d1521",
            border=ft.Border.only(bottom=ft.BorderSide(1, T.LINE)), alignment=ft.Alignment.CENTER_LEFT)

        toolbar = ft.Row([
            ft.Column([ft.Text("Service Control", size=23, weight=ft.FontWeight.W_700, color=T.TEXT),
                       ft.Text("Manage local scripts and background processes.", size=13, color=T.MUTED)], spacing=4),
            ft.Row([T.button("■ Stop All", self._stop_all, "danger"),
                    T.button("＋ Add Service", lambda _: self.configurator.open(), "primary")], spacing=8)],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN, wrap=True)

        def card(lbl, key):
            return ft.Container(ft.Column([T.label(lbl, 10, spacing=1), self.stat[key]], spacing=9),
                                bgcolor=T.PANEL, border=ft.Border.all(1, T.LINE), border_radius=9,
                                padding=ft.Padding.symmetric(vertical=15, horizontal=17), expand=True)
        stats = ft.Row([card("REGISTERED", "total"), card("RUNNING", "running"),
                        card("STOPPED", "stopped"), card("ERRORS", "errors")], spacing=12)

        registry_panel = T.panel(
            ft.Container(self.list_col, padding=14),
            ft.Row([ft.Column([ft.Text("Service Registry", size=14, weight=ft.FontWeight.W_700, color=T.TEXT),
                               T.label("CONFIGURED PROCESSES", 10, spacing=1.5)], spacing=5), self.registry_count],
                   alignment=ft.MainAxisAlignment.SPACE_BETWEEN))
        inspector_panel = T.panel(
            self.inspector,
            ft.Row([ft.Column([ft.Text("Service Inspector", size=14, weight=ft.FontWeight.W_700, color=T.TEXT),
                               T.label("SELECTED INSTANCE", 10, spacing=1.5)], spacing=5),
                    ft.Text("⌘", color=T.CYAN)], alignment=ft.MainAxisAlignment.SPACE_BETWEEN))
        console_panel = T.panel(
            ft.Container(self.console, bgcolor=T.LOG_BG),
            ft.Row([ft.Column([ft.Text("Process Console", size=14, weight=ft.FontWeight.W_700, color=T.TEXT),
                               T.label("EVENT STREAM", 10, spacing=1.5)], spacing=5),
                    T.button("Clear", self._clear_console, small=True)],
                   alignment=ft.MainAxisAlignment.SPACE_BETWEEN))

        layout = ft.ResponsiveRow([
            ft.Container(registry_panel, col={"xs": 12, "lg": 8}),
            ft.Column([inspector_panel, console_panel], spacing=14, col={"xs": 12, "lg": 4}),
        ], vertical_alignment=ft.CrossAxisAlignment.START, spacing=16, run_spacing=16)

        footer = ft.Container(
            ft.Row([T.label("AETHERYON SYSTEMS · SERVICEDECK", 10, spacing=1),
                    T.label("LOCAL INSTANCE · LIVE ENGINE", 10, spacing=1)],
                   alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            padding=ft.Padding.symmetric(vertical=20, horizontal=27), border=ft.Border.only(top=ft.BorderSide(1, T.LINE)))

        self.refresh()
        self.log("ServiceDeck initialized.", "info")
        self.log("Registry loaded.", "ok")
        return ft.Column([header, ft.Container(ft.Column([toolbar, stats, layout], spacing=24),
                                               padding=26, expand=True), footer],
                         scroll=ft.ScrollMode.AUTO, spacing=0, expand=True)

    # ---------------------------------------------------------------- render
    def refresh(self) -> None:
        with self._lock:
            services = self.registry.list()
            statuses = {s.id: self.engine.status(s.id) for s in services}
            self.list_col.controls = [self._row(s, statuses[s.id]) for s in services if s.visible] or \
                [ft.Container(T.label("Sin servicios. Usá “＋ Add Service”.", 13), padding=30, alignment=ft.Alignment.CENTER)]
            count = lambda st: sum(1 for x in statuses.values() if x.state == st)
            self.stat["total"].value = str(len(services))
            self.stat["running"].value = str(count(ServiceState.RUNNING))
            self.stat["stopped"].value = str(count(ServiceState.STOPPED))
            self.stat["errors"].value = str(count(ServiceState.ERROR))
            self.registry_count.value = f"{len(services)} SERVICES"
            if self.selected and not self.registry.get(self.selected):
                self.selected = None
            self._render_inspector()
            try:
                self.page.update()
            except Exception:
                pass

    def _row(self, cfg, st) -> ft.Control:
        busy = st.state in (ServiceState.STARTING, ServiceState.STOPPING)
        active = st.state in ACTIVE_STATES
        color = T.STATE_COLOR[st.state.value]
        sw = ft.Switch(value=st.state in (ServiceState.RUNNING, ServiceState.STARTING), disabled=busy,
                       active_color=T.GREEN, inactive_track_color="#344258", inactive_thumb_color="#ffffff",
                       tooltip=f"Toggle {cfg.name} [{cfg.alias}]", key=f"toggle_{cfg.alias}",
                       on_change=lambda e, i=cfg.id: self._toggle(i, e.control))
        meta = ft.Row([
            T.label(f"PID: {st.pid or '—'}"), T.label(f"MODE: {cfg.mode}"),
            ft.Row([ft.Container(width=7, height=7, border_radius=4, bgcolor=color),
                    ft.Text(st.state.value, size=10, weight=ft.FontWeight.W_700, color=color)], spacing=6)],
            spacing=12, wrap=True)
        main = ft.Column([
            ft.Row([ft.Text(cfg.name, size=14, weight=ft.FontWeight.W_700, color=T.TEXT),
                    ft.Container(ft.Text(cfg.alias, size=10, color=T.CYAN, font_family=T.MONO),
                                 bgcolor="#102735", border_radius=4, padding=ft.Padding.symmetric(vertical=4, horizontal=6))],
                   spacing=9),
            T.label(f"{cfg.executable} {cfg.args}".strip(), 11, mono=True), meta], spacing=7, expand=True)
        actions = ft.Row([sw, T.icon_button("↻", "Restart", lambda _, i=cfg.id: self._restart(i), disabled=busy),
                          T.icon_button("⚙", "Configure", lambda _, i=cfg.id: self.configurator.open(i))], spacing=9)
        sel = self.selected == cfg.id
        return ft.Container(ft.Row([main, actions], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                            padding=14, bgcolor=T.ROW_BG, border_radius=8,
                            border=ft.Border.all(1, T.CYAN if sel else T.LINE),
                            on_click=lambda _, i=cfg.id: self._select(i))

    def _render_inspector(self) -> None:
        cfg = self.registry.get(self.selected) if self.selected else None
        if not cfg:
            self.inspector.content = ft.Container(T.label("Select a service to inspect its configuration.", 13),
                                                  padding=30, alignment=ft.Alignment.CENTER)
            return
        st = self.engine.status(cfg.id)
        active = st.state in ACTIVE_STATES

        def row(k, v, color=T.TEXT):
            return ft.Container(ft.Row([T.label(k, 11), ft.Text(str(v), size=11, color=color, text_align=ft.TextAlign.RIGHT,
                                                                  expand=True, selectable=True)], spacing=10),
                                padding=ft.Padding.symmetric(vertical=9), border=ft.Border.only(bottom=ft.BorderSide(1, T.LINE)))
        uptime = f"{int(time.time() - st.started_at)}s" if st.started_at and st.state == ServiceState.RUNNING else "—"
        self.inspector.content = ft.Column([
            ft.Row([ft.Text(cfg.name, size=14, weight=ft.FontWeight.W_700, color=T.TEXT),
                    ft.Container(ft.Text(cfg.alias, size=10, color=T.CYAN, font_family=T.MONO), bgcolor="#102735",
                                 border_radius=4, padding=ft.Padding.symmetric(vertical=4, horizontal=6))], spacing=9),
            row("Registry ID", cfg.id), row("State", st.state.value, T.STATE_COLOR[st.state.value]),
            row("PID", st.pid or "—"), row("Uptime", uptime), row("Exit code", "—" if st.exit_code is None else st.exit_code),
            row("Executable", cfg.executable), row("Arguments", cfg.args or "—"), row("Working dir", cfg.cwd),
            row("Auto Restart", "ON" if cfg.autorestart else "OFF"),
            *([row("Last message", st.message)] if st.message else []),
            ft.Row([T.button("Edit", lambda _: self.configurator.open(cfg.id), small=True, disabled=active),
                    T.button("Logs", lambda _: self._show_logs(cfg.id), small=True),
                    T.button("Delete", lambda _: self.configurator.confirm_delete(cfg.id), "danger", small=True, disabled=active)],
                   spacing=7)], spacing=6)

    def _select(self, sid: str) -> None:
        self.selected = sid
        self.refresh()

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

    # ---------------------------------------------------------------- logs
    def _show_logs(self, sid: str) -> None:
        cfg = self.registry.get(sid)
        view = ft.Text(selectable=True, size=11, font_family=T.MONO, color=T.TEXT)

        def load(_=None):
            view.value = self.engine.read_log(sid)
            try:
                self.page.update()
            except Exception:
                pass
        load()
        self.page.show_dialog(ft.AlertDialog(
            bgcolor=T.MODAL_BG, title=ft.Text(f"Logs · {cfg.alias if cfg else sid}", size=14),
            content=ft.Container(ft.Column([view], scroll=ft.ScrollMode.AUTO), width=760, height=420,
                                 bgcolor=T.LOG_BG, padding=12, border_radius=6),
            actions=[T.button("Refresh", load), T.button("Close", lambda _: self.page.pop_dialog())]))

    def log(self, message: str, level: str = "info") -> None:
        color = {"ok": T.GREEN, "info": T.CYAN, "warn": T.AMBER, "error": T.RED}.get(level, T.MUTED)
        self.console.controls.append(ft.Text(f"[{time.strftime('%H:%M:%S')}] {message}", size=11,
                                             color=color, font_family=T.MONO, selectable=True))
        del self.console.controls[:-200]
        try:
            self.console.update()
        except Exception:
            pass

    def _clear_console(self, _=None) -> None:
        self.console.controls.clear()
        self.console.update()

    # ---------------------------------------------------------------- engine → UI
    def _on_event(self, ev: EngineEvent) -> None:
        self.log(ev.message if ev.kind == "log" else f"{self._alias(ev.service_id)}: {ev.message}", ev.level)
        if ev.kind == "state":
            self.refresh()

    def _alias(self, sid) -> str:
        cfg = self.registry.get(sid) if sid else None
        return cfg.alias if cfg else str(sid)

    async def on_window_event(self, e: ft.WindowEvent) -> None:
        if e.type == ft.WindowEventType.CLOSE:
            self.log("Closing: stopping managed services…", "warn")
            await asyncio.to_thread(self.engine.shutdown)
            await self.page.window.destroy()
