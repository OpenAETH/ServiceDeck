"""Detección y prueba de intérpretes Python (utilidad del Configurator).

Resuelve el problema típico: el `python` de tu terminal (venv, conda, pyenv, otra
instalación) no es el mismo que encuentra ServiceDeck. Lista los candidatos,
indica versión y qué imports del script les faltan.
"""
from __future__ import annotations

import ast
import glob
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import List, Tuple

from interfaces import split_args

IS_WIN = os.name == "nt"
_NOWIN = subprocess.CREATE_NO_WINDOW if IS_WIN else 0
_PROBE = ("import sys,importlib.util as u;m=sys.argv[1:];"
          "print(sys.version.split()[0]);print(sys.prefix);"
          "print(','.join(x for x in m if u.find_spec(x) is None))")


@dataclass
class Interp:
    path: str
    source: str = ""
    version: str = ""
    prefix: str = ""
    missing: List[str] = field(default_factory=list)
    ok: bool = False
    error: str = ""


def script_imports(cwd: str, args: str) -> List[str]:
    """Módulos de terceros que importa el script (.py) indicado en los argumentos."""
    script = next((t for t in split_args(args) if t.lower().endswith(".py")), None)
    if not script:
        return []
    path = script if os.path.isabs(script) else os.path.join(cwd or ".", script)
    try:
        tree = ast.parse(open(path, encoding="utf-8", errors="replace").read())
    except (OSError, SyntaxError, ValueError):
        return []
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            names.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            names.add(n.module.split(".")[0])
    std = getattr(sys, "stdlib_module_names", set())
    base = os.path.dirname(path)
    local = {x for x in names if os.path.exists(os.path.join(base, x + ".py")) or os.path.isdir(os.path.join(base, x))}
    return sorted(names - set(std) - local)


def _candidates(cwd: str) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []

    def add(p, src):
        if p and os.path.isfile(p):
            out.append((os.path.abspath(p), src))

    names = ("python.exe", "python3.exe") if IS_WIN else ("python3", "python")
    for d in os.environ.get("PATH", "").split(os.pathsep):
        for n in names:
            add(os.path.join(d.strip('"'), n), "PATH")
    for sub in (".venv", "venv", "env", ".env"):
        add(os.path.join(cwd or ".", sub, "Scripts" if IS_WIN else "bin", "python.exe" if IS_WIN else "python"),
            "venv del proyecto")
    if not getattr(sys, "frozen", False):
        add(sys.executable, "Python de ServiceDeck")
    if IS_WIN:
        try:
            r = subprocess.run(["py", "-0p"], capture_output=True, text=True, timeout=5, creationflags=_NOWIN)
            for line in r.stdout.splitlines():
                m = re.search(r"([A-Za-z]:\\.*?python[w]?\.exe)\s*$", line.strip())
                if m:
                    add(m.group(1), "py launcher")
        except (OSError, subprocess.SubprocessError):
            pass
        la = os.environ.get("LOCALAPPDATA", "")
        for p in glob.glob(os.path.join(la, "Programs", "Python", "Python*", "python.exe")):
            add(p, "instalación")
        home = os.path.expanduser("~")
        roots = [os.environ.get("CONDA_PREFIX", "")] + [os.path.join(home, d) for d in
                 ("miniconda3", "anaconda3", "miniforge3", "mambaforge")] + [r"C:\ProgramData\miniconda3", r"C:\ProgramData\anaconda3"]
        for r in filter(None, roots):
            add(os.path.join(r, "python.exe"), "conda")
            for p in glob.glob(os.path.join(r, "envs", "*", "python.exe")):
                add(p, "conda env")
    seen, uniq = set(), []
    for p, src in out:
        key = os.path.normcase(os.path.realpath(p))
        if key in seen:
            continue
        seen.add(key)
        if "windowsapps" in p.lower():
            src += " (alias Microsoft Store)"
        uniq.append((p, src))
    return uniq


def probe(path: str, modules: List[str] = (), cwd: str = "", source: str = "") -> Interp:
    it = Interp(path=path, source=source)
    try:
        r = subprocess.run([path, "-c", _PROBE, *modules], capture_output=True, text=True, timeout=8,
                           cwd=cwd if cwd and os.path.isdir(cwd) else None, creationflags=_NOWIN)
    except (OSError, subprocess.SubprocessError) as e:
        it.error = str(e)
        return it
    lines = r.stdout.splitlines()
    if r.returncode != 0 or len(lines) < 2:
        it.error = (r.stderr.strip().splitlines() or ["no responde como Python"])[-1][:120]
        return it
    it.version, it.prefix = lines[0], lines[1]
    it.missing = [m for m in (lines[2].split(",") if len(lines) > 2 else []) if m]
    it.ok = True
    return it


def discover(cwd: str, args: str) -> List[Interp]:
    mods = script_imports(cwd, args)
    cands = _candidates(cwd)
    with ThreadPoolExecutor(max_workers=8) as ex:
        res = list(ex.map(lambda c: probe(c[0], mods, cwd, c[1]), cands))
    res.sort(key=lambda i: (not i.ok, bool(i.missing), i.path.lower()))
    return res


def resolve_exe(value: str, cwd: str = "") -> str:
    """Lo que ServiceDeck ejecutaría para ese 'Executable' (misma lógica que el motor)."""
    value = (value or "").strip()
    return shutil.which(value) or (os.path.join(cwd, value) if cwd and os.path.isfile(os.path.join(cwd, value)) else "")
