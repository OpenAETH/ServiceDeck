import os, sys, time, tempfile, textwrap, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import psutil
from interfaces import ServiceConfig, ServiceState as S
from process_engine import ProcessEngine
import process_engine as pe
from serviceregistry import ServiceRegistry, RegistryError, ServiceLockedError

PARENT = textwrap.dedent('''
    import subprocess, sys, time
    child = subprocess.Popen([sys.executable, "-c", "import time;print('child up',flush=True);time.sleep(300)"])
    print("parent up child=%d" % child.pid, flush=True)
    sys.stderr.write("err line\\n"); sys.stderr.flush()
    time.sleep(300)
''')

def wait(fn, t=8):
    end = time.time() + t
    while time.time() < end:
        if fn(): return True
        time.sleep(0.05)
    return False

class T(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.work = os.path.join(self.d, "w"); os.makedirs(self.work)
        open(os.path.join(self.work, "parent.py"), "w").write(PARENT)
        self.eng = ProcessEngine(os.path.join(self.d, "data"))
        self.events = []
        self.eng.add_listener(self.events.append)
    def tearDown(self): self.eng.shutdown()
    def cfg(self, **kw):
        base = dict(id="svc_t1", name="T", alias="t", executable=sys.executable,
                    args="parent.py", cwd=self.work)
        base.update(kw); return ServiceConfig(**base)

    def test_lifecycle_and_tree_kill(self):
        c = self.cfg()
        st = self.eng.start(c)
        self.assertEqual(st.state, S.RUNNING); self.assertTrue(psutil.pid_exists(st.pid))
        self.assertTrue(wait(lambda: "child up" in self.eng.read_log(c.id)))
        kids = psutil.Process(st.pid).children(recursive=True)
        self.assertEqual(len(kids), 1)
        log = self.eng.read_log(c.id); self.assertIn("parent up", log); self.assertIn("err line", log)
        self.eng.stop(c.id)
        self.assertEqual(self.eng.status(c.id).state, S.STOPPED)
        self.assertFalse(psutil.pid_exists(st.pid))
        self.assertFalse(any(k.is_running() for k in kids))
        states = [e.status.state for e in self.events if e.kind == "state"]
        self.assertEqual(states[:3], [S.STARTING, S.RUNNING, S.STOPPING]); self.assertEqual(states[-1], S.STOPPED)

    def test_restart_new_pid(self):
        c = self.cfg(); a = self.eng.start(c).pid
        b = self.eng.restart(c)
        self.assertEqual(b.state, S.RUNNING); self.assertNotEqual(a, b.pid)

    def test_bad_executable_and_cwd(self):
        self.assertEqual(self.eng.start(self.cfg(executable="no_such_exe_xyz")).state, S.ERROR)
        self.assertEqual(self.eng.start(self.cfg(cwd=os.path.join(self.d, "nope"))).state, S.ERROR)

    def test_unexpected_crash_is_error(self):
        c = self.cfg(args='-c "import time,sys;time.sleep(1.2);sys.exit(3)"')
        self.eng.start(c)
        self.assertTrue(wait(lambda: self.eng.status(c.id).state == S.ERROR))
        self.assertEqual(self.eng.status(c.id).exit_code, 3)

    def test_error_message_carries_stderr(self):
        c = self.cfg(args='-c "import sys;sys.stderr.write(\'ModuleNotFoundError: No module named dash\\n\');sys.exit(1)"')
        st = self.eng.start(c)
        self.assertEqual(st.state, S.ERROR); self.assertIn("No module named dash", st.message)

    def test_immediate_failure_never_running(self):
        c = self.cfg(args='-c "import sys;sys.exit(2)"')
        self.assertEqual(self.eng.start(c).state, S.ERROR)
        self.assertNotIn(S.RUNNING, [e.status.state for e in self.events if e.kind == "state"])

    def test_leftover_children_killed_when_parent_dies(self):
        c = self.cfg(args='-c "import subprocess,sys,time;subprocess.Popen([sys.executable,\'-c\',\'import time;time.sleep(300)\']);time.sleep(1.5)"')
        st = self.eng.start(c); kids = psutil.Process(st.pid).children(recursive=True)
        self.assertTrue(kids)
        self.assertTrue(wait(lambda: self.eng.status(c.id).state == S.STOPPED))
        self.assertTrue(wait(lambda: not any(k.is_running() and k.status() != psutil.STATUS_ZOMBIE for k in kids)))

    def test_env_and_args_quoting(self):
        c = self.cfg(args='-c "import os,sys;print(os.environ[\'FOO\'],sys.argv[1]);import time;time.sleep(5)" "a b"', env="FOO=bar")
        self.eng.start(c)
        self.assertTrue(wait(lambda: "bar a b" in self.eng.read_log(c.id)))

    def test_reconcile_adopts_own_and_ignores_foreign(self):
        c = self.cfg(); st = self.eng.start(c)
        # nuevo engine = app reiniciada (sin shutdown del anterior)
        e2 = ProcessEngine(os.path.join(self.d, "data")); e2.reconcile([c])
        s = e2.status(c.id); self.assertEqual(s.state, S.RUNNING); self.assertEqual(s.pid, st.pid)
        e2.stop(c.id); self.assertFalse(psutil.pid_exists(st.pid) and psutil.Process(st.pid).status() != psutil.STATUS_ZOMBIE)
        # PID viejo reutilizado por proceso ajeno: no se adopta ni se mata
        foreign = psutil.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
        import json
        json.dump({"svc_x": {"pid": foreign.pid, "create_time": 12345.0}}, open(e2.runtime_path, "w"))
        e3 = ProcessEngine(os.path.join(self.d, "data")); e3.reconcile([self.cfg(id="svc_x", alias="x")])
        self.assertEqual(e3.status("svc_x").state, S.STOPPED); self.assertTrue(foreign.is_running())
        foreign.kill(); foreign.wait()

    def test_autorestart(self):
        c = self.cfg(args='-c "import time,sys;time.sleep(1);sys.exit(1)"', autorestart=True)
        self.eng.start(c)
        self.assertTrue(wait(lambda: sum(1 for e in self.events if e.kind=="state" and e.status.state==S.STARTING) >= 2, 12))

class R(unittest.TestCase):
    def test_registry(self):
        d = tempfile.mkdtemp(); r = ServiceRegistry(os.path.join(d, "services.json"))
        data = dict(name="Prompt Forge", alias=r.unique_alias("Prompt Forge"), executable="python", cwd=d)
        c = r.create(data); self.assertEqual(c.alias, "prompt_forge")
        self.assertEqual(r.unique_alias("Prompt Forge"), "prompt_forge_2")
        with self.assertRaises(RegistryError): r.create(data)            # alias duplicado
        with self.assertRaises(RegistryError): r.create({**data, "alias": "Bad Alias", "name": "x"})
        u = r.update(c.id, {**data, "name": "Renombrado", "id": "hack"})   # id inmutable
        self.assertEqual(u.id, c.id); self.assertEqual(u.alias, "prompt_forge")
        r2 = ServiceRegistry(os.path.join(d, "services.json")); self.assertEqual(r2.list()[0].name, "Renombrado")
        r.set_lock_guard(lambda i: True)
        with self.assertRaises(ServiceLockedError): r.update(c.id, data)
        with self.assertRaises(ServiceLockedError): r.delete(c.id)
        r.set_lock_guard(lambda i: False); r.delete(c.id); self.assertEqual(r.list(), [])

if __name__ == "__main__": unittest.main(verbosity=2)
