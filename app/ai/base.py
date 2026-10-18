"""What every AI platform provides, and how its failures are sorted for fallback.

A platform raises PlatformError with a `kind`:
  limit   - usage/rate limit or quota: skipped until `retry_at` (or an hour), the next platform answers
  auth    - not signed in: skipped until you sign in on Settings
  timeout - no answer in time: the next platform answers
  error   - anything else: the next platform answers
"""
import re
import subprocess
from datetime import datetime, timedelta, timezone


class AIUnavailable(RuntimeError):
    pass


class PlatformError(AIUnavailable):
    def __init__(self, message: str, kind: str = "error", retry_at: datetime | None = None):
        super().__init__(message)
        self.kind = kind
        self.retry_at = retry_at


LIMIT_RE = re.compile(r"usage limit|rate[ _-]?limit|quota|resource_exhausted|too many requests|\b429\b|limit reached|"
                      r"spend cap|out of credits|credit limit", re.I)
# Busy for a moment (Claude's "529 overloaded", 503s): fall back now, but don't pause the platform for an hour.
BUSY_RE = re.compile(r"overloaded|\b529\b|\b503\b|temporarily unavailable|server is busy", re.I)
# Said in a successful-looking reply: only these exact phrasings count, so a draft about "limits" isn't misread.
STRONG_LIMIT_RE = re.compile(r"usage limit reached|hit your usage limit|reached your usage limit|limit reached\|\d{10}|"
                             r"resource_exhausted|quota exceeded", re.I)
AUTH_RE = re.compile(r"not logged in|please (run )?/?log ?in|login required|sign in|auth(entication)? (method|failed|required)|"
                     r"set an auth method|unauthori[sz]ed|\b401\b|invalid api key|invalid_grant|expired token|credentials", re.I)


def classify(text: str) -> str:
    if BUSY_RE.search(text or ""):
        return "error"
    if LIMIT_RE.search(text or ""):
        return "limit"
    if AUTH_RE.search(text or ""):
        return "auth"
    return "error"


def retry_time(text: str, now: datetime | None = None) -> datetime | None:
    """When a limit message says it resets: a Unix time ("|1767225600"), "in 2 hours 5 minutes", or "at 3pm"."""
    now = now or datetime.now(timezone.utc)
    text = text or ""
    m = re.search(r"\|(\d{10})\b", text)
    if m:
        return datetime.fromtimestamp(int(m.group(1)), timezone.utc)
    unit = r"(?:days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b"
    m = re.search(rf"\bin ((?:\d+\s*{unit}(?:\s*,?\s*(?:and\s+)?)?)+)", text, re.I)
    if m:
        total = timedelta()
        for n, unit in re.findall(r"(\d+)\s*([a-z]+)", m.group(1).lower()):
            n = int(n)
            total += {"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n),
                      "s": timedelta(seconds=n)}[unit[0]]
        if total:
            return now + total
    months = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
    m = re.search(rf"\b(?:resets?|try again|available)\b\D{{0,12}}\b({months})[a-z]*\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?"
                  rf"\s*(?:at\s+)?(\d{{1,2}})(?::(\d{{2}}))?\s*([ap])\.?m\b", text, re.I)
    if m:  # "resets Oct 20 at 3pm" (weekly limits)
        local = now.astimezone()
        hour = int(m.group(3)) % 12 + (12 if m.group(5).lower() == "p" else 0)
        try:
            at = local.replace(month=months.split("|").index(m.group(1).lower()[:3]) + 1, day=int(m.group(2)),
                               hour=hour, minute=int(m.group(4) or 0), second=0, microsecond=0)
        except ValueError:
            at = None
        if at:
            if at <= local:
                at = at.replace(year=at.year + 1)
            return at.astimezone(timezone.utc)
    m = re.search(r"\b(?:resets?|try again|available)\b[^0-9]{0,12}(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\b", text, re.I)
    if m:
        hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0)
        local = now.astimezone()
        at = local.replace(hour=hour, minute=int(m.group(2) or 0), second=0, microsecond=0)
        if at <= local:
            at += timedelta(days=1)
        return at.astimezone(timezone.utc)
    return None


def run_cli(label: str, cmd: list[str], prompt: str, timeout: int, cwd=None, env=None) -> str:
    """Run a CLI with the prompt on stdin (Windows' npm .cmd shims cut arguments at newlines and 8191 chars)."""
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, cwd=cwd, env=env)
    except subprocess.TimeoutExpired:
        raise PlatformError(f"{label} did not answer within {timeout} seconds.", "timeout")
    except OSError as exc:
        raise PlatformError(f"{label} could not start: {exc}")
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        err = ((proc.stderr or "") + "\n" + out).strip()[-800:] or f"{label} exited with an error"
        raise PlatformError(err, classify(err), retry_time(err))
    if len(out) < 400 and STRONG_LIMIT_RE.search(out):
        raise PlatformError(out, "limit", retry_time(out))
    return out


class Platform:
    """One AI platform. Subclasses set the class attributes and implement exe/run."""
    name = ""
    label = ""
    web = False   # can search the web (citation lookups need it)
    mcp = False   # can read your MapProspector data through the MCP server (Ask needs it)
    install = ""  # how to install it, shown on Settings
    signin: list[str] | None = None  # command that signs you in, run in a terminal from Settings

    def exe(self) -> str | None:
        return None

    def installed(self) -> bool:
        return self.exe() is not None

    def signed_in(self) -> bool | None:
        """True / False when the platform can tell cheaply, None when only a real call would tell."""
        return None

    def run(self, prompt: str, *, web: bool, mcp: bool, tools: list[str], timeout: int) -> str:
        raise NotImplementedError
