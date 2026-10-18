"""Background work: one APScheduler instance runs one-off jobs (scans, AI checks) and the
recurring tick that fires due schedules, automation waits, profile-lock polls and reports.
Jobs only run while the app is running on your PC."""
import logging
import traceback

from apscheduler.schedulers.background import BackgroundScheduler

log = logging.getLogger("mapprospector.jobs")
scheduler = BackgroundScheduler(job_defaults={"misfire_grace_time": 3600, "coalesce": True})
_ticks: list = []
_startup: list = []
# Tests set this to True so queued work runs inline and deterministically.
INLINE = False


def submit(fn, *args, **kwargs):
    """Run fn in the background (or inline in tests)."""
    if INLINE or not scheduler.running:
        return _safe(fn, *args, **kwargs)
    scheduler.add_job(_safe, args=(fn, *args), kwargs=kwargs)


def on_tick(fn):
    """Register a function called every minute."""
    _ticks.append(fn)
    return fn


def on_startup(fn):
    """Register a function run once when the app starts (recover work interrupted by a crash or close)."""
    _startup.append(fn)
    return fn


def recover():
    for fn in _startup:
        _safe(fn)


def tick():
    for fn in _ticks:
        _safe(fn)


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # background work must never kill the scheduler
        name = getattr(fn, "__qualname__", str(fn))
        log.error("job %s failed\n%s", name, traceback.format_exc())
        from app import journal
        journal.failure(getattr(fn, "__module__", "job").split(".")[-2] if "." in getattr(fn, "__module__", "") else "job",
                        name, f"{type(exc).__name__}: {exc}", data={"args": [str(a)[:80] for a in args],
                                                                  "trace": traceback.format_exc()[-1500:]})


def start():
    if not scheduler.running:
        scheduler.add_job(tick, "interval", minutes=1, id="tick", replace_existing=True)
        scheduler.start()


def stop():
    if scheduler.running:
        scheduler.shutdown(wait=False)
