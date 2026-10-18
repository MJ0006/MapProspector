"""Settings the Windows desktop app relies on (desktop/ starts the server with these)."""
import os
import subprocess
import sys
from pathlib import Path

from app import settings


def test_claude_found_outside_path(isolated, monkeypatch):
    assert settings.claude_exe() is None
    exe = Path(os.environ["HOME"]) / ".local" / "bin" / "claude"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    assert settings.claude_exe() == str(exe)
    assert settings.modules()["claude"] is True


def test_claude_path_override(isolated, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PATH", str(tmp_path / "missing.exe"))
    assert settings.claude_exe() is None
    (tmp_path / "claude.exe").write_text("")
    monkeypatch.setenv("CLAUDE_PATH", str(tmp_path / "claude.exe"))
    assert settings.claude_exe() == str(tmp_path / "claude.exe")


def test_env_file_from_environment(tmp_path):
    env_file = tmp_path / "desktop.env"
    env_file.write_text("GOOGLE_API_KEY=from-desktop-env\n")
    env = {k: v for k, v in os.environ.items() if k != "GOOGLE_API_KEY"}
    env.update(MAPPROSPECTOR_ENV_FILE=str(env_file), DATA_DIR=str(tmp_path / "data"))
    out = subprocess.run([sys.executable, "-c", "from app import settings; print(settings.env('GOOGLE_API_KEY'), "
                          "settings.DATA_DIR)"], capture_output=True, text=True, env=env, cwd=settings.ROOT, check=True)
    assert out.stdout.split() == ["from-desktop-env", str(tmp_path / "data")]


def _serve(tmp_path):
    import socket
    import time
    import urllib.request
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "DATA_DIR": str(tmp_path / "data"), "MAPPROSPECTOR_NO_JOBS": "1",
           "MAPPROSPECTOR_ENV_FILE": str(tmp_path / "none.env"),
           "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}  # conftest moved HOME (user site-packages)
    proc = subprocess.Popen([sys.executable, "-m", "app.desktop_run", "--port", str(port)], cwd=settings.ROOT, env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/static/favicon.svg", timeout=1)
            return proc
        except OSError:
            time.sleep(0.2)
    proc.kill()
    raise AssertionError(proc.stdout.read())


def test_desktop_server_quits_cleanly_on_request(tmp_path):
    proc = _serve(tmp_path)
    proc.stdin.write("quit\n")
    proc.stdin.flush()
    assert proc.wait(timeout=20) == 0
    assert "Application shutdown complete" in proc.stdout.read()


def test_desktop_server_stops_when_the_window_is_gone(tmp_path):
    proc = _serve(tmp_path)
    proc.stdin.close()  # what the OS does when the app window process ends, even if it crashed
    assert proc.wait(timeout=20) == 0
