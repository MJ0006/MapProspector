from datetime import datetime, timezone

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from app import db
from app.modules.clients.routes import company_or_404
from app.modules.reports import builder
from app.providers import gbp_api
from app.web import redirect, render, row_or_404

router = APIRouter()


@router.get("/reports")
def reports_all(request: Request):
    runs = db.all("SELECT rr.*, r.name, c.name company, c.id company_id FROM report_runs rr JOIN reports r ON r.id = rr.report_id "
                  "JOIN companies c ON c.id = r.company_id ORDER BY rr.id DESC LIMIT 100")
    reps = db.all("SELECT r.*, c.name company FROM reports r JOIN companies c ON c.id = r.company_id ORDER BY c.name")
    return render(request, "reports/all.html", title="Reports", runs=runs, reps=reps)


@router.get("/clients/{company_id}/reports")
def client_reports(request: Request, company_id: int):
    co = company_or_404(company_id)
    reps = db.all("SELECT * FROM reports WHERE company_id = ?", company_id)
    runs = db.all("SELECT rr.*, r.name FROM report_runs rr JOIN reports r ON r.id = rr.report_id WHERE r.company_id = ? "
                  "ORDER BY rr.id DESC LIMIT 30", company_id)
    return render(request, "reports/client.html", title=co["name"], co=co, tab="reports", connected=gbp_api.connected(),
                  reps=reps, runs=runs, sections=builder.SECTIONS)


@router.post("/clients/{company_id}/reports")
async def report_new(request: Request, company_id: int):
    company_or_404(company_id)
    form = await request.form()
    sections = [s for s in form.getlist("sections") if s in builder.SECTIONS] or list(builder.SECTIONS)
    schedule = form.get("schedule") if form.get("schedule") in ("none", "weekly", "monthly") else "none"
    db.insert("reports", company_id=company_id, name=form.get("name") or "Local SEO report", sections=sections,
                    recipients=form.get("recipients", ""), schedule=schedule,
                    require_approval=0 if form.get("auto_send") else 1,
                    next_run_at=builder.next_run_at(schedule, datetime.now(timezone.utc)), created_at=db.now())
    return redirect(f"/clients/{company_id}/reports")


@router.post("/reports/{report_id}/build")
def report_build(report_id: int, note: str = Form("")):
    rep = db.one("SELECT * FROM reports WHERE id = ?", report_id)
    if not rep:
        raise HTTPException(404)
    run_id = builder.build_run(report_id, note)
    return redirect(f"/reports/runs/{run_id}")


@router.post("/reports/{report_id}/delete")
def report_delete(report_id: int):
    rep = row_or_404("reports", report_id)
    db.run("DELETE FROM reports WHERE id = ?", report_id)
    return redirect(f"/clients/{rep['company_id']}/reports")


def _run(run_id: int) -> dict:
    run = db.one("SELECT rr.*, r.company_id, r.name, r.recipients FROM report_runs rr JOIN reports r ON r.id = rr.report_id "
                 "WHERE rr.id = ?", run_id)
    if not run:
        raise HTTPException(404)
    return run


@router.get("/reports/runs/{run_id}")
def run_view(request: Request, run_id: int):
    return render(request, "reports/run.html", title="Report", run=_run(run_id))


@router.get("/reports/runs/{run_id}/html")
def run_html(run_id: int, download: int = 0):
    run = _run(run_id)
    headers = {"Content-Disposition": f'attachment; filename="report-{run_id}.html"'} if download else {}
    return Response(run["html"], media_type="text/html", headers=headers)


@router.post("/reports/runs/{run_id}/approve")
def run_approve(run_id: int):
    run = _run(run_id)
    if db.claim("report_runs", run_id, ("pending_approval",), "approved") and run["recipients"]:
        builder.send_run(run_id)
    return redirect(f"/reports/runs/{run_id}")


@router.get("/share/report/{token}")
def run_shared(token: str):
    run = db.one("SELECT * FROM report_runs WHERE token = ? AND status IN ('approved','sent')", token)
    if not run:
        raise HTTPException(404, "This report link is not valid.")
    return HTMLResponse(run["html"])
