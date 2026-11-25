import socket
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from agent.config import Settings
from agent.personalize import build_message

_connect = socket.socket.connect


@pytest.fixture(autouse=True)
def internal_asyncio_socketpair(monkeypatch, no_network):
    # Windows asyncio creates a local wake-up socket pair. Permit only that
    # stdlib call site; app HTTP, DNS and every other socket remain blocked.
    def connect(sock, address):
        if sys._getframe(1).f_code.co_name == "_fallback_socketpair" and address[0] == "127.0.0.1":
            return _connect(sock, address)
        raise AssertionError("App rendering must not use the network")

    monkeypatch.setattr(socket.socket, "connect", connect)


def app_for(monkeypatch, db, settings=None):
    path = db.connection.execute("PRAGMA database_list").fetchone()[2]
    settings = (settings or Settings()).model_copy(update={"database_path": Path(path)})
    monkeypatch.setattr("agent.config.load_settings", lambda: settings)
    return AppTest.from_file(Path("app.py").resolve(), default_timeout=15).run()


@pytest.mark.parametrize(
    "section", ["Discover", "Review", "Send (preview only)", "Tracker", "Jobs", "Settings"]
)
def test_all_screens_empty_without_network(monkeypatch, db, section):
    app = app_for(monkeypatch, db)
    app.sidebar.radio[0].set_value(section).run()
    assert not app.exception
    assert not app.error
    assert db.connection.execute("SELECT count(*) FROM leads").fetchone()[0] == 0


@pytest.mark.parametrize("section", ["Review", "Send (preview only)", "Tracker", "Settings"])
def test_populated_screens_do_not_mutate_or_expose_secrets(monkeypatch, db, phase3, section):
    lead, _, slots, settings = phase3
    db.create_draft(build_message(db, lead.id, slots, settings))
    app = app_for(monkeypatch, db, settings)
    before = db.connection.execute("SELECT count(*) FROM ui_actions").fetchone()[0]
    app.sidebar.radio[0].set_value(section).run()
    assert not app.exception and not app.error
    assert "fake-key" not in str(app)
    assert db.connection.execute("SELECT count(*) FROM ui_actions").fetchone()[0] == before
    assert db.connection.execute("SELECT status FROM messages").fetchone()[0] == "draft"


def test_approve_requires_both_attestations_and_rerun_does_not_repeat(monkeypatch, db, phase3):
    lead, _, slots, settings = phase3
    message = db.create_draft(build_message(db, lead.id, slots, settings))
    app = app_for(monkeypatch, db, settings)
    app.sidebar.radio[0].set_value("Review").run()
    next(b for b in app.button if b.label == "Approve draft").click().run()
    assert db.get_message(message.id).status == "draft"
    next(c for c in app.checkbox if c.label.startswith("I confirm the sender")).check()
    next(c for c in app.checkbox if c.label.startswith("I confirm the opt-out")).check()
    next(b for b in app.button if b.label == "Approve draft").click().run()
    assert not app.exception and not app.error
    assert db.get_message(message.id).status == "approved"
    timestamp = db.get_message(message.id).approved_at
    app.run()
    assert db.get_message(message.id).approved_at == timestamp


def test_settings_submit_persists_pause_and_dry_run(monkeypatch, db):
    app = app_for(monkeypatch, db)
    app.sidebar.radio[0].set_value("Settings").run()
    assert next(c for c in app.checkbox if c.label == "Dry-run enabled").value
    assert next(c for c in app.checkbox if c.label == "Global pause").value
    next(c for c in app.checkbox if c.label == "Global pause").uncheck()
    next(b for b in app.button if b.label == "Save settings").click().run()
    assert not app.error and not app.exception
    app = app_for(monkeypatch, db)
    app.sidebar.radio[0].set_value("Settings").run()
    assert not next(c for c in app.checkbox if c.label == "Global pause").value
    assert next(c for c in app.checkbox if c.label == "Dry-run enabled").value


def test_missing_key_run_renders_actionable_failure(monkeypatch, db):
    app = app_for(monkeypatch, db)
    next(t for t in app.text_input if t.label == "Target niche").input("watercolor")
    next(b for b in app.button if b.label == "Start discovery").click().run()
    assert not app.exception
    assert db.connection.execute("SELECT status FROM discovery_runs").fetchone()[0] == "partial"
    assert not db.connection.execute("SELECT 1 FROM leads").fetchone()


def test_jobs_screen_seeds_boards_and_lists_roles_without_network(monkeypatch, db):
    from agent import jobs

    jobs.add_manual(
        db,
        company="Acme",
        title="AI Engineer Intern",
        url="https://wellfound.com/jobs/1",
        location="Remote · Everywhere",
        remote=True,
    )
    app = app_for(monkeypatch, db)
    app.sidebar.radio[0].set_value("Jobs").run()
    assert not app.exception and not app.error
    assert db.connection.execute("SELECT count(*) FROM job_boards").fetchone()[0] >= 40
    assert list(app.dataframe[0].value["title"]) == ["AI Engineer Intern"]
