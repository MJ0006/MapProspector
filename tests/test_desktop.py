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
