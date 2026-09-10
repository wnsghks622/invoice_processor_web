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

# Shortest normalized alias the alias-inside tier (tier 2) will search for inside a longer
# raw string. processor.match_property - the function this pipeline is modelled on - uses
# the same floor of 5 on both sides of its own substring tier, and tier 2 needs it for the
# same reason: "these few characters appear somewhere in the raw text" is not evidence of
# identity at short lengths, and tier 2 binds at confidence 1.0 with no human review.
# Four bootstrapped vendors already normalize below it ('gas', 'dwp', 'home', 'at t'), and
# without the floor they swallow any unrelated name that happens to contain those letters:
# 'Home Depot' -> HOME SERVICE, 'Great Tile Co' -> AT&T (gre[at t]ile), 'Gasparian
# Plumbing' -> SoCalGas. Below the floor an alias simply stops being a tier-2 candidate;
# it still identifies its vendor through the exact and close-spelling tiers.
MIN_ALIAS_INSIDE_LEN = 5

# Corporate suffixes and filler that carry no identifying information.
_NOISE = re.compile(
    r"\b(inc|llc|ltd|lp|corp|corporation|company|co|the|of|and|dba|"
    r"services|service|us|usa)\b"
)

# Legal-entity-type words normalize() treats as noise (correctly, for match()'s looser
# purpose - a returning vendor rarely changes structure). cluster() is deliberately
# stricter: 'South Coast Mechanical, LLC' and 'South Coast Mechanical, Inc.' normalize
# to the identical string once both suffixes are stripped, but an LLC and an Inc. may be
# related-and-distinct legal entities, not a spelling variant. Only the words that name a
# specific legal structure are listed - 'company'/'co' is generic and stays plain noise.
_ENTITY_TYPE = re.compile(r"\b(inc|llc|ltd|lp|corp|corporation)\b")


def _entity_types(name: Optional[str]) -> frozenset:
    return frozenset(_ENTITY_TYPE.findall((name or "").lower()))


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


def is_exact(a: Optional[str], b: Optional[str]) -> bool:
    """True when two raw strings are the same string modulo case and whitespace.

    Tier 1's rule, exposed. app.py sweeps the vendor-review queue after a new vendor is
    created and binds every invoice printing the same string; that sweep has to apply the
    identical bar match() applies, and a second comparison hand-rolled at the call site
    would drift from this one the moment either is tuned. Deliberately narrower than
    normalize() - a punctuation difference is a close spelling for a human to confirm, not
    a silent bind.

    Two blanks are not exact: a blank vendor string identifies nothing, so without this
    guard one blank-vendor invoice would drag every other blank-vendor invoice in the queue
    onto the vendor it created.
    """
    folded = _fold(a)
    return bool(folded) and folded == _fold(b)


def _aliases_of(vendor: dict) -> list[str]:
    """Every string that identifies this vendor: canonical name, short name, aliases.
    Used for the exact and close-spelling tiers."""
    raw = [vendor.get("canonical_name"), vendor.get("short_name")]
    raw += (vendor.get("aliases") or "").split(";")
    return [a.strip() for a in raw if a and a.strip()]


def _substring_candidates(vendor: dict) -> list[str]:
    """Identifying strings it is safe to search for *inside* a longer raw string:
    canonical name and aliases.

    short_name is deliberately excluded here. It is often a single generic word (a
    vendor's short_name is frequently just its first word, e.g. "Mitsubishi" for
    "Mitsubishi Electric US, Inc."), and "this word appears somewhere in the raw text"
    is too loose a bar to bind at 100% confidence with no human review - it would also
    fire for an unrelated "Mitsubishi Motors" invoice. The full canonical name or a
    whole alias is a much more specific signal.

    The alias list is NOT a purely curated one, so it cannot carry that argument on its
    own. Two paths append to it automatically: bootstrap_vendors.py seeds every raw
    spelling in a cluster as an alias, and app.py's vendor confirmation appends the raw
    model-extracted string whenever a human confirms a match. Both can therefore put a
    short or generic string in this list - which is why length, not provenance, is what
    the caller gates on (MIN_ALIAS_INSIDE_LEN in match()'s tier 2).
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
    #
    #    The alias must also clear MIN_ALIAS_INSIDE_LEN: a three-letter fragment found
    #    somewhere in a longer name is coincidence, not identity. Only the alias side
    #    needs the explicit floor - len(akey) < len(key) already puts the raw key above
    #    it, which is the same pair of bounds match_property() spells out separately.
    best_id, best_len = None, 0
    for v in vendors:
        for alias in _substring_candidates(v):
            akey = normalize(alias)
            if (len(akey) >= MIN_ALIAS_INSIDE_LEN and len(akey) < len(key)
                    and akey in key and len(akey) > best_len):
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

    A high normalize() ratio alone is also NOT enough when it is only high because two
    DIFFERENT legal-entity-type words were both stripped as noise - 'South Coast
    Mechanical, LLC' and 'South Coast Mechanical, Inc.' both reduce to 'south coast
    mechanical', but an LLC and an Inc. may be related-and-distinct legal entities.
    """
    import collections

    counts = collections.Counter(n for n in names if (n or "").strip())
    groups: list[list[str]] = []
    for name in sorted(counts, key=lambda n: (-counts[n], n)):
        key = normalize(name)
        entities = _entity_types(name)
        placed = False
        for group in groups:
            for m in group:
                same_entity = not entities or not _entity_types(m) or entities == _entity_types(m)
                if (same_entity
                        and difflib.SequenceMatcher(None, key, normalize(m)).ratio() >= threshold):
                    group.append(name)
                    placed = True
                    break
            if placed:
                break
        if not placed:
            groups.append([name])
    return groups


def record_fields(result: MatchResult) -> dict:
    """Turn a MatchResult into the two invoice column values it implies.

    A 'suggest' keeps the candidate id so the review screen can pre-select it, but still
    flags the row - a suggestion that bound itself would make the queue pointless.
    """
    return {
        "vendor_id": result.vendor_id if result.outcome != "new" else None,
        "vendor_needs_review": 0 if result.outcome == "bind" else 1,
    }


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


def short_name(canonical: Optional[str]) -> str:
    """A filename-safe short name: the first meaningful word, or an acronym for long names.

    Lives here rather than in scripts/bootstrap_vendors.py (where it started) because two
    paths now mint vendors.short_name - that one-off bootstrap, and the Fixer page's
    'create new vendor' box - and two derivations of the same thing would drift apart.

    Falls back to 'Vendor' when nothing survives: short_name() feeds a NOT NULL column and
    prefills a required form box, so returning '' would put a blank in both. Stripping to
    filename-safe characters can empty a word that looked fine ('***' -> ''), so the
    fallback covers more than the blank-input case.
    """
    words = [w for w in re.split(r"\s+", (canonical or "").strip()) if w]
    if len(words) >= 4:
        acronym = "".join(w[0] for w in words if w[0].isalnum()).upper()[:8]
        if len(acronym) >= 3:
            return acronym
    return (re.sub(r"[^0-9A-Za-z&-]", "", words[0]) if words else "") or "Vendor"


def unique_short_name(canonical: Optional[str], used: set) -> str:
    """short_name(canonical), disambiguated against short names already taken.

    vendors.short_name is NOT NULL UNIQUE, but short_name() only looks at one name at a
    time - it has no way to know that, say, 'Black Shadow III', 'Black Jack Market', and
    'Black Water Operations' are three different real vendors that all reduce to 'Black'.
    This is what stops that correct decision from crashing the insert. `used` is mutated in
    place so later collisions in the same run see earlier picks.

    Compared case-insensitively even though SQLite's UNIQUE is case-sensitive (so 'Black'
    and 'BLACK' would both insert quite legally). The point here is a vendor list a human
    can read, not a legal insert.
    """
    base = short_name(canonical)
    candidate, n = base, 2
    while candidate.strip().lower() in used:
        candidate = f"{base}{n}"
        n += 1
    used.add(candidate.strip().lower())
    return candidate
