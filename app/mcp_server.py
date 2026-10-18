"""MapProspector MCP server (F3): lets Claude Code / Claude Desktop read your data and start scans.

Run from the repo root:   python3 -m app.mcp_server
Claude Code picks it up from .mcp.json in this repo (or: claude mcp add mapprospector -- python3 -m app.mcp_server).
Speaks MCP over stdio (newline-delimited JSON-RPC 2.0). Read tools are side-effect free; start_scan
and create_task change data, and start_scan spends API credit with your rank provider.
"""
import json
import os
import sys
import traceback

from app import db

PROTOCOL = "2024-11-05"


def _scan_summary(s: dict) -> dict:
    st = db.loads(s.get("stats"), {}) or {}
    return {"id": s["id"], "keyword": s["keyword"], "surface": s["surface"], "provider": s["provider"],
            "status": s["status"], "date": (s.get("completed_at") or s.get("created_at") or "")[:10],
            "avg_rank_all_points": st.get("avg_rank_all"), "avg_rank_where_found": st.get("avg_rank"),
            "top3_pct": st.get("top3_pct"), "points": st.get("points"), "quadrants": st.get("quadrants")}


def list_clients() -> list[dict]:
    return db.all("SELECT c.id, c.name, c.website, c.phone, c.place_id, p.rating, p.review_count FROM companies c "
                  "LEFT JOIN places p ON p.id = c.place_id WHERE c.is_client = 1 ORDER BY c.name")


def list_leads(search: str = "", status: str = "", limit: int = 50) -> list[dict]:
    sql = ("SELECT id, business_name, first_name, last_name, email, phone, website, status, source, place_id, "
           "last_activity_at FROM contacts WHERE is_spam = 0")
    args: list = []
    if search:
        sql += " AND (business_name LIKE ? OR email LIKE ?)"
        args += [f"%{search}%"] * 2
    if status:
        sql += " AND status = ?"
        args.append(status)
    return db.all(sql + " ORDER BY last_activity_at DESC LIMIT ?", *args, min(int(limit), 200))


def list_scans(place_id: int | None = None, keyword: str = "", limit: int = 30) -> list[dict]:
    sql, args = "SELECT s.*, p.name business FROM scans s LEFT JOIN places p ON p.id = s.place_id WHERE 1 = 1", []
    if place_id:
        sql += " AND s.place_id = ?"
        args.append(int(place_id))
    if keyword:
        sql += " AND s.keyword LIKE ?"
        args.append(f"%{keyword}%")
    rows = db.all(sql + " ORDER BY s.id DESC LIMIT ?", *args, min(int(limit), 200))
    return [{**_scan_summary(s), "business": s["business"], "place_id": s["place_id"]} for s in rows]


def get_scan(scan_id: int, competitors: int = 10) -> dict:
    from app.modules.rank import engine
    s = db.one("SELECT * FROM scans WHERE id = ?", int(scan_id))
    if not s:
        raise ValueError("scan not found")
    comps = engine.competitors(s["id"]) if s["status"] in ("completed", "incomplete") else []
    top = comps[:int(competitors)]
    top += [c for c in comps[int(competitors):] if c["you"]]  # always include the tracked business
    return {**_scan_summary(s), "competitors": [
        {"name": c["business"]["name"], "you": c["you"], "avg_rank_all_points": c["avg_rank_all"],
         "top3_pct": c["top3_pct"], "share_of_top3": c["share_top3"], "quadrants": c["quadrants"],
         "rating": c["business"].get("rating"), "reviews": c["business"].get("review_count"),
         "category": c["business"].get("primary_category"), "position": c["position"]} for c in top]}


def compare_scans(scan_a: int, scan_b: int) -> dict:
    a, b = get_scan(scan_a, 50), get_scan(scan_b, 50)
    delta = None
    if a["avg_rank_all_points"] is not None and b["avg_rank_all_points"] is not None:
        delta = round(a["avg_rank_all_points"] - b["avg_rank_all_points"], 2)
    return {"a": _scan_summary(db.one("SELECT * FROM scans WHERE id = ?", int(scan_a))),
            "b": _scan_summary(db.one("SELECT * FROM scans WHERE id = ?", int(scan_b))),
            "avg_rank_improvement_from_a_to_b": delta,
            "note": "Positive improvement means scan b ranks better (lower average position)."}


def start_scan(place_id: int, keyword: str, provider: str = "demo", surface: str = "google_maps",
               grid_size: int = 7, spacing_miles: float = 1.0, shape: str = "square") -> dict:
    from app.modules.rank import engine, grid
    if provider not in engine.available_providers():
        raise ValueError(f"Provider {provider} is not configured. Available: {list(engine.available_providers())}")
    ids = engine.create_scans(int(place_id), [keyword], surface=surface, provider=provider, size=int(grid_size),
                              spacing_m=float(spacing_miles) * grid.MILE_M, shape=shape)
    return get_scan(ids[0])


def list_reviews(company_id: int, unreplied_only: bool = True, limit: int = 30) -> list[dict]:
    where = " AND reply_status IN ('none','draft')" if unreplied_only else ""
    return db.all(f"SELECT id, rating, text, review_date, reply_status, reply_text FROM reviews WHERE company_id = ?{where} "
                  "ORDER BY review_date DESC LIMIT ?", int(company_id), min(int(limit), 200))


def get_citations(company_id: int) -> dict:
    a = db.one("SELECT * FROM citation_audits WHERE company_id = ? ORDER BY id DESC LIMIT 1", int(company_id))
    if not a:
        return {"audit": None}
    rows = db.all("SELECT directory, status, listing_url, fields FROM citation_listings WHERE audit_id = ?", a["id"])
    return {"score": a["score"], "grade": a["grade"], "date": (a["completed_at"] or "")[:10],
            "listings": [{**r, "fields": db.loads(r["fields"], {})} for r in rows]}


def get_client_plan(company_id: int) -> dict:
    from app import brain
    co = db.one("SELECT * FROM companies WHERE id = ?", int(company_id))
    if not co or not co["vault_slug"]:
        raise ValueError("client not found")
    return {"client": co["name"], "status": "Draft",
            "health_scorecard": brain.deliverable(co["vault_slug"], "Health Scorecard"),
            "action_roadmap": brain.deliverable(co["vault_slug"], "Action Roadmap"),
            "sources": brain.vault_sources(co["vault_slug"])}


def get_ai_visibility(place_id: int) -> dict:
    r = db.one("SELECT report FROM aivis_runs WHERE place_id = ? AND status = 'completed' ORDER BY id DESC LIMIT 1", int(place_id))
    rep = db.loads(r["report"], {}) if r else {}
    return {k: rep.get(k) for k in ("score", "grade", "answers", "recommended", "mentioned", "competitors", "sources",
                                    "recommendations")} if rep else {"report": None}


def list_tasks(include_done: bool = False) -> list[dict]:
    return db.all("SELECT id, title, due_at, done, contact_id, company_id FROM tasks" + ("" if include_done else " WHERE done = 0")
                  + " ORDER BY due_at IS NULL, due_at LIMIT 100")


def create_task(title: str, due_at: str = "", contact_id: int | None = None, company_id: int | None = None) -> dict:
    tid = db.insert("tasks", title=title, due_at=db.to_utc(due_at), contact_id=contact_id, company_id=company_id,
                    created_at=db.now())
    return {"id": tid, "title": title}


def get_journal(area: str = "", kind: str = "", outcome: str = "", days: int = 14, limit: int = 100) -> dict:
    """Recent events + failure summary, for agents diagnosing or improving the app's workflows."""
    from app import journal
    sql, args = "SELECT id, created_at, area, kind, action, outcome, subject_type, subject_id, detail, data FROM events " \
                "WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)", [f"-{int(days)} days"]
    for col, val in (("area", area), ("kind", kind), ("outcome", outcome)):
        if val:
            sql += f" AND {col} = ?"
            args.append(val)
    rows = db.all(sql + " ORDER BY id DESC LIMIT ?", *args, min(int(limit), 500))
    for r in rows:
        r["data"] = db.loads(r["data"], {})
    return {"summary": journal.stats(int(days)), "events": rows}


def get_lessons(area: str = "") -> list[dict]:
    sql = "SELECT id, area, company_id, text, status, uses FROM lessons WHERE status != 'retired'"
    return db.all(sql + (" AND area = ?" if area else "") + " ORDER BY area, id", *([area] if area else []))


def find_business(name: str) -> list[dict]:
    return db.all("SELECT id AS place_id, name, address, rating, review_count, website FROM places WHERE name LIKE ? LIMIT 20",
                  f"%{name}%")


def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


S, I, N, B = {"type": "string"}, {"type": "integer"}, {"type": "number"}, {"type": "boolean"}
TOOLS = {
    "list_clients": (list_clients, "List clients with rating and review count.", _schema({})),
    "list_leads": (list_leads, "List CRM leads/contacts. Optional search text and status (new, contacted, replied, meeting, client, lost).",
                   _schema({"search": S, "status": S, "limit": I})),
    "find_business": (find_business, "Find saved businesses by name; returns place_id for scans.", _schema({"name": S}, ["name"])),
    "list_scans": (list_scans, "List geo-grid rank scans, optionally for one place_id or keyword.",
                   _schema({"place_id": I, "keyword": S, "limit": I})),
    "get_scan": (get_scan, "Scan detail: target's average rank, top-3 %, quadrant averages and competitors (who owns which side of town).",
                 _schema({"scan_id": I, "competitors": I}, ["scan_id"])),
    "compare_scans": (compare_scans, "Compare two scans (e.g. before/after) for the same keyword.",
                      _schema({"scan_a": I, "scan_b": I}, ["scan_a", "scan_b"])),
    "start_scan": (start_scan, "Run a new geo-grid scan now (spends API credit unless provider=demo). Providers: places, dataforseo, demo.",
                   _schema({"place_id": I, "keyword": S, "provider": S, "surface": S, "grid_size": I, "spacing_miles": N,
                            "shape": S}, ["place_id", "keyword"])),
    "list_reviews": (list_reviews, "Reviews for a client (company_id); unreplied only by default.",
                     _schema({"company_id": I, "unreplied_only": B, "limit": I}, ["company_id"])),
    "get_citations": (get_citations, "Latest citation audit for a client: score, grade and per-directory status.",
                      _schema({"company_id": I}, ["company_id"])),
    "get_ai_visibility": (get_ai_visibility, "Latest AI visibility report for a place_id.", _schema({"place_id": I}, ["place_id"])),
    "get_client_plan": (get_client_plan, "Local SEO Brain Draft health scorecard and roadmap for a client.",
                        _schema({"company_id": I}, ["company_id"])),
    "get_journal": (get_journal, "The app's journal: job runs, Claude calls, your decisions on AI drafts (unchanged/edited/rejected "
                                 "with both texts) and failures with error details. Use it to find what breaks and why.",
                    _schema({"area": S, "kind": S, "outcome": S, "days": I, "limit": I})),
    "get_lessons": (get_lessons, "Lessons learned from the owner's edits (proposed and active).", _schema({"area": S})),
    "list_tasks": (list_tasks, "Open tasks (or all with include_done).", _schema({"include_done": B})),
    "create_task": (create_task, "Create a task / reminder.", _schema({"title": S, "due_at": S, "contact_id": I, "company_id": I}, ["title"])),
}
READ_ONLY = [n for n in TOOLS if n not in ("start_scan", "create_task")]
# Set by the app when an AI platform connects (app/ai/mcp.py): only the read-only tools exist then.
if os.environ.get("MAPPROSPECTOR_MCP_READONLY") == "1":
    TOOLS = {n: TOOLS[n] for n in READ_ONLY}


def handle(msg: dict) -> dict | None:
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None  # notification
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "mapprospector", "version": "1.0.0"}}
    elif method == "tools/list":
        result = {"tools": [{"name": n, "description": d, "inputSchema": s} for n, (_, d, s) in TOOLS.items()]}
    elif method == "tools/call":
        name, args = msg["params"]["name"], msg["params"].get("arguments") or {}
        if name not in TOOLS:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"Unknown tool {name}"}}
        try:
            out = TOOLS[name][0](**args)
            result = {"content": [{"type": "text", "text": json.dumps(out, default=str, indent=1)}]}
        except Exception as exc:
            result = {"content": [{"type": "text", "text": f"Error: {exc}"}], "isError": True}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
    db.init()
    sys.stdin.reconfigure(encoding="utf-8")  # JSON-RPC is UTF-8; Windows would otherwise decode as cp1252
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            reply = handle(json.loads(line))
        except Exception:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": traceback.format_exc()[-300:]}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
