"""Configurator: alta/edición/baja de servicios (modal). No ejecuta procesos."""
from __future__ import annotations

from typing import Callable, Optional

import threading

import flet as ft

import theme as T
import interpreters
from interfaces import EngineAPI, RegistryAPI, ServiceConfig
from serviceregistry import RegistryError, ServiceLockedError, slugify


class Configurator:
    def __init__(self, page: ft.Page, registry: RegistryAPI, engine: EngineAPI,
                 on_change: Callable[[Optional[str]], None]):
        self.page, self.registry, self.engine, self.on_change = page, registry, engine, on_change

    # ------------------------------------------------------------------ modal
    def open(self, service_id: Optional[str] = None) -> None:
        cfg = self.registry.get(service_id) if service_id else None
        locked = bool(cfg and self.engine.is_active(cfg.id))
        manual_alias = {"v": bool(cfg)}

        name = ft.TextField(label="Service Name", value=cfg.name if cfg else "",
                            hint_text="e.g. PromptForge", **T.field_style())
        alias = ft.TextField(label="UI Alias / Binding ID", value=cfg.alias if cfg else "",
                             hint_text="promptforge", expand=True, **T.field_style())
        exe = ft.TextField(label="Executable (ruta completa recomendada)", value=cfg.executable if cfg else "python",
                           expand=True, **T.field_style())
        shell = ft.Dropdown(label="Launch via", value=(cfg.shell if cfg else "none"), expand=True,
                            options=[ft.DropdownOption("none", "Direct (Popen)"),
                                     ft.DropdownOption("cmd", "cmd.exe (terminal)"),
                                     ft.DropdownOption("powershell", "PowerShell (terminal)"),
                                     ft.DropdownOption("bash", "Bash / Git Bash (login)")],
                            **T.field_style(dense=False))
        interp_box = ft.Column(spacing=4, visible=False)
        mode = ft.Dropdown(label="Startup mode", value=cfg.mode if cfg else "background", expand=True,
                           options=[ft.DropdownOption("background", "Background process"),
                                    ft.DropdownOption("console", "Visible console")],
                           **T.field_style(dense=False))
        args = ft.TextField(label="Arguments", value=cfg.args if cfg else "", multiline=True, min_lines=2,
                            max_lines=4, hint_text="promptforge.py --web",
                            helper="Se almacenan separados del ejecutable.",
                            text_style=ft.TextStyle(font_family=T.MONO, size=11), **T.field_style())
        cwd = ft.TextField(label="Working Directory", value=cfg.cwd if cfg else "",
                           hint_text="D:/OpenAETH-GitHub/PromptForge", **T.field_style())
        env = ft.TextField(label="Environment Variables", value=cfg.env if cfg else "", multiline=True,
                           min_lines=2, max_lines=5, hint_text="PORT=8008\nDEBUG=false",
                           helper="Una KEY=VALUE por línea. Evitá guardar secretos en texto plano.",
                           text_style=ft.TextStyle(font_family=T.MONO, size=11), **T.field_style())
        autorestart = ft.Dropdown(label="Auto Restart", value="true" if cfg and cfg.autorestart else "false",
                                  expand=True, options=[ft.DropdownOption("false", "Disabled"),
                                                        ft.DropdownOption("true", "Enabled")],
                                  **T.field_style(dense=False))
        autostart = ft.Dropdown(label="Auto Start", value="true" if cfg and cfg.autostart else "false", expand=True,
                                options=[ft.DropdownOption("false", "Manual"),
                                         ft.DropdownOption("true", "Al abrir ServiceDeck")],
                                **T.field_style(dense=False))
        visible = ft.Dropdown(label="UI Visibility", value="false" if cfg and not cfg.visible else "true",
                              expand=True, options=[ft.DropdownOption("true", "Visible in dashboard"),
                                                    ft.DropdownOption("false", "Hidden")],
                              **T.field_style(dense=False))
        err = ft.Text("", color=T.RED, size=11, visible=False)
        p_alias = ft.Text(cfg.alias if cfg else "service_alias", color=T.CYAN, size=11)
        p_id = ft.Text(cfg.id if cfg else "(se asigna al guardar)", color=T.TEXT, size=11, font_family=T.MONO)

        def sync_preview():
            p_alias.value = alias.value or "service_alias"
            self.page.update()

        def on_name(_):
            if not manual_alias["v"]:
                alias.value = self.registry.unique_alias(name.value, cfg.id if cfg else None) if name.value.strip() else ""
            sync_preview()

        def on_alias(_):
            manual_alias["v"] = True
            sync_preview()

        def generate(_):
            alias.value = self.registry.unique_alias(name.value or "service", cfg.id if cfg else None)
            manual_alias["v"] = False
            sync_preview()

        name.on_change, alias.on_change = on_name, on_alias

        def form():
            return dict(name=name.value, alias=alias.value, executable=exe.value, args=args.value,
                        cwd=cwd.value, env=env.value, mode=mode.value, shell=shell.value or "none",
                        autorestart=autorestart.value == "true", visible=visible.value == "true",
                        autostart=autostart.value == "true")

        def show_err(lines):
            err.value = "\n".join("• " + l for l in lines)
            err.visible = bool(lines)
            self.page.update()

        def save(_):
            data = form()
            try:
                if cfg:
                    saved = self.registry.update(cfg.id, data)
                else:
                    saved = self.registry.create(data)
            except RegistryError as e:
                return show_err(e.problems)
            except ServiceLockedError as e:
                return show_err([str(e)])
            except OSError as e:
                return show_err([f"No se pudo escribir services.json: {e}"])
            self.page.pop_dialog()
            self.on_change(saved.id)

        def pick(path):
            exe.value = path
            interp_box.visible = False
            self.page.update()

        def render(items, header=None):
            rows = [T.label(header, 10, T.MUTED)] if header else []
            for it in items:
                good = it.ok and not it.missing
                tag = "✓ imports OK" if good else (f"✗ falta: {', '.join(it.missing)}" if it.ok else f"✗ {it.error}")
                col = T.GREEN if good else (T.AMBER if it.ok else T.RED)
                rows.append(ft.Container(ft.Row([
                    ft.Text(tag, size=10, color=col, width=150, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                    ft.Text(f"Py {it.version or '?'} · {it.source}", size=10, color=T.MUTED, width=170, max_lines=1,
                            overflow=ft.TextOverflow.ELLIPSIS),
                    ft.Text(it.path, size=10, color=T.TEXT, font_family=T.MONO, expand=True, max_lines=1,
                            overflow=ft.TextOverflow.ELLIPSIS, tooltip=it.path)], spacing=8),
                    on_click=(lambda _, p=it.path: pick(p)) if it.ok else None, ink=True, border_radius=5,
                    padding=ft.Padding.symmetric(vertical=4, horizontal=6), bgcolor=T.INPUT_BG,
                    tooltip="Click para usar este intérprete"))
            interp_box.controls = rows or [T.label("No se encontraron intérpretes.", 11, T.AMBER)]
            interp_box.visible = True
            self.page.update()

        def detect(_):
            interp_box.controls = [T.label("Buscando intérpretes…", 11, T.CYAN)]
            interp_box.visible = True
            self.page.update()
            wd, ar = cwd.value.strip(), args.value

            def work():
                try:
                    items = interpreters.discover(wd, ar)
                except Exception as e:           # nunca romper la UI por una detección
                    interp_box.controls = [T.label(f"Error de detección: {e}", 11, T.RED)]
                    self.page.update()
                    return
                mods = interpreters.script_imports(wd, ar)
                render(items, "Imports del script: " + (", ".join(mods) or "—") + "   (click en una fila para usarla)")
            threading.Thread(target=work, daemon=True).start()

        def test(_):
            wd = cwd.value.strip()
            path = interpreters.resolve_exe(exe.value, wd)
            if not path:
                interp_box.controls = [T.label(f"No se encuentra '{exe.value}' en el PATH de ServiceDeck.", 11, T.RED)]
                interp_box.visible = True
                return self.page.update()
            interp_box.controls = [T.label("Probando…", 11, T.CYAN)]
            interp_box.visible = True
            self.page.update()
            mods = interpreters.script_imports(wd, args.value)
            threading.Thread(target=lambda: render([interpreters.probe(path, mods, wd, "este Executable")],
                                                   "ServiceDeck ejecutaría:"), daemon=True).start()

        def sec(t):
            return ft.Container(T.label(t, 10, T.CYAN, ft.FontWeight.W_700, spacing=1.4),
                                padding=ft.Padding.only(bottom=7),
                                border=ft.Border.only(bottom=ft.BorderSide(1, T.LINE)))

        banner = ft.Container(
            T.label("Servicio en ejecución: detenelo para editar su configuración.", 11, T.AMBER),
            padding=10, bgcolor="#2a2312", border_radius=6, visible=locked)
        for c in (name, alias, exe, mode, shell, args, cwd, env, autorestart, autostart, visible):
            c.disabled = locked

        body = ft.Column([
            banner,
            sec("01 / IDENTITY"), name,
            ft.Row([alias, T.button("Generate", generate, disabled=locked)], vertical_alignment=ft.CrossAxisAlignment.START),
            T.label("Identificador único. Vincula la configuración con su toggle del dashboard.", 10),
            sec("02 / EXECUTION"),
            ft.Row([exe, T.button("Detect", detect, disabled=locked), T.button("Test", test, disabled=locked)],
                   vertical_alignment=ft.CrossAxisAlignment.START),
            interp_box, ft.Row([mode, shell]), args, cwd, env,
            sec("03 / BEHAVIOR"), ft.Row([autorestart, autostart, visible]),
            ft.Container(ft.Column([
                ft.Text("Binding preview", size=11, weight=ft.FontWeight.W_700, color="#a8c9d4"),
                ft.Row([T.label("Toggle ID:", 11, "#a8c9d4"), p_alias], spacing=6),
                ft.Row([T.label("Registry key:", 11, "#a8c9d4"), p_id], spacing=6),
                ft.Row([T.label("Process engine:", 11, "#a8c9d4"),
                        ft.Text("Popen / Managed Process", color=T.GREEN, size=11)], spacing=6),
            ], spacing=3), bgcolor="#102432", border=ft.Border.all(1, "#214454"), border_radius=6, padding=11),
            err,
        ], spacing=14, scroll=ft.ScrollMode.AUTO, width=620, height=min(560, max(320, int(self.page.height or 800) - 200)))

        dlg = ft.AlertDialog(
            modal=True, bgcolor=T.MODAL_BG, shape=ft.RoundedRectangleBorder(radius=12),
            title=ft.Column([ft.Text("Edit Service" if cfg else "New Service", size=14, weight=ft.FontWeight.W_700),
                             T.label("SERVICE DEFINITION & UI BINDING", 10, spacing=1.5)], spacing=4),
            content=body,
            actions=[T.button("Cancel", lambda _: self.page.pop_dialog()),
                     T.button("Save Configuration ↗", save, "primary", disabled=locked)],
            actions_alignment=ft.MainAxisAlignment.END)
        self.page.show_dialog(dlg)

    # ------------------------------------------------------------------ baja
    def confirm_delete(self, service_id: str) -> None:
        cfg = self.registry.get(service_id)
        if not cfg:
            return
        if self.engine.is_active(service_id):
            return self._notice("Detené el proceso antes de eliminarlo.")

        def do(_):
            try:
                self.registry.delete(service_id)
            except ServiceLockedError as e:
                self.page.pop_dialog()
                return self._notice(str(e))
            self.page.pop_dialog()
            self.on_change(None)

        self.page.show_dialog(ft.AlertDialog(
            modal=True, bgcolor=T.MODAL_BG,
            title=ft.Text(f"Delete {cfg.name}?", size=14),
            content=T.label("Se elimina solo la configuración. Los logs quedan en disco.", 12),
            actions=[T.button("Cancel", lambda _: self.page.pop_dialog()),
                     T.button("Delete", do, "danger")]))

    def _notice(self, msg: str) -> None:
        self.page.show_dialog(ft.AlertDialog(
            bgcolor=T.MODAL_BG, content=T.label(msg, 12, T.TEXT),
            actions=[T.button("OK", lambda _: self.page.pop_dialog())]))
