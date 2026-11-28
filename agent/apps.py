"""App-rescue mode: find small businesses whose Android app has gone stale on Google Play.

Google Play raises its target API level every year. Since 31 Aug 2026, existing apps that
target below Android 15 (API 35) stop being shown to new users on newer phones, and no app
can ship an update without targeting API 36. An app whose last update predates 31 Aug 2025
almost certainly targets API 34 or lower, so it is a likely candidate. The page does not
show the target API level, so every lead says "likely", never "definitely".

Reads public Play Store pages only. Never emails anyone: drafting stays a human step.
"""

import html
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from urllib.parse import quote

import httpx

from agent.models import utc_now
from agent.operations import ActionError

PLAY = "https://play.google.com"
DEFAULT_CUTOFF = date(2025, 8, 31)
DEFAULT_COUNTRIES = ("United States",)
MAX_INSTALLS = 1_000_000  # bigger apps have teams; not our buyer
MIN_INSTALLS = 100  # below this nobody is using it
PACKAGE = re.compile(r"^[A-Za-z][\w]*(\.[A-Za-z_][\w]*)+$")
EMAIL = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")
DATE = re.compile(r"Updated on\|([A-Z][a-z]{2} \d{1,2}, \d{4})")
INSTALLS = re.compile(r"\|([\d.,]+[KMB]?\+)\|Downloads")
COUNTRY_ALIASES = {
    "usa": "United States",
    "us": "United States",
    "united states of america": "United States",
    "uk": "United Kingdom",
    "great britain": "United Kingdom",
    "england": "United Kingdom",
}
# App builders and platforms keep their customers' apps current themselves.
BUILDERS = re.compile(
    r"subsplash|ministrybrands|ministrycentered|pushpay|churchtrac|vimeo|buildfire|appypie|"
    r"glide|thunkable|adalo|andromo|appsgeyser|appinventor|kodular|goodbarber|swiftic|"
    r"bizness|appinstitute|appmakr|mindbody|glofox|wodify|zenplanner|triib|pushpress|"
    r"exercise\.com|tapcart|vajro|plobal|shopney|toast|chownow|olo\.com|bentobox|spoton|"
    r"squareup|clover|punchh|thanx|paytronix|mobilebytes|appsolute|tithely|tithe\.ly|"
    r"onechurchsoftware|churchlink|hapana|mariana|branded\s?fitness|uplaunch|appstudio",
    re.IGNORECASE,
)


@dataclass
class Listing:
    package: str
    title: str = ""
    developer: str = ""
    support_email: str = ""
    developer_email: str = ""
    address: str = ""
    country: str = ""
    installs: str = ""
    updated_on: date | None = None
    verdict: str = "unparsed"
    reasons: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"{PLAY}/store/apps/details?id={self.package}&hl=en&gl=US"

    @property
    def email(self) -> str:
        """The address to contact: the developer's own, else the app's support address."""
        return self.developer_email or self.support_email


def flatten(page: str) -> str:
    """Visible text with tags turned into '|' separators."""
    page = re.sub(r"<(script|style)\b[\s\S]*?</\1>", " ", page, flags=re.IGNORECASE)
    text = html.unescape(re.sub(r"<[^>]+>", "|", page))
    return re.sub(r"\|[\s|]*", "|", text)


def search_ids(page: str) -> list[str]:
    return list(dict.fromkeys(m for m in re.findall(r"details\?id=([\w.]+)", page)))


def installs_number(text: str) -> int | None:
    match = re.fullmatch(r"([\d.,]+)([KMB]?)\+", text or "")
    if not match:
        return None
    value = float(match.group(1).replace(",", ""))
    return int(value * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[match.group(2)])


def _country(address: str) -> str:
    lines = [line.strip(" ,") for line in re.split(r"[\n\r]+", address) if line.strip(" ,")]
    if not lines:
        return ""
    last = re.sub(r"\s+", " ", lines[-1])
    return COUNTRY_ALIASES.get(last.casefold(), last)


def parse_listing(page: str, package: str) -> Listing:
    listing = Listing(package=package)
    title = re.search(r"<title[^>]*>([^<]*)</title>", page)
    if title:
        listing.title = html.unescape(title.group(1)).removesuffix(" - Apps on Google Play")
    text = flatten(page)
    updated = DATE.search(text)
    if updated:
        listing.updated_on = datetime.strptime(updated.group(1), "%b %d, %Y").date()
    installs = INSTALLS.search(text)
    if installs:
        listing.installs = installs.group(1)
    support = re.search(r"Support email\|([^|\s]+@[^|\s]+)\|", text)
    if support and EMAIL.match(support.group(1)):
        listing.support_email = support.group(1).lower()
    about = re.search(
        r"About the developer\|(.*?)\|(?:More by|Flag as inappropriate)", text, re.DOTALL
    )
    if about:
        parts = [p.strip() for p in about.group(1).split("|") if p.strip()]
        if parts:
            listing.developer = parts[0]
        for part in parts[1:]:
            if EMAIL.match(part) and not listing.developer_email:
                listing.developer_email = part.lower()
            elif not EMAIL.match(part) and not re.fullmatch(r"[+\d\s()-]{7,}", part):
                listing.address = part if not listing.address else listing.address
        listing.country = _country(listing.address)
    return listing


def assess(
    listing: Listing,
    *,
    cutoff: date = DEFAULT_CUTOFF,
    countries=DEFAULT_COUNTRIES,
    suppressed=lambda email: False,
) -> Listing:
    reasons, verdict = [], "lead"
    if listing.updated_on is None:
        verdict, reasons = "unparsed", ["could not read the last update date"]
    else:
        reasons.append(f"last updated {listing.updated_on.isoformat()}")
        installs = installs_number(listing.installs)
        haystack = (
            f"{listing.package} {listing.support_email} "
            f"{listing.developer_email} {listing.developer}"
        )
        if listing.updated_on > cutoff:
            verdict = "not_stale"
        elif BUILDERS.search(haystack):
            verdict = "builder"
            reasons.append("built on an app platform that maintains it")
        elif installs is not None and installs >= MAX_INSTALLS:
            verdict = "too_big"
        elif installs is not None and installs < MIN_INSTALLS:
            verdict = "too_small"
        elif not listing.email:
            verdict = "no_email"
        elif countries and listing.country not in countries:
            verdict = "wrong_country"
            reasons.append(f"developer country: {listing.country or 'not shown'}")
        elif suppressed(listing.email):
            verdict = "suppressed"
        if listing.installs:
            reasons.append(f"{listing.installs} downloads")
    listing.verdict, listing.reasons = verdict, reasons
    return listing


def _get(client: httpx.Client, path: str) -> str | None:
    try:
        response = client.get(PLAY + path)
    except httpx.HTTPError:
        return None
    return response.text if response.status_code == 200 else None


def fetch_listing(client: httpx.Client, package: str) -> Listing | None:
    if not PACKAGE.match(package):
        return None
    page = _get(client, f"/store/apps/details?id={package}&hl=en&gl=US")
    return parse_listing(page, package) if page else None


def _store(db, listing: Listing, query: str, now: str) -> bool:
    values = (
        listing.title,
        listing.developer,
        listing.support_email,
        listing.developer_email,
        listing.address[:500],
        listing.country,
        listing.installs,
        listing.updated_on.isoformat() if listing.updated_on else None,
        listing.url,
        listing.verdict,
        json.dumps(listing.reasons),
        now,
    )
    exists = db.connection.execute(
        "SELECT id FROM app_leads WHERE package=?", (listing.package,)
    ).fetchone()
    if exists:
        db.connection.execute(
            "UPDATE app_leads SET title=?,developer=?,support_email=?,developer_email=?,"
            "address=?,country=?,installs=?,updated_on=?,url=?,verdict=?,reasons_json=?,"
            "last_checked_at=? WHERE id=?",
            (*values, exists["id"]),
        )
        return False
    db.connection.execute(
        "INSERT INTO app_leads(title,developer,support_email,developer_email,address,country,"
        "installs,updated_on,url,verdict,reasons_json,last_checked_at,package,query,"
        "first_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (*values, listing.package, query[:200], now),
    )
    return True


@dataclass
class ScanReport:
    queries: int = 0
    apps_seen: int = 0
    new_apps: int = 0
    leads: int = 0
    verdicts: dict = field(default_factory=dict)
    failed: list[str] = field(default_factory=list)

    def summary(self) -> str:
        counts = ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in sorted(self.verdicts.items()))
        text = (
            f"Searched {self.queries} queries: {self.apps_seen} apps checked "
            f"({self.new_apps} new), {self.leads} leads"
        )
        if counts:
            text += f" [{counts}]"
        if self.failed:
            text += "; could not load: " + ", ".join(self.failed[:5])
        return text + "."


def scan(
    db,
    queries,
    *,
    cutoff: date = DEFAULT_CUTOFF,
    countries=DEFAULT_COUNTRIES,
    per_query: int = 30,
    client: httpx.Client | None = None,
    clock=utc_now,
) -> ScanReport:
    report = ScanReport()
    own = client is None
    client = client or httpx.Client(
        timeout=20,
        follow_redirects=False,
        trust_env=True,
        headers={"User-Agent": "Mozilla/5.0 LeadForge", "Accept-Language": "en-US,en"},
    )

    def suppressed(email: str) -> bool:
        return db.is_suppressed(email, email.rsplit("@", 1)[1])

    try:
        for query in [q.strip() for q in queries if q.strip()]:
            report.queries += 1
            page = _get(client, f"/store/search?q={quote(query)}&c=apps&hl=en&gl=US")
            if page is None:
                report.failed.append(query)
                continue
            for package in search_ids(page)[:per_query]:
                listing = fetch_listing(client, package)
                if listing is None:
                    report.failed.append(package)
                    continue
                assess(listing, cutoff=cutoff, countries=countries, suppressed=suppressed)
                report.apps_seen += 1
                report.verdicts[listing.verdict] = report.verdicts.get(listing.verdict, 0) + 1
                report.leads += listing.verdict == "lead"
                with db.connection:
                    report.new_apps += _store(db, listing, query, clock().isoformat())
    finally:
        if own:
            client.close()
    return report


STATUSES = ("new", "drafted", "emailed", "replied", "won", "not_interested", "skip")
VERDICTS = (
    "lead",
    "not_stale",
    "builder",
    "too_big",
    "too_small",
    "no_email",
    "wrong_country",
    "suppressed",
    "unparsed",
)


def app_leads(db, *, verdicts=("lead",), statuses=None, query=""):
    sql = (
        "SELECT id,title,developer,country,installs,updated_on,"
        "CASE WHEN developer_email!='' THEN developer_email ELSE support_email END AS email,"
        "verdict,status,url,reasons_json,note FROM app_leads WHERE 1=1"
    )
    params: list = []
    for column, values in (("verdict", verdicts), ("status", statuses)):
        if values:
            sql += f" AND {column} IN ({','.join('?' * len(values))})"
            params += list(values)
    if query:
        sql += " AND (title LIKE ? OR developer LIKE ? OR query LIKE ?)"
        params += [f"%{query}%"] * 3
    result = []
    for row in db.connection.execute(sql + " ORDER BY updated_on ASC LIMIT 500", params):
        item = dict(row)
        item["why"] = "; ".join(json.loads(item.pop("reasons_json")))
        result.append(item)
    return result


def set_status(db, lead_id: int, status: str, note: str = ""):
    if status not in STATUSES:
        raise ActionError("Unknown app lead status")
    with db.connection:
        changed = db.connection.execute(
            "UPDATE app_leads SET status=?,note=CASE WHEN ?='' THEN note ELSE ? END WHERE id=?",
            (status, note, note[:1000], lead_id),
        ).rowcount
    if not changed:
        raise ActionError("App lead not found")


def months_since(updated_on: date, today: date | None = None) -> int:
    today = today or datetime.now(UTC).date()
    return max(0, (today.year - updated_on.year) * 12 + today.month - updated_on.month)
