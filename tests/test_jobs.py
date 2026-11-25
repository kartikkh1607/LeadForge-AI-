import json
from datetime import UTC, datetime

import httpx
import pytest

from agent import jobs
from agent.operations import ActionError

NOW = datetime(2026, 11, 25, 12, tzinfo=UTC)

ASHBY = {
    "jobs": [
        {
            "id": "a1",
            "title": "AI Engineer Intern",
            "location": "Remote",
            "isRemote": True,
            "jobUrl": "https://jobs.ashbyhq.com/acme/a1",
            "descriptionPlain": "Build RAG agents in Python with LangChain.",
            "publishedAt": "2026-11-20T00:00:00Z",
        },
        {
            "id": "a2",
            "title": "Senior Staff ML Engineer",
            "location": "San Francisco",
            "isRemote": False,
            "jobUrl": "https://jobs.ashbyhq.com/acme/a2",
            "descriptionPlain": "Python, PyTorch.",
        },
        {
            "id": "a3",
            "title": "Hidden role",
            "isListed": False,
            "jobUrl": "https://jobs.ashbyhq.com/acme/a3",
        },
    ]
}
GREENHOUSE = {
    "jobs": [
        {
            "id": 7,
            "title": "Software Engineer, New Grad",
            "location": {"name": "Bengaluru, India"},
            "absolute_url": "https://boards.greenhouse.io/beta/jobs/7",
            "content": "&lt;p&gt;FastAPI and AWS&lt;/p&gt;",
            "company_name": "Beta",
        }
    ]
}
LEVER = [
    {
        "id": "l1",
        "text": "Junior Data Engineer",
        "categories": {"location": "Remote - US", "allLocations": ["Remote - US"]},
        "workplaceType": "remote",
        "hostedUrl": "https://jobs.lever.co/gamma/l1",
        "descriptionPlain": "SQL and Python",
        "createdAt": 1795000000000,
        "salaryRange": {"min": 90000, "max": 110000, "currency": "USD"},
    }
]


def transport(routes):
    def handler(request):
        for key, payload in routes.items():
            if key in str(request.url):
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={})

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    "text,expected",
    [
        ("ashby:cohere", ("ashby", "cohere")),
        ("https://jobs.lever.co/acme/", ("lever", "acme")),
        ("boards.greenhouse.io/stripe", ("greenhouse", "stripe")),
        ("https://job-boards.greenhouse.io/figma", ("greenhouse", "figma")),
        ("posthog", ("", "posthog")),
    ],
)
def test_parse_board(text, expected):
    assert jobs.parse_board(text) == expected


@pytest.mark.parametrize("bad", ["", "workday:x", "ashby:../etc", "a b"])
def test_parse_board_rejects(bad):
    with pytest.raises(ValueError):
        jobs.parse_board(bad)


@pytest.mark.parametrize(
    "title,level",
    [
        ("AI Engineer Intern", "intern"),
        ("Machine Learning Intern/Co-op (Winter 2027)", "intern"),
        ("Software Engineer, New Grad", "entry"),
        ("Junior AI Engineer", "entry"),
        ("Senior ML Engineer", "senior"),
        ("Staff Engineer", "senior"),
        ("Internal Tools Lead", "senior"),
        ("Machine Learning Engineer", "mid"),
    ],
)
def test_level(title, level):
    assert jobs.level_of(title) == level


@pytest.mark.parametrize(
    "location,remote,expected",
    [
        ("Remote", True, "remote_global"),
        ("Remote only · Everywhere", True, "remote_global"),
        ("Remote - India", True, "remote_india"),
        ("Bengaluru, India", False, "india_onsite"),
        ("Remote - US", True, "remote_restricted"),
        ("San Francisco", False, "onsite_abroad"),
        ("", True, "remote_global"),
    ],
)
def test_eligibility(location, remote, expected):
    assert jobs.eligibility_of(location, remote) == expected


def test_assess_ranks_reachable_intern_above_senior_onsite():
    posts = jobs.normalize("ashby", "acme", ASHBY)
    assert [p.external_id for p in posts] == ["a1", "a2"]
    intern, senior = (jobs.assess(p) for p in posts)
    assert intern.score == 40 + 35 + 20  # 4 skills x 5
    assert intern.location == "Remote"
    assert senior.score <= 15
    assert any("langchain" in r for r in intern.reasons)


def test_normalize_greenhouse_and_lever():
    gh = jobs.normalize("greenhouse", "beta", GREENHOUSE)[0]
    assert gh.company == "Beta" and gh.description == "FastAPI and AWS"
    lv = jobs.normalize("lever", "gamma", LEVER)[0]
    assert lv.remote and lv.compensation == "90000-110000 USD"
    assert lv.posted_at.startswith("2026")


def test_refresh_resolves_ats_upserts_and_closes(db):
    jobs.add_boards(db, ["acme  # comment", "greenhouse:beta", "# skip", "missing"])
    client = transport(
        {"ashbyhq.com/posting-api/job-board/acme": ASHBY, "boards/beta/jobs": GREENHOUSE}
    )
    report = jobs.refresh(db, client, clock=lambda: NOW)
    assert report.new_postings == 3 and report.failed == ["missing"]
    rows = jobs.postings(db, levels=["intern", "entry"])
    assert [r["title"] for r in rows] == ["AI Engineer Intern", "Software Engineer, New Grad"]
    assert "skills:" in rows[0]["why"]

    jobs.set_status(db, rows[0]["id"], "applied", "sent via site")
    later = datetime(2026, 11, 26, tzinfo=UTC)
    gone = transport({"ashbyhq.com/posting-api/job-board/acme": {"jobs": []}})
    again = jobs.refresh(db, gone, clock=lambda: later)
    assert again.new_postings == 0
    statuses = dict(db.connection.execute("SELECT title,status FROM job_postings").fetchall())
    assert statuses["AI Engineer Intern"] == "applied"  # never overwrite your own status
    assert statuses["Senior Staff ML Engineer"] == "closed"


def test_manual_role_and_status_validation(db):
    posting = jobs.add_manual(
        db,
        company="World Trade Factory",
        title="Drones & Robotics AI Engineering Associate",
        url="https://wellfound.com/jobs/123",
        location="Remote only · Everywhere",
        remote=True,
        note="found on Wellfound",
        clock=lambda: NOW,
    )
    assert posting.level == "entry" and posting.eligibility == "remote_global"
    row = db.connection.execute("SELECT * FROM job_postings").fetchone()
    assert row["source"] == "manual" and row["note"] == "found on Wellfound"
    assert json.loads(row["reasons_json"])
    with pytest.raises(ActionError):
        jobs.add_manual(db, company="x", title="y", url="http://insecure")
    with pytest.raises(ActionError):
        jobs.set_status(db, row["id"], "bogus")
    with pytest.raises(ActionError):
        jobs.set_status(db, 999, "applied")


def test_add_boards_reports_bad_lines(db):
    with pytest.raises(ActionError):
        jobs.add_boards(db, ["workday:acme"])


def test_seed_file_parses(db):
    added = jobs.seed_boards(db)
    assert len(added) >= 40 and "ashby:langchain" in added
    assert jobs.seed_boards(db) == []


def test_seed_file_is_committed_with_the_package():
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "agent/job_boards.txt"],
        capture_output=True,
        check=False,
    )
    assert tracked.returncode == 0, "seed list must be tracked by git, not in ignored data/"
