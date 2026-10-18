"""Bridge to the vendored local-seo-brain (vendor/local-seo-brain, unmodified).

Two jobs:
1. Run the brain's own scripts for client vaults (scaffold, ingest, synthesize, report).
2. Turn the brain's prompt notes and doctrine into prompts for `claude -p`, always
   carrying the brain's refusal rules so drafts stay inside Google's policies.
"""
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

from app import settings

WIKI = settings.BRAIN_DIR / "assets" / "template-brain" / "wiki"
CANON = settings.BRAIN_DIR / "references" / "canon"
WINSHIM = Path(__file__).with_name("winshim")

# Condensed from vendor/local-seo-brain/docs/PRODUCT_BOUNDARIES.md (Refusals) and README.
GUARDRAILS = """Rules (from Local SEO Brain, follow strictly):
- Use only facts supplied below. Never invent prices, awards, years, reviews, stats, quotes or claims of superiority. Mark anything missing as NEEDS INPUT.
- Use the real business name. Never stuff keywords into the name.
- No fake, incentivized, filtered or gated reviews. Never suggest asking only happy customers.
- No address gaming, virtual offices, doorway pages, hidden text, link schemes or mass low-quality directory submissions.
- Never promise rankings, a #1 position, or that an AI assistant will cite the business.
- Do not claim a fixed posting frequency or reply time is required by Google; none is documented.
- Do not claim photo geotagging (EXIF GPS) improves rankings; no evidence supports it.
- Output is a DRAFT for the owner to review and approve before anything is published."""

SCRIPTS = {
    "gbp": "ingest_gbp.py",
    "reviews": "ingest_reviews.py",
    "citations": "ingest_citations.py",
    "geogrid": "ingest_geogrid.py",
    "source": "ingest_source.py",
}


class BrainError(RuntimeError):
    pass


def _today() -> str:
    return date.today().isoformat()


def _run(script: str, *args: str, ok: tuple[int, ...] = (0,)) -> str:
    env = None
    if os.name == "nt":  # the brain is written for macOS/Linux; app/winshim fills the gaps on Windows
        # PYTHONUTF8: the brain reads its notes with open()/read_text() defaults, which are cp1252 on Windows
        env = {**os.environ, "PYTHONUTF8": "1",
               "PYTHONPATH": os.pathsep.join(filter(None, [str(WINSHIM), os.environ.get("PYTHONPATH")]))}
    try:
        proc = subprocess.run([sys.executable, str(settings.BRAIN_DIR / "scripts" / script), *args],
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              cwd=settings.BRAIN_DIR, timeout=300, env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise BrainError(f"{script} did not finish within 5 minutes")
    except OSError as exc:  # e.g. vendor/local-seo-brain missing (clone without --recurse-submodules)
        raise BrainError(f"could not run {script}: {exc}")
    if proc.returncode not in ok:
        raise BrainError((proc.stderr or proc.stdout).strip()[-800:])
    return proc.stdout.strip()


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:60] or "client"


# --- client vaults ------------------------------------------------------------------

def vault_path(slug: str) -> Path:
    return settings.VAULT_DIR / slug


def create_vault(slug: str, owner: str) -> Path:
    path = vault_path(slug)
    if not path.exists():
        settings.ensure_dirs()
        _run("scaffold_vault.py", "--client", slug, "--owner", owner,
             "--out-dir", str(settings.VAULT_DIR), "--as-of", _today())
    return path


def ingest(kind: str, slug: str, file: Path, label: str | None = None) -> str:
    if kind not in SCRIPTS:
        raise BrainError(f"Unknown evidence type: {kind}")
    args = ["--vault", str(vault_path(slug)), "--client", slug, "--file", str(file), "--as-of", _today()]
    if kind == "source" and label:
        args += ["--label", label]
    return _run(SCRIPTS[kind], *args)


def synthesize(slug: str) -> str:
    return _run("synthesize_brain.py", "--vault", str(vault_path(slug)), "--as-of", _today())


def render_report(slug: str) -> Path:
    _run("render_brain_report.py", "--vault", str(vault_path(slug)))
    return vault_path(slug) / f"{slug}-weekly-report.html"


def deliverable(slug: str, name: str) -> str | None:
    """Read 'Health Scorecard' or 'Action Roadmap' markdown from a client vault."""
    path = vault_path(slug) / "wiki" / "deliverables" / f"{name}.md"
    return strip_frontmatter(path.read_text(encoding="utf-8")) if path.exists() else None


def vault_sources(slug: str) -> list[str]:
    folder = vault_path(slug) / "wiki" / "sources"
    if not folder.exists():
        return []
    return sorted(p.stem for p in folder.glob("*.md") if not p.name.startswith("_"))


# --- Brain panel: next action, vault health, evidence age, relationship map -----------------

EVIDENCE_MAX_DAYS = 30  # MapProspector's reminder to re-export client data monthly (not a Brain or Google rule)
SOURCE_KINDS = {"gbp": "Google Business Profile export", "reviews": "Reviews export",
                "citations": "Citations export", "geogrid": "Geo-grid scan"}


def _plain(text: str) -> str:
    """Obsidian [[Target|Label]] links as plain words."""
    return re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", text)


def next_action(slug: str) -> dict:
    """guide_next_action.py: the vault's next step, or the notes past their review date (which it puts first)."""
    if not (vault_path(slug) / "wiki" / "hot.md").exists():
        raise BrainError(f"The vault folder is missing or incomplete: {vault_path(slug)}")
    out = _run("guide_next_action.py", "--vault", str(vault_path(slug)), "--as-of", _today(), ok=(0, 1))
    if not out.startswith("Next:"):  # exit 1 is also used for errors, which go to stderr
        raise BrainError("guide_next_action.py could not read this vault's evidence metadata")
    out = out.removeprefix("Next:").strip()
    due = []
    m = re.match(r"refresh evidence before client recommendations:\s*(.*)", out, re.S)
    if m:
        due = [d.strip() for d in m.group(1).split(",") if d.strip()]
        out = ""
    # The vault template's first steps tell an operator to run the brain's scripts; this panel already does that.
    steps = [_plain(re.sub(r"^\d+\.\s*", "", line)).strip() for line in out.splitlines()]
    steps = [s for s in steps if s and ".py" not in s]
    return {"steps": steps, "due": due}


def lint(slug: str) -> dict:
    """lint_vault.py: frontmatter, links and graph checks for the client's vault."""
    try:
        out = _run("lint_vault.py", "--vault", str(vault_path(slug)))
        return {"ok": True, "lines": [l for l in out.splitlines() if l.strip()]}
    except BrainError as exc:
        if "Traceback" in str(exc):  # the script crashed: that's not a finding about the vault
            raise
        return {"ok": False, "lines": [l for l in str(exc).splitlines() if l.strip()][-40:]}


def relationship_map(slug: str) -> Path:
    """generate_vault_visuals.py: the vault's relationship map (SVG), created on first use."""
    path = vault_path(slug) / "_attachments" / "brain-relationship-map.svg"
    if not (vault_path(slug) / "wiki").is_dir():
        raise BrainError(f"The vault folder is missing or incomplete: {vault_path(slug)}")
    if not path.exists():
        _run("generate_vault_visuals.py", "--vault", str(vault_path(slug)))
    return path


def evidence_age(slug: str) -> list[dict]:
    """The newest import of each evidence type, with its age in days (from the source note's created date)."""
    newest: dict[str, dict] = {}
    for p in (vault_path(slug) / "wiki" / "sources").glob("*.md"):
        kind = next((k for k in SOURCE_KINDS if p.name.startswith(f"{k}-")), None)
        m = re.search(r'^created:\s*"?(\d{4}-\d{2}-\d{2})', p.read_text(encoding="utf-8", errors="replace"), re.M)
        if kind and m and (kind not in newest or m.group(1) > newest[kind]["date"]):
            newest[kind] = {"date": m.group(1)}
    out = []
    for kind, label in SOURCE_KINDS.items():
        row = {"kind": kind, "label": label, "date": None, "days": None, "stale": False}
        if kind in newest:
            row["date"] = newest[kind]["date"]
            row["days"] = (date.fromisoformat(_today()) - date.fromisoformat(row["date"])).days
            row["stale"] = row["days"] > EVIDENCE_MAX_DAYS
        out.append(row)
    return out


# --- knowledge ----------------------------------------------------------------------

def strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:].lstrip()
    return text


def _sections(body: str) -> dict[str, str]:
    out, current = {}, None
    for line in body.splitlines():
        m = re.match(r"^##\s+(.*)", line)
        if m:
            current = m.group(1).strip().lower()
            out[current] = ""
        elif current:
            out[current] += line + "\n"
    return {k: v.strip() for k, v in out.items()}


def prompt_note(name: str) -> dict:
    """Parse wiki/prompts/<name>.md into its task, inputs and acceptance criteria."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name or ""):
        raise BrainError(f"Unknown prompt: {name}")
    path = WIKI / "prompts" / f"{name}.md"
    if not path.exists():
        raise BrainError(f"Brain prompt not found: {name}")
    raw = path.read_text(encoding="utf-8")
    title = re.search(r'^title:\s*"?(.*?)"?\s*$', raw, re.M)
    conf = re.search(r'^confidence:\s*"?(.*?)"?\s*$', raw, re.M)
    sec = _sections(strip_frontmatter(raw))
    return {
        "name": name,
        "title": title.group(1) if title else name,
        "confidence": conf.group(1) if conf else "",
        "task": sec.get("task", ""),
        "inputs": sec.get("required inputs", ""),
        "output": sec.get("output and acceptance", ""),
    }


def list_prompts() -> list[dict]:
    return [prompt_note(p.stem) for p in sorted((WIKI / "prompts").glob("*.md")) if not p.name.startswith("_")]


def chapter(rel: str, limit: int = 4000) -> str:
    """Body text of a wiki chapter, e.g. 'gbp/gbp-posts'."""
    path = WIKI / f"{rel}.md"
    return strip_frontmatter(path.read_text(encoding="utf-8"))[:limit] if path.exists() else ""


def doctrine(number: int, limit: int = 3000) -> str:
    """Current-doctrine note from references/canon (001 GBP, 002 content/spam, 003 local/structured/AI)."""
    for p in CANON.glob(f"{number:03d}-*.md"):
        return strip_frontmatter(p.read_text(encoding="utf-8"))[:limit]
    return ""


def build_prompt(note: str, facts: dict, *, extra: str = "", doctrine_no: int | None = None) -> str:
    """Compose a prompt from a brain prompt note plus the business facts we hold."""
    p = prompt_note(note)
    fact_lines = "\n".join(f"- {k}: {v}" for k, v in facts.items() if v not in (None, "", []))
    parts = [
        f"You are drafting for a local business. Task from the Local SEO Brain prompt \"{p['title']}\":",
        p["task"],
        f"Required inputs: {p['inputs']}",
        f"Acceptance: {p['output']}",
        "Facts supplied (the only facts you may use):\n" + (fact_lines or "- none"),
        GUARDRAILS,
    ]
    if doctrine_no:
        parts.append("Current Google doctrine excerpt:\n" + doctrine(doctrine_no, 2000))
    if extra:
        parts.append(extra)
    return "\n\n".join(parts)
