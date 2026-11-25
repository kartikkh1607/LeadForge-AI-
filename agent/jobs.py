"""Jobs mode: find internships and entry-level roles you can realistically get.

Reads the public, keyless job-board APIs that Greenhouse, Lever and Ashby run for
their customers' careers pages, normalizes every posting, and ranks it for you:

- level        intern / entry / mid / senior, from the title
- eligibility  can someone in India take it? remote-global, remote-India, India
               onsite, remote but country-restricted, onsite abroad
- score        level fit + eligibility + overlap with your skills

Read-only: never applies, never emails. Roles found elsewhere (e.g. Wellfound,
which needs a login) can be added by hand and are tracked the same way.
"""

import html
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from agent.models import utc_now
from agent.operations import ActionError

APIS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true",
}
BOARD_URLS = (
    (
        re.compile(r"(?:job-)?boards(?:-api)?\.greenhouse\.io/(?:v1/boards/)?([\w.-]+)"),
        "greenhouse",
    ),
    (re.compile(r"jobs\.lever\.co/([\w.-]+)"), "lever"),
    (re.compile(r"jobs\.ashbyhq\.com/([\w.-]+)"), "ashby"),
)
SLUG = re.compile(r"^[A-Za-z0-9][\w.-]{0,99}$")

INTERN = re.compile(
    r"\bintern(ship)?\b|\bco-?op\b|apprentice|trainee|working student", re.IGNORECASE
)
ENTRY = re.compile(
    r"junior|\bjr\.?\b|new grad|graduate|entry[- ]level|early career|fresher|associate\b|"
    r"\b(engineer|developer|scientist) i\b|\bsde ?1\b|\bl1\b",
    re.IGNORECASE,
)
SENIOR = re.compile(
    r"senior|\bsr\.?\b|staff|principal|lead\b|head of|director|manager|architect|"
    r"\b(ii|iii|iv)\b|\bsde ?[23]\b|founding",
    re.IGNORECASE,
)
INDIA = re.compile(
    r"india|bangalore|bengaluru|delhi|ncr|gurgaon|gurugram|noida|hyderabad|pune|mumbai|"
    r"chennai|kolkata|ahmedabad|jaipur",
    re.IGNORECASE,
)
GLOBAL = re.compile(r"anywhere|worldwide|global|everywhere|\bremote\b\s*$|apac|asia", re.IGNORECASE)
REMOTE = re.compile(r"remote", re.IGNORECASE)

DEFAULT_SKILLS = (
    "python",
    "llm",
    "genai",
    "generative ai",
    "rag",
    "langchain",
    "agents",
    "machine learning",
    "deep learning",
    "pytorch",
    "nlp",
    "fastapi",
    "aws",
    "prompt",
    "android",
    "kotlin",
    "sql",
)
ROLE_HINTS = re.compile(
    r"engineer|developer|\bml\b|machine learning|\bai\b|\bllm\b|data|software|research|"
    r"applied|backend|full.?stack|python|devrel|forward deployed|solutions|automation",
    re.IGNORECASE,
)


@dataclass
class Posting:
    source: str
    external_id: str
    company: str
    title: str
    location: str
    remote: bool
    url: str
    description: str = ""
    compensation: str = ""
    posted_at: str | None = None
    level: str = "unknown"
    eligibility: str = "unknown"
    score: int = 0
    reasons: list[str] = field(default_factory=list)


def parse_board(text: str) -> tuple[str, str]:
    """'ashby:cohere', 'https://jobs.lever.co/acme' or a bare slug (ats '')."""
    value = text.strip().rstrip("/")
    if not value:
        raise ValueError("Empty board")
    for pattern, ats in BOARD_URLS:
        match = pattern.search(value)
        if match:
            value = f"{ats}:{match.group(1)}"
            break
    ats, _, slug = value.rpartition(":") if ":" in value else ("", "", value)
    ats = ats.lower()
    if ats and ats not in APIS:
        raise ValueError(f"Unsupported job board: {ats}")
    if not SLUG.match(slug):
        raise ValueError(f"Invalid board name: {slug!r}")
    return ats, slug


def plain(text: str, limit: int = 8000) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", html.unescape(text or "")))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _mark_remote(location: str, remote: bool) -> str:
    if remote and not REMOTE.search(location):
        return f"{location} (remote)".strip()
    return location


def normalize(ats: str, slug: str, data) -> list[Posting]:
    found = []
    if ats == "greenhouse":
        for job in data.get("jobs", []):
            location = (job.get("location") or {}).get("name", "")
            found.append(
                Posting(
                    source=f"greenhouse:{slug}",
                    external_id=str(job["id"]),
                    company=job.get("company_name") or slug,
                    title=job.get("title", ""),
                    location=location,
                    remote=bool(REMOTE.search(location)),
                    url=job.get("absolute_url", ""),
                    description=plain(job.get("content", "")),
                    posted_at=job.get("updated_at"),
                )
            )
    elif ats == "lever":
        for job in data:
            cats = job.get("categories") or {}
            location = "; ".join(
                dict.fromkeys([cats.get("location", ""), *(cats.get("allLocations") or [])])
            ).strip("; ")
            workplace = job.get("workplaceType", "")
            created = job.get("createdAt")
            salary = job.get("salaryRange") or {}
            found.append(
                Posting(
                    source=f"lever:{slug}",
                    external_id=str(job["id"]),
                    company=slug,
                    title=job.get("text", ""),
                    location=_mark_remote(location, workplace == "remote"),
                    remote=workplace == "remote" or bool(REMOTE.search(location)),
                    url=job.get("hostedUrl", ""),
                    description=plain(job.get("descriptionPlain") or job.get("description", "")),
                    compensation=(
                        f"{salary.get('min')}-{salary.get('max')} {salary.get('currency', '')}"
                        if salary
                        else ""
                    ).strip(),
                    posted_at=datetime.fromtimestamp(created / 1000, UTC).isoformat()
                    if created
                    else None,
                )
            )
    else:
        for job in data.get("jobs", []):
            if job.get("isListed") is False:
                continue
            places = [job.get("location", "")] + [
                s.get("location", "") for s in job.get("secondaryLocations") or []
            ]
            location = "; ".join(p for p in dict.fromkeys(places) if p)
            found.append(
                Posting(
                    source=f"ashby:{slug}",
                    external_id=str(job["id"]),
                    company=slug,
                    title=job.get("title", ""),
                    location=_mark_remote(location, bool(job.get("isRemote"))),
                    remote=bool(job.get("isRemote")) or bool(REMOTE.search(location)),
                    url=job.get("jobUrl", ""),
                    description=plain(
                        job.get("descriptionPlain") or job.get("descriptionHtml", "")
                    ),
                    compensation=(job.get("compensation") or {}).get("compensationTierSummary", "")
                    or "",
                    posted_at=job.get("publishedAt"),
                )
            )
    return [p for p in found if p.title and p.url.startswith("https://")]


def level_of(title: str) -> str:
    if INTERN.search(title):
        return "intern"
    if SENIOR.search(title):
        return "senior"
    if ENTRY.search(title):
        return "entry"
    return "mid"


def eligibility_of(location: str, remote: bool) -> str:
    if not location.strip():
        return "remote_global" if remote else "unknown"
    india = bool(INDIA.search(location))
    if remote and india:
        return "remote_india"
    if india:
        return "india_onsite"
    if remote and GLOBAL.search(location.replace("(remote)", "")):
        return "remote_global"
    if remote:
        return "remote_restricted"
    return "onsite_abroad"


LEVEL_POINTS = {"intern": 40, "entry": 40, "mid": 15, "senior": 0, "unknown": 10}
ELIGIBILITY_POINTS = {
    "remote_global": 35,
    "remote_india": 35,
    "india_onsite": 25,
    "remote_restricted": 10,
    "onsite_abroad": 5,
    "unknown": 10,
}


def assess(posting: Posting, skills=DEFAULT_SKILLS) -> Posting:
    posting.level = level_of(posting.title)
    posting.eligibility = eligibility_of(posting.location, posting.remote)
    text = f"{posting.title} {posting.description}".lower()
    matched = [s for s in skills if re.search(r"\b" + re.escape(s.lower()) + r"\b", text)]
    skill_points = min(25, 5 * len(matched))
    role_ok = bool(ROLE_HINTS.search(posting.title))
    posting.score = (
        LEVEL_POINTS[posting.level]
        + ELIGIBILITY_POINTS[posting.eligibility]
        + (skill_points if role_ok else 0)
    )
    posting.reasons = [
        f"level: {posting.level}",
        f"eligibility: {posting.eligibility.replace('_', ' ')}",
        ("skills: " + ", ".join(matched[:6])) if matched else "no listed skill overlap",
    ]
    if not role_ok:
        posting.reasons.append("not an engineering/AI role")
    return posting


def fetch_board(client: httpx.Client, ats: str, slug: str) -> tuple[str, list[Posting]]:
    """Try the given ATS, or all three for a bare slug. Returns (resolved_ats, postings)."""
    for candidate in [ats] if ats else list(APIS):
        try:
            response = client.get(APIS[candidate].format(slug=slug))
        except httpx.HTTPError:
            continue
        if response.status_code != 200:
            continue
        try:
            postings = normalize(candidate, slug, response.json())
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        if postings or ats:
            return candidate, postings
    return ats, []


def add_boards(db, lines) -> list[str]:
    added, now = [], utc_now().isoformat()
    with db.connection:
        for line in lines:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            try:
                ats, slug = parse_board(line)
            except ValueError as exc:
                raise ActionError(str(exc)) from None
            ats = ats or "ashby"  # resolved on first refresh
            if db.connection.execute(
                "INSERT OR IGNORE INTO job_boards(ats,slug,company,added_at) VALUES (?,?,?,?)",
                (ats, slug, slug, now),
            ).rowcount:
                added.append(f"{ats}:{slug}")
    return added


def seed_boards(db, path: Path | None = None) -> list[str]:
    path = path or Path(__file__).with_name("job_boards.txt")
    if not path.exists():
        return []
    return add_boards(db, path.read_text(encoding="utf-8").splitlines())


def boards(db):
    return [dict(r) for r in db.connection.execute("SELECT * FROM job_boards ORDER BY slug")]


def _store(db, board_id, posting: Posting, now: str) -> bool:
    exists = db.connection.execute(
        "SELECT id,status FROM job_postings WHERE source=? AND external_id=?",
        (posting.source, posting.external_id),
    ).fetchone()
    values = (
        posting.company,
        posting.title,
        posting.location,
        int(posting.remote),
        posting.url,
        posting.level,
        posting.eligibility,
        posting.score,
        json.dumps(posting.reasons),
        posting.description,
        posting.compensation,
        posting.posted_at,
        now,
    )
    if exists:
        db.connection.execute(
            "UPDATE job_postings SET company=?,title=?,location=?,remote=?,url=?,level=?,"
            "eligibility=?,score=?,reasons_json=?,description=?,compensation=?,posted_at=?,"
            "last_seen_at=?,status=CASE WHEN status='closed' THEN 'new' ELSE status END "
            "WHERE id=?",
            (*values, exists["id"]),
        )
        return False
    db.connection.execute(
        "INSERT INTO job_postings(company,title,location,remote,url,level,eligibility,score,"
        "reasons_json,description,compensation,posted_at,last_seen_at,board_id,source,"
        "external_id,first_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (*values, board_id, posting.source, posting.external_id, now),
    )
    return True


@dataclass
class RefreshReport:
    boards_checked: int = 0
    postings_seen: int = 0
    new_postings: int = 0
    closed: int = 0
    failed: list[str] = field(default_factory=list)

    def summary(self) -> str:
        text = (
            f"Checked {self.boards_checked} boards: {self.postings_seen} open roles, "
            f"{self.new_postings} new, {self.closed} closed"
        )
        if self.failed:
            text += f"; no public board found for {len(self.failed)}: " + ", ".join(
                self.failed[:10]
            )
        return text + "."


def refresh(db, client: httpx.Client | None = None, skills=DEFAULT_SKILLS, clock=utc_now):
    report = RefreshReport()
    own = client is None
    client = client or httpx.Client(timeout=20, follow_redirects=False, trust_env=True)
    try:
        for board in boards(db):
            now = clock().isoformat()
            report.boards_checked += 1
            explicit = board["last_status"] == "ok"
            ats, postings = fetch_board(client, board["ats"] if explicit else "", board["slug"])
            if not postings and not explicit:
                report.failed.append(board["slug"])
                with db.connection:
                    db.connection.execute(
                        "UPDATE job_boards SET last_fetched_at=?,last_status='not_found' WHERE id=?",
                        (now, board["id"]),
                    )
                continue
            with db.connection:
                db.connection.execute(
                    "UPDATE job_boards SET ats=?,last_fetched_at=?,last_status='ok' WHERE id=?",
                    (ats, now, board["id"]),
                )
                for posting in postings:
                    report.postings_seen += 1
                    report.new_postings += _store(db, board["id"], assess(posting, skills), now)
                report.closed += db.connection.execute(
                    "UPDATE job_postings SET status='closed' WHERE board_id=? AND last_seen_at<? "
                    "AND status IN ('new','shortlisted')",
                    (board["id"], now),
                ).rowcount
    finally:
        if own:
            client.close()
    return report


STATUSES = (
    "new",
    "shortlisted",
    "applied",
    "emailed",
    "interview",
    "offer",
    "rejected",
    "hidden",
    "closed",
)


def postings(db, *, levels=None, eligibility=None, min_score=0, statuses=None, query=""):
    sql = (
        "SELECT id,score,company,title,level,eligibility,location,compensation,status,url,"
        "reasons_json,posted_at FROM job_postings WHERE score>=?"
    )
    params: list = [min_score]
    for column, values in (("level", levels), ("eligibility", eligibility), ("status", statuses)):
        if values:
            sql += f" AND {column} IN ({','.join('?' * len(values))})"
            params += list(values)
    if query:
        sql += " AND (title LIKE ? OR company LIKE ? OR description LIKE ?)"
        params += [f"%{query}%"] * 3
    rows = db.connection.execute(sql + " ORDER BY score DESC, posted_at DESC LIMIT 500", params)
    result = []
    for row in rows:
        item = dict(row)
        item["why"] = "; ".join(json.loads(item.pop("reasons_json")))
        result.append(item)
    return result


def set_status(db, posting_id: int, status: str, note: str = ""):
    if status not in STATUSES:
        raise ActionError("Unknown job status")
    with db.connection:
        changed = db.connection.execute(
            "UPDATE job_postings SET status=?,note=CASE WHEN ?='' THEN note ELSE ? END WHERE id=?",
            (status, note, note[:1000], posting_id),
        ).rowcount
    if not changed:
        raise ActionError("Job not found")


def add_manual(db, *, company, title, url, location="", remote=False, note="", clock=utc_now):
    if not url.startswith("https://") or not title.strip() or not company.strip():
        raise ActionError("Company, title and an https:// link are required")
    posting = assess(
        Posting(
            source="manual",
            external_id=url,
            company=company.strip(),
            title=title.strip(),
            location=location.strip(),
            remote=remote,
            url=url.strip(),
        )
    )
    now = clock().isoformat()
    with db.connection:
        _store(db, None, posting, now)
        if note:
            db.connection.execute(
                "UPDATE job_postings SET note=? WHERE source='manual' AND external_id=?",
                (note[:1000], url),
            )
    return posting
