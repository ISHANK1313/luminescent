"""Static bot configuration.

`/v1/metadata` is read by the judge and shown in its report. The identity values
below are PLACEHOLDERS — replace them before submitting (reference.md item P-1).
Every field can also be overridden with an environment variable so a deployment
never needs a code change:

    VERA_TEAM_NAME, VERA_TEAM_MEMBERS (comma-separated), VERA_MODEL,
    VERA_APPROACH, VERA_CONTACT_EMAIL, VERA_VERSION, VERA_SUBMITTED_AT
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

BOT_VERSION = "0.1.0"

_PLACEHOLDER_TEAM_NAME = "TBD — set VERA_TEAM_NAME before submission"
_PLACEHOLDER_MEMBERS = ["TBD"]
_PLACEHOLDER_EMAIL = "tbd@example.com"

_DEFAULT_MODEL = "glm-4.7-flash @ temperature 0 over a deterministic rules engine (cached; deterministic fallback)"
_DEFAULT_APPROACH = (
    "Deterministic 4-context composer: trigger-kind router -> signal selector -> "
    "slot extractor (facts only from pushed contexts) -> category voice renderer "
    "with hi-en code-mix -> single-CTA policy -> suppression/anti-repetition guardrails. "
    "Stateful conversation handler for auto-reply detection, intent hand-off and graceful exit."
)


def _members_from_env() -> List[str]:
    raw = os.getenv("VERA_TEAM_MEMBERS")
    if not raw:
        return list(_PLACEHOLDER_MEMBERS)
    return [part.strip() for part in raw.split(",") if part.strip()]


def metadata() -> Dict[str, Any]:
    return {
        "team_name": os.getenv("VERA_TEAM_NAME", _PLACEHOLDER_TEAM_NAME),
        "team_members": _members_from_env(),
        "model": os.getenv("VERA_MODEL", _DEFAULT_MODEL),
        "approach": os.getenv("VERA_APPROACH", _DEFAULT_APPROACH),
        "contact_email": os.getenv("VERA_CONTACT_EMAIL", _PLACEHOLDER_EMAIL),
        "version": os.getenv("VERA_VERSION", BOT_VERSION),
        "submitted_at": os.getenv("VERA_SUBMITTED_AT", "2026-08-30T00:00:00Z"),
    }


def metadata_is_placeholder() -> bool:
    md = metadata()
    return md["team_name"] == _PLACEHOLDER_TEAM_NAME or md["contact_email"] == _PLACEHOLDER_EMAIL


__all__ = ["metadata", "metadata_is_placeholder", "BOT_VERSION"]
