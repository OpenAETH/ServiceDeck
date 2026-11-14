"""Contratos compartidos entre módulos de ServiceDeck.

Los módulos se comunican únicamente a través de estos tipos y Protocols.
"""
from __future__ import annotations

import enum
import os
import shlex
from dataclasses import dataclass, asdict, fields
from typing import Callable, Dict, List, Optional, Protocol, Tuple


class ServiceState(str, enum.Enum):
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


ACTIVE_STATES = (ServiceState.STARTING, ServiceState.RUNNING, ServiceState.STOPPING)


@dataclass
class ServiceConfig:
    id: str                      # inmutable, interno
    name: str                    # nombre visible (editable)
    alias: str                   # vínculo con el toggle (único)
    executable: str = "python"
    args: str = ""
    cwd: str = ""
    env: str = ""                # KEY=VALUE por línea
    mode: str = "background"     # background | console
    shell: str = "none"          # none | cmd | powershell | bash  (lanzar a través de la terminal)
    autorestart: bool = False
    visible: bool = True
    autostart: bool = False      # iniciar al abrir ServiceDeck

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ServiceConfig":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


@dataclass
class ServiceStatus:
    service_id: str
    state: ServiceState = ServiceState.STOPPED
    pid: Optional[int] = None
    started_at: Optional[float] = None
    exit_code: Optional[int] = None
    message: str = ""
    url: Optional[str] = None    # primera URL http(s) detectada en la salida


@dataclass(frozen=True)
class EngineEvent:
    kind: str                    # "state" | "log" | "output" (línea de stdout/stderr del servicio)
    service_id: Optional[str]
    message: str
    level: str = "info"          # info | ok | warn | error
    status: Optional[ServiceStatus] = None
    stream: str = ""             # "out" | "err" (solo kind="output")


class RegistryAPI(Protocol):
    def list(self) -> List[ServiceConfig]: ...
    def get(self, service_id: str) -> Optional[ServiceConfig]: ...
    def create(self, data: dict) -> ServiceConfig: ...
    def update(self, service_id: str, data: dict) -> ServiceConfig: ...
    def delete(self, service_id: str) -> None: ...
    def validate(self, data: dict, editing_id: Optional[str] = None) -> List[str]: ...
    def unique_alias(self, base: str, exclude_id: Optional[str] = None) -> str: ...


class EngineAPI(Protocol):
    def add_listener(self, cb: Callable[[EngineEvent], None]) -> None: ...
    def start(self, cfg: ServiceConfig) -> ServiceStatus: ...
    def stop(self, service_id: str) -> ServiceStatus: ...
    def restart(self, cfg: ServiceConfig) -> ServiceStatus: ...
    def stop_all(self) -> None: ...
    def status(self, service_id: str) -> ServiceStatus: ...
    def is_active(self, service_id: str) -> bool: ...
    def read_log(self, service_id: str, stream: str = "both", tail: int = 300) -> str: ...
    def read_log_lines(self, service_id: str, tail: int = 500) -> List[Tuple[str, str]]: ...
    def shutdown(self) -> None: ...


# ---------------------------------------------------------------- helpers
def parse_env(text: str) -> Dict[str, str]:
    """KEY=VALUE por línea. Ignora vacías y comentarios (#). Lanza ValueError."""
    out: Dict[str, str] = {}
    for n, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"Línea {n} de variables de entorno sin '=': {line!r}")
        k, v = line.split("=", 1)
        k = k.strip()
        if not k or any(c.isspace() for c in k):
            raise ValueError(f"Línea {n}: nombre de variable inválido: {k!r}")
        out[k] = v.strip()
    return out


def split_args(text: str) -> List[str]:
    """Separa argumentos respetando comillas; seguro con rutas Windows."""
    text = (text or "").strip()
    if not text:
        return []
    if os.name == "nt":
        parts = shlex.split(text, posix=False)
        return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts]
    return shlex.split(text)
