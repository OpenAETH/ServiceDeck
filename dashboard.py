"""Dashboard: interfaz compacta de una sola pantalla (sin scroll de página).

Solo habla con EngineAPI/RegistryAPI/Configurator; nunca lanza comandos.
"""
from __future__ import annotations

import asyncio
import collections
import threading
import time
import webbrowser
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
        self.configurator = Configurator(page, registry, engine, self._configured)

        # --- estado compartido con hilos del motor: SOLO datos; los controles se tocan en _flush_now (hilo de la UI)
        self._plock = threading.RLock()
        self._ev_q: list = []                              # entradas de consola pendientes de dibujar
        self._pending: list = []                           # líneas de salida pendientes de dibujar
        self._toasts: list = []
        self._need_refresh = False
        self._rerender_needed = False
        self._scheduled = False
        self._buf = collections.deque(maxlen=3000)        # (stream, línea) del servicio seleccionado
        self._entries: list = []                           # entradas dibujadas de la consola de eventos
        self._filter = ""
        self._follow = True
        self._level_mode = 0                               # 0 todo · 1 warn+ · 2 error
        self._prev_state: dict = {}
        self._clip = None

        self.list_view = ft.ListView(spacing=6, expand=True, padding=ft.Padding.all(10))
        self.inspector = ft.Container(padding=ft.Padding.symmetric(vertical=8, horizontal=12))
        self.events = ft.ListView(spacing=1, auto_scroll=True, expand=True, padding=ft.Padding.all(8))
        self.logview = ft.ListView(spacing=0, auto_scroll=True, expand=True, padding=ft.Padding.all(8))
        self.filter_tf = ft.TextField(hint_text="filtrar…", width=130, visible=False, on_change=self._on_filter,
                                      **T.field_style(text_size=11, content_padding=ft.Padding.symmetric(vertical=4, horizontal=8)))
        self.follow_btn = T.button("⇣ Follow", self._toggle_follow, "primary", small=True)
        self.follow_btn.visible = False
        self.level_btn = T.button("LEVEL: ALL", self._cycle_level, small=True)
        self.sel_events = ft.SelectionArea(self.events, expand=True)     # selección multilínea con el mouse
        self.sel_log = ft.SelectionArea(self.logview, expand=True)
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
                    T.label("V1.4 · LOCAL PROCESS CONTROL", 9, spacing=1.2)], spacing=7,
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
            ft.Container(ft.Stack([self.sel_events, self.sel_log], expand=True),
                         bgcolor=T.LOG_BG, expand=True),
            ft.Row([self._tabs(), ft.Row([self.filter_tf, self.follow_btn, self.level_btn,
                                          T.button("⧉ Copy", self._copy, small=True),
                                          T.button("Clear", self._clear_console, small=True)],
                                         spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER)],
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
        try:                                          # servicio de portapapeles (Flet 1.0)
            self._clip = ft.Clipboard()
            self.page.services.append(self._clip)
        except Exception:
            self._clip = None
        self._set_tab("events", update=False)
        self._need_refresh = True
        self.log("ServiceDeck initialized.", "info")
        self.log("Registry loaded.", "ok")
        self._flush_now(update=False)
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
        """Pide redibujar lista/estadísticas/inspector (seguro desde cualquier hilo)."""
        with self._plock:
            self._need_refresh = True
        self._kick()

    def _refresh_now(self) -> None:
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
        open_btn = [T.icon_button("↗", f"Abrir {st.url}", lambda _, u=st.url: self._open(u))] if st.url and st.state == ServiceState.RUNNING else []
        actions = ft.Row([*open_btn, sw, T.icon_button("↻", "Restart", lambda _, i=cfg.id: self._restart(i), disabled=busy),
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
            *([wide("URL", st.url, T.CYAN)] if st.url else []),
            *([wide("Msg", st.message, T.RED if st.state == ServiceState.ERROR else T.MUTED)] if st.message else []),
            ft.Row([T.button("Edit", lambda _: self.configurator.open(cfg.id), small=True, disabled=active),
                    T.button("Logs", lambda _: self._set_tab("log"), small=True),
                    *([T.button("Open ↗", lambda _: self._open(st.url), "primary", small=True)] if st.url else []),
                    T.button("Delete", lambda _: self.configurator.confirm_delete(cfg.id), "danger", small=True,
                             disabled=active)], spacing=6)], spacing=5, tight=True)

    def _select(self, sid: str) -> None:
        self.selected = sid
        self._load_buffer(sid)
        self._set_tab("log", update=False)
        self.refresh()

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
        self.sel_events.visible = name == "events"
        self.sel_log.visible = name == "log"
        self.filter_tf.visible = self.follow_btn.visible = name == "log"
        self.level_btn.visible = name == "events"
        self._render_tabs()
        if name == "log":
            self._rerender()
        if update:
            self._safe_page_update()

    # ---- helpers de hilo/UI
    def _safe(self, ctl) -> None:
        try:
            ctl.update()
        except Exception:
            pass

    def _safe_page_update(self) -> None:
        try:
            self.page.update()
        except Exception:
            pass

    def _kick(self) -> None:
        """Agenda UN flush en el loop de la UI (coalesce ~80 ms). Es la única vía por la que
        los hilos del motor llegan a la pantalla: update() desde un hilo suelto no despierta
        al cliente hasta el siguiente click."""
        with self._plock:
            if self._scheduled:
                return
            self._scheduled = True
        try:
            self.page.run_task(self._flush_async)
        except Exception:
            with self._plock:
                self._scheduled = False

    async def _flush_async(self) -> None:
        await asyncio.sleep(0.08)
        self._flush_now()

    def _flush_now(self, update: bool = True) -> None:
        with self._plock:
            self._scheduled = False
            need_ref, self._need_refresh = self._need_refresh, False
            rerender, self._rerender_needed = self._rerender_needed, False
            evs, self._ev_q = self._ev_q, []
            out, self._pending = self._pending, []
            toasts, self._toasts = self._toasts, []
        try:
            if need_ref:
                self._refresh_now()
                self._render_tabs()
            if evs:
                for e in evs:
                    self._add_entry(e)
                del self._entries[:-400]
                self.events.controls = [e["row"] for e in self._entries]
            if self.tab == "log":
                if rerender:
                    self._rerender(update=False)
                elif out:
                    if self.logview.controls and isinstance(self.logview.controls[0], ft.Text) and \
                            self.logview.controls[0].value.startswith(("(sin salida", "Seleccioná")):
                        self.logview.controls.clear()
                    self.logview.controls.extend(self._line_ctl(s, l) for s, l in out if self._match(l))
                    del self.logview.controls[:-1500]
            if update:
                if need_ref or rerender:
                    self._safe_page_update()
                else:
                    if evs:
                        self._safe(self.events)
                    if out and self.tab == "log":
                        self._safe(self.logview)
            for text, color in toasts:
                self._show_toast(text, color)
        except Exception as e:                           # nunca silencioso
            self._ev_q.append(self._mk_entry("error", None, "UI", f"error al dibujar: {e}"))

    def _show_toast(self, text: str, color: str) -> None:
        try:
            self.page.show_dialog(ft.SnackBar(ft.Text(text, color=T.TEXT, size=12), bgcolor=color, duration=4500,
                                              show_close_icon=True))
        except Exception:
            pass

    # ---- consola de eventos (entradas estructuradas, con deduplicado y filtro por nivel)
    GLYPH = {"ok": "✓", "info": "•", "warn": "▲", "error": "✗"}
    COLOR = {"ok": T.GREEN, "info": T.CYAN, "warn": T.AMBER, "error": T.RED}
    RANK = {"info": 0, "ok": 0, "warn": 1, "error": 2}

    def _mk_entry(self, level, sid, alias, text):
        return {"ts": time.strftime("%H:%M:%S"), "level": level, "sid": sid, "alias": alias, "text": text, "count": 1}

    def log(self, message: str, level: str = "info", sid: Optional[str] = None, alias: Optional[str] = None) -> None:
        """Seguro desde cualquier hilo: encola y agenda el dibujo."""
        with self._plock:
            self._ev_q.append(self._mk_entry(level, sid, alias, message))
        self._kick()

    def _add_entry(self, e: dict) -> None:
        last = self._entries[-1] if self._entries else None
        if last and (last["sid"], last["alias"], last["text"], last["level"]) == (e["sid"], e["alias"], e["text"], e["level"]):
            last["count"] += 1
            last["ts"] = e["ts"]
            self._paint(last)
            return
        e["row"] = ft.Container(padding=ft.Padding.symmetric(vertical=1, horizontal=2), border_radius=3)
        self._paint(e)
        self._entries.append(e)

    def _paint(self, e: dict) -> None:
        col = self.COLOR.get(e["level"], T.MUTED)
        cells = [T.label(e["ts"], 10, T.MUTED, mono=True),
                 ft.Text(self.GLYPH.get(e["level"], "•"), size=11, color=col, weight=ft.FontWeight.W_700)]
        if e["alias"]:
            chip = _chip(e["alias"], col if e["level"] in ("warn", "error") else T.CYAN)
            if e["sid"]:
                chip.on_click = lambda _, i=e["sid"]: self._select(i)
                chip.tooltip = "Ver este servicio"
            cells.append(chip)
        cells.append(ft.Text(e["text"], size=11, color=col if e["level"] != "info" else T.TEXT, font_family=T.MONO,
                             expand=True))
        if e["count"] > 1:
            cells.append(ft.Container(ft.Text(f"×{e['count']}", size=10, color=T.BG, weight=ft.FontWeight.W_700),
                                      bgcolor=col, border_radius=8, padding=ft.Padding.symmetric(horizontal=6)))
        e["row"].content = ft.Row(cells, spacing=6, vertical_alignment=ft.CrossAxisAlignment.START)
        e["row"].bgcolor = "#2a1318" if e["level"] == "error" else None
        e["row"].visible = self.RANK.get(e["level"], 0) >= self._level_mode

    def _cycle_level(self, _=None) -> None:
        self._level_mode = (self._level_mode + 1) % 3
        self.level_btn.content.value = ["LEVEL: ALL", "LEVEL: WARN+", "LEVEL: ERROR"][self._level_mode]
        for e in self._entries:
            e["row"].visible = self.RANK.get(e["level"], 0) >= self._level_mode
        self._safe_page_update()

    # ---- salida en vivo del servicio seleccionado
    def _line_ctl(self, stream: str, line: str) -> ft.Control:
        color = T.AMBER if stream == "err" else T.TEXT
        low = line.lower()
        if stream == "err" and any(w in low for w in ("error", "traceback", "exception", "critical")):
            color = T.RED
        return ft.Text(line, size=11, color=color, font_family=T.MONO)     # selección: la da SelectionArea

    def _match(self, line: str) -> bool:
        return not self._filter or self._filter in line.lower()

    def _load_buffer(self, sid: str) -> None:
        with self._plock:
            self._buf.clear()
            self._pending.clear()
            self._buf.extend(self.engine.read_log_lines(sid, 500))
            self._rerender_needed = True

    def _rerender(self, update: bool = True) -> None:
        with self._plock:
            items = [(s, l) for s, l in self._buf if self._match(l)][-1500:]
            self._pending.clear()
        if not self.selected:
            self.logview.controls = [T.label("Seleccioná un servicio para ver su salida en vivo.", 12)]
        else:
            self.logview.controls = [self._line_ctl(s, l) for s, l in items] or [T.label("(sin salida todavía)", 11)]
        self.logview.auto_scroll = self._follow
        if update:
            self._safe(self.logview)

    def _on_filter(self, e) -> None:
        self._filter = (e.control.value or "").strip().lower()
        self._rerender()

    def _toggle_follow(self, _=None) -> None:
        self._follow = not self._follow
        self.follow_btn.bgcolor = T.CYAN if self._follow else T.PANEL2
        self.follow_btn.content.color = "#06141c" if self._follow else T.TEXT
        self.logview.auto_scroll = self._follow
        self._safe_page_update()

    # ---- copiar / limpiar
    def _console_text(self) -> str:
        if self.tab == "events":
            rows = []
            for e in self._entries:
                if self.RANK.get(e["level"], 0) < self._level_mode:
                    continue
                who = f"{e['alias']}: " if e["alias"] else ""
                n = f" (×{e['count']})" if e["count"] > 1 else ""
                rows.append(f"[{e['ts']}] {e['level'].upper():5s} {who}{e['text']}{n}")
            return "\n".join(rows)
        with self._plock:
            return "\n".join(l for _, l in self._buf if self._match(l))

    def _copy(self, _=None) -> None:
        text = self._console_text()
        if not text:
            return self._show_toast("No hay nada para copiar.", T.PANEL2)
        n = text.count("\n") + 1
        self.page.run_task(self._copy_async, text, n)

    async def _copy_async(self, text: str, n: int) -> None:
        try:
            if self._clip is None:
                raise RuntimeError("portapapeles no disponible")
            await self._clip.set(text)
            self._show_toast(f"Copiadas {n} líneas al portapapeles", "#0f3d2e")
        except Exception as e:
            self._show_toast(f"No se pudo copiar: {e}", "#4a1b22")

    def _clear_console(self, _=None) -> None:
        if self.tab == "events":
            with self._plock:
                self._entries.clear()
                self._ev_q.clear()
            self.events.controls = []
            self._safe(self.events)
        else:
            with self._plock:
                self._buf.clear()
                self._pending.clear()
            self._rerender()

    def _open(self, url) -> None:
        if url and url.startswith(("http://", "https://")):
            self.log(f"Abriendo {url}", "info")
            threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()

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

    # ---------------------------------------------------------------- engine → UI (hilos del motor: solo datos)
    def _on_event(self, ev: EngineEvent) -> None:
        if ev.kind == "output":
            if ev.service_id == self.selected:
                with self._plock:
                    self._buf.append((ev.stream, ev.message))
                    self._pending.append((ev.stream, ev.message))
                self._kick()
            return
        sid = ev.service_id
        alias = self._alias(sid) if sid else None
        text = ev.message
        if alias and text.startswith(alias + ": "):
            text = text[len(alias) + 2:]
        with self._plock:
            self._ev_q.append(self._mk_entry(ev.level, sid, alias, text))
        if ev.kind == "state":
            prev = self._prev_state.get(sid)
            now = ev.status.state if ev.status else None
            self._prev_state[sid] = now
            with self._plock:
                self._need_refresh = True
                if sid == self.selected and now == ServiceState.STARTING and prev != ServiceState.STARTING:
                    self._buf.clear()                     # nueva ejecución: vista limpia
                    self._pending.clear()
                    self._rerender_needed = True
                if now == ServiceState.ERROR and prev != ServiceState.ERROR:
                    self._toasts.append((f"{alias}: {text}", "#4a1b22"))
        self._kick()


    def _alias(self, sid) -> str:
        cfg = self.registry.get(sid) if sid else None
        return cfg.alias if cfg else str(sid)

    async def on_window_event(self, e: ft.WindowEvent) -> None:
        if e.type == ft.WindowEventType.CLOSE:
            self.log("Closing: stopping managed services…", "warn")
            await asyncio.to_thread(self.engine.shutdown)
            await self.page.window.destroy()
