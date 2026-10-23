"""Find a contact email on a business's own website (homepage + a few contact/about pages).

Only the business's site is fetched, never Google. Emails are what the site publishes (mailto: links and
plain text); nothing is guessed. The same private-address guard as the website audit applies.
"""
import re
from html import unescape
from urllib.parse import urljoin, urlparse

import httpx

from app.modules.audit.checks import UA, only_public

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}")
HREF = re.compile(r"""href\s*=\s*["']([^"'#]+)["']""", re.I)
CONTACT_WORDS = ("contact", "about", "impressum", "kontakt", "team", "reach", "get-in-touch")
JUNK_DOMAINS = ("example.com", "domain.com", "email.com", "sentry.io", "wixpress.com", "sentry-next.wixpress.com")
JUNK_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js")
MAX_PAGES = 4


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def extract(html: str) -> list[str]:
    text = unescape(html).replace("%40", "@")
    found = []
    for e in EMAIL.findall(text):
        e = e.strip(".").lower()
        if e not in found and not e.endswith(JUNK_SUFFIXES) and e.split("@", 1)[1] not in JUNK_DOMAINS:
            found.append(e)
    return found


def contact_links(html: str, base: str) -> list[str]:
    """Same-site links whose path looks like a contact/about page."""
    out = []
    for href in HREF.findall(html):
        url = urljoin(base, href.strip())
        if _host(url) == _host(base) and any(w in urlparse(url).path.lower() for w in CONTACT_WORDS) and url not in out:
            out.append(url)
    return out


def find(website: str, client: httpx.Client | None = None) -> dict:
    """{'emails': [...best first], 'pages': [urls read], 'errors': [...]}. Emails on the site's own domain come first."""
    if not urlparse(website).scheme:
        website = "https://" + website
    http = client or httpx.Client(timeout=10, follow_redirects=True, headers={"User-Agent": UA},
                                  event_hooks={"request": [only_public]})
    emails, pages, errors, queue = [], [], [], [website]
    try:
        while queue and len(pages) < MAX_PAGES:
            url = queue.pop(0)
            try:
                r = http.get(url)
            except httpx.HTTPError as exc:
                errors.append(f"{url}: {exc}")
                continue
            pages.append(str(r.url))
            if r.status_code >= 400:
                errors.append(f"{url}: HTTP {r.status_code}")
                continue
            emails += [e for e in extract(r.text) if e not in emails]
            if len(pages) == 1:
                queue += [u for u in contact_links(r.text, str(r.url)) if u not in queue][:MAX_PAGES - 1]
    finally:
        if client is None:
            http.close()
    site = _host(pages[0] if pages else website)
    emails.sort(key=lambda e: not (e.endswith("@" + site) or e.endswith("." + site)))
    return {"emails": emails, "pages": pages, "errors": errors}
