"""Request protection.

1. Cross-site request forgery: a POST from another website (whose Origin/Referer is not this app)
   is rejected, except on the public endpoints customers use.
2. Access: if APP_PASSWORD is set, admin pages need a login cookie. If it is not set, admin pages
   only answer requests made directly on this computer; requests arriving through a tunnel or
   from the network get a 403, while public pages (forms, review links, share links) keep working.
"""
import hashlib
import hmac
import ipaddress
import secrets
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from app import settings

PUBLIC_PREFIXES = ("/f/", "/q/", "/r/", "/share/", "/static/", "/media/photos/")
PUBLIC_EXACT = {"/login", "/gbp/oauth/callback", "/favicon.ico"}
FORWARD_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-real-ip", "cf-connecting-ip", "forwarded")
COOKIE = "mp_session"


def is_public(path: str) -> bool:
    return path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIXES)


def _secret() -> bytes:
    path = settings.DATA_DIR / ".session_secret"
    if not path.exists():
        settings.ensure_dirs()
        path.write_text(secrets.token_hex(32))
    return path.read_text().strip().encode()


def session_value() -> str:
    pw = settings.env("APP_PASSWORD")
    return hmac.new(_secret(), pw.encode(), hashlib.sha256).hexdigest()


def is_local(request: Request) -> bool:
    if any(h in request.headers for h in FORWARD_HEADERS):
        return False
    host = (request.client.host if request.client else "") or ""
    if host == "testclient":
        return True
    try:
        if not ipaddress.ip_address(host).is_loopback:
            return False
    except ValueError:
        return False
    hostname = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
    return hostname in ("127.0.0.1", "localhost", "::1", "testserver", "")


def same_origin(request: Request) -> bool:
    """True unless the browser says this POST came from another site."""
    src = request.headers.get("origin") or request.headers.get("referer")
    if not src or src == "null":
        return src != "null"  # non-browser clients send neither; sandboxed frames send "null"
    want = (request.headers.get("host") or "").lower()
    return urlsplit(src).netloc.lower() == want


async def middleware(request: Request, call_next):
    path = request.url.path
    if is_public(path):
        return await call_next(request)
    if request.method not in ("GET", "HEAD", "OPTIONS") and not same_origin(request):
        return PlainTextResponse("Blocked: this request came from another website.", status_code=403)
    if settings.env("APP_PASSWORD"):
        cookie = request.cookies.get(COOKIE, "")
        if not hmac.compare_digest(cookie, session_value()):
            if request.method == "GET":
                return RedirectResponse(f"/login?next={path}", status_code=303)
            return PlainTextResponse("Login required.", status_code=401)
    elif not is_local(request):
        return PlainTextResponse("MapProspector admin pages are only available on this computer. "
                                 "Set APP_PASSWORD in .env to allow remote access.", status_code=403)
    return await call_next(request)


LOGIN_HTML = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Log in · MapProspector</title><link rel="stylesheet" href="/static/theme.css"></head>
<body><main style="max-width:360px;margin:12vh auto;padding:16px"><form class="card" method="post" action="/login">
<h1>MapProspector</h1>{error}<input type="hidden" name="next" value="{next}">
<div class="field"><label>Password</label><input type="password" name="password" autofocus required></div>
<button class="primary" type="submit">Log in</button></form></main></body></html>"""


def login_page(next_url: str = "/", error: str = "") -> HTMLResponse:
    import html
    err = f'<div class="notice bad">{html.escape(error)}</div>' if error else ""
    return HTMLResponse(LOGIN_HTML.format(error=err, next=html.escape(safe_next(next_url))))


def safe_next(url: str | None, default: str = "/") -> str:
    """Only allow same-site relative paths (blocks open redirects like //evil.com or https://…)."""
    if not url or not url.startswith("/") or url.startswith("//") or "\\" in url:
        return default
    return url


def login(password: str, next_url: str):
    pw = settings.env("APP_PASSWORD")
    if not pw or not hmac.compare_digest(password.encode(), pw.encode()):
        return login_page(next_url, "Wrong password.")
    resp = RedirectResponse(safe_next(next_url), status_code=303)
    resp.set_cookie(COOKIE, session_value(), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return resp
