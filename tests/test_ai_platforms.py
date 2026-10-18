"""AI platforms and fallback, with fake `claude` / `codex` / `gemini` programs and a fake Ollama (no real AI calls)."""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from app import ai, db, notify, settings
from app.ai import base, ollama

FAKE = '''
import json, os, sys
from pathlib import Path
name, prompt = os.environ["FAKE_NAME"], sys.stdin.read()
log = Path(os.environ["FAKE_LOG"])
with log.open("a", encoding="utf-8") as f:
    f.write(json.dumps({"name": name, "args": sys.argv[1:], "prompt": prompt, "cwd": os.getcwd()}) + "\\n")
mode = os.environ.get("FAKE_MODE_" + name.upper(), "ok")
if name == "codex" and sys.argv[1:3] == ["login", "status"]:
    print("Logged in using ChatGPT"); sys.exit(0)
reply = name + " says hello"
if mode == "limit":
    print(os.environ.get("FAKE_LIMIT_TEXT", "usage limit reached"), file=sys.stderr); sys.exit(1)
if mode == "auth":
    print("ERROR: unexpected status 401 Unauthorized", file=sys.stderr); sys.exit(1)
if name == "gemini":
    if mode == "gemini-auth":
        print(json.dumps({"error": {"type": "Error", "message": "Please set an Auth method", "code": 41}})); sys.exit(41)
    print(json.dumps({"response": reply, "stats": {}})); sys.exit(0)
if name == "codex":
    out = sys.argv[sys.argv.index("-o") + 1]
    Path(out).write_text(reply, encoding="utf-8")
    print("[codex progress output]"); sys.exit(0)
print(reply)
'''


@pytest.fixture
def fakes(tmp_path, monkeypatch):
    """Install fake CLIs for every platform. Returns a reader for the call log."""
    script = tmp_path / "fake_cli.py"
    script.write_text(FAKE, encoding="utf-8")
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_LOG", str(log))
    for name in ("claude", "codex", "gemini"):
        if os.name == "nt":
            exe = tmp_path / f"{name}.cmd"
            exe.write_text(f'@set FAKE_NAME={name}\r\n@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        else:
            exe = tmp_path / name
            exe.write_text(f"#!/bin/sh\nFAKE_NAME={name} exec '{sys.executable}' '{script}' \"$@\"\n")
            exe.chmod(0o755)
        monkeypatch.setenv(f"{name.upper()}_PATH", str(exe))
    ai.set_order(["claude", "codex", "gemini", "ollama"])

    def calls():
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    return calls


def test_first_platform_answers_with_prompt_on_stdin(fakes):
    prompt = "Line one\nLine two " + "x" * 9000  # past cmd.exe's 8191-character limit
    assert ai.ask(prompt, purpose="posts") == "claude says hello"
    [call] = fakes()
    assert call["prompt"] == prompt and call["args"][:1] == ["-p"]
    assert ai.last_platform() == "claude"
    ev = db.one("SELECT * FROM events WHERE kind = 'ai_call'")
    assert ev["outcome"] == "ok" and json.loads(ev["data"])["platform"] == "claude"


def test_usage_limit_falls_back_and_pauses_until_reset(fakes, monkeypatch):
    reset = int(time.time()) + 2 * 3600
    monkeypatch.setenv("FAKE_MODE_CLAUDE", "limit")
    monkeypatch.setenv("FAKE_LIMIT_TEXT", f"Claude AI usage limit reached|{reset}")
    assert ai.ask("Draft a post", purpose="posts") == "codex says hello"
    assert [c["name"] for c in fakes()] == ["claude", "codex"]
    pause = ai.paused("claude")
    assert pause["kind"] == "limit" and datetime.fromisoformat(pause["until"]) == datetime.fromtimestamp(reset, timezone.utc)
    assert db.one("SELECT * FROM events WHERE action = 'fallback'")["detail"].startswith("ChatGPT answered after: Claude")
    texts = [n["text"] for n in notify.notices()]
    assert any(t.startswith("Claude usage limit reached: using ChatGPT until") for t in texts), texts

    monkeypatch.setenv("FAKE_MODE_CLAUDE", "ok")
    assert ai.ask("Another", purpose="posts") == "codex says hello"  # still paused: Claude isn't tried
    assert [c["name"] for c in fakes()] == ["claude", "codex", "codex"]
    ai.resume("claude")
    assert ai.ask("Again", purpose="posts") == "claude says hello"


def test_codex_command_is_read_only(fakes, monkeypatch):
    monkeypatch.setenv("FAKE_MODE_CLAUDE", "auth")
    assert ai.ask("Look this up", web=True, purpose="citations") == "codex says hello"
    codex = [c for c in fakes() if c["name"] == "codex"][0]
    args = codex["args"]
    assert args[0] == "exec" and args[args.index("-s") + 1] == "read-only" and args[-1] == "-"
    assert 'approval_policy="never"' in args and 'web_search="live"' in args and "--ephemeral" in args
    assert ai.paused("claude")["kind"] == "auth"
    assert not list(settings.DATA_DIR.glob("codex-reply-*"))  # reply file cleaned up


def test_gemini_json_and_sign_in_error(fakes, monkeypatch):
    monkeypatch.setenv("FAKE_MODE_CLAUDE", "limit")
    monkeypatch.setenv("FAKE_MODE_CODEX", "auth")
    assert ai.ask("Hi", purpose="reviews") == "gemini says hello"
    ai.resume("claude"), ai.resume("codex")
    monkeypatch.setenv("FAKE_MODE_GEMINI", "gemini-auth")
    with pytest.raises(ai.AIUnavailable, match="No AI platform could answer"):
        ai.ask("Hi", purpose="reviews")
    assert ai.paused("gemini")["kind"] == "auth"


def test_ask_with_app_data_uses_read_only_mcp(fakes, monkeypatch):
    from app.mcp_server import READ_ONLY
    monkeypatch.setenv("FAKE_MODE_CLAUDE", "limit")
    monkeypatch.setenv("FAKE_MODE_CODEX", "limit")
    assert ai.ask("Which client?", mcp=True, purpose="ask") == "gemini says hello"
    gem = [c for c in fakes() if c["name"] == "gemini"][0]
    assert gem["args"][gem["args"].index("--allowed-mcp-server-names") + 1] == "mapprospector"
    cfg = json.loads((Path(gem["cwd"]) / ".gemini" / "settings.json").read_text(encoding="utf-8"))
    srv = cfg["mcpServers"]["mapprospector"]
    assert srv["includeTools"] == READ_ONLY and srv["env"]["MAPPROSPECTOR_MCP_READONLY"] == "1"
    assert srv["env"]["DATA_DIR"] == str(settings.DATA_DIR)  # the server reads your database, not an empty one
    assert srv["command"] == sys.executable and srv["cwd"] == str(settings.ROOT)
    codex = [c for c in fakes() if c["name"] == "codex"][0]["args"]
    env_arg = next(a for a in codex if a.startswith("mcp_servers.mapprospector.env="))
    assert 'MAPPROSPECTOR_MCP_READONLY="1"' in env_arg and f"DATA_DIR={json.dumps(str(settings.DATA_DIR))}" in env_arg


def test_mcp_server_read_only_mode_hides_write_tools():
    env = {**os.environ, "MAPPROSPECTOR_MCP_READONLY": "1", "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    msgs = "\n".join(json.dumps(m) for m in [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "start_scan", "arguments": {}}}]) + "\n"
    out = subprocess.run([sys.executable, "-m", "app.mcp_server"], input=msgs, capture_output=True, text=True,
                         cwd=settings.ROOT, env=env, timeout=60).stdout.splitlines()
    tools = {t["name"] for t in json.loads(out[0])["result"]["tools"]}
    assert "list_clients" in tools and not tools & {"start_scan", "create_task"}
    assert "Unknown tool" in json.loads(out[1])["error"]["message"]


def _fake_ollama(monkeypatch, reply="local draft"):
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"role": "assistant", "content": reply}})
    monkeypatch.setattr(ollama, "models", lambda: ["llama3.1:8b", "qwen2.5:7b"])
    monkeypatch.setattr(ollama, "client", lambda timeout: httpx.Client(transport=httpx.MockTransport(handler)))
    return seen


def test_ollama_answers_drafts_but_never_web_tasks(monkeypatch):
    seen = _fake_ollama(monkeypatch)
    db.set_setting("ollama_model", "qwen2.5:7b")
    assert ai.available() and not ai.available(web=True) and not ai.available(mcp=True)
    assert ai.ask("Write a review reply", purpose="reviews") == "local draft"
    assert seen["body"]["model"] == "qwen2.5:7b" and seen["body"]["messages"][0]["content"] == "Write a review reply"
    with pytest.raises(ai.AIUnavailable, match="search the web"):
        ai.ask("Find listings", web=True)


def test_ai_visibility_claude_engine_never_falls_back(fakes, monkeypatch):
    from app.providers import ai_engines
    monkeypatch.setenv("FAKE_MODE_CLAUDE", "limit")
    with pytest.raises(ai.AIUnavailable):
        ai_engines.ask("claude", "Best plumber in Denver?")
    assert [c["name"] for c in fakes()] == ["claude"]


def test_retry_time_and_limit_text():
    now = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
    assert base.retry_time("try again in 2 hours 5 minutes", now) == now + timedelta(hours=2, minutes=5)
    assert base.retry_time("limit reached|1767225600", now) == datetime(2026, 1, 1, tzinfo=timezone.utc)
    at = base.retry_time("Your limit resets 3pm", now)
    assert at > now and at.astimezone().hour == 15
    assert base.retry_time("something broke", now) is None
    assert base.classify("429 Too Many Requests") == "limit" and base.classify("Not logged in") == "auth"
    assert base.classify("connection refused") == "error"


def test_limit_said_in_a_normal_reply_is_caught(fakes, monkeypatch, tmp_path):
    exe = tmp_path / ("claude.cmd" if os.name == "nt" else "claude")
    if os.name == "nt":
        exe.write_text("@echo You've hit your usage limit. Try again at 3pm.\r\n", encoding="utf-8")
    else:
        exe.write_text("#!/bin/sh\necho \"You've hit your usage limit. Try again at 3pm.\"\n")
        exe.chmod(0o755)
    monkeypatch.setenv("CLAUDE_PATH", str(exe))
    assert ai.ask("Hi") == "codex says hello"
    assert ai.paused("claude")["kind"] == "limit"


def test_draft_records_platform(client, fakes, monkeypatch):
    from app.modules.gbp import posts
    co = db.insert("companies", name="Acme Plumbing", is_client=1, created_at=db.now())
    monkeypatch.setenv("FAKE_MODE_CLAUDE", "limit")
    posts.draft(co, "Fall maintenance")
    ev = db.one("SELECT data FROM events WHERE action = 'draft'")
    assert json.loads(ev["data"])["platform"] == "codex"


def test_settings_card_order_toggle_and_sign_in(client, fakes, monkeypatch):
    from app.modules.ai_platforms import routes
    opened = {}
    monkeypatch.setattr(routes, "open_terminal", lambda cmd: opened.setdefault("cmd", cmd) and True)
    monkeypatch.setattr(ai.PLATFORMS["codex"], "signed_in", lambda: False)
    page = client.get("/settings").text
    assert "AI platforms" in page and "Local (Ollama)" in page and "not signed in" in page
    client.post("/settings/ai/gemini/move", data={"direction": "up"})
    assert ai.order() == ["claude", "gemini", "codex", "ollama"]
    client.post("/settings/ai/claude/toggle")
    assert ai.order() == ["gemini", "codex", "ollama"]
    r = client.post("/settings/ai/codex/signin", follow_redirects=False)
    assert r.status_code == 303 and opened["cmd"][1:] == ["login"] and opened["cmd"][0].endswith(("codex", "codex.cmd"))


def test_pages_enable_ai_buttons_for_any_ready_platform(client, monkeypatch):
    _fake_ollama(monkeypatch)  # only a local model: drafts yes, Ask and web lookups no
    co = db.insert("companies", name="Acme Plumbing", is_client=1, created_at=db.now())
    posts = client.get(f"/clients/{co}/posts").text
    assert 'disabled>Draft</button>' not in posts and "Needs an AI platform" not in posts
    ask = client.get("/ask").text
    assert "local models can't read app data" in ask and "disabled>Ask</button>" in ask
    assert "Automatic checks need an AI platform with web search" in client.get("/citations").text


def test_ai_visibility_check_asks_each_subscription_cli_itself(fakes):
    from app.modules.aivis import engine
    rid = db.insert("aivis_runs", business_name="Acme Plumbing", city="Austin", category="plumber", status="queue",
                    created_at=db.now())
    engine.run(rid)
    calls = [c for c in fakes() if c["args"][:2] != ["login", "status"]]
    assert sorted({c["name"] for c in calls}) == ["claude", "codex", "gemini"] and len(calls) == 15  # 5 questions each
    assert all('web_search="live"' in c["args"] for c in calls if c["name"] == "codex")
    report = db.loads(db.one("SELECT report FROM aivis_runs WHERE id = ?", rid)["report"])
    assert {e["name"] for e in report["engines"]} == {"Claude", "ChatGPT", "Gemini"}
