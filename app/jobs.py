"""Background work: one APScheduler instance runs one-off jobs (scans, AI checks) and the
recurring tick that fires due schedules, automation waits, profile-lock polls and reports.
Jobs only run while the app is running on your PC."""
import logging
import traceback

from apscheduler.schedulers.background import BackgroundScheduler

log = logging.getLogger("mapprospector.jobs")
scheduler = BackgroundScheduler(job_defaults={"misfire_grace_time": 3600, "coalesce": True})
_ticks: list = []
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


def tick():
    for fn in _ticks:
        _safe(fn)


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:  # background work must never kill the scheduler
        log.error("job %s failed\n%s", getattr(fn, "__name__", fn), traceback.format_exc())


def start():
    if not scheduler.running:
        scheduler.add_job(tick, "interval", minutes=1, id="tick", replace_existing=True)
        scheduler.start()


def stop():
    if scheduler.running:
        scheduler.shutdown(wait=False)
