"""Conservative HTML evidence extraction; structured data must agree with visible text."""

import json
import re

from bs4 import BeautifulSoup, Tag

from agent.evidence import normalized_text
from agent.web_models import FAQ


def text(node):
    return " ".join(node.get_text(" ", strip=True).split())


def navigation(node):
    return any(
        p.name in {"nav", "header", "footer"} or p.get("role") == "navigation"
        for p in [node, *node.parents]
        if isinstance(p, Tag)
    )


PROMO = re.compile(
    r"^(ready\b|not sure\b|building a\b|that sounds\b|already a customer\b)", re.IGNORECASE
)
ACCORDION_TITLE = re.compile(
    r"(accordion|toggle|tab|faq|collapsible)[-_]{0,2}(title|header|heading|question|trigger)",
    re.IGNORECASE,
)
ACCORDION_CONTENT = re.compile(
    r"(accordion|toggle|tab|faq|collapsible)[-_]{0,2}(content|body|answer|panel)", re.IGNORECASE
)
ACCORDION_ITEM = re.compile(
    r"(accordion|toggle|faq|collapsible)[-_]{0,2}(item|row|entry)?", re.IGNORECASE
)
TOPIC = re.compile(
    r"\b(course|class|lesson|video|access|enrol|enroll|refund|supplies|start|help|login|download|materials|beginner|experience|payment)\w*\b",
    re.IGNORECASE,
)


def unique_nodes(nodes):
    """De-duplicate by identity; bs4 Tag equality compares content, not position."""
    seen = set()
    return [n for n in nodes if not (id(n) in seen or seen.add(id(n)))]


def faq_pairs(soup, url, visible, structured):
    pairs = []
    seen = set()

    def add(q, a):
        q, a = " ".join(q.split()), " ".join(a.split())
        if (
            not q.endswith("?")
            or not a
            or PROMO.search(q)
            or a.casefold() in {"faq", "faqs", "learn more", "get started", "contact us", "sign up"}
            or len(q) > 300
            or len(a) > 4000
        ):
            return
        if normalized_text(q) not in normalized_text(visible) or normalized_text(
            a
        ) not in normalized_text(visible):
            return
        if (q, a) not in seen:
            pairs.append(FAQ(question=q, answer=a, source_url=url))
            seen.add((q, a))

    # Only FAQPage.mainEntity Question/acceptedAnswer pairs; never execute JSON-LD.
    pending = list(structured)
    for _ in range(500):
        if not pending:
            break
        node = pending.pop()
        if isinstance(node, list):
            pending.extend(node[:100])
        elif isinstance(node, dict):
            types = node.get("@type", [])
            if types == "FAQPage" or isinstance(types, list) and "FAQPage" in types:
                entries = node.get("mainEntity", [])
                for entry in (entries if isinstance(entries, list) else [entries])[:100]:
                    if not isinstance(entry, dict) or entry.get("@type") != "Question":
                        continue
                    answer = entry.get("acceptedAnswer", {})
                    q = entry.get("name")
                    a = answer.get("text") if isinstance(answer, dict) else None
                    if isinstance(q, str) and isinstance(a, str):
                        add(
                            text(BeautifulSoup(q, "html.parser")),
                            text(BeautifulSoup(a, "html.parser")),
                        )
            pending.extend(v for v in node.values() if isinstance(v, (dict, list)))

    for details in soup.find_all("details"):
        summary = details.find("summary")
        if summary and not navigation(details):
            q = text(summary)
            add(q, text(details).removeprefix(q).strip())
    tag_questions = soup.find_all(["h2", "h3", "h4", "dt", "button", "strong", "p"])
    # Accordion widgets (Elementor, Divi, Kadence, Shopify themes) mark titles by class.
    widget_questions = soup.find_all(class_=ACCORDION_TITLE)
    widget_ids = {id(n) for n in widget_questions}
    for question in unique_nodes([*tag_questions, *widget_questions]):
        q = text(question)
        widget = id(question) in widget_ids
        if navigation(question) or not q.endswith("?"):
            continue
        if widget:
            # Toggle anchors point at the same page; a real outbound link is not a question.
            if any(not str(a.get("href", "#")).startswith("#") for a in question.find_all("a")):
                continue
            if question.name == "a" and not str(question.get("href", "#")).startswith("#"):
                continue
        elif question.find("a"):
            continue
        explicit = any(
            re.search(
                r"faq|accordion|question",
                " ".join([str(p.get("id", "")), str(p.get("class", ""))]),
                re.IGNORECASE,
            )
            for p in [question, *question.parents]
            if isinstance(p, Tag)
        ) or bool(
            soup.find(
                re.compile("^h[1-6]$"), string=re.compile(r"FAQ|frequently asked", re.IGNORECASE)
            )
        )
        explicit = explicit or widget
        if not explicit and not TOPIC.search(q):
            continue
        target = question.get("aria-controls")
        answer = soup.find(id=target) if isinstance(target, str) else question.find_next_sibling()
        if answer is None and question.parent.name in {"h2", "h3", "h4", "div", "dt"}:
            answer = question.parent.find_next_sibling()
        if (
            answer is not None
            and answer.get("class")
            and ACCORDION_TITLE.search(" ".join(answer.get("class", [])))
        ):
            answer = None
        if answer is None and widget:
            content = question.find_parent(class_=ACCORDION_ITEM)
            answer = content.find(class_=ACCORDION_CONTENT) if content else None
        if (
            answer
            and answer.name in {"p", "div", "dd", "ul", "ol", "section"}
            and not navigation(answer)
        ):
            chunks = []
            # A heading/paragraph question may be answered by an intro plus a list or more
            # paragraphs; collection still stops at the next question, heading or promo.
            nodes = (
                [answer]
                if target or widget
                else [answer, *answer.find_next_siblings(limit=20 if explicit else 3)]
            )
            for index, part in enumerate(nodes):
                if part.name not in {"p", "div", "dd", "ul", "ol"} or navigation(part):
                    break
                # Outside an explicit FAQ section, only a list directly continuing the
                # answer is joined; following paragraphs may be unrelated page copy.
                if index and not explicit and part.name not in {"ul", "ol"}:
                    break
                value = text(part)
                if (
                    part.find(["h1", "h2", "h3", "h4", "button", "summary"])
                    or value.endswith("?")
                    or PROMO.search(value)
                ):
                    break
                if value.casefold() in {"learn more", "get started", "sign up", "faqs"}:
                    break
                chunks.append(value)
                if len(" ".join(chunks)) > 4000:
                    chunks = []
                    break
            a = " ".join(chunks)
            if not a.endswith("?"):
                add(q, a)
    return pairs


def structured_faq_data(soup):
    values = []
    for script in soup.find_all("script", type="application/ld+json")[:20]:
        raw = script.string or ""
        if len(raw) > 100_000:
            continue
        try:
            values.append(json.loads(raw))
        except (ValueError, RecursionError):
            continue
    return values
