"""Google Business Profile APIs + Search Console via one OAuth connection.

Business Profile API access must be approved by Google for your Cloud project
(https://developers.google.com/my-business/content/prereqs). Until then every GBP feature runs
in draft mode: the app drafts, you paste into Google. Search Console needs no approval.
"""
import time
from urllib.parse import quote, urlencode

import httpx

from app import db, settings

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = ["https://www.googleapis.com/auth/business.manage",
          "https://www.googleapis.com/auth/webmasters.readonly"]
ACCOUNTS = "https://mybusinessaccountmanagement.googleapis.com/v1"
INFO = "https://mybusinessbusinessinformation.googleapis.com/v1"
V4 = "https://mybusiness.googleapis.com/v4"
PERF = "https://businessprofileperformance.googleapis.com/v1"
GSC = "https://www.googleapis.com/webmasters/v3"


class GBPError(RuntimeError):
    pass


def redirect_uri() -> str:
    return f"{settings.PUBLIC_BASE_URL}/gbp/oauth/callback"


def auth_url(state: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_id": settings.env("GOOGLE_OAUTH_CLIENT_ID"), "redirect_uri": redirect_uri(),
        "response_type": "code", "scope": " ".join(SCOPES), "access_type": "offline",
        "prompt": "consent", "state": state,
    })


def exchange(code: str, client: httpx.Client | None = None) -> None:
    http = client or httpx.Client(timeout=30)
    r = http.post(TOKEN_URL, data={"code": code, "client_id": settings.env("GOOGLE_OAUTH_CLIENT_ID"),
                                   "client_secret": settings.env("GOOGLE_OAUTH_CLIENT_SECRET"),
                                   "redirect_uri": redirect_uri(), "grant_type": "authorization_code"})
    if r.status_code != 200:
        raise GBPError(f"Google sign-in failed: {r.text[:300]}")
    _save(r.json())


def _save(tok: dict) -> None:
    old = db.loads((db.one("SELECT data FROM oauth_tokens WHERE provider = 'google'") or {}).get("data"), {}) or {}
    tok = {**old, **tok, "expires_at": time.time() + int(tok.get("expires_in", 3600)) - 60}
    db.run("INSERT INTO oauth_tokens (provider, data, updated_at) VALUES ('google', ?, ?) "
           "ON CONFLICT(provider) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
           __import__("json").dumps(tok), db.now())


def connected() -> bool:
    return settings.enabled("google_oauth") and db.one("SELECT 1 FROM oauth_tokens WHERE provider = 'google'") is not None


def _token(http: httpx.Client) -> str:
    row = db.one("SELECT data FROM oauth_tokens WHERE provider = 'google'")
    if not row:
        raise GBPError("Google account not connected (Settings → Connect Google account).")
    tok = db.loads(row["data"], {})
    if tok.get("expires_at", 0) < time.time():
        r = http.post(TOKEN_URL, data={"client_id": settings.env("GOOGLE_OAUTH_CLIENT_ID"),
                                       "client_secret": settings.env("GOOGLE_OAUTH_CLIENT_SECRET"),
                                       "refresh_token": tok.get("refresh_token"), "grant_type": "refresh_token"})
        if r.status_code != 200:
            raise GBPError(f"Could not refresh Google access: {r.text[:200]}")
        _save(r.json())
        tok = db.loads(db.one("SELECT data FROM oauth_tokens WHERE provider = 'google'")["data"], {})
    return tok["access_token"]


def call(method: str, url: str, client: httpx.Client | None = None, **kw) -> dict:
    http = client or httpx.Client(timeout=60)
    try:
        r = http.request(method, url, headers={"Authorization": f"Bearer {_token(http)}"}, **kw)
    finally:
        if client is None:
            http.close()
    if r.status_code >= 400:
        raise GBPError(f"Google API {r.status_code}: {r.text[:300]}")
    return r.json() if r.content else {}


# --- Business Profile ----------------------------------------------------------------

def list_locations(client=None) -> list[dict]:
    """[{name: 'accounts/1/locations/2', title, address}] across your accounts."""
    out = []
    for acc in call("GET", f"{ACCOUNTS}/accounts", client).get("accounts", []):
        data = call("GET", f"{INFO}/{acc['name']}/locations", client,
                    params={"readMask": "name,title,storefrontAddress", "pageSize": 100})
        for loc in data.get("locations", []):
            addr = loc.get("storefrontAddress") or {}
            out.append({"name": f"{acc['name']}/{loc['name']}", "title": loc.get("title"),
                        "address": ", ".join((addr.get("addressLines") or []) + [addr.get("locality", "")]).strip(", ")})
    return out


def _loc_id(gbp_location: str) -> str:
    return gbp_location.split("/", 2)[2] if gbp_location.startswith("accounts/") else gbp_location


def get_location(gbp_location: str, client=None) -> dict:
    return call("GET", f"{INFO}/{_loc_id(gbp_location)}", client, params={
        "readMask": "name,title,phoneNumbers,categories,websiteUri,regularHours,profile,storefrontAddress,openInfo"})


def update_location(gbp_location: str, fields: dict, update_mask: str, client=None) -> dict:
    return call("PATCH", f"{INFO}/{_loc_id(gbp_location)}", client, params={"updateMask": update_mask}, json=fields)


def create_post(gbp_location: str, summary: str, cta: str | None = None, cta_url: str | None = None,
                photo_url: str | None = None, client=None) -> dict:
    body: dict = {"languageCode": "en", "summary": summary, "topicType": "STANDARD"}
    if cta and cta_url:
        body["callToAction"] = {"actionType": cta, "url": cta_url}
    if photo_url:
        body["media"] = [{"mediaFormat": "PHOTO", "sourceUrl": photo_url}]
    return call("POST", f"{V4}/{gbp_location}/localPosts", client, json=body)


def list_reviews(gbp_location: str, client=None) -> list[dict]:
    out, token = [], None
    while True:
        data = call("GET", f"{V4}/{gbp_location}/reviews", client, params={"pageSize": 50, **({"pageToken": token} if token else {})})
        out += data.get("reviews", [])
        token = data.get("nextPageToken")
        if not token or len(out) >= 500:
            return out


def reply_review(gbp_location: str, review_id: str, comment: str, client=None) -> dict:
    return call("PUT", f"{V4}/{gbp_location}/reviews/{quote(review_id)}/reply", client, json={"comment": comment})


def upload_photo(gbp_location: str, public_url: str, category: str = "ADDITIONAL", client=None) -> dict:
    return call("POST", f"{V4}/{gbp_location}/media", client,
                json={"mediaFormat": "PHOTO", "locationAssociation": {"category": category}, "sourceUrl": public_url})


METRICS = ["BUSINESS_IMPRESSIONS_DESKTOP_MAPS", "BUSINESS_IMPRESSIONS_DESKTOP_SEARCH",
           "BUSINESS_IMPRESSIONS_MOBILE_MAPS", "BUSINESS_IMPRESSIONS_MOBILE_SEARCH",
           "CALL_CLICKS", "WEBSITE_CLICKS", "BUSINESS_DIRECTION_REQUESTS"]


def performance(gbp_location: str, start: str, end: str, client=None) -> dict:
    """Daily metrics between ISO dates → {metric: {date: value}}."""
    y1, m1, d1 = map(int, start.split("-"))
    y2, m2, d2 = map(int, end.split("-"))
    data = call("GET", f"{PERF}/{_loc_id(gbp_location)}:fetchMultiDailyMetricsTimeSeries", client, params=[
        *[("dailyMetrics", m) for m in METRICS],
        ("dailyRange.startDate.year", y1), ("dailyRange.startDate.month", m1), ("dailyRange.startDate.day", d1),
        ("dailyRange.endDate.year", y2), ("dailyRange.endDate.month", m2), ("dailyRange.endDate.day", d2)])
    out: dict = {}
    for block in data.get("multiDailyMetricTimeSeries", []):
        for s in block.get("dailyMetricTimeSeries", []):
            series = out.setdefault(s["dailyMetric"], {})
            for v in (s.get("timeSeries") or {}).get("datedValues", []):
                d = v["date"]
                series[f"{d['year']:04d}-{d['month']:02d}-{d['day']:02d}"] = int(v.get("value", 0))
    return out


# --- Search Console ------------------------------------------------------------------

def gsc_sites(client=None) -> list[str]:
    return [s["siteUrl"] for s in call("GET", f"{GSC}/sites", client).get("siteEntry", [])]


def gsc_queries(site: str, start: str, end: str, client=None, row_limit: int = 250) -> list[dict]:
    data = call("POST", f"{GSC}/sites/{quote(site, safe='')}/searchAnalytics/query", client,
                json={"startDate": start, "endDate": end, "dimensions": ["query"], "rowLimit": row_limit})
    return [{"query": r["keys"][0], "clicks": r.get("clicks", 0), "impressions": r.get("impressions", 0),
             "ctr": round(100 * r.get("ctr", 0), 2), "position": round(r.get("position", 0), 1)}
            for r in data.get("rows", [])]
