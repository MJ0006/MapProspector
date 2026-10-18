"""Settings → API keys: paste keys in the app instead of editing .env by hand. They take effect at once.

Local requests only: keys are never shown to, or changed from, another computer (even with APP_PASSWORD).
"""
import os

from fastapi import APIRouter, HTTPException, Request

from app import envfile, journal, security, settings
from app.modules.guide.routes import KEYS
from app.web import redirect

router = APIRouter()

NOT_EDITABLE = {"DATA_DIR"}  # the Windows app always sets it; changing it would move your data
RESTART_NEEDED = {"PUBLIC_BASE_URL"}  # read once when the server starts


def is_secret(key: str) -> bool:
    return key.endswith(("_KEY", "_PASSWORD", "_SECRET")) or key == "APP_PASSWORD"


def mask(value: str) -> str:
    return ("••••" + value[-4:]) if len(value) > 4 else "••••" if value else ""


def groups() -> list[dict]:
    """The Guide's key sections, with what's set now (secrets masked)."""
    current = envfile.read(settings.ENV_FILE)
    out = []
    for g in KEYS:
        fields = []
        for key in g["keys"] + g.get("optional_keys", []):
            if key in NOT_EDITABLE:
                continue
            value = current.get(key) or settings.env(key)
            fields.append({"key": key, "secret": is_secret(key), "set": bool(value),
                           "shown": mask(value) if is_secret(key) else value, "restart": key in RESTART_NEEDED,
                           # rarely needed: folded under "More options" unless already set
                           "more": (key in g.get("optional_keys", []) or g["id"] == "advanced") and not value})
        if fields:
            out.append({"id": g["id"], "title": g["title"], "fields": fields})
    return out


@router.post("/settings/keys")
async def save_keys(request: Request):
    if not security.is_local(request):
        raise HTTPException(403, "API keys can only be changed on the computer that runs MapProspector.")
    form = await request.form()
    editable = {f["key"]: f for g in groups() for f in g["fields"]}
    changes: dict[str, str | None] = {}
    for key, field in editable.items():
        if form.get(f"clear_{key}"):
            if field["set"]:
                changes[key] = None
            continue
        if key not in form:
            continue  # not on the submitted form: leave it alone
        value = str(form.get(key, "")).strip()
        if field["secret"]:
            if value:  # a blank secret field keeps what's there (the page never shows the real value)
                changes[key] = value
        elif value != field["shown"]:
            changes[key] = value or None
    if changes:
        try:
            envfile.update(settings.ENV_FILE, changes)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        for key, value in changes.items():  # no restart: settings.env() reads os.environ on every call
            if value:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
        journal.record("settings", "decision", "keys_saved", detail=", ".join(sorted(changes)))  # names only
    restart = int(bool(set(changes) & RESTART_NEEDED))
    return redirect(f"/settings?keys_saved={len(changes)}&restart={restart}#keys")
