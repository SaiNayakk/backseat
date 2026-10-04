"""The supervisor must never leave a second copy of an app running.

Two bugs caused a duplicate Cloudflare tunnel on the phone:
- stopping an app killed only its wrapper shell, orphaning the real program;
- re-adoption after an agent restart could pick an unrelated shell that sat
  in the same folder, then start a new copy when that shell exited.
"""
import sys
import time

import psutil
import pytest

from backseat import agent

# A wrapper that starts a long-lived child, like `sh -c "cd … && ( cloudflared … )"`.
CHILD = f'"{sys.executable}" -c "import time; time.sleep(120)"'


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "AGENT_HOME", tmp_path)
    monkeypatch.setattr(agent, "APPS_FILE", tmp_path / "apps.json")
    monkeypatch.setattr(agent, "RUNTIME_FILE", tmp_path / "runtime.json")
    monkeypatch.setattr(agent, "LOGS_DIR", tmp_path / "logs")
    agent._apps.clear()
    yield
    with agent._apps_lock:
        for a in list(agent._apps.values()):
            agent._stop_app_process(a)
    agent._apps.clear()


def _tree(pid):
    p = psutil.Process(pid)
    return [p] + p.children(recursive=True)


def test_stop_ends_the_whole_process_tree():
    app = agent._new_app_entry("demo", CHILD, None)
    agent._apps["demo"] = app
    with agent._apps_lock:
        agent._spawn_app(app)
    time.sleep(1.5)
    procs = _tree(app["pid"])
    assert any("sleep" in " ".join(p.cmdline()) for p in procs), "child should be running"
    with agent._apps_lock:
        agent._stop_app_process(app)
    gone, alive = psutil.wait_procs(procs, timeout=5)
    assert not alive, f"left running: {[p.pid for p in alive]}"


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")
def test_stop_works_when_the_process_tree_cant_be_read(monkeypatch):
    """Android denies reading other processes' /proc, so psutil's children() fails there."""
    app = agent._new_app_entry("demo", CHILD, None)
    agent._apps["demo"] = app
    with agent._apps_lock:
        agent._spawn_app(app)
    time.sleep(1.5)
    procs = _tree(app["pid"])

    def denied(self, recursive=False):
        raise psutil.AccessDenied(self.pid)

    monkeypatch.setattr(psutil.Process, "children", denied)
    with agent._apps_lock:
        agent._stop_app_process(app)
    gone, alive = psutil.wait_procs(procs, timeout=5)
    assert not alive, f"left running: {[p.pid for p in alive]}"


def test_restart_adopts_the_exact_process():
    app = agent._new_app_entry("demo", CHILD, None)
    agent._apps["demo"] = app
    with agent._apps_lock:
        agent._spawn_app(app)
    pid = app["pid"]
    runtime = agent._load_runtime()
    assert runtime["demo"]["pid"] == pid and runtime["demo"]["create_time"]

    # Simulate an agent restart: forget the in-memory state, reload from disk.
    (agent.APPS_FILE).write_text('[{"name": "demo", "command": %s, "desired_state": "running"}]'
                                 % __import__("json").dumps(CHILD), encoding="utf-8")
    agent._apps.clear()
    agent._load_and_start_apps()
    assert agent._apps["demo"]["pid"] == pid, "should adopt the same process, not start another"
