"""Settings → AI platforms: order, on/off, local model choice, and sign-in for each platform."""
import os
import shlex
import shutil
import subprocess
import sys

from fastapi import APIRouter, Form, HTTPException, Request

from app import ai, db, journal, security
from app.ai import ollama
from app.web import redirect

router = APIRouter()


def _platform(name: str):
    if name not in ai.PLATFORMS:
        raise HTTPException(404, "Unknown AI platform")
    return ai.PLATFORMS[name]


@router.post("/settings/ai/{name}/move")
def move(name: str, direction: str = Form(...)):
    _platform(name)
    names = ai.order()
    if name in names:
        i = names.index(name)
        j = i - 1 if direction == "up" else i + 1
        if 0 <= j < len(names):
            names[i], names[j] = names[j], names[i]
            ai.set_order(names)
    return redirect("/settings#ai")


@router.post("/settings/ai/{name}/toggle")
def toggle(name: str):
    _platform(name)
    names = ai.order()
    ai.set_order([n for n in names if n != name] if name in names else names + [name])
    journal.record("ai", "decision", "platform_off" if name in names else "platform_on", detail=name)
    return redirect("/settings#ai")


@router.post("/settings/ai/{name}/resume")
def resume(name: str):
    _platform(name)
    ai.resume(name)
    return redirect("/settings#ai")


@router.post("/settings/ai/ollama/model")
def ollama_model(model: str = Form("")):
    db.set_setting("ollama_model", model.strip())
    return redirect("/settings#ai")


def open_terminal(cmd: list[str]) -> bool:
    """Open a terminal window running `cmd` on this computer (sign-in pages open in your browser from there)."""
    try:
        if os.name == "nt":
            subprocess.Popen(["cmd", "/c", "start", "MapProspector sign-in", "cmd", "/k", *cmd])
            return True
        line = " ".join(shlex.quote(c) for c in cmd)
        if sys.platform == "darwin":
            subprocess.Popen(["osascript", "-e", f'tell application "Terminal" to do script "{line}"',
                              "-e", 'tell application "Terminal" to activate'])
            return True
        for term, args in (("x-terminal-emulator", ["-e"]), ("gnome-terminal", ["--"]), ("konsole", ["-e"])):
            if shutil.which(term):
                subprocess.Popen([term, *args, *cmd])
                return True
    except OSError:
        pass
    return False


@router.post("/settings/ai/{name}/signin")
def signin(request: Request, name: str):
    p = _platform(name)
    if not p.signin or not p.installed():
        raise HTTPException(400, f"Install {p.label} first: {p.install}")
    if not security.is_local(request):
        raise HTTPException(403, "Sign in on the computer that runs MapProspector.")
    cmd = [p.exe(), *p.signin[1:]]
    opened = open_terminal(cmd)
    ai.resume(name)
    return redirect(f"/settings?signin={name}&opened={int(opened)}#ai")


def card_context() -> dict:
    return {"ai_platforms": ai.status(), "ollama_models": ollama.models() or [],
            "ollama_model": ollama.chosen_model() or ""}
