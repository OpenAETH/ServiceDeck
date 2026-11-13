"""Paleta y helpers visuales del mockup aprobado."""
import flet as ft

BG, PANEL, PANEL2 = "#090e17", "#111a28", "#162235"
LINE, TEXT, MUTED = "#26364c", "#e8eff9", "#8192aa"
CYAN, GREEN, RED, AMBER = "#36c9df", "#32ce91", "#f16c7b", "#eab96a"
ROW_BG, MODAL_BG, INPUT_BG, LOG_BG = "#101a28", "#111b2a", "#09121e", "#080e16"
MONO = "Consolas"

STATE_COLOR = {"RUNNING": GREEN, "STARTING": AMBER, "STOPPING": AMBER,
               "STOPPED": MUTED, "ERROR": RED}


def label(text, size=10, color=MUTED, weight=None, mono=False, spacing=None, selectable=False):
    return ft.Text(text, size=size, color=color, weight=weight, selectable=selectable,
                   font_family=MONO if mono else None, style=ft.TextStyle(letter_spacing=spacing) if spacing else None)


def button(text, on_click=None, kind="normal", small=False, disabled=False):
    fg = {"primary": "#06141c", "danger": RED}.get(kind, TEXT)
    bg = {"primary": CYAN}.get(kind, PANEL2)
    bd = CYAN if kind == "primary" else LINE
    return ft.Container(
        content=ft.Text(text, size=11 if small else 13, weight=ft.FontWeight.W_600, color=fg),
        padding=ft.Padding.symmetric(vertical=6 if small else 9, horizontal=9 if small else 13),
        bgcolor=bg, border=ft.Border.all(1, bd), border_radius=6,
        on_click=None if disabled else on_click, ink=True, opacity=0.4 if disabled else 1,
    )


def icon_button(glyph, tooltip, on_click, disabled=False):
    return ft.Container(
        content=ft.Text(glyph, size=14, color=TEXT), width=30, height=30,
        alignment=ft.Alignment.CENTER, bgcolor=PANEL2, border=ft.Border.all(1, LINE),
        border_radius=5, tooltip=tooltip, on_click=None if disabled else on_click,
        ink=True, opacity=0.35 if disabled else 1,
    )


def panel(content, header=None):
    kids = []
    if header is not None:
        kids.append(ft.Container(header, padding=ft.Padding.symmetric(vertical=15, horizontal=17),
                                 border=ft.Border.only(bottom=ft.BorderSide(1, LINE))))
    kids.append(content)
    return ft.Container(ft.Column(kids, spacing=0), bgcolor=PANEL,
                        border=ft.Border.all(1, LINE), border_radius=9)


def field_style(**kw):
    base = dict(bgcolor=INPUT_BG, border_color=LINE, focused_border_color=CYAN, color=TEXT,
                border_radius=6, dense=True, text_size=13, content_padding=ft.Padding.all(10),
                label_style=ft.TextStyle(size=11, color="#aab8cb", weight=ft.FontWeight.W_600))
    base.update(kw)
    return base
