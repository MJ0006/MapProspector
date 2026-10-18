"""Sales pipelines (D2): multiple Kanban pipelines, custom stages with win %, deals per contact."""
from fastapi import APIRouter, Form, HTTPException, Request

from app import db
from app.modules.crm import automations
from app.web import redirect, render, safe_back

router = APIRouter()

DEFAULT_STAGES = [("New lead", 5), ("Contacted", 10), ("Replied", 25), ("Audit sent", 40),
                  ("Meeting", 60), ("Proposal", 80), ("Won", 100), ("Lost", 0)]


def ensure_default() -> int:
    p = db.one("SELECT id FROM pipelines ORDER BY id LIMIT 1")
    if p:
        return p["id"]
    pid = db.insert("pipelines", name="Local SEO sales", created_at=db.now())
    for i, (name, pct) in enumerate(DEFAULT_STAGES):
        db.insert("pipeline_stages", pipeline_id=pid, name=name, position=i, win_pct=pct)
    return pid


def pipelines_with_stages() -> list[dict]:
    ensure_default()
    out = db.all("SELECT * FROM pipelines ORDER BY id")
    for p in out:
        p["stages"] = db.all("SELECT * FROM pipeline_stages WHERE pipeline_id = ? ORDER BY position", p["id"])
    return out


def create_deal(contact_id: int, title: str, pipeline_id: int | None = None, stage_id: int | None = None,
                value: float | None = None) -> int:
    pipeline_id = pipeline_id or ensure_default()
    if not stage_id:
        stage_id = db.one("SELECT id FROM pipeline_stages WHERE pipeline_id = ? ORDER BY position LIMIT 1", pipeline_id)["id"]
    did = db.insert("deals", pipeline_id=pipeline_id, stage_id=stage_id, contact_id=contact_id, title=title,
                    value=value, created_at=db.now(), updated_at=db.now())
    automations.fire("deal_created", contact_id, deal_id=did)
    return did


def move_deal(deal_id: int, stage_id: int) -> None:
    deal = db.one("SELECT * FROM deals WHERE id = ?", deal_id)
    stage = db.one("SELECT * FROM pipeline_stages WHERE id = ? AND pipeline_id = ?", stage_id, deal["pipeline_id"])
    if not stage:
        raise HTTPException(400, "Stage is not in this pipeline")
    status = "won" if stage["win_pct"] == 100 else "lost" if stage["name"].lower() == "lost" else "open"
    db.update("deals", deal_id, stage_id=stage_id, status=status, updated_at=db.now())
    if deal["contact_id"]:
        db.insert("activities", contact_id=deal["contact_id"], type="stage",
                  body=f"Deal \"{deal['title']}\" → {stage['name']}", created_at=db.now())
        automations.fire("deal_stage", deal["contact_id"], deal_id=deal_id, stage_id=stage_id)


@router.get("/crm/pipeline")
def board(request: Request, pipeline_id: int | None = None):
    pls = pipelines_with_stages()
    current = next((p for p in pls if p["id"] == pipeline_id), pls[0])
    deals = db.all("SELECT d.*, c.business_name, c.first_name FROM deals d LEFT JOIN contacts c ON c.id = d.contact_id "
                   "WHERE d.pipeline_id = ? ORDER BY d.updated_at DESC", current["id"])
    for s in current["stages"]:
        s["deals"] = [d for d in deals if d["stage_id"] == s["id"]]
        s["value"] = sum(d["value"] or 0 for d in s["deals"])
    weighted = sum((d["value"] or 0) * next(s["win_pct"] for s in current["stages"] if s["id"] == d["stage_id"]) / 100
                   for d in deals if d["status"] == "open")
    return render(request, "crm/pipeline.html", title="Pipeline", pipelines=pls, current=current,
                  weighted=round(weighted, 2),
                  contacts=db.all("SELECT id, business_name, first_name FROM contacts WHERE is_spam = 0 ORDER BY business_name"))


@router.post("/crm/deals")
def deal_new(contact_id: int = Form(...), title: str = Form(""), value: str = Form(""),
             pipeline_id: str = Form(""), back: str = Form("/crm/pipeline")):
    c = db.one("SELECT * FROM contacts WHERE id = ?", contact_id)
    create_deal(contact_id, title or (c["business_name"] or "Deal"), int(pipeline_id) if pipeline_id else None,
                value=float(value) if value else None)
    return redirect(safe_back(back, "/crm/pipeline"))


@router.post("/crm/deals/{deal_id}/move")
def deal_move(deal_id: int, stage_id: int = Form(...)):
    move_deal(deal_id, stage_id)
    return {"ok": True}


@router.post("/crm/pipelines")
def pipeline_new(name: str = Form(...), stages: str = Form("")):
    pid = db.insert("pipelines", name=name, created_at=db.now())
    lines = [s.strip() for s in stages.splitlines() if s.strip()] or [s for s, _ in DEFAULT_STAGES]
    for i, line in enumerate(lines):
        label, _, pct = line.partition("|")
        db.insert("pipeline_stages", pipeline_id=pid, name=label.strip(), position=i,
                  win_pct=int(pct) if pct.strip().isdigit() else 0)
    return redirect(f"/crm/pipeline?pipeline_id={pid}")


@router.post("/crm/stages/{stage_id}")
def stage_update(stage_id: int, name: str = Form(...), win_pct: int = Form(0)):
    s = db.one("SELECT * FROM pipeline_stages WHERE id = ?", stage_id)
    db.update("pipeline_stages", stage_id, name=name, win_pct=max(0, min(100, win_pct)))
    return redirect(f"/crm/pipeline?pipeline_id={s['pipeline_id']}")
