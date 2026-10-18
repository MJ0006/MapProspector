"""Make the screenshots for the in-app Guide (/guide) from a throwaway copy of the app on demo data.

    pip install playwright && python -m playwright install chromium   (dev-only; not needed to run the app)
    python scripts/guide_screenshots.py

Starts the app on port 8799 with an empty temporary data folder, walks through every feature with demo data
(no API keys, no real AI calls), and writes compressed WebP images to app/static/guide/.
Re-run it after UI changes so the Guide matches the app.
"""
import io
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "app" / "static" / "guide"
BRAIN_FX = ROOT / "vendor" / "local-seo-brain" / "tests" / "fixtures"
PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"
CHROMIUM = os.environ.get("CHROMIUM_PATH")  # optional: an already-installed Chromium


def save(page, name: str, *, full: bool = False, selector: str | None = None) -> None:
    png = page.locator(selector).screenshot() if selector else page.screenshot(full_page=full)
    im = Image.open(io.BytesIO(png)).convert("RGB")
    if im.width > 1200:
        im = im.resize((1200, round(im.height * 1200 / im.width)), Image.LANCZOS)
    if full and im.height > 2200:
        im = im.crop((0, 0, im.width, 2200))
    im.save(OUT / f"{name}.webp", "WEBP", quality=78, method=6)
    print("  saved", name)


def wait_port(port: int, timeout: float = 30) -> None:
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.3)
    raise SystemExit("app did not start")


def post_form(page, action: str, fields: dict) -> None:
    """Submit a form the way the page would (for steps that have no single obvious button)."""
    with page.expect_navigation():
        page.evaluate("""([action, fields]) => { const f = document.createElement('form'); f.method = 'post';
            f.action = action; for (const [k, v] of Object.entries(fields)) { const i = document.createElement('input');
            i.name = k; i.value = v; f.append(i); } document.body.append(f); f.submit(); }""", [action, fields])
    page.wait_for_load_state("networkidle")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    data = Path(tempfile.mkdtemp(prefix="mp-guide-"))
    env = {**os.environ, "DATA_DIR": str(data), "MAPPROSPECTOR_ENV_FILE": str(data / "none.env"),
           "PATH": os.environ.get("PATH", ""), "CLAUDE_PATH": str(data / "no-claude"), "CODEX_PATH": str(data / "x"),
           "GEMINI_PATH": str(data / "x"), "OLLAMA_HOST": "127.0.0.1:9"}
    srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)], cwd=ROOT, env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        wait_port(PORT)
        with sync_playwright() as p:
            browser = p.chromium.launch(**({"executable_path": CHROMIUM} if CHROMIUM else {}))
            page = browser.new_page(viewport={"width": 1280, "height": 820}, device_scale_factor=1)
            walk(page)
            browser.close()
    finally:
        srv.terminate()


def walk(page) -> None:
    print("prospecting")
    page.goto(BASE + "/")
    save(page, "dashboard")
    page.goto(BASE + "/leads/search?niche=roofer&area=Denver&lat=39.7392&lng=-104.9903")
    page.wait_for_selector("text=Save lead")
    save(page, "find-leads")
    with page.expect_navigation():
        page.locator("button:has-text('Save + heat map')").first.click()
    page.fill("textarea[name=keywords]", "roofer")
    page.select_option("#size", "7")
    page.wait_for_timeout(1500)
    save(page, "new-scan")
    with page.expect_navigation():
        page.click("button:has-text('Run scan')")
    for _ in range(40):
        page.wait_for_load_state("networkidle")
        if page.locator("#progress").count() == 0:
            break
        page.wait_for_timeout(1000)
    page.wait_for_selector(".rank-pin", timeout=20000)
    page.wait_for_timeout(1500)  # map tiles
    save(page, "heatmap")
    page.locator("#complist button").nth(0).click()
    page.wait_for_timeout(800)
    save(page, "heatmap-flip")
    scan_url = page.url
    page.click("button:has-text('Share link')")
    share = page.locator("#share a").get_attribute("href")
    page.goto(BASE + share)
    page.wait_for_selector(".rank-pin")
    page.wait_for_timeout(1200)
    save(page, "share-report")
    page.goto(BASE + "/rank")
    save(page, "rank-tracker")
    for path, name in [("/aivis", "ai-visibility"), ("/citations", "citation-audit"), ("/tools", "content-tools")]:
        page.goto(BASE + path)
        save(page, name)

    print("crm")
    page.goto(BASE + "/crm/contacts")
    save(page, "contacts")
    page.locator("table a").first.click()
    page.fill("input[name=title]", "Local SEO retainer")
    page.fill("input[name=value]", "900")
    page.click("button:has-text('Add deal')")
    page.wait_for_load_state("networkidle")
    save(page, "contact")
    contact_url = page.url
    page.goto(BASE + "/crm/pipeline")
    save(page, "pipeline")
    post_form(page, "/crm/tasks", {"title": "Call about the audit", "due_at": "2026-10-20T10:00"})
    page.goto(BASE + "/crm/tasks")
    save(page, "tasks")
    page.goto(contact_url)
    post_form(page, contact_url, {"business_name": "Summit Roofer (demo)", "email": "owner@summit-roofer.example"})
    post_form(page, contact_url + "/message", {"channel": "email", "subject": "Quick idea for your Google listing",
                                                "body": "Hi, I ran a free local ranking check for your business..."})
    page.goto(BASE + "/crm/outbox")
    save(page, "outbox")
    page.goto(BASE + "/crm/automations/0")
    page.select_option("#newaction", "send_email")
    page.click("button:has-text('+ Add action')")
    page.select_option("#newaction", "wait")
    page.click("button:has-text('+ Add action')")
    save(page, "automations")
    page.goto(BASE + "/crm/forms")
    save(page, "forms")
    page.goto(BASE + "/crm/questionnaires")
    save(page, "questionnaires")

    print("clients")
    page.goto(contact_url)
    with page.expect_navigation():
        page.click("button:has-text('Convert to client')")
    client_url = page.url
    for kind, f in [("gbp", "sample-gbp.csv"), ("reviews", "sample-reviews.json"), ("citations", "sample-citations.csv")]:
        page.select_option("select[name=kind]", kind)
        page.set_input_files("input[type=file][name=file]", str(BRAIN_FX / f))
        with page.expect_navigation():
            page.click("button:has-text('Upload to vault')")
    with page.expect_navigation():
        page.click("button:has-text('Update plan')")
    save(page, "client-plan")
    page.goto(BASE + "/clients")
    save(page, "clients")
    for tab in ["posts", "reviews", "photos", "lock", "insights", "optimize", "citations", "reports"]:
        page.goto(f"{client_url}/{tab}")
        save(page, f"client-{tab}")
    page.fill("input[name=recipients]", "owner@example.com")
    with page.expect_navigation():
        page.click("button:has-text('Create')")
    with page.expect_navigation():
        page.click("button:has-text('Build now')")
    save(page, "report-run")

    print("system")
    page.goto(BASE + "/ask")
    save(page, "ask")
    page.goto(BASE + "/journal")
    save(page, "journal")
    page.goto(BASE + "/settings")
    save(page, "settings")
    save(page, "settings-ai", selector="#ai")
    page.goto(scan_url)
    page.wait_for_selector(".rank-pin")
    page.click("button:has-text('Theme')")
    page.wait_for_timeout(1200)
    save(page, "heatmap-dark")


if __name__ == "__main__":
    main()
