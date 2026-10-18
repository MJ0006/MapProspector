import json
import subprocess

from app import ai, db, mcp_server, places_store
from app.modules.rank import engine


def _client_with_scan(client):
    pid = places_store.upsert({"google_place_id": "ChIJr", "name": "Ridgeline Roofing", "lat": 39.74, "lng": -104.99})
    cid = db.insert("contacts", place_id=pid, business_name="Ridgeline Roofing", source="manual", status="new")
    from app.modules.clients.routes import convert
    co = convert(cid)
    engine.create_scans(pid, ["roofing"], surface="google_maps", provider="demo", size=3, spacing_m=1000)
    return co, pid


def test_report_build_approve_share(client):
    co, _ = _client_with_scan(client)
    client.post(f"/clients/{co}/reports", data={"name": "Monthly", "sections": ["rankings", "reviews", "activity"],
                                                "recipients": "owner@ridgeline.example", "schedule": "monthly"})
    rep = db.one("SELECT * FROM reports")
    assert rep["require_approval"] == 1 and rep["next_run_at"]
    r = client.post(f"/reports/{rep['id']}/build", data={"note": "Great month!"}, follow_redirects=True)
    assert r.status_code == 200
    run = db.one("SELECT * FROM report_runs")
    assert run["status"] == "pending_approval" and "Great month!" in run["html"] and '"roofing"' in run["html"]
    assert "Demo data" in run["html"]
    assert client.get(f"/share/report/{run['token']}").status_code == 404  # not visible before approval
    client.post(f"/reports/runs/{run['id']}/approve")
    assert db.one("SELECT status FROM report_runs")["status"] == "sent"
    m = db.one("SELECT * FROM outbox")
    assert m["to_addr"] == "owner@ridgeline.example" and f"/share/report/{run['token']}" in m["body"]
    assert client.get(f"/share/report/{run['token']}").status_code == 200
    assert client.get(f"/reports/runs/{run['id']}/html?download=1").headers["content-disposition"].startswith("attachment")
    for url in ("/reports", f"/clients/{co}/reports", f"/reports/runs/{run['id']}"):
        assert client.get(url).status_code == 200


def test_mcp_protocol_and_tools(client):
    co, pid = _client_with_scan(client)
    init = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["serverInfo"]["name"] == "mapprospector"
    assert mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = {t["name"] for t in mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
    assert {"get_scan", "start_scan", "list_clients", "get_client_plan"} <= names

    def call(name, **args):
        r = mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": name, "arguments": args}})
        return r["result"]
    assert json.loads(call("list_clients")["content"][0]["text"])[0]["name"] == "Ridgeline Roofing"
    scans = json.loads(call("list_scans", place_id=pid)["content"][0]["text"])
    detail = json.loads(call("get_scan", scan_id=scans[0]["id"])["content"][0]["text"])
    assert detail["competitors"] and any(c["you"] for c in detail["competitors"])
    new = json.loads(call("start_scan", place_id=pid, keyword="roof repair", grid_size=3)["content"][0]["text"])
    assert new["status"] == "completed" and new["provider"] == "demo"
    cmp_ = json.loads(call("compare_scans", scan_a=scans[0]["id"], scan_b=new["id"])["content"][0]["text"])
    assert "avg_rank_improvement_from_a_to_b" in cmp_
    assert call("start_scan", place_id=pid, keyword="x", provider="dataforseo")["isError"]  # not configured
    assert "start_scan" not in mcp_server.READ_ONLY and "get_scan" in mcp_server.READ_ONLY


def test_ask_uses_mcp_read_only(client, monkeypatch):
    seen = {}

    def fake(prompt, **kw):
        seen.update(kw)
        return "Summit Roofing owns the north-west."
    monkeypatch.setattr(ai, "ask", fake)
    r = client.post("/ask", data={"question": "Who owns the north side?"})
    assert "Summit Roofing owns" in r.text
    assert seen["mcp"] is True and "mcp__mapprospector__get_scan" in seen["tools"]
    assert not any("start_scan" in t for t in seen["tools"])


def test_ai_ask_command_line(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda n, *a, **k: "/usr/bin/claude")
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"], captured["cwd"] = cmd, kw.get("cwd")
        return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert ai.ask("hi", mcp=True, tools=["mcp__mapprospector__list_clients"], web=True) == "ok"
    cmd = captured["cmd"]
    assert cmd[:3] == ["claude", "-p", "hi"] and "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--allowedTools") + 1] == "mcp__mapprospector__list_clients,WebSearch,WebFetch"
    assert str(captured["cwd"]).endswith("mapprospector")
