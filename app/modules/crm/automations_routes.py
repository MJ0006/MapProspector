import json

from fastapi import APIRouter, Form, HTTPException, Request

from app import db
from app.modules.crm import automations
from app.modules.crm.pipeline import pipelines_with_stages
from app.web import redirect, render

router = APIRouter()


@router.get("/crm/automations")
def automations_list(request: Request):
    rows = db.all("SELECT a.*, (SELECT COUNT(*) FROM automation_runs r WHERE r.automation_id = a.id) runs, "
                  "(SELECT COUNT(*) FROM automation_runs r WHERE r.automation_id = a.id AND r.status = 'waiting') waiting "
                  "FROM automations a ORDER BY a.id DESC")
    return render(request, "crm/automations.html", title="Automations", rows=rows, triggers=automations.TRIGGERS)


@router.get("/crm/automations/{aid}")
def automation_edit(request: Request, aid: int):
    a = db.one("SELECT * FROM automations WHERE id = ?", aid) if aid else None
    if aid and not a:
        raise HTTPException(404)
    runs = db.all("SELECT r.*, c.business_name, c.first_name FROM automation_runs r LEFT JOIN contacts c "
                  "ON c.id = r.contact_id WHERE r.automation_id = ? ORDER BY r.id DESC LIMIT 100", aid) if aid else []
    return render(request, "crm/automation_edit.html", title=a["name"] if a else "New automation", a=a, runs=runs,
                  triggers=automations.TRIGGERS, actions=automations.ACTIONS, fields=automations.CONTACT_FIELDS,
                  pipelines=pipelines_with_stages(),
                  questionnaires=db.all("SELECT id, name FROM questionnaires"),
                  companies=db.all("SELECT id, name FROM companies WHERE is_client = 1"))


@router.post("/crm/automations/{aid}")
def automation_save(aid: int, name: str = Form(...), trigger: str = Form(...), trigger_config: str = Form("{}"),
                    filters: str = Form("[]"), actions: str = Form("[]"), active: str = Form("")):
    if trigger not in automations.TRIGGERS:
        raise HTTPException(400, "Unknown trigger")
    try:
        cfg, flt, acts = json.loads(trigger_config or "{}"), json.loads(filters or "[]"), json.loads(actions or "[]")
    except ValueError:
        raise HTTPException(400, "Automation definition is not valid JSON")
    for act in acts:
        if act.get("type") not in automations.ACTIONS:
            raise HTTPException(400, f"Unknown action {act.get('type')}")
    cols = dict(name=name, trigger=trigger, trigger_config=cfg, filters=flt, actions=acts, active=1 if active else 0)
    if aid:
        db.update("automations", aid, **cols)
    else:
        aid = db.insert("automations", created_at=db.now(), **cols)
    return redirect(f"/crm/automations/{aid}")


@router.post("/crm/automations/{aid}/duplicate")
def automation_duplicate(aid: int):
    a = db.one("SELECT * FROM automations WHERE id = ?", aid)
    new = db.insert("automations", name=a["name"] + " (copy)", trigger=a["trigger"], trigger_config=a["trigger_config"],
                    filters=a["filters"], actions=a["actions"], active=0, created_at=db.now())
    return redirect(f"/crm/automations/{new}")


@router.post("/crm/automations/{aid}/delete")
def automation_delete(aid: int):
    db.run("DELETE FROM automations WHERE id = ?", aid)
    return redirect("/crm/automations")


@router.post("/crm/runs/{rid}/stop")
def run_stop(rid: int):
    r = db.one("SELECT * FROM automation_runs WHERE id = ?", rid)
    db.update("automation_runs", rid, status="stopped", updated_at=db.now())
    return redirect(f"/crm/automations/{r['automation_id']}")
