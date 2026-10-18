"""AI visibility checker (A6): ask answer engines the questions local customers ask, and record
whether the business is recommended (top 3), mentioned lower down, or absent; which competitors
are named instead; and which sources the answers cite.

Score (shown with its formula): 100 × (recommended + 0.5 × mentioned) ÷ answers.
AI answers change from run to run, so a report is a snapshot. No one can promise an AI will
recommend a business (local-seo-brain doctrine 003).
"""
import re
from collections import Counter, defaultdict
from urllib.parse import urlparse

from app import ai, db, jobs, places_store, settings
from app.modules.rank import grid
from app.providers import ai_engines, dataforseo

ANSWER_FORMAT = ('\n\nAfter your answer, add one final line containing only JSON in this form: '
                 '{"businesses": [{"name": "Business Name", "reason": "why you recommend it"}]} '
                 'listing the businesses you recommended, best first.')
DIRECTORY_HINTS = ("yelp", "bbb.org", "angi", "homeadvisor", "thumbtack", "yellowpages", "facebook", "nextdoor",
                   "tripadvisor", "reddit", "google.", "mapquest", "houzz", "manta", "bing.", "apple.")



def safe_error(exc: Exception) -> str:
    """Error text for the report (shown on share pages): no URLs or query strings, which can carry API keys."""
    text = re.sub(r"https?://\S+", "[url]", str(exc))
    text = re.sub(r"(?i)(key|token|secret|password)=[^\s&'\"]+", r"\1=[hidden]", text)
    return text[:200]

def questions(category: str, city: str) -> list[str]:
    c, where = category.strip().lower(), city.strip()
    return [
        f"What are the best {c} businesses in {where}?",
        f"Who should I call for {c} in {where}?",
        f"Recommend a highly rated {c} near {where}.",
        f"I need a reliable {c} in {where}. Which companies do locals trust?",
        f"Which {c} in {where} has the best reviews?",
    ]


def _core(name: str) -> str:
    n = re.sub(r"\b(llc|inc|co|corp|company|ltd|the|and|&)\b", " ", (name or "").lower())
    return re.sub(r"[^a-z0-9]", "", n)


def same_business(a: str, b: str) -> bool:
    ca, cb = _core(a), _core(b)
    if not (ca and cb):
        return False
    if ca == cb:
        return True
    # "Acme Plumbing" in "Acme Plumbing & Drain" is the same business; a generic "The Plumber" in "Joe the Plumber"
    # isn't: the shorter name must be specific (2+ words after dropping "the/llc/…", or 10+ letters).
    shorter = a if len(ca) <= len(cb) else b
    specific = len(re.findall(r"[a-z0-9]+", re.sub(r"\b(llc|inc|co|corp|company|ltd|the|and)\b", " ", shorter.lower()))) >= 2
    return (specific or min(len(ca), len(cb)) >= 10) and (ca in cb or cb in ca)


def parse_answer(text: str) -> list[dict]:
    """Ordered businesses from an answer: the trailing JSON line, else numbered/bold list items."""
    for line in reversed([l for l in text.strip().splitlines() if '"businesses"' in l]):
        try:
            data = ai.extract_json(line[line.index("{"):] if "{" in line else line)
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("businesses"), list):
            return [{"name": str(b.get("name") or "").strip(), "reason": str(b.get("reason") or "").strip()}
                    for b in data["businesses"] if isinstance(b, dict) and str(b.get("name") or "").strip()]
    out = []
    for m in re.finditer(r"^\s*(?:\d+[.)]|[-*•])\s*\**([^*:\n]{3,80})", text, re.M):
        name = re.split(r"\s+[-–—]\s+", m.group(1))[0].strip(" .*")
        if not name or "://" in name or "](" in name or name.lower().startswith(("http", "www.", "[")):
            continue  # a source link, not a business
        if not any(same_business(name, o["name"]) for o in out):
            out.append({"name": name, "reason": ""})
    return out


def domain(url: str) -> str:
    try:
        host = urlparse(url if "//" in url else "https://" + url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def presence(target: str, businesses: list[dict]) -> tuple[str, int | None]:
    for i, b in enumerate(businesses, 1):
        if same_business(target, b["name"]):
            return ("recommended" if i <= 3 else "mentioned"), i
    return "absent", None


def grade(score: int) -> str:
    return "A" if score >= 80 else "B" if score >= 60 else "C" if score >= 40 else "D" if score >= 20 else "F"


def build_report(business: dict, rows: list[dict], aio_grid: dict | None = None) -> dict:
    """Aggregate per-question rows: [{engine, prompt, businesses, sources, text}]."""
    name, site = business["name"], domain(business.get("website") or "")
    prompts, engines, mentions, wins, reasons, src = [], {}, Counter(), Counter(), [], Counter()
    per_engine_top: dict[str, Counter] = defaultdict(Counter)
    per_engine_wins: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        if r.get("error"):
            prompts.append({**r, "presence": "error", "position": None, "cited": False})
            continue
        status, pos = presence(name, r["businesses"])
        doms = {domain(u) for u in r["sources"] if u}
        cited = bool(site) and any(d == site or d.endswith("." + site) for d in doms)
        prompts.append({"engine": r["engine"], "prompt": r["prompt"], "presence": status, "position": pos,
                        "cited": cited, "excerpt": r["text"][:600]})
        e = engines.setdefault(r["engine"], {"engine": r["engine"], "name": ai_engines.ENGINES[r["engine"]],
                                             "prompts": 0, "appearances": 0, "recommended": 0, "cited": 0,
                                             "you_best": None})
        e["prompts"] += 1
        e["appearances"] += status != "absent"
        e["recommended"] += status == "recommended"
        e["cited"] += cited
        if pos:
            e["you_best"] = pos if e["you_best"] is None else min(e["you_best"], pos)
        for i, b in enumerate(r["businesses"], 1):
            key = name if same_business(name, b["name"]) else b["name"]
            mentions[key] += 1
            per_engine_top[r["engine"]][key] += 1
            if i == 1:
                wins[key] += 1
                per_engine_wins[r["engine"]][key] += 1
            if b.get("reason") and key != name and len(reasons) < 12:
                reasons.append({"name": b["name"], "engine": ai_engines.ENGINES[r["engine"]], "quote": b["reason"]})
        for d in doms:
            src[d] += 1
    for eng, counter in per_engine_top.items():
        engines[eng]["top"] = [{"name": n, "mentions": c, "wins": per_engine_wins[eng][n]} for n, c in counter.most_common(5)]
    total_mentions = sum(mentions.values()) or 1
    competitors = [{"name": n, "share": round(100 * c / total_mentions, 1), "wins": wins[n], "you": n == name}
                   for n, c in mentions.most_common(10)]
    if not any(c["you"] for c in competitors):
        competitors.append({"name": name, "share": 0.0, "wins": 0, "you": True})
    total_src = sum(src.values()) or 1
    sources = [{"name": d, "share": round(100 * c / total_src, 1), "is_you": bool(site) and d == site,
                "directory": any(h in d for h in DIRECTORY_HINTS)} for d, c in src.most_common(12)]
    answered = [p for p in prompts if p["presence"] != "error"]
    rec = sum(p["presence"] == "recommended" for p in answered)
    men = sum(p["presence"] == "mentioned" for p in answered)
    score = round(100 * (rec + 0.5 * men) / len(answered)) if answered else 0
    report = {
        "business": {"name": name, "city": business.get("city"), "category": business.get("category"),
                     "website": business.get("website")},
        "score": score, "grade": grade(score), "answers": len(answered), "recommended": rec, "mentioned": men,
        "engines": list(engines.values()), "prompts": prompts, "competitors": competitors,
        "sources": sources, "reasons": reasons, "aio_grid": aio_grid,
    }
    report["recommendations"] = recommendations(report)
    return report


def recommendations(r: dict) -> list[dict]:
    """Next steps tied to local-seo-brain chapters. Advice, not promises."""
    recs = []
    dir_share = sum(s["share"] for s in r["sources"] if s["directory"])
    if dir_share >= 40:
        recs.append({"title": "Make your directory listings complete and consistent",
                     "body": f"{round(dir_share)}% of the sources these answers cited were directories and review sites. "
                             "Name, address, phone, hours and categories should match your Google profile everywhere.",
                     "source": "backlinks/backlinking-local-citations"})
    if not any(s["is_you"] for s in r["sources"]):
        recs.append({"title": "Give AI answers a page on your site worth citing",
                     "body": "Your website was not cited. Clear service pages that answer customers' real questions, "
                             "with your name, phone and service area stated plainly, are what you can control.",
                     "source": "llm-seo/llm-seo"})
    if r["recommended"] == 0:
        top = next((c for c in r["competitors"] if not c["you"]), None)
        recs.append({"title": "Close the review and profile gap with the businesses being named",
                     "body": (f"{top['name']} was named most often. " if top else "")
                             + "Compare your review count, categories and profile completeness with theirs; ask every "
                               "customer for a review and reply to all of them.",
                     "source": "reviews/_index"})
    recs.append({"title": "Re-check monthly",
                 "body": "Answers vary run to run. Track the pattern across engines and questions, not one result.",
                 "source": "freshness/2026-ai-overviews-state"})
    return recs


def run_aio_grid(keyword: str, name: str, lat: float, lng: float, spacing_m: float = 1609) -> dict:
    pts = grid.generate(lat, lng, 5, spacing_m)
    pins = []
    for p in pts:
        try:
            aio = dataforseo.ai_overview(keyword, p["lat"], p["lng"])
        except Exception:
            pins.append({**p, "status": "error", "position": None})
            continue
        if not aio["present"]:
            pins.append({**p, "status": "none", "position": None})
            continue
        found = parse_answer(aio["text"])
        status, pos = presence(name, found)
        if status == "absent" and _core(name) and _core(name) in _core(aio["text"]):
            status = "mentioned"
        pins.append({**p, "status": status, "position": pos})
    return {"size": 5, "spacing_miles": round(spacing_m / grid.MILE_M, 2), "keyword": keyword, "pins": pins}


def run(run_id: int) -> None:
    if not db.claim("aivis_runs", run_id, ("queue",), "in_progress"):
        return
    try:
        _run(run_id)
    except Exception as exc:
        db.update("aivis_runs", run_id, status="failed", error=f"Check stopped: {exc}"[:500])
        raise


def _run(run_id: int) -> None:
    row = db.one("SELECT * FROM aivis_runs WHERE id = ?", run_id)
    place = places_store.get(row["place_id"]) if row["place_id"] else None
    business = {"name": row["business_name"], "city": row["city"], "category": row["category"],
                "website": place.get("website") if place else None}
    engines = ai_engines.available()
    if not engines:
        db.update("aivis_runs", run_id, status="failed",
                  error="No answer engine available. Install the claude CLI or add an AI API key.")
        return
    rows = []
    for engine in engines:
        for q in questions(row["category"], row["city"]):
            try:
                ans = ai_engines.ask(engine, q + ANSWER_FORMAT)
                if not (ans.get("text") or "").strip():
                    raise ValueError("the engine returned an empty answer")
                rows.append({"engine": engine, "prompt": q, "text": ans["text"], "sources": ans["sources"],
                             "businesses": parse_answer(ans["text"])})
            except Exception as exc:
                rows.append({"engine": engine, "prompt": q, "error": safe_error(exc), "text": "", "sources": [],
                             "businesses": []})
    aio = None
    if settings.enabled("dataforseo") and place and place.get("lat") is not None:
        aio = run_aio_grid(f"best {row['category']} near me", row["business_name"], place["lat"], place["lng"])
    report = build_report(business, rows, aio)
    ok = report["answers"] > 0
    db.update("aivis_runs", run_id, status="completed" if ok else "failed", report=report,
              error=None if ok else "Every engine call failed; see the question table.", completed_at=db.now())


@jobs.on_startup
def recover_runs() -> None:
    db.run("UPDATE aivis_runs SET status = 'queue' WHERE status = 'in_progress'")
    for r in db.all("SELECT id FROM aivis_runs WHERE status = 'queue'"):
        jobs.submit(run, r["id"])
