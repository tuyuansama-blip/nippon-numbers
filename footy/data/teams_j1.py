"""The Odds API <-> football-data.co.uk `new/JPN.csv` team-name crosswalk
(DESIGN_PHASE2.md 8.2-2).

Explicit table only, ever. Fuzzy/edit-distance matching is banned here on
purpose and by name in DESIGN_PHASE2.md's 不採用 list: `Yokohama F. Marinos`
and `Yokohama FC` are different clubs and any edit-distance matcher collides
them. An Odds-API spelling with no entry here and no exact (punctuation/case
-insensitive) match to a `teams.JPN_CANONICAL` name is a hard error -- the
same discipline `teams.canonical_team_j1` already applies to football-data's
own spellings.
"""

from __future__ import annotations

from footy.data.teams import canonical_team_j1, team_id_j1

# Measured directly off a live snapshot (DESIGN_PHASE2.md 8.2-2). Add to this
# table, never to a fuzzy matcher, the moment a new spelling is seen.
ODDS_API_ALIASES: dict[str, str] = {
    "Hiroshima Sanfrecce FC": "Sanfrecce Hiroshima",
    "Kyoto Purple Sanga": "Kyoto",
    "Urawa Red Diamonds": "Urawa Reds",
    "JEF United Chiba": "Chiba",
    "Mito HollyHock": "Mito",
    "Fagiano Okayama": "Okayama",
    "FC Machida Zelvia": "Machida",
    "Shimizu S Pulse": "Shimizu S-Pulse",
    "Yokohama F Marinos": "Yokohama F. Marinos",
    "Tokyo Verdy": "Verdy",
}


def resolve_odds_api_team(name: str) -> str | None:
    """Odds-API spelling -> JPN.csv canonical name, or None if unknown.

    The alias table is checked first (some aliases, e.g. `Kyoto Purple
    Sanga` -> `Kyoto`, are not decidable by punctuation/case normalisation
    alone), then an exact case/punctuation-insensitive match against
    `JPN_CANONICAL` -- not a similarity match, a normalisation
    (DESIGN_PHASE2.md 8.2-2's ban is on edit-distance matching specifically).
    """
    if name is None:
        return None
    text = str(name).strip()
    if not text:
        return None
    if text in ODDS_API_ALIASES:
        return ODDS_API_ALIASES[text]
    return canonical_team_j1(text)


def odds_api_team_id(name: str) -> str | None:
    canonical = resolve_odds_api_team(name)
    if canonical is None:
        return None
    return team_id_j1(canonical)
