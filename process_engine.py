"""ProcessEngine: ciclo de vida real de procesos. Sin dependencias de UI."""
from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from typing import Callable, Dict, List, Optional

import psutil

from interfaces import (ACTIVE_STATES, EngineEvent, ServiceConfig, ServiceState,
                        ServiceStatus, parse_env, split_args)

IS_WIN = os.name == "nt"
ENV_MARK = "SERVICEDECK_SERVICE_ID"
STARTUP_GRACE = 0.8          # s que debe sobrevivir para confirmar RUNNING
STOP_GRACE = 5.0             # s entre terminate y kill
LOG_MAX_BYTES = 5 * 1024 * 1024
RESTART_WINDOW = 60.0
RESTART_MAX = 5
TAIL_INTERVAL = 0.15
# host válido (nombre, IPv4 o [IPv6]) + puerto opcional + ruta/consulta opcional: ignora "http://{" de código fuente
URL_RE = re.compile(r"https?://(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?)(?::\d{1,5})?(?:[/?#][^\s'\"<>]*)?")
LOOPBACK = ("127.0.0.1", "localhost", "0.0.0.0", "::", "::1", "[::]", "[::1]")


# ------------------------------------------------------------ Windows Job
class _Job:
    """Windows Job Object con KILL_ON_JOB_CLOSE: si ServiceDeck muere, el
    sistema mata todo el árbol. Un job por ejecución de servicio."""

    class _Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", ctypes.c_uint32),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", ctypes.c_uint32),
                    ("SchedulingClass", ctypes.c_uint32)]

    class _IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    def __init__(self):
        self.handle = None
        if not IS_WIN:
            return
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k32 = k32
        k32.CreateJobObjectW.restype = ctypes.c_void_p
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        k32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k32.CreateJobObjectW(None, None)
        if not h:
            return
        info = _JobExt()
        info.Basic.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info)):
            k32.CloseHandle(h)
            return
        self.handle = h

    def assign(self, pid: int) -> bool:
        if not self.handle:
            return False
        hp = self._k32.OpenProcess(0x0101, False, pid)   # SET_QUOTA | TERMINATE
        if not hp:
            return False
        try:
            return bool(self._k32.AssignProcessToJobObject(self.handle, hp))
        finally:
            self._k32.CloseHandle(hp)

    def terminate(self) -> None:
        if self.handle:
            self._k32.TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        if self.handle:
            self._k32.CloseHandle(self.handle)
            self.handle = None


class _JobExt(ctypes.Structure):
    _fields_ = [("Basic", _Job._Basic), ("IO", _Job._IO),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


# ----------------------------------------------------------------- estado
class _Managed:
    def __init__(self, cfg: ServiceConfig):
        self.cfg = cfg
        self.oplock = threading.RLock()      # serializa start/stop
        self.lock = threading.RLock()        # protege campos de estado
        self.state = ServiceState.STOPPED
        self.pid: Optional[int] = None
        self.create_time: Optional[float] = None
        self.started_at: Optional[float] = None
        self.exit_code: Optional[int] = None
        self.message = ""
        self.proc: Optional[subprocess.Popen] = None
        self.ps: Optional[psutil.Process] = None     # adoptado tras reinicio de la app
        self.job: Optional[_Job] = None
        self.files: list = []
        self.stop_requested = False
        self.exited = threading.Event()
        self.restarts: List[float] = []
        self.url: Optional[str] = None
        self.run = 0                         # generación de ejecución (para el tailer)
        self.offsets = [0, 0]                # dónde empieza la salida de esta ejecución
        self.ends: Dict[int, List[int]] = {} # run -> tamaño final de logs (acota al tailer)

    def snapshot(self) -> ServiceStatus:
        return ServiceStatus(self.cfg.id, self.state, self.pid, self.started_at,
                             self.exit_code, self.message, self.url)


class ProcessEngine:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.log_dir = os.path.join(data_dir, "logs")
        self.runtime_path = os.path.join(data_dir, "runtime.json")
        os.makedirs(self.log_dir, exist_ok=True)
        self._items: Dict[str, _Managed] = {}
        self._glock = threading.RLock()
        self._listeners: List[Callable[[EngineEvent], None]] = []
        self._shutting_down = False
        self._rt_lock = threading.Lock()

    # ------------------------------------------------------------ eventos
    def add_listener(self, cb):
        self._listeners.append(cb)

    def _emit(self, ev: EngineEvent):
        for cb in list(self._listeners):
            try:
                cb(ev)
            except Exception:
                pass

    def _log(self, m: _Managed, msg: str, level="info"):
        self._emit(EngineEvent("log", m.cfg.id, f"{m.cfg.alias}: {msg}", level))

    def _set(self, m: _Managed, state: ServiceState, msg: str = "", level="info", **kw):
        with m.lock:
            m.state = state
            m.message = msg
            for k, v in kw.items():
                setattr(m, k, v)
            snap = m.snapshot()
        self._emit(EngineEvent("state", m.cfg.id, msg or state.value, level, snap))

    def _get(self, cfg: ServiceConfig) -> _Managed:
        with self._glock:
            m = self._items.get(cfg.id)
            if m is None:
                m = self._items[cfg.id] = _Managed(cfg)
            elif m.state not in ACTIVE_STATES:
                m.cfg = cfg                    # toma la config vigente
            return m

    # --------------------------------------------------------- consultas
    def status(self, service_id: str) -> ServiceStatus:
        with self._glock:
            m = self._items.get(service_id)
        return m.snapshot() if m else ServiceStatus(service_id)

    def is_active(self, service_id: str) -> bool:
        return self.status(service_id).state in ACTIVE_STATES

    # --------------------------------------------------------- runtime.json
    def _save_runtime(self):
        with self._rt_lock:
            data = {}
            with self._glock:
                for sid, m in self._items.items():
                    if m.pid and m.state in (ServiceState.RUNNING, ServiceState.STARTING, ServiceState.STOPPING):
                        data[sid] = {"pid": m.pid, "create_time": m.create_time,
                                     "alias": m.cfg.alias}
            tmp = self.runtime_path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                os.replace(tmp, self.runtime_path)
            except OSError:
                pass

    @staticmethod
    def _is_ours(pid: int, create_time: float, service_id: str) -> Optional[psutil.Process]:
        """Verifica identidad (pid + create_time + marca de entorno si es legible).
        Un PID viejo por sí solo NO prueba nada."""
        try:
            p = psutil.Process(pid)
            if abs(p.create_time() - create_time) > 2.0:
                return None
            if not p.is_running() or p.status() == psutil.STATUS_ZOMBIE:
                return None
            try:
                env = p.environ()
            except (psutil.AccessDenied, OSError, NotImplementedError):
                env = None
            if env is not None and env.get(ENV_MARK) != service_id:
                return None
            return p
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
            return None

    def reconcile(self, configs: List[ServiceConfig]) -> None:
        """Reconstruye el estado real al iniciar. Adopta procesos propios que
        sigan vivos (verificados); jamás toca procesos ajenos."""
        try:
            with open(self.runtime_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, json.JSONDecodeError):
            saved = {}
        by_id = {c.id: c for c in configs}
        for sid, rec in saved.items():
            p = self._is_ours(rec.get("pid", -1), rec.get("create_time", 0) or 0, sid)
            if p is None:
                continue
            cfg = by_id.get(sid)
            if cfg is None:                       # servicio ya eliminado: limpiar lo nuestro
                self._kill_tree_ps(p)
                self._emit(EngineEvent("log", sid, f"Proceso huérfano {p.pid} de servicio eliminado terminado", "warn"))
                continue
            m = self._get(cfg)
            m.ps, m.pid, m.create_time = p, p.pid, p.create_time()
            m.started_at = m.create_time
            m.exited.clear()
            m.run += 1
            sizes = [os.path.getsize(os.path.join(self.log_dir, f"{sid}.{x}.log")) if os.path.exists(os.path.join(self.log_dir, f"{sid}.{x}.log")) else 0 for x in ("out", "err")]
            threading.Thread(target=self._tailer, args=(m, m.run, sizes), daemon=True).start()
            self._set(m, ServiceState.RUNNING, f"Adoptado proceso existente (PID {p.pid})", "warn")
            threading.Thread(target=self._monitor, args=(m,), daemon=True).start()
        self._save_runtime()

    # --------------------------------------------------------------- start
    def _resolve(self, cfg: ServiceConfig, env: dict) -> List[str]:
        exe = cfg.executable.strip()
        found = shutil.which(exe, path=env.get("PATH") or os.environ.get("PATH"))
        if not found:
            cand = os.path.join(cfg.cwd, exe)
            if os.path.isfile(cand):
                found = cand
        if not found:
            raise FileNotFoundError(f"Ejecutable no encontrado: {exe}")
        return [found] + split_args(cfg.args)

    def _build_cmd(self, cfg: ServiceConfig, env: dict):
        """Directo (Popen con lista) o a través de la shell elegida, para heredar
        el PATH/entorno de tu terminal (venv, conda, pyenv…)."""
        sh = (getattr(cfg, "shell", "none") or "none").lower()
        line = f"{cfg.executable} {cfg.args}".strip()
        path = env.get("PATH") or os.environ.get("PATH")
        if sh == "none":
            return self._resolve(cfg, env)
        if sh == "cmd":
            if not IS_WIN:
                raise OSError("La shell 'cmd' solo existe en Windows")
            return f'{os.environ.get("COMSPEC", "cmd.exe")} /d /s /c "{line}"'
        if sh == "powershell":
            exe = shutil.which("powershell", path=path) or shutil.which("pwsh", path=path)
            if not exe:
                raise FileNotFoundError("PowerShell no encontrado")
            return [exe, "-NoLogo", "-Command", line]
        if sh == "bash":
            exe = shutil.which("bash", path=path)
            if not exe and IS_WIN:
                for c in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"):
                    if os.path.isfile(c):
                        exe = c
                        break
            if not exe:
                raise FileNotFoundError("bash no encontrado (¿Git Bash / WSL en el PATH?)")
            return [exe, "-lc", line]
        raise ValueError(f"Shell desconocida: {sh}")

    def _open_logs(self, m: _Managed):
        out = []
        for suffix in ("out", "err"):
            path = os.path.join(self.log_dir, f"{m.cfg.id}.{suffix}.log")
            try:
                if os.path.getsize(path) > LOG_MAX_BYTES:
                    os.replace(path, path + ".1")
            except OSError:
                pass
            f = open(path, "ab", buffering=0)
            f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} start {m.cfg.alias} ===\n".encode())
            out.append(f)
        m.offsets = [os.path.getsize(os.path.join(self.log_dir, f"{m.cfg.id}.{x}.log")) for x in ("out", "err")]
        return out

    def start(self, cfg: ServiceConfig) -> ServiceStatus:
        m = self._get(cfg)
        with m.oplock:
            if m.state in ACTIVE_STATES:
                return m.snapshot()
            if self._shutting_down:
                return m.snapshot()
            m.stop_requested = False
            m.exited.clear()
            m.exit_code = None
            m.run += 1
            self._set(m, ServiceState.STARTING, "Iniciando…", pid=None, started_at=None, ps=None, url=None)
            try:
                if not os.path.isdir(m.cfg.cwd):
                    raise FileNotFoundError(f"Directorio de trabajo inexistente: {m.cfg.cwd}")
                env = dict(os.environ)
                env.update(parse_env(m.cfg.env))
                env[ENV_MARK] = m.cfg.id
                env.setdefault("PYTHONUNBUFFERED", "1")
                # stdout/stderr van a archivos: sin esto Python usa cp1252 en Windows y revienta con '→', emojis, etc.
                env.setdefault("PYTHONUTF8", "1")
                env.setdefault("PYTHONIOENCODING", "utf-8")
                cmd = self._build_cmd(m.cfg, env)
                self._log(m, "launch → " + (cmd if isinstance(cmd, str) else " ".join(cmd))[:240], "info")
                console = m.cfg.mode == "console"
                kwargs: dict = dict(cwd=m.cfg.cwd, env=env, stdin=subprocess.DEVNULL)
                if console:
                    m.files = []
                    self._log(m, "modo consola visible: la salida no se captura en logs", "warn")
                else:
                    m.files = self._open_logs(m)
                    kwargs.update(stdout=m.files[0], stderr=m.files[1])
                if IS_WIN:
                    flags = subprocess.CREATE_NEW_PROCESS_GROUP
                    flags |= subprocess.CREATE_NEW_CONSOLE if console else subprocess.CREATE_NO_WINDOW
                    kwargs["creationflags"] = flags
                else:
                    kwargs["start_new_session"] = True      # grupo propio: killpg
                m.job = _Job() if IS_WIN else None
                m.proc = subprocess.Popen(cmd, **kwargs)
                if m.job and not m.job.assign(m.proc.pid):
                    self._log(m, "no se pudo asignar al Job Object; se usará terminación por árbol", "warn")
                try:
                    ct = psutil.Process(m.proc.pid).create_time()
                except psutil.Error:
                    ct = time.time()
                m.pid, m.create_time, m.started_at = m.proc.pid, ct, time.time()
            except Exception as e:
                self._close_files(m)
                if m.job:
                    m.job.close()
                    m.job = None
                self._set(m, ServiceState.ERROR, f"No se pudo iniciar: {e}", "error", pid=None)
                return m.snapshot()

            self._save_runtime()
            threading.Thread(target=self._monitor, args=(m,), daemon=True).start()
            if m.files:
                threading.Thread(target=self._tailer, args=(m, m.run, list(m.offsets)), daemon=True).start()
            # confirmar inicio: debe sobrevivir el período de gracia
            if not m.exited.wait(STARTUP_GRACE):
                with m.lock:
                    still = m.state == ServiceState.STARTING
                if still:
                    self._set(m, ServiceState.RUNNING, f"En ejecución (PID {m.pid})", "ok")
            return m.snapshot()

    # ------------------------------------------------------------- monitor
    def _monitor(self, m: _Managed):
        proc, ps, run = m.proc, m.ps, m.run
        try:
            if proc is not None:
                code = proc.wait()
            else:
                code = ps.wait()       # type: ignore[union-attr]  (None si no es hijo)
                while ps.is_running() and ps.status() != psutil.STATUS_ZOMBIE:
                    time.sleep(0.3)
        except psutil.Error:
            code = None
        self._reap_leftovers(m)
        self._close_files(m)
        m.ends[run] = [self._size(m.cfg.id, x) for x in ("out", "err")]
        for old in [k for k in m.ends if k < run - 4]:
            m.ends.pop(old, None)
        if m.job:
            m.job.close()
            m.job = None
        with m.lock:
            requested = m.stop_requested
            m.exit_code = code
            m.proc = m.ps = None
            m.url = None
            old_pid = m.pid
        if requested:
            self._set(m, ServiceState.STOPPED, "Detenido", "warn", pid=None)
        elif code in (0, None):
            self._set(m, ServiceState.STOPPED, f"Finalizó (código {code})", "info", pid=None)
        else:
            tail = self._err_tail(m.cfg.id, 6)
            hint = f": {tail[-1][:160]}" if tail else " (ver pestaña LOG / stderr)"
            self._set(m, ServiceState.ERROR, f"Cierre inesperado (código {code}){hint}", "error", pid=None)
            for line in tail:
                self._log(m, "stderr> " + line[:200], "error")
        self._save_runtime()
        m.exited.set()
        unexpected = (not requested) and code not in (0, None)
        if unexpected and m.cfg.autorestart and not self._shutting_down:
            self._maybe_autorestart(m, old_pid)

    def _maybe_autorestart(self, m: _Managed, old_pid):
        now = time.time()
        m.restarts = [t for t in m.restarts if now - t < RESTART_WINDOW]
        if len(m.restarts) >= RESTART_MAX:
            self._log(m, f"autorestart desactivado: {RESTART_MAX} fallos en {int(RESTART_WINDOW)}s", "error")
            return
        m.restarts.append(now)
        delay = min(2 ** len(m.restarts), 15)
        self._log(m, f"autorestart en {delay}s (intento {len(m.restarts)})", "warn")

        def run():
            time.sleep(delay)
            if not self._shutting_down and not m.stop_requested and m.state == ServiceState.ERROR:
                self.start(m.cfg)
        threading.Thread(target=run, daemon=True).start()

    def _reap_leftovers(self, m: _Managed):
        """El proceso principal terminó: ningún descendiente debe quedar vivo."""
        if IS_WIN:
            if m.job:
                m.job.terminate()
        elif m.pid:
            try:
                os.killpg(m.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    # ------------------------------------------------------------- salida en vivo
    def _size(self, service_id: str, stream: str) -> int:
        try:
            return os.path.getsize(os.path.join(self.log_dir, f"{service_id}.{stream}.log"))
        except OSError:
            return 0

    def _tailer(self, m: _Managed, run: int, offsets: List[int]) -> None:
        """Sigue stdout/stderr del servicio y emite cada línea como evento 'output'.
        Termina tras drenar lo último cuando el proceso sale o empieza otra ejecución."""
        sid = m.cfg.id
        paths = [os.path.join(self.log_dir, f"{sid}.{x}.log") for x in ("out", "err")]
        pending = [b"", b""]
        offs = list(offsets)
        while True:
            done = m.run != run or m.exited.is_set()
            lim = m.ends.get(run)                     # fin de ESTA ejecución (si ya terminó)
            for i, stream in enumerate(("out", "err")):
                if lim is not None and offs[i] >= lim[i]:
                    continue
                try:
                    with open(paths[i], "rb") as f:
                        f.seek(offs[i])
                        chunk = f.read() if lim is None else f.read(lim[i] - offs[i])
                except OSError:
                    continue
                if not chunk:
                    continue
                offs[i] += len(chunk)
                data = pending[i] + chunk
                *lines, pending[i] = data.split(b"\n")
                for raw in lines:
                    self._output(m, run, stream, raw.decode("utf-8", "replace").rstrip("\r"))
            if done:
                for i, stream in enumerate(("out", "err")):     # resto sin salto de línea final
                    if pending[i]:
                        self._output(m, run, stream, pending[i].decode("utf-8", "replace").rstrip("\r"))
                return
            time.sleep(TAIL_INTERVAL)

    def _output(self, m: _Managed, run: int, stream: str, line: str) -> None:
        self._emit(EngineEvent("output", m.cfg.id, line, "error" if stream == "err" else "info", stream=stream))
        if m.run == run:
            self._detect_url(m, line)

    def _detect_url(self, m: _Managed, line: str) -> None:
        for raw in URL_RE.findall(line):
            url = raw.rstrip(".,;:)'\"")
            host = re.match(r"https?://(\[[^\]]+\]|[^:/]+)", url)
            if not host:
                continue
            h = host.group(1)
            loop = h in LOOPBACK
            if h in ("0.0.0.0", "[::]", "::"):
                url = url.replace(h, "127.0.0.1", 1)
            score = 2 if loop else 1
            cur = m.url
            cur_score = 0 if not cur else (2 if re.match(r"https?://(127\.0\.0\.1|localhost|\[::1\])", cur) else 1)
            if score > cur_score:
                with m.lock:
                    m.url = url
                    snap = m.snapshot()
                self._emit(EngineEvent("state", m.cfg.id, f"URL detectada: {url}", "ok", snap))
                return

    def read_log_lines(self, service_id: str, tail: int = 500):
        """[(stream, línea)] de las últimas líneas de cada archivo (stdout primero)."""
        res = []
        for stream in ("out", "err"):
            path = os.path.join(self.log_dir, f"{service_id}.{stream}.log")
            try:
                with open(path, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    f.seek(max(0, f.tell() - 128 * 1024))
                    text = f.read().decode("utf-8", "replace")
            except OSError:
                continue
            res += [(stream, l.rstrip("\r")) for l in text.splitlines()[-tail:]]
        return res

    def _err_tail(self, service_id: str, n: int = 6) -> List[str]:
        """Últimas líneas no vacías de stderr de la ejecución actual (diagnóstico)."""
        path = os.path.join(self.log_dir, f"{service_id}.err.log")
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 16 * 1024))
                text = f.read().decode("utf-8", "replace")
        except OSError:
            return []
        text = text.rsplit("=== ", 1)[-1]            # solo desde el último start
        lines = [l.rstrip() for l in text.splitlines()[1:] if l.strip()]
        return lines[-n:]

    @staticmethod
    def _close_files(m: _Managed):
        for f in m.files:
            try:
                f.close()
            except OSError:
                pass
        m.files = []

    # ---------------------------------------------------------------- stop
    @staticmethod
    def _kill_tree_ps(root: psutil.Process, grace: float = STOP_GRACE):
        try:
            procs = root.children(recursive=True) + [root]
        except psutil.Error:
            procs = [root]
        for p in procs:
            try:
                p.terminate()
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(procs, timeout=grace)
        for p in alive:
            try:
                p.kill()
            except psutil.Error:
                pass
        psutil.wait_procs(alive, timeout=3)

    def stop(self, service_id: str) -> ServiceStatus:
        with self._glock:
            m = self._items.get(service_id)
        if m is None:
            return ServiceStatus(service_id)
        with m.oplock:
            if m.state not in (ServiceState.RUNNING, ServiceState.STARTING):
                if m.state == ServiceState.ERROR:
                    self._set(m, ServiceState.STOPPED, "Estado ERROR reconocido", "info")
                return m.snapshot()
            m.stop_requested = True
            self._set(m, ServiceState.STOPPING, "Deteniendo…", "warn")
            pid = m.pid
            root = None
            try:
                root = m.ps or (psutil.Process(pid) if pid else None)
            except psutil.Error:
                root = None
            if root is not None:
                # SIGTERM / TerminateProcess a todo el árbol y escalada a kill
                self._kill_tree_ps(root)
            if not IS_WIN and pid:
                try:
                    os.killpg(pid, signal.SIGTERM)
                    time.sleep(0.05)
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            if IS_WIN and m.job:
                m.job.terminate()
            if not m.exited.wait(STOP_GRACE + 5):
                self._set(m, ServiceState.ERROR, "No se pudo confirmar la detención", "error")
            return m.snapshot()

    def restart(self, cfg: ServiceConfig) -> ServiceStatus:
        self.stop(cfg.id)
        return self.start(cfg)

    def stop_all(self) -> None:
        with self._glock:
            ids = [sid for sid, m in self._items.items() if m.state in ACTIVE_STATES]
        ts = [threading.Thread(target=self.stop, args=(i,)) for i in ids]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

    def shutdown(self) -> None:
        """Cierre de la app: detener todo y dejar runtime.json vacío."""
        self._shutting_down = True
        self.stop_all()
        self._save_runtime()

    # ---------------------------------------------------------------- logs
    def read_log(self, service_id: str, stream: str = "both", tail: int = 300) -> str:
        chunks = []
        for suffix in (("out", "err") if stream == "both" else (stream,)):
            path = os.path.join(self.log_dir, f"{service_id}.{suffix}.log")
            try:
                with open(path, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    f.seek(max(0, size - 64 * 1024))
                    text = f.read().decode("utf-8", "replace")
            except OSError:
                continue
            lines = text.splitlines()[-tail:]
            if lines:
                chunks.append(f"--- {suffix.upper()} ---\n" + "\n".join(lines))
        return "\n".join(chunks) if chunks else "(sin salida registrada)"
