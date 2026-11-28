from datetime import UTC, date, datetime

import httpx
import pytest

from agent import apps
from agent.db import Database
from agent.operations import ActionError

NOW = datetime(2026, 11, 28, 12, tzinfo=UTC)


def detail_page(
    *,
    title="Bayside Fitness",
    updated="Mar 4, 2024",
    installs="5K+",
    support="help@baysidefit.com",
    developer="Bayside Fitness LLC",
    dev_email="owner@baysidefit.com",
    address="12 Shore Rd\nTampa, FL 33601\nUnited States",
    phone="+1 813-555-0100",
):
    about = "".join(
        f'<div class="x">{part}</div>' for part in (developer, dev_email, address, phone) if part
    )
    support_html = f'<div>Support email</div><div class="y">{support}</div>' if support else ""
    return f"""<html><head><title>{title} - Apps on Google Play</title>
    <script>var junk = "Updated on Jan 1, 2030 x@y.com";</script></head><body>
    <div class="ClM7O">{installs}</div><div class="g1rdde">Downloads</div>
    <div class="lXlx5">Updated on</div><div class="xg1aie">{updated}</div>
    <section><h2>App support</h2><span>expand_more</span>
      <a href="https://baysidefit.com"><span>Website</span></a>
      <a href="mailto:{support}"><span>email</span>{support_html}</a>
      <div>Privacy Policy</div>
      <h3>About the developer</h3>{about}
    </section><div>More by {developer}</div><div>Flag as inappropriate</div></body></html>"""


def test_parse_listing_reads_visible_fields_not_scripts():
    listing = apps.parse_listing(detail_page(), "com.baysidefit.app")
    assert listing.title == "Bayside Fitness"
    assert listing.updated_on == date(2024, 3, 4)
    assert listing.installs == "5K+"
    assert listing.support_email == "help@baysidefit.com"
    assert listing.developer == "Bayside Fitness LLC"
    assert listing.developer_email == "owner@baysidefit.com"
    assert listing.country == "United States"
    assert listing.email == "owner@baysidefit.com"


def test_country_aliases_and_missing_about_section():
    page = detail_page(address="1 High St\nLeeds\nUK")
    assert apps.parse_listing(page, "a.b").country == "United Kingdom"
    bare = apps.parse_listing(detail_page(developer="", dev_email="", address="", phone=""), "a.b")
    assert bare.country == "" and bare.email == "help@baysidefit.com"


@pytest.mark.parametrize(
    "kwargs,package,verdict",
    [
        ({}, "com.baysidefit.app", "lead"),
        ({"updated": "Oct 2, 2025"}, "com.baysidefit.app", "not_stale"),
        ({}, "com.subsplash.thechurchapp.s_ABC", "builder"),
        ({"support": "ottsupport@vimeo.com", "dev_email": ""}, "com.x.y", "builder"),
        ({"installs": "5M+"}, "com.x.y", "too_big"),
        ({"installs": "50+"}, "com.x.y", "too_small"),
        ({"support": "", "dev_email": ""}, "com.x.y", "no_email"),
        ({"address": "Lagos\nNigeria"}, "com.x.y", "wrong_country"),
        ({"updated": ""}, "com.x.y", "unparsed"),
    ],
)
def test_assess_verdicts(kwargs, package, verdict):
    listing = apps.assess(apps.parse_listing(detail_page(**kwargs), package))
    assert listing.verdict == verdict


def test_assess_respects_suppression_and_cutoff():
    listing = apps.parse_listing(detail_page(), "com.x.y")
    assert apps.assess(listing, suppressed=lambda e: True).verdict == "suppressed"
    assert apps.assess(listing, cutoff=date(2024, 1, 1)).verdict == "not_stale"
    assert apps.assess(listing, countries=()).verdict == "lead"


def test_search_ids_dedupes_in_order():
    page = '<a href="/store/apps/details?id=com.a.b">x</a><a href="details?id=com.c.d"></a>' * 2
    assert apps.search_ids(page) == ["com.a.b", "com.c.d"]


def test_installs_number():
    assert apps.installs_number("10K+") == 10_000
    assert apps.installs_number("1,000+") == 1000
    assert apps.installs_number("1.5M+") == 1_500_000
    assert apps.installs_number("") is None


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "t.sqlite3") as database:
        database.migrate()
        yield database


def fake_client(pages):
    def handler(request):
        key = request.url.params.get("id") or request.url.params.get("q")
        if key in pages:
            return httpx.Response(200, text=pages[key])
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler), base_url=apps.PLAY)


def test_scan_stores_and_updates_without_duplicates(db):
    search = '<a href="/store/apps/details?id=com.good.app"></a>' + (
        '<a href="/store/apps/details?id=com.fresh.app"></a>'
        '<a href="/store/apps/details?id=com.missing.app"></a>'
    )
    pages = {
        "gym app": search,
        "com.good.app": detail_page(),
        "com.fresh.app": detail_page(updated="Nov 1, 2026"),
    }
    client = fake_client(pages)
    report = apps.scan(db, ["gym app", "  "], client=client, clock=lambda: NOW)
    assert (report.queries, report.apps_seen, report.new_apps, report.leads) == (1, 2, 2, 1)
    assert report.failed == ["com.missing.app"]
    assert "1 lead" in report.summary()
    rows = apps.app_leads(db)
    assert [r["title"] for r in rows] == ["Bayside Fitness"]
    assert rows[0]["email"] == "owner@baysidefit.com"
    assert "last updated 2024-03-04" in rows[0]["why"]

    again = apps.scan(db, ["gym app"], client=client, clock=lambda: NOW)
    assert again.new_apps == 0
    assert db.connection.execute("SELECT count(*) FROM app_leads").fetchone()[0] == 2


def test_status_updates(db):
    client = fake_client(
        {"q": '<a href="details?id=com.good.app"></a>', "com.good.app": detail_page()}
    )
    apps.scan(db, ["q"], client=client, clock=lambda: NOW)
    lead_id = apps.app_leads(db)[0]["id"]
    apps.set_status(db, lead_id, "drafted", "gmail draft 1")
    row = apps.app_leads(db, statuses=("drafted",))[0]
    assert row["note"] == "gmail draft 1"
    with pytest.raises(ActionError):
        apps.set_status(db, lead_id, "bogus")
    with pytest.raises(ActionError):
        apps.set_status(db, 9999, "skip")


def test_invalid_package_is_not_fetched():
    client = fake_client({})
    assert apps.fetch_listing(client, "bad id/../x") is None


def test_months_since():
    assert apps.months_since(date(2024, 3, 4), date(2026, 11, 28)) == 32
