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
TOPIC = re.compile(
    r"\b(course|class|lesson|video|access|enrol|enroll|refund|supplies|start|help|login|download|materials|beginner|experience|payment)\w*\b",
    re.IGNORECASE,
)


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
    for question in soup.find_all(["h2", "h3", "h4", "dt", "button", "strong", "p"]):
        q = text(question)
        if navigation(question) or question.find("a") or not q.endswith("?"):
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
        if not explicit and not TOPIC.search(q):
            continue
        target = question.get("aria-controls")
        answer = soup.find(id=target) if isinstance(target, str) else question.find_next_sibling()
        if answer is None and question.parent.name in {"h2", "h3", "h4", "div", "dt"}:
            answer = question.parent.find_next_sibling()
        if (
            answer
            and answer.name in {"p", "div", "dd", "ul", "ol", "section"}
            and not navigation(answer)
        ):
            chunks = []
            nodes = (
                [answer]
                if target or not explicit
                else [answer, *answer.find_next_siblings(limit=20)]
            )
            for part in nodes:
                if part.name not in {"p", "div", "dd", "ul", "ol"} or navigation(part):
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
