"""Explicit OAuth and non-retrying Gmail transport. Never invoked by discovery."""

import base64
import json
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses

import httpx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from agent.verify import normalize_email

SCOPES = (
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
)
API = "https://gmail.googleapis.com/gmail/v1/users/me"


class GmailError(ValueError):
    """Sanitized OAuth/transport diagnostic."""


class CredentialStore:
    """Use only OS-backed encrypted stores; reject fallback/plaintext backends."""

    def __init__(self):
        import keyring

        try:
            self.backend = keyring.get_keyring()
        except Exception:  # noqa: BLE001 -- never expose backend exception data
            raise GmailError("Secure OS credential store unavailable") from None
        if type(self.backend).__module__ not in {
            "keyring.backends.Windows",
            "keyring.backends.macOS",
            "keyring.backends.SecretService",
        }:
            raise GmailError("Secure OS credential store unavailable")

    def get(self, account):
        return self.backend.get_password("LeadForge-Gmail", account.casefold())

    def put(self, account, value):
        self.backend.set_password("LeadForge-Gmail", account.casefold(), value)


def authorize(client_file, account, store, flow_factory=InstalledAppFlow.from_client_secrets_file):
    """Interactive desktop OAuth, loopback callback + PKCE; never sends a message."""
    try:
        flow = flow_factory(str(client_file), scopes=list(SCOPES), autogenerate_code_verifier=True)
        credentials = flow.run_local_server(host="127.0.0.1", port=0, timeout_seconds=180)
        validate_credentials(credentials)
        transport = GmailTransport(credentials.token)
        try:
            if transport.profile() != normalize_email(account).casefold():
                raise GmailError("Authenticated Gmail account does not match configuration")
        finally:
            transport.close()
        store.put(account, credentials.to_json())
    except Exception:  # noqa: BLE001 -- sanitize provider errors; uncertain delivery fails closed
        raise GmailError(
            "OAuth connection failed; check account, consent and desktop client"
        ) from None


def validate_credentials(credentials):
    granted = credentials.granted_scopes
    if granted is None:
        granted = credentials.scopes or ()
    if not set(SCOPES).issubset(granted):
        raise GmailError("Gmail permissions missing; reconnect")
    if not credentials.valid:
        raise GmailError("Gmail credentials expired or invalid; reconnect")


def load_credentials(account, store, request_factory=Request):
    try:
        raw = store.get(account)
        if not raw:
            raise GmailError("Gmail is not connected")
        credentials = Credentials.from_authorized_user_info(json.loads(raw))
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(request_factory())
            validate_credentials(credentials)
            store.put(account, credentials.to_json())
        validate_credentials(credentials)
        return credentials
    except GmailError:
        raise
    except Exception:  # noqa: BLE001 -- sanitize provider errors; uncertain delivery fails closed
        raise GmailError("Gmail credentials unavailable or revoked; reconnect") from None


@dataclass(frozen=True)
class Outcome:
    state: str
    code: str
    message_id: str | None = None
    thread_id: str | None = None
    sent_at: str | None = None


def mime_message(account, recipient, message, rfc_id, in_reply_to=None):
    mail = EmailMessage(policy=policy.SMTP)
    mail["From"] = f"Kartik Khandelwal <{normalize_email(account)}>"
    mail["To"] = normalize_email(recipient)
    mail["Subject"] = message.subject
    mail["Message-ID"] = rfc_id
    if in_reply_to:
        # Threads the follow-up under the original in the recipient's mail client.
        mail["In-Reply-To"] = in_reply_to
        mail["References"] = in_reply_to
    mail.set_content(message.body)
    return base64.urlsafe_b64encode(mail.as_bytes()).decode("ascii")


class GmailTransport:
    """HTTPX does not retry POSTs, refresh tokens, or follow redirects here."""

    def __init__(self, token, client=None, *, owner_addresses=(), account=""):
        self.client = client or httpx.Client(timeout=30, trust_env=False, follow_redirects=False)
        self.headers = {"Authorization": f"Bearer {token}"}
        self.owner_addresses = {normalize_email(a).casefold() for a in owner_addresses}
        self.account = account.casefold()

    def close(self):
        self.client.close()

    def profile(self):
        try:
            response = self.client.get(API + "/profile", headers=self.headers)
            response.raise_for_status()
            return normalize_email(response.json()["emailAddress"]).casefold()
        except Exception:  # noqa: BLE001 -- sanitize provider errors; uncertain delivery fails closed
            raise GmailError(
                "Gmail connection check failed; token or permissions unavailable"
            ) from None

    def send(self, raw, thread_id=None):
        try:
            mail = BytesParser(policy=policy.SMTP).parsebytes(base64.urlsafe_b64decode(raw))
            recipients = getaddresses(mail.get_all("To", []))
            senders = getaddresses(mail.get_all("From", []))
            if (
                mail.defects
                or any(getattr(h, "defects", ()) for h in mail.values())
                or any(
                    len(mail.get_all(h, [])) != 1 for h in ("To", "From", "Subject", "Message-ID")
                )
                or len(recipients) != 1
                or recipients[0][1].casefold() not in self.owner_addresses
                or len(senders) != 1
                or senders[0][1].casefold() != self.account
                or mail.get_all("Cc")
                or mail.get_all("Bcc")
                or mail.get_all("Sender")
                or any(k.lower().startswith("resent-") for k in mail)
            ):
                return Outcome("rejected", "owner_envelope_required")
        except Exception:  # noqa: BLE001 -- sanitize provider errors; uncertain delivery fails closed
            return Outcome("rejected", "invalid_envelope")
        try:
            payload = {"raw": raw}
            if thread_id:
                payload["threadId"] = str(thread_id)
            response = self.client.post(API + "/messages/send", headers=self.headers, json=payload)
            # Only definite client rejections are terminal. Timeouts/5xx/unknowns are uncertain.
            if response.status_code in {400, 401, 403, 404, 413, 429}:
                return Outcome("rejected", f"http_{response.status_code}")
            if response.status_code == 200:
                data = response.json()
                if all(
                    isinstance(data.get(k), str) and data[k].strip() for k in ("id", "threadId")
                ):
                    return Outcome("sent", "accepted", data["id"], data["threadId"])
            return Outcome("uncertain", f"http_{response.status_code}_ambiguous")
        except Exception:  # noqa: BLE001 -- sanitize provider errors; uncertain delivery fails closed
            return Outcome("uncertain", "transport_ambiguous")

    def thread(self, thread_id):
        """Read-only thread metadata for reply/bounce sync; never modifies the mailbox."""
        try:
            response = self.client.get(
                API + "/threads/" + str(thread_id),
                headers=self.headers,
                params=[
                    ("format", "metadata"),
                    *(
                        ("metadataHeaders", h)
                        for h in (
                            "From",
                            "Subject",
                            "Auto-Submitted",
                            "X-Autoreply",
                            "X-Autorespond",
                            "Content-Type",
                            "Message-ID",
                        )
                    ),
                ],
            )
            response.raise_for_status()
            messages = []
            for item in response.json().get("messages", [])[:100]:
                headers = {
                    h["name"].lower(): h["value"]
                    for h in item.get("payload", {}).get("headers", [])
                    if isinstance(h, dict) and isinstance(h.get("value"), str)
                }
                messages.append(
                    {
                        "id": item["id"],
                        "thread_id": item.get("threadId", thread_id),
                        "labels": list(item.get("labelIds", [])),
                        "snippet": str(item.get("snippet", ""))[:500],
                        "internal_date": int(item.get("internalDate", 0)),
                        "headers": headers,
                    }
                )
            return messages
        except Exception:  # noqa: BLE001 -- sanitize provider errors
            raise GmailError("Gmail thread unavailable; sync skipped this thread") from None

    def reconcile(self, rfc_id):
        from datetime import UTC, datetime

        try:
            response = self.client.get(
                API + "/messages",
                headers=self.headers,
                params={"q": f"in:sent rfc822msgid:{rfc_id}", "maxResults": 2},
            )
            response.raise_for_status()
            data = response.json()
            hits = data.get("messages", [])
            if len(hits) != 1 or data.get("nextPageToken"):
                return Outcome("uncertain", "reconciliation_no_unique_match")
            response = self.client.get(
                API + "/messages/" + hits[0]["id"],
                headers=self.headers,
                params={"format": "metadata", "metadataHeaders": "Message-ID"},
            )
            response.raise_for_status()
            found = response.json()
            headers = found.get("payload", {}).get("headers", [])
            ids = [h["value"].strip() for h in headers if h["name"].lower() == "message-id"]
            if (
                "SENT" not in found.get("labelIds", [])
                or ids != [rfc_id]
                or not all(
                    isinstance(found.get(k), str) and found[k].strip() for k in ("id", "threadId")
                )
            ):
                return Outcome("uncertain", "reconciliation_header_mismatch")
            when = datetime.fromtimestamp(int(found["internalDate"]) / 1000, UTC).isoformat()
            return Outcome("sent", "reconciled", found["id"], found["threadId"], when)
        except Exception:  # noqa: BLE001 -- sanitize provider errors; uncertain delivery fails closed
            return Outcome("uncertain", "reconciliation_unavailable")
