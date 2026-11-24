"""Public HTML extraction and bounded, resumable crawling. Never drafts or sends."""

import re
from collections import deque
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, ParserRejectedMarkup, Tag

from agent.course_extraction import course_offers
from agent.db import Database
from agent.extraction import faq_pairs, structured_faq_data
from agent.safe_http import FetchError, SafeFetcher, public_url, same_site
from agent.verify import normalize_domain, normalize_email
from agent.web_models import CrawlResult, Observation, PageData

COMMON_PATHS = (
    "/faq",
    "/faqs",
    "/pages/faq",
    "/p/faq",
    "/frequently-asked-questions",
    "/contact",
    "/pages/contact",
    "/about",
    "/help",
)
# Only explicit bracketed/parenthesised "at"/"dot" tokens count; bare words never do.
OBFUSCATED_EMAIL_RE = re.compile(
    r"\b([a-zA-Z0-9._%+-]+)\s*[(\[{]\s*at\s*[)\]}]\s*"
    r"([a-zA-Z0-9-]+(?:\s*(?:[(\[{]\s*dot\s*[)\]}]|\.)\s*[a-zA-Z0-9-]+)+)",
    re.IGNORECASE,
)


def deobfuscate_email(visible: str) -> str:
    match = OBFUSCATED_EMAIL_RE.fullmatch(visible.strip())
    if not match:
        raise ValueError("Not an explicitly obfuscated email")
    domain = re.sub(r"\s*(?:[(\[{]\s*dot\s*[)\]}]|\.)\s*", ".", match.group(2), flags=re.IGNORECASE)
    return f"{match.group(1)}@{domain}"


RELEVANT = re.compile(
    r"faq|help|contact|support|courses?|classes|workshop|lessons|pricing|about", re.IGNORECASE
)
EMAIL_RE = re.compile(r"[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,63}")
IGNORED_EMAIL_DOMAINS = (
    "teachable.com",
    "thinkific.com",
    "kajabi.com",
    "wix.com",
    "wixpress.com",
    "sentry.io",
    "sentry.wixpress.com",
    "example.com",
)
CHATBOT_HOSTS = {
    "intercom": ("widget.intercom.io", "js.intercomcdn.com"),
    "tidio": ("code.tidio.co",),
    "crisp": ("client.crisp.chat",),
    "drift": ("js.driftt.com",),
    "tawk.to": ("embed.tawk.to",),
    "chatbase": ("www.chatbase.co", "chatbase.co"),
}


def clean_text(node: Tag | str) -> str:
    return " ".join((node.get_text(" ", strip=True) if isinstance(node, Tag) else node).split())


def email_rank(value: str) -> tuple[int, str]:
    local = value.split("@", 1)[0].casefold()
    rank = 1 if local in {"hello", "contact"} else 2 if local in {"support", "info"} else 0
    return rank, value.casefold()


def extract_page(html: str, url: str) -> PageData:
    """Extract explicit HTML observations; JSON-LD FAQ pairs require visible corroboration."""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except (ParserRejectedMarkup, AssertionError, RecursionError) as exc:
        raise FetchError("malformed_html") from exc
    if soup.select_one('input[type="password"], #challenge-form, [data-restricted="true"]'):
        raise FetchError("restricted_or_challenge_content")
    title = clean_text(soup.title) if soup.title else ""
    if title.casefold() in {"just a moment...", "access denied", "verify you are human"}:
        raise FetchError("restricted_or_challenge_content")
    page = PageData(url=url)
    structured = structured_faq_data(soup)
    for script in soup.find_all("script", src=True):
        src = urljoin(url, str(script["src"]))
        try:
            host = (urlsplit(src).hostname or "").lower()
        except ValueError:
            continue
        for name, hosts in CHATBOT_HOSTS.items():
            if host in hosts:
                page.chatbots.append(
                    Observation(value=name, source_url=url, quote=str(script), method="script_src")
                )
    # Do not collect content hidden by explicit HTML/CSS indicators.
    for node in list(soup.select('[hidden], [aria-hidden="true"]')):
        node.decompose()
    for node in list(soup.find_all(style=True)):
        if re.search(
            r"display\s*:\s*none|visibility\s*:\s*hidden", node.get("style", ""), re.IGNORECASE
        ):
            node.decompose()
    business = soup.find("meta", attrs={"property": "og:site_name"})
    if business and business.get("content", "").strip():
        page.business_names.append(
            Observation(
                value=clean_text(business["content"]),
                source_url=url,
                quote=str(business),
                method="og:site_name",
            )
        )
    # A title is a labelled candidate, not a guessed owner or registered business name.
    elif title:
        page.business_names.append(
            Observation(value=title, source_url=url, quote=title, method="title")
        )
    for person in soup.select('[itemtype$="/Person"] [itemprop="name"], [rel="author"]'):
        name = clean_text(person)
        if name:
            page.owner_names.append(
                Observation(value=name, source_url=url, quote=name, method="explicit_person")
            )
    emails: dict[str, Observation] = {}

    def add_email(value: str, quote: str, method: str) -> None:
        try:
            value = normalize_email(value.strip(".,;:()<>"))
        except ValueError:
            return
        domain = value.split("@")[1]
        if any(domain == d or domain.endswith("." + d) for d in IGNORED_EMAIL_DOMAINS):
            return
        if domain.rsplit(".", 1)[-1] in {"png", "jpg", "jpeg", "svg", "webp", "gif"}:
            return
        if value.split("@")[0].lower() in {"noreply", "no-reply", "donotreply"}:
            return
        emails.setdefault(
            value.casefold(), Observation(value=value, source_url=url, quote=quote, method=method)
        )

    for node in soup.select("[data-cfemail]"):
        encoded = str(node.get("data-cfemail", ""))
        try:
            add_email(decode_cfemail(encoded), encoded, "cloudflare")
        except ValueError:
            pass
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"])
        if href.lower().startswith("mailto:"):
            for value in unquote(href[7:].split("?", 1)[0]).split(","):
                add_email(value, href, "mailto")
        elif "/cdn-cgi/l/email-protection#" in href:
            try:
                add_email(decode_cfemail(href.split("#", 1)[1]), href, "cloudflare")
            except ValueError:
                pass
        if "nofollow" in anchor.get("rel", []):
            continue
        if RELEVANT.search(href + " " + clean_text(anchor)):
            try:
                target = public_url(urljoin(url, href))
                if same_site(target, normalize_domain(url)) and target not in page.links:
                    page.links.append(target)
            except ValueError:
                pass
    for node in soup(["script", "style", "noscript", "template", "svg"]):
        node.decompose()
    page.text = soup.get_text(" ", strip=True)
    for value in EMAIL_RE.findall(page.text):
        add_email(value, value, "text")
    for match in OBFUSCATED_EMAIL_RE.finditer(page.text):
        try:
            add_email(deobfuscate_email(match.group(0)), match.group(0), "obfuscated_text")
        except ValueError:
            pass
    page.emails = sorted(emails.values(), key=lambda item: email_rank(item.value))
    page.faqs = faq_pairs(soup, url, page.text, structured)
    page.courses = course_offers(soup, url, structured)
    # Bound the discovery frontier, never impose a minimum FAQ length.
    page.links = page.links[:32]
    return page


def crawl_lead(
    db: Database, lead_id: int, fetcher: SafeFetcher | None = None, *, retry_failed: bool = False
) -> CrawlResult:
    lead = db.get_lead(lead_id)
    result = CrawlResult(lead_id=lead_id)
    if db.is_suppressed(lead.email, lead.domain) or lead.stage != "new":
        result.errors["lead"] = "suppressed_or_not_new"
        return result
    fetcher = fetcher or SafeFetcher()
    fetcher.begin_crawl()
    try:
        seed = public_url(lead.website or "https://" + lead.domain)
        if not same_site(seed, lead.domain):
            raise FetchError("outside_site")
    except FetchError as exc:
        result.errors["seed"] = str(exc)
        return result
    origin = f"{urlsplit(seed).scheme}://{urlsplit(seed).netloc}"
    queue = deque(dict.fromkeys([seed, origin + "/", *(origin + p for p in COMMON_PATHS)]))
    records = db.crawl_records(lead_id)
    seen = set()
    final_urls = set()
    discovered_links = set()
    while queue and len(seen) < fetcher.limits.max_pages:
        url = queue.popleft()
        if url in seen:
            continue
        record = records.get(url)
        if record is None and len(records) >= fetcher.limits.max_pages:
            continue
        seen.add(url)
        page = None
        if record and record["state"] == "ok":
            page = PageData.model_validate_json(record["result_json"])
            result.cached_pages += 1
        elif record and (not retry_failed or record["attempts"] >= 3):
            result.errors[url] = record["error"]
        else:
            try:
                response = fetcher.fetch(url, lead.domain)
                if response.status != 200:
                    raise FetchError(f"http_status_{response.status}")
                if response.headers.get("content-type", "").split(";")[0].strip().lower() not in {
                    "text/html",
                    "application/xhtml+xml",
                }:
                    raise FetchError("not_html")
                page = extract_page(response.text, response.url)
                db.save_crawl_page(lead_id, url, page=page)
            except FetchError as exc:
                result.errors[url] = str(exc)
                db.save_crawl_page(lead_id, url, error=str(exc))
            records = db.crawl_records(lead_id)
        if page:
            if page.url not in final_urls:
                result.pages.append(page)
                final_urls.add(page.url)

            # Real relevant links take precedence over guessing common paths.
            discovered_links.update(page.links)

            # Preserve the search landing page, then prioritize FAQ/contact/course links.
            def priority(link):
                path = urlsplit(link).path.casefold()
                return (
                    0 if link in discovered_links else 1 if link == origin + "/" else 2,
                    0
                    if "faq" in path
                    else 1
                    if "contact" in path
                    else 2
                    if any(word in path for word in ("course", "class", "workshop", "product"))
                    else 3,
                    link,
                )

            frontier = sorted(set(queue) | set(page.links), key=priority)
            queue = deque(link for link in frontier if link not in seen)
    # Include cached pages not reached by today's frontier, preserving completed work.
    for record in records.values():
        if record["state"] == "ok":
            page = PageData.model_validate_json(record["result_json"])
            if page.url not in final_urls:
                result.pages.append(page)
                final_urls.add(page.url)
    db.apply_scraped_facts(lead_id, result.pages)
    result.requests = fetcher.requests
    return result


def decode_cfemail(encoded: str) -> str:
    try:
        raw = bytes.fromhex(encoded)
        if len(raw) < 2:
            raise ValueError("Missing encoded email")
        return bytes(byte ^ raw[0] for byte in raw[1:]).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("Invalid Cloudflare email encoding") from exc
