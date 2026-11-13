"""ServiceRegistry: identidades, alias y persistencia en services.json."""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import unicodedata
import uuid
from typing import Callable, List, Optional

from interfaces import ServiceConfig, parse_env

ALIAS_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
SCHEMA = 1


class RegistryError(ValueError):
    """Datos inválidos (lista de problemas en .problems)."""

    def __init__(self, problems: List[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


class ServiceLockedError(RuntimeError):
    """El servicio está en ejecución: su configuración no puede cambiar."""


def slugify(text: str) -> str:
    s = unicodedata.normalize("NFD", text or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").lower().strip()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    if not s or not s[0].isalpha():
        s = "svc_" + s if s else "service"
    return s


class ServiceRegistry:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._items: List[ServiceConfig] = []
        self._is_locked: Callable[[str], bool] = lambda _id: False
        self.load()

    # ---- guard (lo inyecta main; el registro no conoce al engine)
    def set_lock_guard(self, fn: Callable[[str], bool]) -> None:
        self._is_locked = fn

    # ---- persistencia
    def load(self) -> None:
        with self._lock:
            self._items = []
            if not os.path.exists(self.path):
                return
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                # conservar el archivo dañado, no perderlo
                bad = self.path + ".corrupt"
                try:
                    os.replace(self.path, bad)
                except OSError:
                    pass
                raise RegistryError([f"services.json ilegible ({e}); copia en {bad}"])
            for d in raw.get("services", []):
                try:
                    self._items.append(ServiceConfig.from_dict(d))
                except TypeError:
                    continue

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        payload = {"schema": SCHEMA, "services": [s.to_dict() for s in self._items]}
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)      # escritura atómica
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---- consultas
    def list(self) -> List[ServiceConfig]:
        with self._lock:
            return [ServiceConfig.from_dict(s.to_dict()) for s in self._items]

    def get(self, service_id: str) -> Optional[ServiceConfig]:
        with self._lock:
            for s in self._items:
                if s.id == service_id:
                    return ServiceConfig.from_dict(s.to_dict())
        return None

    def get_by_alias(self, alias: str) -> Optional[ServiceConfig]:
        with self._lock:
            for s in self._items:
                if s.alias == alias:
                    return ServiceConfig.from_dict(s.to_dict())
        return None

    # ---- alias
    def unique_alias(self, base: str, exclude_id: Optional[str] = None) -> str:
        base = slugify(base)
        with self._lock:
            taken = {s.alias for s in self._items if s.id != exclude_id}
        value, n = base, 2
        while value in taken:
            value = f"{base}_{n}"
            n += 1
        return value

    # ---- validación
    def validate(self, data: dict, editing_id: Optional[str] = None) -> List[str]:
        p: List[str] = []
        name = (data.get("name") or "").strip()
        alias = (data.get("alias") or "").strip()
        exe = (data.get("executable") or "").strip()
        cwd = (data.get("cwd") or "").strip()
        if not name:
            p.append("El nombre es obligatorio.")
        if not alias:
            p.append("El alias es obligatorio.")
        elif not ALIAS_RE.match(alias):
            p.append("Alias inválido: minúsculas, números, _ o -, y debe empezar con letra.")
        else:
            with self._lock:
                if any(s.alias == alias and s.id != editing_id for s in self._items):
                    p.append(f"El alias '{alias}' ya está asignado a otro servicio.")
        if not exe:
            p.append("El ejecutable es obligatorio.")
        if not cwd:
            p.append("El directorio de trabajo es obligatorio.")
        elif not os.path.isdir(cwd):
            p.append(f"El directorio de trabajo no existe: {cwd}")
        if data.get("mode", "background") not in ("background", "console"):
            p.append("Modo de ejecución inválido.")
        try:
            parse_env(data.get("env", ""))
        except ValueError as e:
            p.append(str(e))
        return p

    # ---- altas / cambios / bajas
    @staticmethod
    def _clean(data: dict) -> dict:
        return {
            "name": (data.get("name") or "").strip(),
            "alias": (data.get("alias") or "").strip(),
            "executable": (data.get("executable") or "").strip(),
            "args": (data.get("args") or "").strip(),
            "cwd": (data.get("cwd") or "").strip(),
            "env": (data.get("env") or "").strip(),
            "mode": data.get("mode", "background"),
            "autorestart": bool(data.get("autorestart", False)),
            "visible": bool(data.get("visible", True)),
        }

    def create(self, data: dict) -> ServiceConfig:
        with self._lock:
            problems = self.validate(data)
            if problems:
                raise RegistryError(problems)
            cfg = ServiceConfig(id="svc_" + uuid.uuid4().hex[:12], **self._clean(data))
            self._items.append(cfg)
            self._save()
            return ServiceConfig.from_dict(cfg.to_dict())

    def update(self, service_id: str, data: dict) -> ServiceConfig:
        with self._lock:
            if self._is_locked(service_id):
                raise ServiceLockedError("Detené el proceso antes de editar su configuración.")
            idx = next((i for i, s in enumerate(self._items) if s.id == service_id), None)
            if idx is None:
                raise RegistryError(["Servicio inexistente."])
            problems = self.validate(data, editing_id=service_id)
            if problems:
                raise RegistryError(problems)
            # el id es inmutable: se ignora cualquier 'id' en data
            cfg = ServiceConfig(id=service_id, **self._clean(data))
            self._items[idx] = cfg
            self._save()
            return ServiceConfig.from_dict(cfg.to_dict())

    def delete(self, service_id: str) -> None:
        with self._lock:
            if self._is_locked(service_id):
                raise ServiceLockedError("Detené el proceso antes de eliminarlo.")
            n = len(self._items)
            self._items = [s for s in self._items if s.id != service_id]
            if len(self._items) != n:
                self._save()
