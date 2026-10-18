"""The in-app Guide covers every key and page, and every screenshot it shows exists."""
import re

from app import settings
from app.modules.guide.routes import KEYS
from app.web import NAV


def test_guide_renders_with_setup_checklist(client):
    page = client.get("/guide").text
    assert "Your setup" in page and "API keys, click by click" in page
    assert "An AI platform for drafts" in page and "none ready yet" in page


def test_every_env_key_is_explained():
    documented = {k for g in KEYS for k in g["keys"] + g.get("optional_keys", [])}
    example = re.findall(r"^([A-Z_]+)=", (settings.ROOT / ".env.example").read_text(encoding="utf-8"), re.M)
    assert set(example) <= documented, set(example) - documented


def test_every_page_in_the_menu_is_covered(client):
    page = client.get("/guide").text
    for _, items in NAV:
        for href, label in items:
            assert f'href="{href}"' in page, label


def test_every_screenshot_exists(client):
    page = client.get("/guide").text
    shots = set(re.findall(r'src="/static/guide/([\w-]+\.webp)"', page))
    assert len(shots) >= 30
    for name in shots:
        assert (settings.ROOT / "app" / "static" / "guide" / name).is_file(), name
        assert client.get(f"/static/guide/{name}").status_code == 200


def test_dashboard_points_new_users_to_the_guide(client):
    assert 'href="/guide"' in client.get("/").text
