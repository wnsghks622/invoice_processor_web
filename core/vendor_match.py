# -*- coding: utf-8 -*-
"""Vendor identity: turning the raw vendor string Claude extracted into a vendor_id.

Why this matters beyond tidiness: recurring-invoice expectations key on vendor_id, not
on a string. Without this layer 'Athens Services' and 'ATHENS SERVICES' are two
half-confident expectations that each look sporadic; with it they are one confident
monthly expectation.

The pipeline mirrors match_property() in processor.py - exact, alias-inside, then close
spelling - with an added confidence band so anything uncertain reaches a human instead
of being guessed at. Confirming a suggestion writes the raw string into that vendor's
aliases, so the same spelling is never asked about twice.
"""
import difflib
import re
from typing import NamedTuple, Optional

# Score at or above which a match is applied without asking. Below SUGGEST_THRESHOLD the
# candidate is discarded entirely and the vendor is treated as new.
BIND_THRESHOLD = 0.92
SUGGEST_THRESHOLD = 0.80

# Corporate suffixes and filler that carry no identifying information.
_NOISE = re.compile(
    r"\b(inc|llc|ltd|lp|corp|corporation|company|co|the|of|and|dba|"
    r"services|service|us|usa)\b"
)


class MatchResult(NamedTuple):
    outcome: str              # "bind" | "suggest" | "new"
    vendor_id: Optional[int]
    score: float
    reason: str               # "exact" | "alias-inside" | "close-spelling" | "no-match"


def normalize(name: Optional[str]) -> str:
    """Lowercase, strip punctuation and corporate suffixes, collapse whitespace.

    Deliberately aggressive: this is what the alias-inside and close-spelling tiers
    compare on, so two spellings that differ only by punctuation or a "Inc." / "Co."
    suffix reduce to the same key.
    """
    text = (name or "").lower()
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    text = _NOISE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _fold(name: Optional[str]) -> str:
    """Case- and whitespace-only fold, used for the exact-match tier only.

    Deliberately lighter than normalize(): the exact tier should fire only when the raw
    text is the *same string* modulo case, not merely the same company after punctuation
    and suffixes are stripped away. "Mitsubishi Electric US Inc" (missing a comma and a
    period) is a close spelling of "Mitsubishi Electric US, Inc." - not an exact match -
    even though normalize() reduces both to "mitsubishi electric".
    """
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _aliases_of(vendor: dict) -> list[str]:
    """Every string that identifies this vendor: canonical name, short name, aliases.
    Used for the exact and close-spelling tiers."""
    raw = [vendor.get("canonical_name"), vendor.get("short_name")]
    raw += (vendor.get("aliases") or "").split(";")
    return [a.strip() for a in raw if a and a.strip()]


def _substring_candidates(vendor: dict) -> list[str]:
    """Identifying strings it is safe to search for *inside* a longer raw string:
    canonical name and curated aliases only.

    short_name is deliberately excluded here. It is often a single generic word (a
    vendor's short_name is frequently just its first word, e.g. "Mitsubishi" for
    "Mitsubishi Electric US, Inc."), and "this word appears somewhere in the raw text"
    is too loose a bar to bind at 100% confidence with no human review - it would also
    fire for an unrelated "Mitsubishi Motors" invoice. The full canonical name or an
    explicitly curated alias is a much more specific signal.
    """
    raw = [vendor.get("canonical_name")]
    raw += (vendor.get("aliases") or "").split(";")
    return [a.strip() for a in raw if a and a.strip()]


def _classify(score: float, vendor_id: Optional[int], reason: str) -> MatchResult:
    """Apply the confidence bands. Split out so the boundaries are directly testable."""
    if vendor_id is not None and score >= BIND_THRESHOLD:
        return MatchResult("bind", vendor_id, score, reason)
    if vendor_id is not None and score >= SUGGEST_THRESHOLD:
        return MatchResult("suggest", vendor_id, score, reason)
    return MatchResult("new", None, score, "no-match")


def match(raw: Optional[str], vendors: list[dict]) -> MatchResult:
    """Match an extracted vendor string against the known vendor list."""
    key = normalize(raw)
    if not key or not vendors:
        return MatchResult("new", None, 0.0, "no-match")

    # 1) Exact: the raw text is the same string as a known one, modulo case and
    #    whitespace only. A punctuation- or suffix-only difference is NOT exact - it
    #    still binds, but via the close-spelling tier below (tier 3).
    raw_fold = _fold(raw)
    for v in vendors:
        if any(_fold(a) == raw_fold for a in _aliases_of(v)):
            return MatchResult("bind", v["id"], 1.0, "exact")

    # 2) A known alias appears inside the extracted name - common when the invoice
    #    prints a department or billing suffix, so there is leftover text in the raw
    #    string beyond the alias itself (len(akey) < len(key)). When an alias's
    #    normalized form matches the *whole* key there is no such leftover - that is a
    #    close spelling (tier 3), not an alias embedded in a longer name. Longest alias
    #    wins as most specific.
    best_id, best_len = None, 0
    for v in vendors:
        for alias in _substring_candidates(v):
            akey = normalize(alias)
            if akey and len(akey) < len(key) and akey in key and len(akey) > best_len:
                best_id, best_len = v["id"], len(akey)
    if best_id is not None:
        return MatchResult("bind", best_id, 1.0, "alias-inside")

    # 3) Close spelling. Take the single best candidate, then let the bands decide.
    best_id, best_score = None, 0.0
    for v in vendors:
        for alias in _aliases_of(v):
            score = difflib.SequenceMatcher(None, key, normalize(alias)).ratio()
            if score > best_score:
                best_id, best_score = v["id"], score
    return _classify(best_score, best_id, "close-spelling")


def cluster(names: list[str], threshold: float = 0.86) -> list[list[str]]:
    """Group raw vendor strings that are probably the same vendor.

    Used once, at bootstrap, to turn the raw strings already in the invoice table into a
    starting vendor list. Deliberately conservative: it is far cheaper for a human to
    merge two groups than to discover months later that two real vendors were silently
    combined and their expectations tangled.

    Substring containment alone is NOT treated as a match - 'Michelle Suh' is inside
    'Michelle Suh (Rooter Plumbing)' but they may be a person and a plumbing company.
    """
    import collections

    counts = collections.Counter(n for n in names if (n or "").strip())
    groups: list[list[str]] = []
    for name in sorted(counts, key=lambda n: (-counts[n], n)):
        key = normalize(name)
        placed = False
        for group in groups:
            if any(difflib.SequenceMatcher(None, key, normalize(m)).ratio() >= threshold
                   for m in group):
                group.append(name)
                placed = True
                break
        if not placed:
            groups.append([name])
    return groups


def append_alias(existing: Optional[str], raw: Optional[str]) -> str:
    """Add a raw spelling to a vendor's semicolon-separated alias list.

    Case-insensitive dedup, blank segments dropped. The review screen calls this when a
    vendor is confirmed - it is what stops the same spelling being asked about twice.
    """
    aliases = [a.strip() for a in (existing or "").split(";") if a.strip()]
    candidate = (raw or "").strip()
    if candidate and candidate.lower() not in {a.lower() for a in aliases}:
        aliases.append(candidate)
    return "; ".join(aliases)
