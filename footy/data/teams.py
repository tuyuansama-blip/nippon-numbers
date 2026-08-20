"""Canonical team names and the alias table.

A team keeps one identity across relegation and promotion: the model's `a_i`
and `d_i` for Sunderland in 2016/17 and Sunderland in 2005/06 must be the same
slot, or the six-season training window silently splits a club into two.

`football-data.co.uk` is well behaved inside E0 -- all 31 downloaded seasons
use exactly the 49 spellings in `CANONICAL` -- but the aliases below exist
because the loader is meant to accept other divisions later (DESIGN.md 1.4,
5) and those files are less tidy. An unknown spelling is a hard error, never
a silently-new team: `footy check` exits 1 on it.
"""

from __future__ import annotations

import re
import unicodedata

# Every spelling football-data.co.uk uses for an E0 club, 1995/96 - 2025/26.
CANONICAL: tuple[str, ...] = (
    "Arsenal",
    "Aston Villa",
    "Barnsley",
    "Birmingham",
    "Blackburn",
    "Blackpool",
    "Bolton",
    "Bournemouth",
    "Bradford",
    "Brentford",
    "Brighton",
    "Burnley",
    "Cardiff",
    "Charlton",
    "Chelsea",
    "Coventry",
    "Crystal Palace",
    "Derby",
    "Everton",
    "Fulham",
    "Huddersfield",
    "Hull",
    "Ipswich",
    "Leeds",
    "Leicester",
    "Liverpool",
    "Luton",
    "Man City",
    "Man United",
    "Middlesbrough",
    "Newcastle",
    "Norwich",
    "Nott'm Forest",
    "Portsmouth",
    "QPR",
    "Reading",
    "Sheffield United",
    "Sheffield Weds",
    "Southampton",
    "Stoke",
    "Sunderland",
    "Swansea",
    "Tottenham",
    "Watford",
    "West Brom",
    "West Ham",
    "Wigan",
    "Wimbledon",
    "Wolves",
)

# Alternative spellings seen in other football-data divisions / older files.
# key: raw spelling (matched case- and punctuation-insensitively)
ALIASES: dict[str, str] = {
    "afc bournemouth": "Bournemouth",
    "birmingham city": "Birmingham",
    "blackburn rovers": "Blackburn",
    "bolton wanderers": "Bolton",
    "brighton and hove albion": "Brighton",
    "brighton hove albion": "Brighton",
    "cardiff city": "Cardiff",
    "charlton athletic": "Charlton",
    "coventry city": "Coventry",
    "crystal palace fc": "Crystal Palace",
    "derby county": "Derby",
    "huddersfield town": "Huddersfield",
    "hull city": "Hull",
    "ipswich town": "Ipswich",
    "leeds united": "Leeds",
    "leicester city": "Leicester",
    "luton town": "Luton",
    "manchester city": "Man City",
    "man c": "Man City",
    "manchester united": "Man United",
    "man utd": "Man United",
    "middlesboro": "Middlesbrough",
    "middlesbro": "Middlesbrough",
    "newcastle united": "Newcastle",
    "newcastle utd": "Newcastle",
    "norwich city": "Norwich",
    "nottingham forest": "Nott'm Forest",
    "notts forest": "Nott'm Forest",
    "queens park rangers": "QPR",
    "sheff united": "Sheffield United",
    "sheff utd": "Sheffield United",
    "sheff wed": "Sheffield Weds",
    "sheffield wednesday": "Sheffield Weds",
    "stoke city": "Stoke",
    "swansea city": "Swansea",
    "spurs": "Tottenham",
    "tottenham hotspur": "Tottenham",
    "west bromwich albion": "West Brom",
    "west bromwich": "West Brom",
    "west ham united": "West Ham",
    "wigan athletic": "Wigan",
    "afc wimbledon": "Wimbledon",
    "wolverhampton": "Wolves",
    "wolverhampton wanderers": "Wolves",
}

# Division -> stable league id kept in the parquet so a later multi-league run
# does not have to re-key anything (DESIGN.md 5).
LEAGUE_IDS: dict[str, str] = {
    "E0": "eng_1",
    "E1": "eng_2",
    "E2": "eng_3",
    "E3": "eng_4",
    "EC": "eng_5",
}


def _key(name: str) -> str:
    """Fold a raw spelling to a comparison key: ascii, lowercase, alnum only."""
    text = unicodedata.normalize("NFKD", str(name))
    text = text.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


_CANONICAL_BY_KEY = {_key(n): n for n in CANONICAL}
_ALIAS_BY_KEY = {_key(k): v for k, v in ALIASES.items()}


def canonical_team(name: str) -> str | None:
    """Canonical display name, or None when the spelling is unknown."""
    if name is None:
        return None
    key = _key(name)
    if not key:
        return None
    if key in _CANONICAL_BY_KEY:
        return _CANONICAL_BY_KEY[key]
    return _ALIAS_BY_KEY.get(key)


def team_id(name: str) -> str | None:
    """Stable slug id for a canonical or aliased name."""
    canonical = canonical_team(name)
    if canonical is None:
        return None
    return _key(canonical).replace(" ", "_")


def league_id(div: str) -> str:
    return LEAGUE_IDS.get(str(div).strip().upper(), str(div).strip().lower())


def unknown_names(names) -> list[str]:
    """Raw spellings that resolve to nothing, sorted and de-duplicated."""
    seen: dict[str, None] = {}
    for name in names:
        if name is None:
            continue
        text = str(name).strip()
        if not text:
            continue
        if canonical_team(text) is None:
            seen[text] = None
    return sorted(seen)


# ==============================================================================
# Phase 2 (DESIGN_PHASE2.md 5.4, 6). Two more divisions get team identity:
#
# * 15 OOS-LEAGUES divisions -- ~400 clubs is unmaintainable as a hand-curated
#   whitelist, so identity there is mechanical: `f"{league_id}:{_key(raw)}"`.
#   An unknown spelling is never a hard error for these; `check` instead
#   warns on near-duplicate keys (a likely spelling split) and on clubs with
#   too few appearances to be a real top-flight side that season.
# * J1 keeps E0's discipline -- a small, hand-maintained whitelist, spellings
#   measured directly off `new/JPN.csv` (2012-2026) -- because it is the
#   project's second real holdout and deserves the same hard-error guarantee
#   E0 has.
# ==============================================================================

# Division -> league id for the 15 OOS-LEAGUES divisions (DESIGN_PHASE2.md 5.2).
OOS_LEAGUE_IDS: dict[str, str] = {
    "E1": "eng_2", "SC0": "sco_1", "D1": "ger_1", "D2": "ger_2",
    "I1": "ita_1", "I2": "ita_2", "SP1": "esp_1", "SP2": "esp_2",
    "F1": "fra_1", "F2": "fra_2", "N1": "ned_1", "B1": "bel_1",
    "P1": "por_1", "T1": "tur_1", "G1": "gre_1",
}

JPN_DIV = "JPN1"
_JPN_LEAGUE_ID = "jpn_1"

LEAGUE_IDS.update(OOS_LEAGUE_IDS)
LEAGUE_IDS[JPN_DIV] = _JPN_LEAGUE_ID

# Every spelling `new/JPN.csv` uses across 2012-03-10 .. 2026-08-15, measured
# directly off the file (DESIGN_PHASE2.md 0.12). This includes the J2 clubs
# that leak into the J1 rows through promotion play-offs (0.20) -- Kofu,
# Kumamoto, Montedio Yamagata, Tokushima, Yamaga -- on purpose: `check`
# should see them as *known but low-appearance* (footy/data/j1_filter.py
# drops them), not as an unknown-spelling failure.
JPN_CANONICAL: tuple[str, ...] = (
    "Albirex Niigata", "Avispa Fukuoka", "Cerezo Osaka", "Chiba", "FC Tokyo",
    "Gamba Osaka", "Hokkaido Consadole Sapporo", "Iwata", "Kashima Antlers",
    "Kashiwa Reysol", "Kawasaki Frontale", "Kofu", "Kumamoto", "Kyoto",
    "Machida", "Mito", "Montedio Yamagata", "Nagoya Grampus", "Oita Trinita",
    "Okayama", "Omiya Ardija", "Sagan Tosu", "Sanfrecce Hiroshima",
    "Shimizu S-Pulse", "Shonan Bellmare", "Tokushima", "Urawa Reds",
    "V-Varen Nagasaki", "Vegalta Sendai", "Verdy", "Vissel Kobe", "Yamaga",
    "Yokohama F. Marinos", "Yokohama FC",
)
# No alternate spellings are known yet; kept as a dict (not None) so a future
# variant is a one-line addition, the same shape as E0's ALIASES.
JPN_ALIASES: dict[str, str] = {}

_JPN_CANONICAL_BY_KEY = {_key(n): n for n in JPN_CANONICAL}
_JPN_ALIAS_BY_KEY = {_key(k): v for k, v in JPN_ALIASES.items()}


def canonical_team_j1(name: str) -> str | None:
    """Canonical J1 display name, or None when the spelling is unknown."""
    if name is None:
        return None
    key = _key(name)
    if not key:
        return None
    if key in _JPN_CANONICAL_BY_KEY:
        return _JPN_CANONICAL_BY_KEY[key]
    return _JPN_ALIAS_BY_KEY.get(key)


def team_id_j1(name: str) -> str | None:
    canonical = canonical_team_j1(name)
    if canonical is None:
        return None
    return f"{_JPN_LEAGUE_ID}:{_key(canonical).replace(' ', '_')}"


def unknown_names_j1(names) -> list[str]:
    """Raw J1 spellings that resolve to nothing."""
    seen: dict[str, None] = {}
    for name in names:
        if name is None:
            continue
        text = str(name).strip()
        if not text:
            continue
        if canonical_team_j1(text) is None:
            seen[text] = None
    return sorted(seen)


def resolve_team(div: str, name: str) -> tuple[str | None, str | None]:
    """(canonical_name, team_id) for a raw spelling seen in division `div`.

    E0 and J1 (`JPN1`) go through their hand-maintained whitelists -- an
    unknown spelling there resolves to `(None, None)` and `footy check` turns
    that into a hard failure, same as before phase 2. Every other division
    auto-registers: the id is derived mechanically from `(league_id, key)`
    and the "canonical" name is just the stripped raw spelling, because there
    is no whitelist to canonicalise against (DESIGN_PHASE2.md 5.4).
    """
    if name is None:
        return None, None
    text = str(name).strip()
    if not text:
        return None, None
    div = str(div).strip().upper()
    if div == "E0":
        canonical = canonical_team(text)
        return (canonical, team_id(canonical)) if canonical else (None, None)
    if div == JPN_DIV:
        canonical = canonical_team_j1(text)
        return (canonical, team_id_j1(canonical)) if canonical else (None, None)
    lid = league_id(div)
    return text, f"{lid}:{_key(text)}"


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance. Small strings only (team-name keys)."""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    prev = list(range(lb + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i] + [0] * lb
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[lb]


def near_duplicate_keys(names_by_group) -> list[tuple[str, str, str]]:
    """(group, key_a, key_b) pairs within edit distance <= 2 of each other.

    `names_by_group` maps a grouping label (e.g. a season) to the raw names
    seen in it. Two auto-registered clubs whose keys are this close, in the
    same group, are very likely one club auto-registered under two spellings
    rather than two different clubs (DESIGN_PHASE2.md 5.4) -- a warning, not
    a failure, because it can also be a real coincidence (short names).
    """
    out = []
    for group, names in names_by_group.items():
        keys = sorted({_key(n) for n in names if _key(n)})
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                if _edit_distance(keys[i], keys[j]) <= 2:
                    out.append((group, keys[i], keys[j]))
    return out
