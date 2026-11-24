"""Course observations assembled within a card or a single-course landing page.

Every supporting span is visible source text. Metadata locates candidates but never
supplies invisible descriptions, prices, availability, or unsupported URLs.
"""

import re
from urllib.parse import urljoin, urlsplit

from agent.evidence import normalized_text
from agent.extraction import navigation, text, unique_nodes
from agent.safe_http import FetchError, public_url
from agent.web_models import Observation

COURSE = re.compile(r"\b(course|courses|class|classes|workshop|lessons|tutoring)\b", re.IGNORECASE)
ONLINE = re.compile(r"\b(online|self[- ]paced|videos?|recordings?|recorded)\b", re.IGNORECASE)
PURCHASE = re.compile(
    r"\b(enroll|enrol|buy|purchase|join|register|sign up|add to cart)\b|[$£€]\s*\d", re.IGNORECASE
)
GENERIC = re.compile(
    r"^(about (this|the)|course (outline|materials|tuition|login)|our courses|all courses|recorded courses|online courses|ready\b|why\b|how\b|what\b)",
    re.IGNORECASE,
)
NEGATIVE = re.compile(
    r"\b(not (?:available|for sale)|sold out|enrollment (?:is )?closed|discontinued|coming soon)\b",
    re.IGNORECASE,
)
SUBSECTION = re.compile(
    r"^(about|overview|curriculum|course (outline|materials|tuition)|what you|who (is|it's)|how |faq|frequently|pricing|price|enroll|register|sign up|tuition|requirements|materials|lessons|modules|reviews|testimonials|access|instructor)",
    re.IGNORECASE,
)


CARD_TITLE = re.compile(
    r"(course|class|workshop|lesson|product|card)[-_]{0,2}(title|name|heading)", re.IGNORECASE
)
COURSE_CLASS = re.compile(r"(course|class|workshop|lesson)", re.IGNORECASE)


def card_title(node):
    classes = " ".join(node.get("class", []) or [])
    return bool(classes and CARD_TITLE.search(classes))


def safe_link(node, url):
    if node is None or not node.get("href"):
        return None
    try:
        return public_url(urljoin(url, str(node["href"])))
    except FetchError:
        return None


def schema_nodes(values):
    pending = list(values)
    for _ in range(500):
        if not pending:
            return
        node = pending.pop()
        if isinstance(node, list):
            pending.extend(node[:100])
        elif isinstance(node, dict):
            yield node
            pending.extend(v for v in node.values() if isinstance(v, (dict, list)))


def course_offers(soup, url, structured=()):
    candidates = []
    metadata = {}
    visible = text(soup)
    for item in schema_nodes(structured):
        kind = item.get("@type", [])
        kinds = [kind] if isinstance(kind, str) else kind if isinstance(kind, list) else []
        name = item.get("name")
        if not isinstance(name, str) or normalized_text(name) not in normalized_text(visible):
            continue
        if "Course" in kinds or "Product" in kinds and COURSE.search(name):
            metadata[normalized_text(name)] = (
                "schema_course" if "Course" in kinds else "schema_product"
            )

    landing_hint = bool(
        re.search(r"course|class|workshop|drawing-painting", urlsplit(url).path, re.IGNORECASE)
    )
    # A non-home page whose main content sells a course treats its <h1> as the product
    # title even when the title itself lacks the word "course" (e.g. "Loose Florals").
    main = soup.find("main") or soup.find("article") or soup.body or soup
    main_text = " ".join(
        text(n) for n in main.find_all(["p", "li", "a", "button", "span"]) if not navigation(n)
    )
    path = urlsplit(url).path.strip("/")
    sells_course = bool(path and COURSE.search(main_text) and PURCHASE.search(main_text))
    landing_hint = landing_hint or sells_course
    tagged = soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "a", "strong", "b", "p", "span"])
    # Page builders often render card titles as classed <div>s rather than headings.
    classed = [n for n in soup.find_all(["div", "span", "p", "a"]) if card_title(n)]
    for node in unique_nodes([*tagged, *classed]):
        name = text(node)
        if (
            navigation(node)
            or not name
            or len(name) > 160
            or len(name.split()) > 20
            or "?" in name
            or GENERIC.search(name)
        ):
            continue
        structured_method = metadata.get(normalized_text(name))
        is_card = card_title(node)
        heading = node.name.startswith("h") and node.name[1:].isdigit() or is_card
        course_card = is_card and bool(COURSE_CLASS.search(" ".join(node.get("class", []))))
        if not (
            structured_method
            or COURSE.search(name)
            or course_card
            or (node.name == "h1" and landing_hint)
        ):
            continue
        if not heading and not structured_method:
            if name.endswith((".", "!", ":")) or len(name.split()) > 12:
                continue
            # Avoid matching a container merely because a nested title contains 'course'.
            if node.find(["h1", "h2", "h3", "h4", "a", "strong", "b", "p"]):
                continue
        candidates.append((node, name, structured_method))

    results = []
    seen = set()
    for node, name, structured_method in candidates[:100]:
        scope = node
        method = structured_method or "course_card"
        # Climb only while all other named course candidates belong to this title.
        for parent in list(node.parents)[:8]:
            if parent.name in {"html", "[document]"} or navigation(parent):
                break
            if len(text(parent)) > 12000:
                break
            other_titles = {
                normalized_text(title)
                for other, title, _ in candidates
                if other is not node
                and parent in other.parents
                and normalized_text(title) not in normalized_text(name)
                and normalized_text(name) not in normalized_text(title)
            }
            if other_titles:
                break
            unrelated = [
                h
                for h in parent.find_all(re.compile(r"^h[1-6]$"))
                if text(h)
                and "?" not in text(h)
                and not SUBSECTION.search(text(h))
                and normalized_text(text(h)) not in normalized_text(name)
                and normalized_text(name) not in normalized_text(text(h))
            ]
            if unrelated:
                break
            scope = parent
            if parent.name in {"article", "main", "body"}:
                method = structured_method or "single_course_sections"
                break

        # Local sibling blocks still work when the enclosing page lists other courses.
        local = [node]
        for sibling in node.find_next_siblings(limit=12):
            if sibling.name in {"h1", "h2", "h3", "h4", "h5", "h6", "nav", "footer", "section"}:
                break
            if any(
                other is sibling or sibling in other.parents
                for other, title, _ in candidates
                if title != name
            ):
                break
            if sibling.find(re.compile(r"^h[1-6]$")):
                break
            local.append(sibling)
        local_quote = " ".join(text(n) for n in local)
        quote = text(scope)
        if len(local_quote) > len(quote) and len(local_quote) <= 4000:
            quote = local_quote
            method = structured_method or "adjacent_course_text"
        # Do not use navigation or footer pricing to confirm the course.
        blocks = [
            n
            for n in scope.find_all(
                [
                    "p",
                    "li",
                    "h1",
                    "h2",
                    "h3",
                    "h4",
                    "h5",
                    "h6",
                    "a",
                    "button",
                    "span",
                    "strong",
                    "b",
                ]
            )
            if not navigation(n)
        ]
        spans = [name]
        for block in [*local, *blocks]:
            value = text(block)
            if (
                value
                and len(value) <= 4000
                and (ONLINE.search(value) or PURCHASE.search(value))
                and not navigation(block)
                and normalized_text(value) in normalized_text(visible)
                and value not in spans
            ):
                spans.append(value)
        # Scope text is corroborating evidence only when it contains no navigation.
        if (
            len(quote) <= 4000
            and not scope.find(["nav", "header", "footer"])
            and quote not in spans
        ):
            spans.append(quote)
        combined = " ".join(spans)
        educational = bool(COURSE.search(combined) or structured_method == "schema_course")
        online = educational and bool(ONLINE.search(combined))
        offered = online and bool(PURCHASE.search(combined)) and not NEGATIVE.search(combined)
        anchors = ([node] if node.name == "a" else []) + list(scope.find_all("a", href=True))
        title_link = next(
            (a for a in anchors if normalized_text(name) == normalized_text(text(a))), None
        )
        purchase_link = next(
            (a for a in anchors if PURCHASE.search(text(a)) and not navigation(a)), None
        )
        landing = safe_link(title_link, url) or public_url(url)
        classification = (
            "online_offer" if offered else "online_course" if online else "course_candidate"
        )
        key = (name, landing, classification)
        if key in seen:
            continue
        seen.add(key)
        purchase = next((s for s in spans if PURCHASE.search(s)), None)
        results.append(
            Observation(
                value=name,
                source_url=url,
                quote=quote,
                method=method,
                landing_url=landing,
                enrollment_text=purchase,
                enrollment_url=safe_link(purchase_link, url),
                evidence_classification=classification,
                supporting_text=spans,
            )
        )
    if not any(r.evidence_classification == "online_offer" for r in results):
        page_offer = single_course_page(soup, url, candidates, visible)
        if page_offer:
            results.append(page_offer)
    return results


def under_course_section(block, title):
    """True when the block's nearest preceding heading is the title or a course subsection.

    Sections headed by another product ("Paint Brush Kit"), a promo question or unrelated
    copy ("Meet your teacher") never contribute format or purchase evidence.
    """
    heading = block.find_previous(re.compile(r"^h[1-6]$"))
    if heading is None:
        return False
    if heading is title:
        return True
    value = text(heading)
    return "?" not in value and bool(SUBSECTION.search(value))


def single_course_page(soup, url, candidates, visible):
    """Join a sales page whose title, format and price live in separate sections.

    Only applies when the page names exactly one course title (its <h1>); with two or
    more course titles the page is a catalogue and evidence could cross products.
    """
    titles = {}
    for node, name, structured_method in candidates:
        is_title = node.name in {"h1", "h2", "h3", "h4", "h5", "h6"} or card_title(node)
        if is_title and not SUBSECTION.search(name):
            titles.setdefault(normalized_text(name), (node, name, structured_method))
    h1s = [entry for entry in titles.values() if entry[0].name == "h1"]
    if len(h1s) != 1:
        return None
    node, name, structured_method = h1s[0]
    others = [
        key
        for key in titles
        if key != normalized_text(name)
        and key not in normalized_text(name)
        and normalized_text(name) not in key
    ]
    if others:
        return None
    root = soup.find("main") or soup.find("article") or soup.body or soup
    spans = [name]
    purchase_link = None
    for block in root.find_all(["p", "li", "a", "button", "span", "strong"]):
        if navigation(block) or not under_course_section(block, node):
            continue
        value = text(block)
        if (
            not value
            or len(value) > 600
            or value in spans
            or normalized_text(value) not in normalized_text(visible)
            or not (
                ONLINE.search(value)
                or PURCHASE.search(value)
                or COURSE.search(value)
                # Hero copy directly under the title describes this product.
                or block.find_previous(re.compile(r"^h[1-6]$")) is node
            )
        ):
            continue
        # Skip containers whose text is already represented by a child span.
        if any(normalized_text(s) in normalized_text(value) for s in spans[1:]) and block.find(
            ["p", "li", "a", "button", "span", "strong"]
        ):
            continue
        spans.append(value)
        if purchase_link is None and block.name == "a" and PURCHASE.search(value):
            purchase_link = block
        if len(spans) >= 30:
            break
    combined = " ".join(spans)
    educational = bool(COURSE.search(combined) or structured_method == "schema_course")
    online = educational and bool(ONLINE.search(combined))
    offered = online and bool(PURCHASE.search(combined)) and not NEGATIVE.search(combined)
    if not online:
        return None
    return Observation(
        value=name,
        source_url=url,
        quote=" ".join(spans)[:4000],
        method=structured_method or "single_course_page",
        landing_url=public_url(url),
        enrollment_text=next((s for s in spans if PURCHASE.search(s)), None),
        enrollment_url=safe_link(purchase_link, url),
        evidence_classification="online_offer" if offered else "online_course",
        supporting_text=spans,
    )
