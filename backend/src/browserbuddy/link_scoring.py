"""Deterministic session-wide ranking before output truncation."""

from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass
class RankedLinks:
    items: list[dict]
    eligible: int
    omitted: int
    invalid: int


def score_outbound_links(links: list[dict], *, visited_urls: set[str]) -> RankedLinks:
    """Exclude followed destinations and rank unique HTTP links.

    Args:
        links: Snapshot link rows with page URL and novelty flags.
        visited_urls: All page URLs included in the session snapshot.
    """
    clicked = {link["href"] for link in links if link["clicked"]}
    grouped, invalid = {}, 0
    for link in links:
        try:
            url = urlsplit(link["href"])
            valid = url.scheme in ("http", "https") and url.hostname
        except ValueError:
            valid = False
        if not valid:
            invalid += 1
        elif link["href"] not in clicked | visited_urls:
            grouped.setdefault(link["href"], []).append(link)
    ranked = []
    for occurrences in grouped.values():
        best = max(occurrences, key=_score)
        frequency = len({link["page_visit_id"] for link in occurrences})
        ranked.append({**best, "score": _score(best) - min(3, frequency - 1)})
    ranked.sort(key=lambda link: (-link["score"], link["href"]))
    return RankedLinks(ranked[:50], len(ranked), max(0, len(ranked) - 50), invalid)


def _score(link: dict) -> int:
    try:
        offsite = urlsplit(link["href"]).hostname != urlsplit(link["page_url"]).hostname
    except ValueError:
        offsite = False
    anchor = link["anchor_text"].strip().lower()
    descriptive = len(anchor.split()) >= 3 and anchor not in ("click here", "read more")
    return 2 * bool(link["in_main"]) + offsite + bool(link["novel"]) + descriptive
