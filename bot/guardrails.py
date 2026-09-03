"""Output validation — the last line of defence before anything is sent.

Everything here is a pure function of its inputs (rule R2). The composer runs these
checks on the deterministic draft AND on any LLM-polished variant; a variant that
fails validation is discarded in favour of the grounded deterministic body.

Checks map 1:1 onto the judge's penalties:
    URL in body                  -> -3, hard fail   (api-call-examples.md F.4)
    fabricated number            -> -2              (brief §11)
    category taboo word          -> category-fit collapse (rules.md R9)
    internal jargon leaked       -> -1              (rules.md R27/R35)
    multiple or buried CTA       -> -2 / -1         (rules.md R6/R12)
    repeated body                -> -2              (handled in state.py)
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

URL_RE = re.compile(r"(https?://|www\.|\b[a-z0-9-]+\.(com|in|org|net|co)\b)", re.IGNORECASE)
NUMBER_RE = re.compile(r"\d[\d,]*\.?\d*")
PLACEHOLDER_RE = re.compile(r"\{\{?\s*\w+\s*\}?\}")
# Machine spans that must never ship in a body: ISO dates and bare '7d'/'30d' unit slugs.
RAW_SLUG_RE = re.compile(r"(\b\d{4}-\d{2}-\d{2}\b|\b\d+[dwmy]\b(?![a-z]))")

# Machine-facing vocabulary that must never reach a merchant or customer.
JARGON_TOKENS = (
    "merchant_id",
    "customer_id",
    "trigger_id",
    "suppression_key",
    "context_id",
    "category_slug",
    "conversation_id",
    "send_as",
    "payload",
    "urgency",
    "expires_at",
    "vocab_taboo",
    "peer_stats",
    "offer_catalog",
    "customer_aggregate",
    "delta_7d",
    "_pct",
    "lapsed_180d",
    "ctr_below_peer_median",
    "stale_posts",
    "dormant_with_vera",
    "high_risk_adult_cohort",
    "engaged_in_last",
    "perf_dip",
    "perf_spike",
    "winback_eligible",
    "trial_ending_soon",
    "unverified_gbp",
    "no_active_offers",
    "lapsed_soft",
    "lapsed_hard",
    "recall_due",
    "research_digest",
    "null",
    "none",
    "todo",
    "tbd",
)

# CTA markers: an imperative ask (a direct question is detected separately via "?").
CTA_MARKERS = (
    "reply ",
    "want me",
    "shall i",
    "shall we",
    "should i",
    "chahiye",
    "bhej dun",
    "bhej dijiye",
    "bata dijiye",
    "bol dijiye",
    "bataiye",
    "batayein",
    "kar dun",
    "kijiye",
    "tell us",
    "let me know",
    "say the word",
    "confirm",
)

MAX_BODY_CHARS = 700


def sentences(body: str) -> List[str]:
    parts = re.split(r"(?<=[.!?])\s+", body.strip())
    return [p for p in parts if p.strip()]


def numbers_in(text: str) -> Set[str]:
    """Numeric tokens, normalised so 2,100 == 2100 and 38.0 == 38."""
    out: Set[str] = set()
    for raw in NUMBER_RE.findall(text or ""):
        cleaned = raw.replace(",", "")
        if cleaned.endswith("."):
            cleaned = cleaned[:-1]
        if "." in cleaned:
            cleaned = cleaned.rstrip("0").rstrip(".")
        if cleaned:
            out.add(cleaned)
    return out


def taboo_words(category: Optional[Dict[str, Any]]) -> List[str]:
    """Read whichever key the category uses (reference.md gotcha G12)."""
    voice = (category or {}).get("voice") or {}
    raw = voice.get("vocab_taboo") or voice.get("taboos") or []
    out: List[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        # Entries like "FDA-approved (use only when actually applicable)" -> match the head.
        head = item.split("(")[0].strip().lower()
        if head:
            out.append(head)
    return sorted(set(out))


def count_ctas(body: str) -> int:
    """How many distinct asks the body makes.

    A single ask may legitimately carry several markers ("reply 1 or 2, or tell us a
    time"), so questions are the unit of counting: two question marks means the
    merchant has been asked two things, which is the penalised pattern.
    """
    return (body or "").count("?")


def cta_is_present_and_last(body: str) -> Tuple[bool, bool]:
    """Return `(present, in_last_sentence)`.

    A trailing source citation ("... — JIDA Oct 2026, p.14") is not a sentence and
    does not count as burying the CTA: the brief's own model message ends exactly
    that way.
    """
    parts = sentences(body)
    while len(parts) > 1 and parts[-1].lstrip().startswith(("—", "–", "-")):
        parts = parts[:-1]
    if not parts:
        return False, False
    tail = parts[-1].lower()
    questions = count_ctas(body)
    has_marker = any(m in tail for m in CTA_MARKERS)
    present = questions >= 1 or has_marker
    in_last = tail.endswith("?") or ("?" in tail) or has_marker
    return present, in_last


def validate_body(
    body: str,
    category: Optional[Dict[str, Any]] = None,
    allowed_numbers: Optional[Iterable[str]] = None,
    require_cta: bool = True,
) -> Tuple[bool, List[str]]:
    """Return `(ok, problems)`. Any problem means: do not send this body."""
    problems: List[str] = []

    if not body or not body.strip():
        return False, ["empty_body"]

    if len(body) > MAX_BODY_CHARS:
        problems.append(f"too_long:{len(body)}")

    if URL_RE.search(body):
        problems.append("contains_url")

    if PLACEHOLDER_RE.search(body):
        problems.append("unfilled_placeholder")

    lowered = f" {body.lower()} "
    for word in taboo_words(category):
        if word and word in lowered:
            problems.append(f"taboo:{word}")

    for token in JARGON_TOKENS:
        needle = token.lower()
        if needle in ("null", "none", "todo", "tbd"):
            if re.search(rf"\b{re.escape(needle)}\b", lowered):
                problems.append(f"jargon:{token}")
        elif needle in lowered:
            problems.append(f"jargon:{token}")

    if "#" in body or "//" in body:
        problems.append("code_comment_in_body")

    slug = RAW_SLUG_RE.search(body)
    if slug:
        problems.append(f"raw_slug:{slug.group()}")

    questions = count_ctas(body)
    if questions > 1:
        problems.append(f"multiple_ctas:{questions}")

    if require_cta:
        present, in_last = cta_is_present_and_last(body)
        if not present:
            problems.append("no_cta")
        elif not in_last:
            problems.append("cta_not_in_last_sentence")

    if allowed_numbers is not None:
        allowed = set(allowed_numbers)
        used = numbers_in(body)
        invented = sorted(n for n in used if n not in allowed)
        if invented:
            problems.append(f"unverified_numbers:{','.join(invented[:5])}")

    return (not problems), problems


def has_specific_anchor(body: str) -> bool:
    """At least one verifiable anchor: a number, a price or a citation dash."""
    return bool(NUMBER_RE.search(body or "")) or "—" in (body or "")


__all__ = [
    "validate_body",
    "numbers_in",
    "taboo_words",
    "count_ctas",
    "cta_is_present_and_last",
    "sentences",
    "has_specific_anchor",
    "MAX_BODY_CHARS",
]
