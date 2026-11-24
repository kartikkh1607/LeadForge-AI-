import base64
from email import policy
from email.parser import BytesParser
from unittest.mock import Mock

import httpx
import pytest

from agent.gmail import (
    SCOPES,
    GmailError,
    GmailTransport,
    authorize,
    load_credentials,
    mime_message,
)


def wire(draft):
    return mime_message(
        "developer@example.com", "owner@example.com", draft, "<test@leadforge.local>"
    )


def adapter(handler):
    return GmailTransport(
        "fake",
        httpx.Client(transport=httpx.MockTransport(handler)),
        account="developer@example.com",
        owner_addresses=["owner@example.com"],
    )


@pytest.mark.parametrize(
    "status,state",
    [
        (200, "sent"),
        (400, "rejected"),
        (401, "rejected"),
        (403, "rejected"),
        (429, "rejected"),
        (500, "uncertain"),
        (302, "uncertain"),
    ],
)
def test_api_outcomes(draft, status, state):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, json={"id": "g", "threadId": "t"})

    result = adapter(handle).send(wire(draft))
    assert result.state == state and len(calls) == 1
    parsed = BytesParser(policy=policy.SMTP).parsebytes(base64.urlsafe_b64decode(wire(draft)))
    assert parsed["Message-ID"] == "<test@leadforge.local>"
    assert parsed["To"] == "owner@example.com" and not parsed["Cc"] and not parsed["Bcc"]


def test_timeout_no_retry(draft):
    calls = []

    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout("secret")

    assert adapter(handle).send(wire(draft)).state == "uncertain"
    assert len(calls) == 1


def test_adapter_owner_envelope(draft):
    handler = Mock(side_effect=AssertionError("Network must not run"))
    api = adapter(handler)
    assert (
        api.send(
            mime_message("developer@example.com", "external@example.com", draft, "<x@y>")
        ).state
        == "rejected"
    )
    raw = base64.urlsafe_b64decode(wire(draft)).replace(b"To:", b"Bcc: external@example.com\r\nTo:")
    assert api.send(base64.urlsafe_b64encode(raw).decode()).state == "rejected"
    handler.assert_not_called()


@pytest.mark.parametrize(
    "mode",
    ["found", "empty", "duplicate", "forbidden", "wrong_header", "duplicate_headers", "invalid_id"],
)
def test_reconciliation(mode):
    def handle(request):
        if request.url.path.endswith("/messages"):
            assert "rfc822msgid" in request.url.params["q"]
            if mode == "forbidden":
                return httpx.Response(403)
            hits = [] if mode == "empty" else [{"id": "g"}] * (2 if mode == "duplicate" else 1)
            return httpx.Response(200, json={"messages": hits})
        return httpx.Response(
            200,
            json={
                "id": 123 if mode == "invalid_id" else "g",
                "threadId": "t",
                "labelIds": ["SENT"],
                "internalDate": "1795348800000",
                "payload": {
                    "headers": [
                        {
                            "name": "Message-ID",
                            "value": "wrong" if mode == "wrong_header" else "<test@id>",
                        }
                    ]
                    * (2 if mode == "duplicate_headers" else 1)
                },
            },
        )

    result = adapter(handle).reconcile("<test@id>")
    assert result.state == ("sent" if mode == "found" else "uncertain")


def test_oauth_failure_is_sanitized():
    store = Mock()
    factory = Mock(side_effect=RuntimeError("secret client data"))
    with pytest.raises(GmailError, match="OAuth connection failed") as exc:
        authorize("credentials.json", "developer@example.com", store, factory)
    assert "secret" not in str(exc.value)
    store.put.assert_not_called()


@pytest.mark.parametrize("mode", ["missing", "revoked", "expired", "permissions", "refresh"])
def test_credentials(monkeypatch, mode):
    store = Mock()
    store.get.return_value = None if mode == "missing" else "{}"
    credentials = Mock()
    credentials.expired = mode in {"revoked", "expired", "refresh"}
    credentials.refresh_token = None if mode == "expired" else "refresh"
    credentials.valid = mode not in {"revoked", "expired"}
    credentials.granted_scopes = [] if mode == "permissions" else SCOPES
    credentials.scopes = credentials.granted_scopes
    if mode == "revoked":
        credentials.refresh.side_effect = RuntimeError("secret revoked token")
    monkeypatch.setattr(
        "agent.gmail.Credentials.from_authorized_user_info", Mock(return_value=credentials)
    )
    if mode == "refresh":
        assert load_credentials("developer@example.com", store, Mock()) == credentials
        store.put.assert_called_once()
    else:
        with pytest.raises(GmailError):
            load_credentials("developer@example.com", store, Mock())
        store.put.assert_not_called()


def test_oauth_success_and_scopes(monkeypatch):
    store, flow, credentials = Mock(), Mock(), Mock()
    credentials.valid = True
    credentials.granted_scopes = SCOPES
    flow.run_local_server.return_value = credentials
    factory = Mock(return_value=flow)
    api = Mock()
    api.profile.return_value = "developer@example.com"
    monkeypatch.setattr("agent.gmail.GmailTransport", Mock(return_value=api))
    authorize("credentials.json", "developer@example.com", store, factory)
    assert factory.call_args.kwargs["scopes"] == list(SCOPES)
    assert factory.call_args.kwargs["autogenerate_code_verifier"]
    assert flow.run_local_server.call_args.kwargs["host"] == "127.0.0.1"
    store.put.assert_called_once()


def test_plaintext_store_refused(monkeypatch):
    from agent.gmail import CredentialStore

    monkeypatch.setattr("keyring.get_keyring", lambda: Mock())
    with pytest.raises(GmailError, match="Secure OS"):
        CredentialStore()


def test_malformed_success_is_ambiguous(draft):
    assert adapter(lambda r: httpx.Response(200, json={})).send(wire(draft)).state == "uncertain"


@pytest.mark.parametrize("status", [401, 403])
def test_connection_revoked_or_missing_permissions(status):
    with pytest.raises(GmailError, match="connection check failed"):
        adapter(lambda r: httpx.Response(status, text="secret")).profile()


@pytest.mark.parametrize("header", [b"Subject: duplicate\r\n", b"Sender: external@example.com\r\n"])
def test_ambiguous_headers_rejected_before_network(draft, header):
    request = Mock(side_effect=AssertionError("Must not call Gmail"))
    raw = header + base64.urlsafe_b64decode(wire(draft))
    assert adapter(request).send(base64.urlsafe_b64encode(raw).decode()).state == "rejected"
    request.assert_not_called()


def test_non_string_gmail_ids_are_ambiguous(draft):
    assert (
        adapter(lambda r: httpx.Response(200, json={"id": 123, "threadId": True}))
        .send(wire(draft))
        .state
        == "uncertain"
    )
