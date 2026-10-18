"""Form spam filters (D4): keyword/phrase, email, domain, IP, country, URL in message, phone format."""
import re

from app import db

KINDS = {
    "keyword": "Keyword or phrase",
    "email": "Email address",
    "domain": "Domain (email or website)",
    "ip": "IP address",
    "country": "Country code (needs a proxy header such as CF-IPCountry)",
    "url_in_message": "Any link in the message",
    "phone_format": "Phone number not 7–15 digits",
}
URL_RE = re.compile(r"https?://|www\.", re.I)


def _emails(data: dict) -> list[str]:
    return [v.strip().lower() for v in data.values() if isinstance(v, str) and "@" in v]


def check(data: dict, ip: str | None = None, country: str | None = None) -> tuple[bool, str | None]:
    """Return (is_spam, reason) for a submission."""
    text = " ".join(str(v) for v in data.values() if v).lower()
    emails = _emails(data)
    for rule in db.all("SELECT * FROM spam_rules WHERE active = 1"):
        kind, value = rule["kind"], (rule["value"] or "").strip().lower()
        # Whole words: a rule "sex" must not block "Essex".
        if kind == "keyword" and value and re.search(rf"(?<![a-z0-9]){re.escape(value)}(?![a-z0-9])", text):
            return True, f"keyword: {value}"
        if kind == "email" and value in emails:
            return True, f"email: {value}"
        # The domain or its subdomains ("mail.spam.com"), never a longer name ("nospam.com").
        if kind == "domain" and value and (any(re.search(rf"[@.]{re.escape(value)}$", e) for e in emails)
                                           or re.search(rf"(?<![a-z0-9.-]){re.escape(value)}(?![a-z0-9-])", text)):
            return True, f"domain: {value}"
        if kind == "ip" and ip and value == ip:
            return True, f"ip: {ip}"
        if kind == "country" and country and value == country.lower():
            return True, f"country: {country}"
        if kind == "url_in_message":
            msg = " ".join(str(v) for k, v in data.items() if "message" in k.lower() or "comment" in k.lower())
            if URL_RE.search(msg):
                return True, "link in message"
        if kind == "phone_format":
            for k, v in data.items():
                if "phone" in k.lower() and v:
                    digits = re.sub(r"\D", "", str(v))
                    if not 7 <= len(digits) <= 15:
                        return True, "invalid phone number"
    return False, None
