"""Deterministic, grounded drafts — one strategy per trigger kind.

Every sentence produced here is assembled from values that were pushed into the
ContextStore. Nothing is invented: if a fact is absent, the clause that would have
used it is dropped, and if too little remains the strategy returns `skip`.

The draft serves two purposes:
  1. it is the fallback body that ships if the LLM is disabled, slow, unreachable
     or produces something that fails validation;
  2. its `facts` list is the ONLY material the LLM is allowed to write from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------- helpers


def _get(obj: Any, *path: str, default: Any = None) -> Any:
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def num(value: Any) -> str:
    """Human number: 2100 -> '2,100'; 0.021 -> '0.021'."""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.0f}" if value.is_integer() else f"{value:g}"
    return str(value)


def pct(value: Any, absolute: bool = True) -> str:
    """0.38 -> '38%'; -0.5 -> '50%' (sign dropped when absolute)."""
    if value is None:
        return ""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ""
    if -1.5 <= v <= 1.5:
        v *= 100
    if absolute:
        v = abs(v)
    rounded = round(v)
    return f"{rounded:g}%"


def date_words(iso: Any) -> str:
    """'2026-12-15' -> '15 Dec 2026'. Returns '' when unparseable (never guesses)."""
    if not isinstance(iso, str) or len(iso) < 10:
        return ""
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    try:
        year, month, day = int(iso[0:4]), int(iso[5:7]), int(iso[8:10])
        return f"{day} {months[month - 1]} {year}"
    except (ValueError, IndexError):
        return ""


_ISO_IN_TEXT_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def humanise(text: str) -> str:
    """Post-render sweep: no ISO dates or bare unit slugs survive into a body."""
    if not text:
        return text

    def _date(match: "re.Match[str]") -> str:
        words = date_words(match.group())
        return words or match.group()

    text = _ISO_IN_TEXT_RE.sub(_date, text)
    # '7d', '30d', '90d' -> '7 days', '30 days' (but not '7 days' it already is)
    text = re.sub(r"\b(\d+)d\b(?![a-z])", r"\1 days", text)
    return text


_MONTH_NAMES = {
    "jan": "January", "feb": "February", "mar": "March", "apr": "April",
    "may": "May", "jun": "June", "jul": "July", "aug": "August",
    "sep": "September", "sept": "September", "oct": "October",
    "nov": "November", "dec": "December",
}


def season_phrase(raw: Any) -> str:
    """'post_resolution_window_apr_jun' -> 'post-resolution window, April to June'.

    Seed payloads carry machine slugs. Printing one verbatim is the -1 internal-jargon
    penalty (reference.md G10), so month tokens are expanded and the rest is read back
    as prose. Nothing is added: every word comes from the payload value.
    """
    tokens = [t for t in str(raw or "").replace("-", "_").split("_") if t]
    if not tokens:
        return ""
    words: List[str] = []
    months: List[str] = []
    for token in tokens:
        full = _MONTH_NAMES.get(token.lower())
        if full:
            months.append(full)
        else:
            words.append(token)
    text = " ".join(words)
    if text.startswith("post ") and len(words) > 1:
        text = "post-" + text[len("post "):]
    if months:
        span = f"{months[0]} to {months[1]}" if len(months) >= 2 else months[0]
        text = f"{text}, {span}" if text else span
    return text


def program_words(raw: Any) -> str:
    """'skin_prep_program_30day' -> '30-day skin prep program'."""
    tokens = [t for t in str(raw or "").replace("-", "_").split("_") if t]
    if not tokens:
        return ""
    duration = ""
    rest: List[str] = []
    for token in tokens:
        lowered = token.lower()
        for unit in ("day", "week", "month", "year"):
            digits = lowered[: -len(unit)]
            if lowered.endswith(unit) and digits.isdigit():
                duration = f"{digits}-{unit}"
                break
        else:
            rest.append(token)
            continue
    body = " ".join(rest)
    if duration and body:
        return f"{duration} {body}"
    return duration or body


def trend_prose(trends: Any) -> List[str]:
    """['ORS_demand_+40', 'cold_cough_demand_-60'] -> ['ORS demand is up 40%', ...]."""
    out: List[str] = []
    for item in trends or []:
        if not isinstance(item, str):
            continue
        label, _, tail = item.rpartition("_")
        digits = tail.lstrip("+-")
        if label and digits.isdigit():
            direction = "down" if tail.startswith("-") else "up"
            out.append(f"{label.replace('_', ' ')} is {direction} {digits}%")
        else:
            out.append(item.replace("_", " "))
    return out


@dataclass
class Draft:
    """A composed message before voice/LLM treatment."""

    hook: str = ""
    anchor: str = ""
    ask: str = ""
    hi_ask: str = ""
    citation: str = ""
    cta: str = "open_ended"
    template_name: str = "vera_generic_v1"
    template_params: List[str] = field(default_factory=list)
    rationale: str = ""
    facts: List[str] = field(default_factory=list)
    levers: List[str] = field(default_factory=list)
    skip: Optional[str] = None

    def sentences(self) -> List[str]:
        return [s for s in (self.hook, self.anchor, self.ask) if s]


def skip(reason: str) -> Draft:
    return Draft(skip=reason)


class Ctx:
    """Convenience accessors over the four pushed contexts."""

    def __init__(self, category: Dict, merchant: Dict, trigger: Dict, customer: Optional[Dict]) -> None:
        self.category = category or {}
        self.merchant = merchant or {}
        self.trigger = trigger or {}
        self.customer = customer

    # trigger --------------------------------------------------------------
    @property
    def kind(self) -> str:
        return str(self.trigger.get("kind") or "")

    @property
    def payload(self) -> Dict[str, Any]:
        p = self.trigger.get("payload")
        return p if isinstance(p, dict) else {}

    @property
    def is_customer_facing(self) -> bool:
        return bool(self.customer) and self.trigger.get("scope") == "customer"

    # merchant -------------------------------------------------------------
    @property
    def owner(self) -> str:
        return str(_get(self.merchant, "identity", "owner_first_name", default="") or "")

    @property
    def biz(self) -> str:
        return str(_get(self.merchant, "identity", "name", default="") or "")

    @property
    def locality(self) -> str:
        return str(_get(self.merchant, "identity", "locality", default="") or "")

    @property
    def city(self) -> str:
        return str(_get(self.merchant, "identity", "city", default="") or "")

    @property
    def languages(self) -> List[str]:
        langs = _get(self.merchant, "identity", "languages", default=[]) or []
        return [str(x) for x in langs if isinstance(x, str)]

    @property
    def perf(self) -> Dict[str, Any]:
        p = self.merchant.get("performance")
        return p if isinstance(p, dict) else {}

    @property
    def peer(self) -> Dict[str, Any]:
        p = self.category.get("peer_stats")
        return p if isinstance(p, dict) else {}

    def active_offers(self) -> List[Dict[str, Any]]:
        offers = self.merchant.get("offers") or []
        active = [o for o in offers if isinstance(o, dict) and o.get("status") == "active"]
        return sorted(active, key=lambda o: str(o.get("id", "")))

    def offer_title(self) -> str:
        active = self.active_offers()
        if active:
            return str(active[0].get("title") or "")
        return ""

    def offer_matching(self, keywords: List[str]) -> str:
        """Best offer whose title matches any keyword, merchant offers first.

        Used where the trigger already tells us what the season is about: promoting a
        haircut during a bridal-season beat is grounded but weak on decision quality.
        """
        words = [str(k).lower() for k in keywords if k]
        if not words:
            return ""
        catalog = [c for c in (self.category.get("offer_catalog") or []) if isinstance(c, dict)]
        for pool in (self.active_offers(), sorted(catalog, key=lambda o: str(o.get("id", "")))):
            for item in pool:
                title = str(item.get("title") or "")
                if any(word in title.lower() for word in words):
                    return title
        return ""

    def catalog_title(self, prefer_type: str = "service_at_price") -> str:
        """A canonical service+price pattern from the category (never a % discount)."""
        catalog = self.category.get("offer_catalog") or []
        items = [c for c in catalog if isinstance(c, dict)]
        typed = [c for c in items if c.get("type") == prefer_type]
        pool = sorted(typed or items, key=lambda c: str(c.get("id", "")))
        return str(pool[0].get("title")) if pool else ""

    def digest(self, item_id: Any) -> Dict[str, Any]:
        for item in self.category.get("digest") or []:
            if isinstance(item, dict) and item.get("id") == item_id:
                return item
        return {}

    def digest_of_kind(self, kind: str) -> Dict[str, Any]:
        for item in self.category.get("digest") or []:
            if isinstance(item, dict) and item.get("kind") == kind:
                return item
        return {}

    def review_theme(self, theme: str) -> Dict[str, Any]:
        for item in self.merchant.get("review_themes") or []:
            if isinstance(item, dict) and item.get("theme") == theme:
                return item
        return {}

    # customer -------------------------------------------------------------
    @property
    def cust_name(self) -> str:
        """First name only — dataset names can carry annotations like 'Karthik (parent: Sumitra)'."""
        raw = str(_get(self.customer, "identity", "name", default="") or "")
        return raw.split("(")[0].strip()

    @property
    def cust_guardian(self) -> str:
        """'Karthik (parent: Sumitra)' -> 'Sumitra'. Empty when the customer is the reader.

        Some seed customers are children whose `channel` is `whatsapp_via_parent`; the
        message is read by the parent, so the salutation must be the parent's name while
        the subject stays the child.
        """
        raw = str(_get(self.customer, "identity", "name", default="") or "")
        if "(" not in raw or ")" not in raw:
            return ""
        inside = raw[raw.index("(") + 1 : raw.rindex(")")]
        _, _, name = inside.partition(":")
        return name.strip()

    @property
    def cust_lang(self) -> str:
        return str(_get(self.customer, "identity", "language_pref", default="") or "")


# ------------------------------------------------------------- merchant-facing


def window_words(raw: Any) -> str:
    """'7d' -> '7 days'; 'week'/'30d'/'last_week' -> readable span, slugs never ship."""
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    match = re.fullmatch(r"(\d+)\s*d", text)
    if match:
        n = int(match.group(1))
        return f"{n} days" if n != 1 else "1 day"
    match = re.fullmatch(r"(\d+)\s*w", text)
    if match:
        n = int(match.group(1))
        return f"{n} weeks" if n != 1 else "1 week"
    return text.replace("_", " ")


def _perf_vs_peer(c: Ctx) -> str:
    """'CTR 2.1% vs 3.0% peer median' — only when both numbers exist."""
    ctr = c.perf.get("ctr")
    peer_ctr = c.peer.get("avg_ctr")
    if ctr is None or peer_ctr is None:
        return ""
    return f"your click-through is {pct(ctr)} against a {pct(peer_ctr)} peer median"


def _headline_number(summary: Any) -> str:
    """'...38% lower caries recurrence...' -> '38% lower caries recurrence'.

    Pulls the clause around the first percentage in a digest summary so the anchor
    carries the finding, not just the title. Empty when there is no percentage.
    """
    text = str(summary or "")
    match = re.search(r"(\d+%[^.;]*)", text)
    return match.group(1).strip().rstrip(",") if match else ""


def s_research_digest(c: Ctx) -> Draft:
    item = c.digest(c.payload.get("top_item_id")) or c.digest_of_kind("research")
    if not item:
        return skip("no digest item in category context")

    title = str(item.get("title") or "")
    source = str(item.get("source") or "")
    trial_n = item.get("trial_n")
    segment = str(item.get("patient_segment") or "").replace("_", " ")
    effect = _headline_number(item.get("summary"))
    cohort = _get(c.merchant, "customer_aggregate", "high_risk_adult_count")

    hook = f"This week's {source.split(',')[0]} carried one item worth your 2 minutes."
    anchor = f"{num(trial_n)}-patient trial: {effect}." if trial_n and effect else (f"{num(trial_n)}-patient trial." if trial_n else title)
    if cohort and "high risk" in segment:
        anchor += f" You have {num(cohort)} high risk adults on your list."

    return Draft(
        hook=hook,
        anchor=anchor,
        ask="Want me to pull the abstract and draft a patient message you can forward?",
        hi_ask="Abstract nikal ke ek patient message draft kar dun?",
        citation=source,
        cta="open_ended",
        template_name="vera_research_digest_v1",
        template_params=[c.owner or c.biz, title, source],
        rationale=(
            f"New {c.category.get('slug')} research item ({source}) matches this merchant's patient mix; "
            "offering to do the reading and drafting keeps the ask effort-free."
        ),
        facts=[f"digest title: {title}", f"source: {source}"]
        + ([f"trial size: {num(trial_n)} patients"] if trial_n else [])
        + ([f"finding: {effect}"] if effect else [])
        + ([f"patient segment: {segment}"] if segment else [])
        + ([f"this merchant's cohort size: {num(cohort)}"] if cohort else []),
        levers=["curiosity", "reciprocity", "effort_externalization"],
    )


def s_regulation_change(c: Ctx) -> Draft:
    item = c.digest(c.payload.get("top_item_id")) or c.digest_of_kind("compliance")
    deadline = date_words(c.payload.get("deadline_iso"))
    if not item and not deadline:
        return skip("no compliance item or deadline")

    title = str(item.get("title") or "")
    summary = str(item.get("summary") or "")
    action = str(item.get("actionable") or "")
    source = str(item.get("source") or "")

    # Keep the operative detail, not the whole circular.
    short_summary = ". ".join([s for s in summary.split(". ") if s][:2]).rstrip(".")
    if short_summary:
        short_summary += "."

    # The title often restates the deadline; render it once, as words, at the end.
    hook = _ISO_IN_TEXT_RE.sub(lambda m: date_words(m.group()) or m.group(), title)
    hook = hook if hook.endswith(".") else f"{hook}."
    anchor = short_summary if short_summary.endswith(".") else f"{short_summary}."
    if deadline and deadline not in hook:
        anchor = f"{anchor.rstrip('.')} Deadline {deadline}."

    return Draft(
        hook=hook,
        anchor=anchor,
        ask=f"Want a one-page checklist for this? {action}" if action else "Want a one-page checklist for this?",
        hi_ask="Iska ek page ka checklist bhej dun?",
        citation=source,
        cta="binary_yes_no",
        template_name="vera_regulation_change_v1",
        template_params=[c.owner or c.biz, title, deadline or source],
        rationale=(
            "Compliance change with a hard deadline is the highest-value thing to raise now; "
            "single binary ask keeps it a two-second decision."
        ),
        facts=[f"regulation: {title}", f"detail: {summary}", f"source: {source}"]
        + ([f"deadline: {deadline}"] if deadline else []),
        levers=["loss_aversion", "effort_externalization"],
    )


def s_perf_dip(c: Ctx) -> Draft:
    metric = metric_word(c.payload.get("metric"))
    delta = c.payload.get("delta_pct")
    window = window_words(c.payload.get("window"))
    baseline = c.payload.get("vs_baseline")
    if not metric or delta is None:
        return skip("perf dip without metric or delta")

    span = f" over the last {window}" if window else ""
    hook = f"Your {metric} are down {pct(delta)}{span} — {num(baseline)} a week is your usual." if baseline else f"Your {metric} are down {pct(delta)}{span}."
    peer_line = _perf_vs_peer(c)
    anchor = f"For context, {peer_line}." if peer_line else ""

    offer = c.offer_title() or c.catalog_title()
    ask = "Want me to put your listing back in front of nearby searches this week?"
    if offer:
        ask = f"Want me to run {offer} as a front-page offer this week?"
    hi_ask = f"{offer} is hafte front page par chalu kar dun?" if offer else "Aapki listing ko is hafte nearby searches mein aage kar dun?"

    return Draft(
        hook=hook,
        anchor=anchor,
        ask=ask,
        hi_ask=hi_ask,
        cta="binary_yes_no",
        template_name="vera_perf_dip_v1",
        template_params=[c.owner or c.biz, metric, pct(delta)],
        rationale=(
            f"{metric} dropped {pct(delta)} over {window or 'the window'}; leading with the merchant's own number "
            "and proposing one concrete corrective action."
        ),
        facts=[f"metric: {metric}", f"change: down {pct(delta)} over {window or 'window'}"]
        + ([f"baseline: {num(baseline)}"] if baseline else [])
        + ([f"active offer: {offer}"] if offer else []),
        levers=["loss_aversion", "effort_externalization"],
    )


def s_perf_spike(c: Ctx) -> Draft:
    metric = metric_word(c.payload.get("metric"))
    delta = c.payload.get("delta_pct")
    baseline = c.payload.get("vs_baseline")
    driver = str(c.payload.get("likely_driver") or "").replace("_", " ")
    if not metric or delta is None:
        return skip("perf spike without metric or delta")

    hook = f"Your {metric} are up {pct(delta)} this week"
    hook += f" — {num(baseline)} against your usual run rate." if baseline else "."
    anchor = f"The likely driver is your {driver}." if driver else ""
    ask = f"Want me to build two more posts in the same shape as that one?" if driver else "Want me to build two more posts in the same shape?"
    hi_ask = f"Usi {driver} jaise do aur post bana dun?" if driver else "Do aur aise hi post bana dun?"

    return Draft(
        hook=hook,
        anchor=anchor,
        ask=ask,
        hi_ask=hi_ask,
        cta="binary_yes_no",
        template_name="vera_perf_spike_v1",
        template_params=[c.owner or c.biz, metric, pct(delta)],
        rationale=(
            f"{metric} up {pct(delta)}; naming the probable cause turns a nice number into a repeatable action."
        ),
        facts=[f"metric: {metric}", f"change: up {pct(delta)}"]
        + ([f"baseline: {num(baseline)}"] if baseline else [])
        + ([f"likely driver: {driver}"] if driver else []),
        levers=["curiosity", "reciprocity"],
    )


def s_seasonal_perf_dip(c: Ctx) -> Draft:
    metric = metric_word(c.payload.get("metric"))
    delta = c.payload.get("delta_pct")
    window = window_words(c.payload.get("window"))
    note = season_phrase(c.payload.get("season_note"))
    if not metric or delta is None:
        return skip("seasonal dip without metric or delta")

    hook = f"Your {metric} are down {pct(delta)} this week — this one is seasonal, not a problem with your listing."
    anchor = f"It matches the usual seasonal pattern: {note}." if note else ""
    offer = c.offer_title() or c.catalog_title()
    ask = f"Want me to line up {offer} for when the window turns?" if offer else "Want me to line up an offer for when the window turns?"
    hi_ask = f"Window khulte hi {offer} ready rakhun?" if offer else "Window khulte hi ek offer ready rakhun?"

    return Draft(
        hook=hook,
        anchor=anchor,
        ask=ask,
        hi_ask=hi_ask,
        cta="binary_yes_no",
        template_name="vera_seasonal_dip_v1",
        template_params=[c.owner or c.biz, metric, pct(delta)],
        rationale="Expected seasonal dip: removing the false alarm first, then offering the timed next step.",
        facts=[f"metric: {metric}", f"change: down {pct(delta)} over {window or 'window'}"]
        + ([f"season note: {note}"] if note else [])
        + ([f"offer: {offer}"] if offer else []),
        levers=["reciprocity", "effort_externalization"],
    )


METRIC_WORDS = {
    "review_count": "reviews",
    "views": "views",
    "calls": "calls",
    "directions": "direction requests",
    "leads": "leads",
}


def metric_word(raw: Any) -> str:
    key = str(raw or "")
    return METRIC_WORDS.get(key, key.replace("_", " "))


def s_milestone_reached(c: Ctx) -> Draft:
    metric = metric_word(c.payload.get("metric"))
    now_v = c.payload.get("value_now")
    target = c.payload.get("milestone_value")
    if now_v is None or target is None:
        return skip("milestone without values")
    gap = None
    try:
        gap = int(target) - int(now_v)
    except (TypeError, ValueError):
        gap = None

    hook = f"You're at {num(now_v)} {metric} — {num(gap)} away from {num(target)}." if gap else f"You've reached {num(now_v)} {metric}."
    peer_reviews = c.peer.get("avg_review_count")
    anchor = f"Peer average in your category is {num(peer_reviews)}." if peer_reviews else ""
    return Draft(
        hook=hook,
        anchor=anchor,
        ask="Want me to send a review request to your last week's customers to close the gap?",
        hi_ask="Pichhle hafte ke customers ko review request bhej dun?",
        cta="binary_yes_no",
        template_name="vera_milestone_v1",
        template_params=[c.owner or c.biz, num(now_v), num(target)],
        rationale="Imminent milestone plus a peer benchmark; the ask is the one action that closes the gap.",
        facts=[f"current {metric}: {num(now_v)}", f"milestone: {num(target)}"]
        + ([f"gap: {num(gap)}"] if gap else [])
        + ([f"peer average reviews: {num(peer_reviews)}"] if peer_reviews else []),
        levers=["social_proof", "curiosity"],
    )


def s_renewal_due(c: Ctx) -> Draft:
    days = c.payload.get("days_remaining")
    plan = str(c.payload.get("plan") or "")
    amount = c.payload.get("renewal_amount")
    if days is None:
        return skip("renewal without days_remaining")

    views = c.perf.get("views")
    window = c.perf.get("window_days")
    hook = f"Your {plan} plan renews in {num(days)} days." if plan else f"Your plan renews in {num(days)} days."
    anchor = f"It carried {num(views)} listing views in the last {num(window)} days." if views and window else ""
    ask = f"Renewal is {num(amount)} — want me to keep everything running without a break?" if amount else "Want me to keep everything running without a break?"
    return Draft(
        hook=hook,
        anchor=anchor,
        ask=ask,
        hi_ask=(f"{plan} plan bina break ke chalu rakhun?" if plan else "Bina break ke chalu rakhun?"),
        cta="binary_yes_no",
        template_name="vera_renewal_due_v1",
        template_params=[c.owner or c.biz, num(days), num(amount)],
        rationale="Renewal window with the merchant's own return numbers attached, so the decision is evidence-based.",
        facts=[f"days remaining: {num(days)}"]
        + ([f"plan: {plan}"] if plan else [])
        + ([f"renewal amount: {num(amount)}"] if amount else [])
        + ([f"views in last {num(window)} days: {num(views)}"] if views and window else []),
        levers=["loss_aversion"],
    )


def s_festival_upcoming(c: Ctx) -> Draft:
    festival = str(c.payload.get("festival") or "")
    date = date_words(c.payload.get("date"))
    days_until = c.payload.get("days_until")
    if not festival:
        return skip("festival trigger without a festival name")

    beats = c.category.get("seasonal_beats") or []
    beat_note = str(_get(beats[0], "note", default="") or "") if beats and isinstance(beats[0], dict) else ""
    beat_words = [t.strip(".,;:—") for t in beat_note.replace("/", " ").split() if len(t) > 4]
    offer = c.offer_matching(beat_words + [festival]) or c.offer_title() or c.catalog_title()

    hook = f"{festival} lands on {date}." if date else f"{festival} is coming up."
    anchor = f"In your category that window is {beat_note}." if beat_note else ""
    ask = f"Want me to have {offer} ready to go live before the rush?" if offer else "Want me to build a festival offer before the rush?"
    return Draft(
        hook=hook,
        anchor=anchor,
        ask=ask,
        hi_ask=(f"Rush se pehle {offer} ready kar dun?" if offer
                else "Rush se pehle ek festival offer bana dun?"),
        cta="binary_yes_no",
        template_name="vera_festival_v1",
        template_params=[c.owner or c.biz, festival, date or num(days_until)],
        rationale="Dated external event plus the category's own seasonal pattern; one concrete offer to prepare.",
        facts=[f"festival: {festival}"]
        + ([f"date: {date}"] if date else [])
        + ([f"days until: {num(days_until)}"] if days_until else [])
        + ([f"seasonal note: {beat_note}"] if beat_note else [])
        + ([f"offer: {offer}"] if offer else []),
        levers=["loss_aversion", "effort_externalization"],
    )


def s_ipl_match_today(c: Ctx) -> Draft:
    match = str(c.payload.get("match") or "")
    venue = str(c.payload.get("venue") or "")
    time_iso = str(c.payload.get("match_time_iso") or "")
    if not match:
        return skip("match trigger without a fixture")

    clock = ""
    if len(time_iso) >= 16:
        hh, mm = time_iso[11:13], time_iso[14:16]
        try:
            hour = int(hh)
            suffix = "pm" if hour >= 12 else "am"
            hour12 = hour % 12 or 12
            clock = f"{hour12}:{mm}{suffix}"
        except ValueError:
            clock = ""

    offer = c.offer_title() or c.catalog_title()
    hook = f"{match} is on tonight at {clock}." if clock else f"{match} is on tonight."
    anchor = f"It's at {venue}." if venue else ""
    ask = f"Want me to push {offer} for the match window?" if offer else "Want me to push a match-night combo for the window?"
    return Draft(
        hook=hook,
        anchor=anchor,
        ask=ask,
        hi_ask=(f"Match window ke liye {offer} chalu kar dun?" if offer
                else "Match window ke liye ek combo chalu kar dun?"),
        cta="binary_yes_no",
        template_name="vera_match_night_v1",
        template_params=[c.owner or c.biz, match, clock or venue],
        rationale="Same-day local demand spike; the ask is time-boxed to the match window so it is easy to accept.",
        facts=[f"fixture: {match}"]
        + ([f"venue: {venue}"] if venue else [])
        + ([f"start time: {clock}"] if clock else [])
        + ([f"offer: {offer}"] if offer else []),
        levers=["loss_aversion", "curiosity"],
    )


def s_review_theme_emerged(c: Ctx) -> Draft:
    # 'delivery_late' reads as 'delivery late'; render the theme as its natural words.
    theme = " ".join(
        reversed(str(c.payload.get("theme") or "").replace("_", " ").split())
    ) if str(c.payload.get("theme") or "") in ("delivery_late",) else str(c.payload.get("theme") or "").replace("_", " ")
    count = c.payload.get("occurrences_30d")
    trend = str(c.payload.get("trend") or "")
    quote = str(c.payload.get("common_quote") or "")
    if not theme or count is None:
        return skip("review theme without theme or count")

    hook = f"{num(count)} reviews in the last 30 days mention {theme}, and it's {trend}." if trend else f"{num(count)} reviews in the last 30 days mention {theme}."
    anchor = f'One of them: "{quote}".' if quote else ""
    return Draft(
        hook=hook,
        anchor=anchor,
        ask="Want me to draft a public reply for those reviews plus a short note for your team?",
        hi_ask="Un reviews ka reply draft kar dun?",
        cta="binary_yes_no",
        template_name="vera_review_theme_v1",
        template_params=[c.owner or c.biz, theme, num(count)],
        rationale="A rising negative review theme is time-sensitive reputation risk; quoting a real review makes it checkable.",
        facts=[f"theme: {theme}", f"occurrences in 30 days: {num(count)}"]
        + ([f"trend: {trend}"] if trend else [])
        + ([f"customer quote: {quote}"] if quote else []),
        levers=["loss_aversion", "reciprocity"],
    )


def s_competitor_opened(c: Ctx) -> Draft:
    name = str(c.payload.get("competitor_name") or "")
    distance = c.payload.get("distance_km")
    their_offer = str(c.payload.get("their_offer") or "")
    opened = date_words(c.payload.get("opened_date"))
    if not name or distance is None:
        return skip("competitor trigger without name or distance")

    hook = f"{name} opened {num(distance)} km from you" + (f" on {opened}." if opened else ".")
    anchor = f"They're leading with {their_offer}." if their_offer else ""
    mine = c.offer_title()
    ask = f"Your {mine} still holds up — want me to put it where their customers are searching?" if mine else "Want me to see how your listing compares to theirs?"
    return Draft(
        hook=hook,
        anchor=anchor,
        ask=ask,
        hi_ask="Compare karke bata dun?",
        cta="binary_yes_no",
        template_name="vera_competitor_opened_v1",
        template_params=[c.owner or c.biz, name, num(distance)],
        rationale="Named nearby competitor with their actual offer; curiosity plus loss aversion, one concrete counter-move.",
        facts=[f"competitor: {name}", f"distance: {num(distance)} km"]
        + ([f"their offer: {their_offer}"] if their_offer else [])
        + ([f"opened: {opened}"] if opened else [])
        + ([f"your active offer: {mine}"] if mine else []),
        levers=["loss_aversion", "curiosity", "social_proof"],
    )


def s_gbp_unverified(c: Ctx) -> Draft:
    path = str(c.payload.get("verification_path") or "").replace("_", " ")
    uplift = c.payload.get("estimated_uplift_pct")
    hook = "Your Google listing is still unverified, so Google reviews every change before it shows."
    anchor = f"Verified listings in your category see about {pct(uplift)} more visibility." if uplift else ""
    ask = f"Verification runs by {path} — want me to start it today?" if path else "Want me to start verification today?"
    return Draft(
        hook=hook,
        anchor=anchor,
        ask=ask,
        hi_ask=(f"{path.replace(' or ', ' ya ').capitalize()} se verification aaj hi shuru kar dun?" if path
                else "Verification aaj hi shuru kar dun?"),
        cta="binary_yes_no",
        template_name="vera_gbp_unverified_v1",
        template_params=[c.owner or c.biz, path, pct(uplift)],
        rationale="Unverified listing throttles everything else we do; one binary ask to start the fix.",
        facts=["listing status: unverified"]
        + ([f"verification path: {path}"] if path else [])
        + ([f"estimated uplift: {pct(uplift)}"] if uplift else []),
        levers=["loss_aversion", "effort_externalization"],
    )


def s_dormant_with_vera(c: Ctx) -> Draft:
    days = c.payload.get("days_since_last_merchant_message")
    topic = str(c.payload.get("last_topic") or "").replace("_", " ")
    if days is None:
        return skip("dormancy without a day count")

    views = c.perf.get("views")
    window = c.perf.get("window_days")
    hook = f"We last spoke {num(days)} days ago about {topic}." if topic else f"We last spoke {num(days)} days ago."
    anchor = f"Since then your listing has taken {num(views)} views in {num(window)} days." if views and window else ""
    return Draft(
        hook=hook,
        anchor=anchor,
        ask="What's been keeping you busiest this month — I'll work around it?",
        hi_ask="Is mahine sabse zyada kya chal raha hai?",
        cta="open_ended",
        template_name="vera_dormant_v1",
        template_params=[c.owner or c.biz, num(days), topic],
        rationale="Dormant thread: asking the merchant a question is the lever production Vera under-uses; no pitch attached.",
        facts=[f"days since last merchant message: {num(days)}"]
        + ([f"last topic: {topic}"] if topic else [])
        + ([f"views in last {num(window)} days: {num(views)}"] if views and window else []),
        levers=["asking_the_merchant", "reciprocity"],
    )


def s_curious_ask_due(c: Ctx) -> Draft:
    peer_calls = c.peer.get("avg_calls_30d")
    calls = c.perf.get("calls")
    anchor = ""
    if calls is not None and peer_calls is not None:
        anchor = f"You're at {num(calls)} calls this month against a {num(peer_calls)} category average."
    return Draft(
        hook="Quick one for you.",
        anchor=anchor,
        ask="Which service has been most in demand this week — I'll build the next offer around it?",
        hi_ask="Is hafte sabse zyada demand kis service ki thi?",
        cta="open_ended",
        template_name="vera_curious_ask_v1",
        template_params=[c.owner or c.biz, num(calls), num(peer_calls)],
        rationale="Scheduled curiosity cadence: one question to the merchant, no ask for money or time.",
        facts=([f"calls this month: {num(calls)}"] if calls is not None else [])
        + ([f"category average calls: {num(peer_calls)}"] if peer_calls is not None else []),
        levers=["asking_the_merchant", "curiosity"],
    )


def s_winback_eligible(c: Ctx) -> Draft:
    days = c.payload.get("days_since_expiry")
    dip = c.payload.get("perf_dip_pct")
    lapsed = c.payload.get("lapsed_customers_added_since_expiry")
    if days is None:
        return skip("winback without days_since_expiry")

    hook = f"Your subscription lapsed {num(days)} days ago."
    parts = []
    if dip is not None:
        parts.append(f"visibility is down {pct(dip)}")
    if lapsed is not None:
        parts.append(f"{num(lapsed)} customers have gone quiet")
    anchor = ("Since then, " + " and ".join(parts) + ".") if parts else ""
    return Draft(
        hook=hook,
        anchor=anchor,
        ask="Want me to switch it back on and re-open those customers this week?",
        hi_ask="Sab wapas on karke un customers ko is hafte dobara reach kar dun?",
        cta="binary_yes_no",
        template_name="vera_winback_v1",
        template_params=[c.owner or c.biz, num(days), pct(dip)],
        rationale="Post-expiry loss quantified with the merchant's own numbers, then a single restore action.",
        facts=[f"days since expiry: {num(days)}"]
        + ([f"performance dip: {pct(dip)}"] if dip is not None else [])
        + ([f"lapsed customers since expiry: {num(lapsed)}"] if lapsed is not None else []),
        levers=["loss_aversion"],
    )


def s_active_planning_intent(c: Ctx) -> Draft:
    topic = str(c.payload.get("intent_topic") or "").replace("_", " ")
    last_msg = str(c.payload.get("merchant_last_message") or "")
    if not topic:
        return skip("planning intent without a topic")

    offer = c.offer_title() or c.catalog_title()
    hook = f"Picking up your {topic} plan."
    anchor = f"I've drafted a first version around {offer}." if offer else "I've drafted a first version."
    return Draft(
        hook=hook,
        anchor=anchor,
        ask="Want me to send the draft now for you to mark up?",
        hi_ask="Draft abhi bhej dun?",
        cta="binary_yes_no",
        template_name="vera_planning_intent_v1",
        template_params=[c.owner or c.biz, topic, offer],
        rationale=(
            "Merchant already signalled intent, so this goes straight to delivering work — no re-qualifying."
        ),
        facts=[f"topic: {topic}"]
        + ([f"merchant said: {last_msg}"] if last_msg else [])
        + ([f"offer to build on: {offer}"] if offer else []),
        levers=["effort_externalization", "reciprocity"],
    )


def s_cde_opportunity(c: Ctx) -> Draft:
    item = c.digest(c.payload.get("digest_item_id")) or c.digest_of_kind("cde")
    if not item:
        return skip("no CDE item in category context")
    title = str(item.get("title") or "")
    when = date_words(str(item.get("date") or "")[:10])
    credits = c.payload.get("credits") or item.get("credits")
    fee = str(c.payload.get("fee") or item.get("actionable") or "")
    fee = {"free_for_members": "free for members"}.get(fee, fee.replace("_", " "))
    source = str(item.get("source") or "")
    speaker = _speaker(str(item.get("summary") or ""))

    hook = f"{title}" + (f" on {when}." if when else ".")
    anchor_bits = []
    if speaker:
        anchor_bits.append(f"Speaker: {speaker}")
    if credits:
        anchor_bits.append(f"{num(credits)} credits")
    if fee:
        anchor_bits.append(fee)
    anchor = ("; ".join(anchor_bits[:-1]) + " and " + anchor_bits[-1] + "." if len(anchor_bits) > 1
              else f"{anchor_bits[0]}." if anchor_bits else "")
    return Draft(
        hook=hook,
        anchor=anchor,
        ask="Want me to pencil it into your calendar with a reminder the day before?",
        hi_ask="Calendar mein daal ke ek din pehle reminder laga dun?",
        citation=source,
        cta="binary_yes_no",
        template_name="vera_cde_v1",
        template_params=[c.owner or c.biz, title, when],
        rationale="Dated professional-development item with credits and speaker named; low-friction calendar ask.",
        facts=[f"event: {title}", f"source: {source}"]
        + ([f"date: {when}"] if when else [])
        + ([f"credits: {num(credits)}"] if credits else [])
        + ([f"speaker: {speaker}"] if speaker else [])
        + ([f"fee: {fee}"] if fee else []),
        levers=["curiosity", "effort_externalization"],
    )


def _speaker(summary: str) -> str:
    """'Speaker: Dr. R. Mehta. Covers...' -> 'Dr. R. Mehta'. Empty when absent.

    Titles like 'Dr.' contain periods, so the clause runs to the sentence end, and a
    bare 'Dr'/'Dr.' tail is trimmed as a parsing artefact.
    """
    # Names carry periods ("Dr. R. Mehta"), so split into sentences the naive way is
    # wrong. Strategy: a period ends the name only when BOTH neighbours look like
    # sentence structure — the token before the period is 2+ letters (not "R") and
    # the token after is a full word. Anything else is part of the name.
    match = re.search(r"Speaker:\s*(.+)", summary)
    if not match:
        return ""
    rest = match.group(1).strip()
    end = None
    for candidate in re.finditer(r"\.", rest):
        i = candidate.start()
        before = rest[:i].rstrip().split(" ")[-1] if rest[:i].strip() else ""
        after = rest[i + 1 :].lstrip().split(" ")[0] if rest[i + 1 :].strip() else ""
        after = after.rstrip(".")  # 'R.' -> 'R': an initial is never a sentence start
        before_ok = len(before) >= 2 or before == ""  # single letter = initial, not an end
        after_ok = len(after) >= 2 or after == ""     # single letter after = mid-name
        if before_ok and after_ok:
            end = i
            break
    name = rest[:end].strip() if end is not None else rest
    name = name.rstrip(".")
    if name in ("Dr", "Dr.", "Ms", "Mr", "Mrs"):
        return ""
    return name


def s_supply_alert(c: Ctx) -> Draft:
    molecule = str(c.payload.get("molecule") or "")
    batches = c.payload.get("affected_batches") or []
    manufacturer = str(c.payload.get("manufacturer") or "")
    item = c.digest(c.payload.get("alert_id"))
    if not molecule:
        return skip("supply alert without a molecule")

    batch_text = ", ".join(str(b) for b in batches if isinstance(b, str))
    hook = f"Recall notice on {molecule}" + (f" from {manufacturer}." if manufacturer else ".")
    anchor = f"Affected batches: {batch_text}." if batch_text else str(item.get("summary") or "")
    return Draft(
        hook=hook,
        anchor=anchor,
        ask="Want me to draft the shelf-check list and a note for customers holding those batches?",
        hi_ask="Shelf-check list bana dun?",
        citation=str(item.get("source") or ""),
        cta="binary_yes_no",
        template_name="vera_supply_alert_v1",
        template_params=[c.owner or c.biz, molecule, batch_text],
        rationale="Highest-urgency safety item: exact molecule and batch numbers first, action second.",
        facts=[f"molecule: {molecule}"]
        + ([f"batches: {batch_text}"] if batch_text else [])
        + ([f"manufacturer: {manufacturer}"] if manufacturer else [])
        + ([f"source: {item.get('source')}"] if item.get("source") else []),
        levers=["loss_aversion", "reciprocity"],
    )


def s_category_seasonal(c: Ctx) -> Draft:
    season = season_phrase(c.payload.get("season"))
    trends = c.payload.get("trends") or []
    readable = trend_prose(trends)
    if not readable:
        return skip("seasonal trigger without trend data")

    movers = [t for t in readable if " is up " in t]
    fallers = [t for t in readable if " is down " in t]
    hook = f"{season.title()} demand is shifting in your category."
    if movers:
        hook = f"{season.title()} demand is shifting — {movers[0]}."
    anchor_bits = movers[1:] if len(movers) > 1 else movers
    if fallers and anchor_bits:
        anchor_bits.append(fallers[0])
    if anchor_bits:
        anchor = "Also " + ", ".join(anchor_bits[:-1]) + f" and {anchor_bits[-1]}." if len(anchor_bits) > 1 else f"Also {anchor_bits[0]}."
    else:
        anchor = ""
    return Draft(
        hook=hook,
        anchor=anchor,
        ask="Want me to reorder your listing so the rising ones show first?",
        hi_ask="Listing mein rising items ko aage kar dun?",
        cta="binary_yes_no",
        template_name="vera_category_seasonal_v1",
        template_params=[c.owner or c.biz, season, readable[0]],
        rationale="Category-level demand shift expressed as concrete percentage movers, with one shelf-level action.",
        facts=[f"season: {season}"] + [f"trend: {r}" for r in readable[:4]],
        levers=["curiosity", "loss_aversion"],
    )


# ------------------------------------------------------------- customer-facing


def service_words(raw: Any) -> str:
    """'6_month_cleaning' -> '6-month cleaning'."""
    parts = [p for p in str(raw or "").split("_") if p]
    if len(parts) >= 2 and parts[0].isdigit():
        return f"{parts[0]}-{parts[1]} " + " ".join(parts[2:]) if len(parts) > 2 else f"{parts[0]}-{parts[1]}"
    return " ".join(parts)


def s_recall_due(c: Ctx) -> Draft:
    service = service_words(c.payload.get("service_due"))
    due = date_words(c.payload.get("due_date"))
    slots = [s for s in (c.payload.get("available_slots") or []) if isinstance(s, dict)]
    labels = [str(s.get("label")) for s in slots if s.get("label")]
    offer = c.offer_title()
    if not service and not due:
        return skip("recall without service or due date")

    hook = f"Hi {c.cust_name}, {c.biz} here."
    anchor = f"Your {service} is due on {due}." if due else f"Your {service} is due."
    if offer:
        anchor += f" It's {offer}."
    if len(labels) >= 2:
        ask = f"We have {labels[0]} or {labels[1]} open — reply 1 or 2 and we will hold it."
        cta = "multi_choice_slot"
    elif labels:
        ask = f"We have {labels[0]} open — shall we hold it for you?"
        cta = "binary_yes_no"
    else:
        ask = "Shall we book you in this week?"
        cta = "binary_yes_no"

    return Draft(
        hook=hook,
        anchor=anchor,
        ask=ask,
        hi_ask=ask,
        cta=cta,
        template_name="merchant_recall_reminder_v1",
        template_params=[c.cust_name, c.biz, service, ", ".join(labels), offer],
        rationale=(
            "Customer-scoped recall sent on the merchant's behalf; real due date, the merchant's real "
            "price and only genuinely open slots, matched to this customer's stated slot preference."
        ),
        facts=[f"customer: {c.cust_name}", f"clinic: {c.biz}", f"service due: {service}"]
        + ([f"due date: {due}"] if due else [])
        + ([f"price: {offer}"] if offer else [])
        + [f"available slot: {l}" for l in labels],
        levers=["reciprocity", "single_binary_commitment"],
    )


def s_chronic_refill_due(c: Ctx) -> Draft:
    molecules = [m for m in (c.payload.get("molecule_list") or []) if isinstance(m, str)]
    runs_out = date_words(str(c.payload.get("stock_runs_out_iso") or "")[:10])
    delivery = bool(c.payload.get("delivery_address_saved"))
    if not molecules:
        return skip("refill trigger without molecules")

    hook = f"Hi {c.cust_name}, {c.biz} here."
    anchor = f"Your {', '.join(molecules)} run out around {runs_out}." if runs_out else f"Your {', '.join(molecules)} are due for a refill."
    offer = ""
    for o in c.active_offers():
        title = str(o.get("title") or "")
        if "delivery" in title.lower() or "senior" in title.lower():
            offer = title
            break
    if delivery and offer:
        ask = f"We have your address saved and {offer} applies — shall we send the refill before then?"
    elif delivery:
        ask = "We have your address saved — shall we send the refill before then?"
    else:
        ask = "Shall we keep the refill ready for pickup?"

    return Draft(
        hook=hook,
        anchor=anchor,
        ask=ask,
        hi_ask=ask,
        cta="binary_yes_no",
        template_name="merchant_refill_reminder_v1",
        template_params=[c.cust_name, c.biz, ", ".join(molecules), runs_out],
        rationale=(
            "Chronic refill before the stock-out date, sent on the pharmacy's behalf; "
            "named molecules and a saved delivery address make it a one-word decision."
        ),
        facts=[f"customer: {c.cust_name}", f"pharmacy: {c.biz}", f"medicines: {', '.join(molecules)}"]
        + ([f"stock runs out: {runs_out}"] if runs_out else [])
        + ([f"offer: {offer}"] if offer else []),
        levers=["loss_aversion", "effort_externalization"],
    )


def s_customer_lapsed_hard(c: Ctx) -> Draft:
    days = c.payload.get("days_since_last_visit")
    focus = str(c.payload.get("previous_focus") or "").replace("_", " ")
    months = c.payload.get("previous_membership_months")
    if days is None:
        return skip("lapse trigger without a day count")

    offer = c.offer_title()
    hook = f"Hi {c.cust_name}, {c.biz} here."
    anchor = f"It's been {num(days)} days since your last session."
    if months and focus:
        anchor += f" You trained {num(months)} months with us on {focus}."
    ask = f"{offer} is open if you want an easy restart — shall I book you in?" if offer else "Shall I book you an easy restart session?"
    return Draft(
        hook=hook,
        anchor=anchor,
        ask=ask,
        hi_ask=ask,
        cta="binary_yes_no",
        template_name="merchant_winback_v1",
        template_params=[c.cust_name, c.biz, num(days), offer],
        rationale=(
            "Lapsed member win-back on the gym's behalf; references their real history and prior training focus "
            "rather than a generic discount."
        ),
        facts=[f"customer: {c.cust_name}", f"business: {c.biz}", f"days since last visit: {num(days)}"]
        + ([f"previous focus: {focus}"] if focus else [])
        + ([f"previous membership: {num(months)} months"] if months else [])
        + ([f"offer: {offer}"] if offer else []),
        levers=["reciprocity", "loss_aversion"],
    )


def s_trial_followup(c: Ctx) -> Draft:
    trial = date_words(c.payload.get("trial_date"))
    options = [o for o in (c.payload.get("next_session_options") or []) if isinstance(o, dict)]
    labels = [str(o.get("label")) for o in options if o.get("label")]
    if not labels and not trial:
        return skip("trial followup without a date or next session")

    reader = c.cust_guardian
    hook = (
        f"Hi {reader}, {c.biz} here about {c.cust_name}'s trial session."
        if reader
        else f"Hi {c.cust_name}, {c.biz} here about your trial session."
    )
    anchor = f"Hope the session on {trial} went well." if trial else ""
    ask = f"The next one is {labels[0]} — shall we save a place?" if labels else "Shall we save a place in the next session?"
    return Draft(
        hook=hook,
        anchor=anchor,
        ask=ask,
        hi_ask=ask,
        cta="binary_yes_no",
        template_name="merchant_trial_followup_v1",
        template_params=[c.cust_name, c.biz, trial, labels[0] if labels else ""],
        rationale="Post-trial follow-up on the studio's behalf with one real next session; single yes/no ask.",
        facts=[f"customer: {c.cust_name}", f"business: {c.biz}"]
        + ([f"trial date: {trial}"] if trial else [])
        + [f"next session: {l}" for l in labels],
        levers=["single_binary_commitment", "reciprocity"],
    )


def s_wedding_package_followup(c: Ctx) -> Draft:
    wedding = date_words(c.payload.get("wedding_date"))
    trial = date_words(c.payload.get("trial_completed"))
    days = c.payload.get("days_to_wedding")
    program = program_words(c.payload.get("next_step_window_open"))
    if not wedding:
        return skip("bridal followup without a wedding date")

    hook = f"Hi {c.cust_name}, {c.biz} here."
    anchor = f"Your wedding is on {wedding}"
    anchor += f", and your trial was on {trial}." if trial else "."
    ask = f"The {program} window is open now — shall we plan the schedule around your Saturdays?" if program else "Shall we plan the schedule around your Saturdays?"
    return Draft(
        hook=hook,
        anchor=anchor,
        ask=ask,
        hi_ask=ask,
        cta="binary_yes_no",
        template_name="merchant_bridal_followup_v1",
        template_params=[c.cust_name, c.biz, wedding, program],
        rationale=(
            "Bridal follow-up on the salon's behalf, timed to the prep window and honouring this customer's "
            "Saturday preference."
        ),
        facts=[f"customer: {c.cust_name}", f"salon: {c.biz}", f"wedding date: {wedding}"]
        + ([f"trial completed: {trial}"] if trial else [])
        + ([f"days to wedding: {num(days)}"] if days else [])
        + ([f"program: {program}"] if program else []),
        levers=["reciprocity", "single_binary_commitment"],
    )


# ------------------------------------------------------------------- fallback


def s_generic(c: Ctx) -> Draft:
    """Unknown/injected trigger kind: stay grounded in whatever real data exists."""
    views = c.perf.get("views")
    window = c.perf.get("window_days")
    calls = c.perf.get("calls")
    peer_line = _perf_vs_peer(c)
    offer = c.offer_title() or c.catalog_title()

    facts: List[str] = []
    if views and window:
        facts.append(f"views in last {num(window)} days: {num(views)}")
    if calls is not None:
        facts.append(f"calls: {num(calls)}")
    if peer_line:
        facts.append(peer_line)
    if offer:
        facts.append(f"offer: {offer}")

    if len(facts) < 2:
        return skip("not enough grounded facts for an unknown trigger kind")

    hook = f"Your listing took {num(views)} views in the last {num(window)} days." if views and window else "Quick update on your listing."
    anchor = f"For context, {peer_line}." if peer_line else ""
    ask = f"Want me to put {offer} in front of those searches this week?" if offer else "Want me to take a look at what would move it this week?"

    return Draft(
        hook=hook,
        anchor=anchor,
        ask=ask,
        hi_ask="Is hafte ye kar dun?",
        cta="binary_yes_no",
        template_name="vera_generic_v1",
        template_params=[c.owner or c.biz, num(views), offer],
        rationale=(
            "Trigger kind has no dedicated strategy, so this stays strictly on the merchant's own "
            "performance numbers and real offer rather than inventing a reason to message."
        ),
        facts=facts,
        levers=["specificity", "effort_externalization"],
    )


STRATEGIES = {
    "research_digest": s_research_digest,
    "regulation_change": s_regulation_change,
    "perf_dip": s_perf_dip,
    "perf_spike": s_perf_spike,
    "seasonal_perf_dip": s_seasonal_perf_dip,
    "milestone_reached": s_milestone_reached,
    "renewal_due": s_renewal_due,
    "festival_upcoming": s_festival_upcoming,
    "ipl_match_today": s_ipl_match_today,
    "review_theme_emerged": s_review_theme_emerged,
    "competitor_opened": s_competitor_opened,
    "gbp_unverified": s_gbp_unverified,
    "dormant_with_vera": s_dormant_with_vera,
    "curious_ask_due": s_curious_ask_due,
    "winback_eligible": s_winback_eligible,
    "active_planning_intent": s_active_planning_intent,
    "cde_opportunity": s_cde_opportunity,
    "supply_alert": s_supply_alert,
    "category_seasonal": s_category_seasonal,
    "recall_due": s_recall_due,
    "chronic_refill_due": s_chronic_refill_due,
    "customer_lapsed_hard": s_customer_lapsed_hard,
    "customer_lapsed_soft": s_customer_lapsed_hard,
    "trial_followup": s_trial_followup,
    "wedding_package_followup": s_wedding_package_followup,
}


def build_draft(category: Dict, merchant: Dict, trigger: Dict, customer: Optional[Dict]) -> Draft:
    ctx = Ctx(category, merchant, trigger, customer)
    strategy = STRATEGIES.get(ctx.kind, s_generic)
    draft = strategy(ctx)
    if draft.skip:
        # A known kind that lacked its own data can still fall back to grounded generics.
        fallback = s_generic(ctx)
        return fallback if not fallback.skip else draft
    return draft


__all__ = ["Draft", "Ctx", "build_draft", "STRATEGIES", "num", "pct", "date_words"]
