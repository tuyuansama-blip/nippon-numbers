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
