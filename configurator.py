"""Configurator: alta/edición/baja de servicios (modal). No ejecuta procesos."""
from __future__ import annotations

from typing import Callable, Optional

import flet as ft

import theme as T
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
        exe = ft.TextField(label="Executable", value=cfg.executable if cfg else "python",
                           expand=True, **T.field_style())
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
                        cwd=cwd.value, env=env.value, mode=mode.value,
                        autorestart=autorestart.value == "true", visible=visible.value == "true")

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

        def sec(t):
            return ft.Container(T.label(t, 10, T.CYAN, ft.FontWeight.W_700, spacing=1.4),
                                padding=ft.Padding.only(bottom=7),
                                border=ft.Border.only(bottom=ft.BorderSide(1, T.LINE)))

        banner = ft.Container(
            T.label("Servicio en ejecución: detenelo para editar su configuración.", 11, T.AMBER),
            padding=10, bgcolor="#2a2312", border_radius=6, visible=locked)
        for c in (name, alias, exe, mode, args, cwd, env, autorestart, visible):
            c.disabled = locked

        body = ft.Column([
            banner,
            sec("01 / IDENTITY"), name,
            ft.Row([alias, T.button("Generate", generate, disabled=locked)], vertical_alignment=ft.CrossAxisAlignment.START),
            T.label("Identificador único. Vincula la configuración con su toggle del dashboard.", 10),
            sec("02 / EXECUTION"), ft.Row([exe, mode]), args, cwd, env,
            sec("03 / BEHAVIOR"), ft.Row([autorestart, visible]),
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
