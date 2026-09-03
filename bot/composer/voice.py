"""Category voice + language treatment.

Turns a `Draft` into a final body string:
  * correct salutation for the vertical (dentists get "Dr.", per the judge's rubric)
  * Hindi-English code-mix when the recipient's language preference asks for it —
    facts stay in English (they are quoted from the context), the ask switches to
    Hindi, which is exactly the pattern in the brief's own Vera transcripts
  * source citation appended after the ask, matching the brief's model message
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .strategies import Ctx, Draft, humanise


def salutation(ctx: Ctx) -> str:
    """Merchant-facing opener. Returns '' when we have no name to use."""
    slug = str(ctx.category.get("slug") or "")
    owner = ctx.owner.strip()
    name = owner or ctx.biz.strip()
    if not name:
        return ""
    if slug == "dentists" and not name.lower().startswith("dr"):
        return f"Dr. {name}"
    return name


# Only these openers are safe to lower-case after "Name, ...". Anything else is a
# proper noun, an acronym or a number ("Diwali", "DC vs MI", "JIDA", "3-month ...")
# and must keep its original casing.
_LOWERABLE_FIRST_WORDS = frozenset(
    {
        "your",
        "you",
        "you're",
        "we",
        "we're",
        "it's",
        "that's",
        "quick",
        "picking",
        "it",
        "this",
        "the",
        "recall",
        "one",
        "new",
        "there",
        "here",
    }
)


def _decapitalise(hook: str) -> str:
    first = hook.split(" ", 1)[0].strip(",.:;").lower()
    if first in _LOWERABLE_FIRST_WORDS:
        return hook[0].lower() + hook[1:]
    return hook


def wants_hindi(ctx: Ctx) -> bool:
    """Does the recipient's language preference include Hindi?"""
    if ctx.is_customer_facing:
        pref = ctx.cust_lang.lower()
        return "hi" == pref or "hi-en" in pref or pref.startswith("hi ")
    return "hi" in [l.lower() for l in ctx.languages]


def render(draft: Draft, ctx: Ctx) -> str:
    """Assemble the final body. The CTA is always the last sentence by construction."""
    ask = draft.ask
    if wants_hindi(ctx) and draft.hi_ask and draft.hi_ask != draft.ask:
        ask = draft.hi_ask

    parts: List[str] = []

    if not ctx.is_customer_facing:
        opener = salutation(ctx)
        hook = draft.hook
        if opener and hook:
            hook = f"{opener}, {_decapitalise(hook)}"
        elif opener:
            hook = opener
        parts.append(hook)
    else:
        parts.append(draft.hook)

    if draft.anchor:
        parts.append(draft.anchor)
    parts.append(ask)

    body = " ".join(p.strip() for p in parts if p and p.strip())

    if draft.citation:
        body = f"{body} — {draft.citation}"

    # No machine artefacts (ISO dates, '7d' slugs) ever reach a recipient.
    return humanise(" ".join(body.split()))


__all__ = ["render", "salutation", "wants_hindi"]
