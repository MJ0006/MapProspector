"""Prospect audit (G1): observable checks on the Google listing and the homepage.

Follows vendor/local-seo-brain wiki/audits/property-technical-audit.md: every finding names the
control tested, what was observed, and the URL. No priority score; the count of failed checks
is just a count.
"""
import json
import re
import time
from html.parser import HTMLParser

import httpx

from app import settings

UA = "Mozilla/5.0 (compatible; MapProspector audit; +local)"
LOCAL_TYPES = {"localbusiness", "organization", "plumber", "roofingcontractor", "dentist", "electrician",
               "hvacbusiness", "homeandconstructionbusiness", "professionalservice", "restaurant", "store",
               "medicalbusiness", "legalservice", "automotivebusiness", "healthandbeautybusiness",
               "lodgingbusiness", "foodestablishment", "financialservice", "generalcontractor", "locksmith",
               "movingcompany", "housepainter", "attorney", "physician", "veterinarycare"}


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.meta, self.h1, self.jsonld = "", {}, [], []
        self.links, self.imgs, self.canonical, self.text = [], [], None, []
        self._in, self._buf = None, ""

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self._in, self._buf = "title", ""
        elif tag == "h1":
            self._in, self._buf = "h1", ""
        elif tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self._in, self._buf = "jsonld", ""
        elif tag in ("script", "style", "noscript"):
            self._in = "skip"
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key:
                self.meta[key] = a.get("content", "")
        elif tag == "link" and "canonical" in a.get("rel", "").lower():
            self.canonical = a.get("href")
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])
        elif tag == "img":
            self.imgs.append(a.get("alt", None) if "alt" in a else None)

    def handle_endtag(self, tag):
        if self._in == "title" and tag == "title":
            self.title = self._buf.strip()
        elif self._in == "h1" and tag == "h1":
            self.h1.append(self._buf.strip())
        elif self._in == "jsonld" and tag == "script":
            self.jsonld.append(self._buf)
        if tag in ("title", "h1", "script", "style", "noscript"):
            self._in = None

    def handle_data(self, data):
        if self._in in ("title", "h1", "jsonld"):
            self._buf += data
        if self._in not in ("skip", "jsonld"):
            self.text.append(data)


def _check(key, label, group, status, detail, source=""):
    return {"key": key, "label": label, "group": group, "status": status, "detail": detail, "source": source}


def schema_types(blocks: list[str]) -> set[str]:
    found = set()

    def walk(node):
        if isinstance(node, dict):
            t = node.get("@type")
            for v in (t if isinstance(t, list) else [t]):
                if isinstance(v, str):
                    found.add(v.lower())
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    for b in blocks:
        try:
            walk(json.loads(b))
        except ValueError:
            continue
    return found


def listing_checks(place: dict) -> list[dict]:
    g, src = "Google listing", "gbp/gbp-optimization"
    out = [
        _check("gbp_website", "Website linked on the listing", g, "pass" if place.get("website") else "fail",
               place.get("website") or "No website on the Google listing.", src),
        _check("gbp_phone", "Phone number on the listing", g, "pass" if place.get("phone") else "fail",
               place.get("phone") or "No phone number listed.", src),
        _check("gbp_category", "Primary category set", g, "pass" if place.get("primary_category") else "warn",
               place.get("primary_category") or "No primary category returned.", "gbp/gbp-categories"),
    ]
    if place.get("hours") is not None or settings.enabled("places"):
        out.append(_check("gbp_hours", "Opening hours listed", g, "pass" if place.get("hours") else "fail",
                          "Hours listed." if place.get("hours") else "No regular opening hours.", src))
    rating, reviews = place.get("rating"), place.get("review_count") or 0
    out.append(_check("gbp_rating", "Average rating", g,
                      "na" if rating is None else "pass" if rating >= 4.0 else "warn",
                      f"{rating} from {reviews} reviews" if rating is not None else "No rating yet.", "reviews/_index"))
    out.append(_check("gbp_reviews", "Number of reviews", g, "pass" if reviews >= 25 else "warn",
                      f"{reviews} reviews. Asking every customer for a review (never filtering) builds this over time.",
                      "reviews/_index"))
    if place.get("photos_count") is not None:
        out.append(_check("gbp_photos", "Photos on the listing", g, "pass" if place["photos_count"] >= 10 else "warn",
                          f"{place['photos_count']} photos returned by Google.", src))
    if place.get("business_status") and place["business_status"] != "OPERATIONAL":
        out.append(_check("gbp_status", "Business shown as open", g, "fail",
                          f"Google shows status {place['business_status']}.", "gbp/warning-for-gbp-changes"))
    return out


def page_checks(url: str, html: str, final_url: str, status: int, elapsed: float, place: dict) -> list[dict]:
    g, tsrc = "Website", "audits/property-technical-audit"
    p = PageParser()
    p.feed(html)
    text = " ".join(p.text)
    out = [
        _check("site_reachable", "Homepage loads", g, "pass" if status < 400 else "fail", f"HTTP {status} at {final_url}", tsrc),
        _check("site_https", "Served over HTTPS", g, "pass" if final_url.startswith("https://") else "fail", final_url, tsrc),
        _check("site_speed", "Server response time", g, "pass" if elapsed < 2.5 else "warn",
               f"{elapsed:.1f} s to download the HTML (one sample, not Core Web Vitals).", tsrc),
    ]
    tl = len(p.title)
    out.append(_check("title", "Title tag", g, "fail" if not tl else "pass" if 25 <= tl <= 65 else "warn",
                      f'"{p.title}" ({tl} chars)' if tl else "No <title>.", "audits/property-ctr-audit"))
    name = (place.get("name") or "").split("(")[0].strip()
    if name:
        first = re.sub(r"[^a-z0-9]", "", name.lower().split()[0]) if name.split() else ""
        out.append(_check("title_brand", "Business name in title", g,
                          "pass" if first and first in re.sub(r"[^a-z0-9]", "", p.title.lower()) else "warn",
                          f'Looked for "{name}" in the title.', "audits/property-ctr-audit"))
    md = p.meta.get("description", "")
    out.append(_check("meta_description", "Meta description", g,
                      "fail" if not md else "pass" if 50 <= len(md) <= 170 else "warn",
                      f"{len(md)} chars" if md else "No meta description.", "audits/property-ctr-audit"))
    out.append(_check("h1", "One H1 heading", g, "pass" if len(p.h1) == 1 else "warn" if p.h1 else "fail",
                      "; ".join(h[:80] for h in p.h1) or "No H1.", "content/content-homepage"))
    out.append(_check("viewport", "Mobile viewport tag", g, "pass" if "viewport" in p.meta else "fail",
                      p.meta.get("viewport") or "No <meta name=viewport>.", tsrc))
    robots = p.meta.get("robots", "").lower()
    out.append(_check("indexable", "Page allows indexing", g, "fail" if "noindex" in robots else "pass",
                      f"robots: {robots}" if robots else "No robots meta (indexable).", tsrc))
    out.append(_check("canonical", "Canonical URL", g, "pass" if p.canonical else "warn",
                      p.canonical or "No canonical link.", tsrc))
    types = schema_types(p.jsonld)
    local = sorted(types & LOCAL_TYPES)
    out.append(_check("schema_local", "LocalBusiness structured data", g, "pass" if local else "fail",
                      f"Found: {', '.join(local)}" if local else (f"Schema types: {', '.join(sorted(types))}" if types
                                                                   else "No JSON-LD structured data."),
                      "frameworks/schema-explanation"))
    phone_digits = re.sub(r"\D", "", place.get("phone") or "")[-10:]
    if phone_digits:
        on_page = phone_digits in re.sub(r"\D", "", text) or any(phone_digits in re.sub(r"\D", "", l) for l in p.links)
        out.append(_check("nap_phone", "Listing phone shown on the site", g, "pass" if on_page else "fail",
                          f"Looked for {place['phone']}.", "backlinks/backlinking-local-citations"))
    out.append(_check("tel_link", "Click-to-call link", g, "pass" if any(l.startswith("tel:") for l in p.links) else "warn",
                      "tel: link found." if any(l.startswith("tel:") for l in p.links) else "No tel: link for mobile callers.",
                      "content/content-homepage"))
    city = _city(place.get("address"))
    if city:
        out.append(_check("city_mention", "City named on the homepage", g, "pass" if city.lower() in text.lower() else "warn",
                          f'Looked for "{city}".', "audits/content-planning-geographical-relevance-part-1"))
    if p.imgs:
        with_alt = sum(1 for a in p.imgs if a)
        out.append(_check("img_alt", "Images have alt text", g, "pass" if with_alt / len(p.imgs) >= 0.7 else "warn",
                          f"{with_alt} of {len(p.imgs)} images.", tsrc))
    return out


def _city(address: str | None) -> str | None:
    parts = [s.strip() for s in (address or "").split(",")]
    return parts[-3] if len(parts) >= 3 else None


def pagespeed(url: str, client: httpx.Client) -> dict | None:
    key = settings.env("GOOGLE_API_KEY")
    if not key:
        return None
    r = client.get("https://www.googleapis.com/pagespeedonline/v5/runPagespeed",
                   params={"url": url, "strategy": "mobile", "category": "performance", "key": key}, timeout=90)
    if r.status_code != 200:
        return None
    lh = r.json().get("lighthouseResult", {})
    score = (lh.get("categories", {}).get("performance") or {}).get("score")
    audits = lh.get("audits", {})
    return {"score": round(score * 100) if score is not None else None,
            "lcp": (audits.get("largest-contentful-paint") or {}).get("displayValue"),
            "cls": (audits.get("cumulative-layout-shift") or {}).get("displayValue")}


def run(place: dict, client: httpx.Client | None = None) -> tuple[str | None, list[dict]]:
    checks = listing_checks(place)
    url = place.get("website")
    if not url:
        return None, checks
    http = client or httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": UA})
    try:
        t0 = time.monotonic()
        try:
            r = http.get(url)
        except httpx.HTTPError as exc:
            checks.append(_check("site_reachable", "Homepage loads", "Website", "fail", f"Could not load {url}: {exc}",
                                 "audits/property-technical-audit"))
            return url, checks
        checks += page_checks(url, r.text, str(r.url), r.status_code, time.monotonic() - t0, place)
        try:
            ps = pagespeed(str(r.url), http)
        except httpx.HTTPError:
            ps = None
        if ps and ps["score"] is not None:
            checks.append(_check("pagespeed", "PageSpeed mobile performance (Lighthouse)", "Website",
                                 "pass" if ps["score"] >= 70 else "warn",
                                 f"Score {ps['score']}/100 · LCP {ps['lcp']} · CLS {ps['cls']}. Passing does not guarantee rankings.",
                                 "audits/property-technical-audit"))
    finally:
        if client is None:
            http.close()
    return url, checks
