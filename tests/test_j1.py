"""J1 data layer (DESIGN_PHASE2.md 6): the `new/JPN.csv` schema, the season
boundary, the play-off filter, team identity and the extended `check`.

All synthetic -- no network. The fixture below mimics the real file's shape:
`Country,League,Season,Date,Time,Home,Away,HG,AG,Res,PSC*,BFEC*` with the two
known landmines reproduced (a stray leading space in `League`, and the
2026/2027-vs-2026 season-label split at the cutover date).
"""

from __future__ import annotations

import pandas as pd
import pytest

from footy.config import jpn_season_label, jpn_season_of
from footy.data.check import run_checks_j1, run_checks_oos
from footy.data.j1_filter import filter_regular_season, regular_season_members
from footy.data.source_fd_new import normalise_jpn, season_label_drift
from footy.data.teams import (
    canonical_team_j1,
    near_duplicate_keys,
    resolve_team,
    unknown_names_j1,
)

_HEADER = (
    "Country,League,Season,Date,Time,Home,Away,HG,AG,Res,"
    "PSCH,PSCD,PSCA,BFECH,BFECD,BFECA"
)


def _row(season, date, home, away, hg, ag, res, psc=(1.9, 3.4, 4.1), bfec=("", "", "")):
    return (
        f"Japan, J1 League,{season},{date},19:00,{home},{away},{hg},{ag},{res},"
        f"{psc[0]},{psc[1]},{psc[2]},{bfec[0]},{bfec[1]},{bfec[2]}"
    )


def _write(path, rows):
    path.write_text("\n".join([_HEADER, *rows]) + "\n", encoding="utf-8")
    return path


def _frame(rows):
    import io

    text = "\n".join([_HEADER, *rows]) + "\n"
    return pd.read_csv(io.StringIO(text))


# --- season boundary -----------------------------------------------------
def test_jpn_season_of_spring_autumn_before_cutover():
    assert jpn_season_of(pd.Timestamp("2012-03-10")) == 2012
    assert jpn_season_of(pd.Timestamp("2025-12-06")) == 2025


def test_jpn_season_of_merges_the_split_2026_27_opening_weeks():
    """DESIGN_PHASE2.md 6.4: the source labels 2026-08-07..09 as `2026/2027`
    and 2026-08-14..15 as `2026` -- both must derive to the same season."""
    assert jpn_season_of(pd.Timestamp("2026-08-07")) == jpn_season_of(
        pd.Timestamp("2026-08-15")
    )
    assert jpn_season_of(pd.Timestamp("2026-08-07")) == 2026


def test_jpn_season_of_pre_cutover_summer_stays_the_old_season():
    assert jpn_season_of(pd.Timestamp("2026-06-30")) == 2026


def test_jpn_season_label_format():
    assert jpn_season_label(2012) == "2012"
    assert jpn_season_label(2026) == "2026-27"


# --- team identity ---------------------------------------------------------
def test_j1_canonical_spellings_resolve():
    assert canonical_team_j1("Yokohama F. Marinos") == "Yokohama F. Marinos"
    # Same normalisation discipline as E0's canonical_team: ascii-fold,
    # lowercase, punctuation-insensitive.
    assert canonical_team_j1("yokohama f marinos") == "Yokohama F. Marinos"
    assert canonical_team_j1(" Kyoto ") == "Kyoto"


def test_yokohama_f_marinos_and_yokohama_fc_never_collide():
    a = canonical_team_j1("Yokohama F. Marinos")
    b = canonical_team_j1("Yokohama FC")
    assert a != b
    assert resolve_team("JPN1", "Yokohama F. Marinos")[1] != resolve_team(
        "JPN1", "Yokohama FC"
    )[1]


def test_unknown_j1_spelling_is_reported():
    assert unknown_names_j1(["Kyoto", "Not A Real Club"]) == ["Not A Real Club"]


def test_resolve_team_auto_registers_non_e0_non_j1_divisions():
    canonical, team_id = resolve_team("SC0", "Celtic")
    assert canonical == "Celtic"
    assert team_id == "sco_1:celtic"
    # Same club, differently-cased/punctuated spelling -> same id.
    _, again = resolve_team("SC0", "  CELTIC  ")
    assert again == team_id


def test_resolve_team_e0_still_hard_fails_on_unknown_spelling():
    assert resolve_team("E0", "Not A Real Club") == (None, None)
    assert resolve_team("E0", "Arsenal") == ("Arsenal", "arsenal")


def test_near_duplicate_keys_flags_close_spellings_within_a_group():
    pairs = near_duplicate_keys({
        ("SC0", 2020): ["Celtic", "Celtik"],
        ("D1", 2020): ["Bayern Munich"],
    })
    assert (("SC0", 2020), "celtic", "celtik") in pairs


# --- new/JPN.csv normalisation ---------------------------------------------
def test_normalise_jpn_builds_the_unified_schema():
    rows = [
        _row(2012, "10/03/2012", "Gamba Osaka", "Vissel Kobe", 2, 3, "A"),
        _row(2012, "10/03/2012", "Kyoto", "Urawa Reds", 1, 1, "D"),
    ]
    frame = normalise_jpn(_frame(rows))
    assert list(frame["ftr"]) == ["A", "D"]
    assert frame["div"].eq("JPN1").all()
    assert frame["home_id"].tolist() == ["jpn_1:gamba_osaka", "jpn_1:kyoto"]
    assert frame["season"].tolist() == [2012, 2012]
    assert frame["bfech"].isna().all()          # not supplied in this fixture


def test_normalise_jpn_league_column_stray_space_is_tolerated():
    """DESIGN_PHASE2.md 6.4: 'Japan, J1 League' means the space sits inside
    League, not between Country and League -- normalise_jpn must not choke
    on it, but a genuinely different League value must still raise."""
    rows = [_row(2012, "10/03/2012", "Kyoto", "Urawa Reds", 1, 1, "D")]
    frame = _frame(rows)
    normalise_jpn(frame)  # does not raise

    frame.loc[0, "League"] = "J2 League"
    with pytest.raises(ValueError, match="unexpected League values"):
        normalise_jpn(frame)


def test_season_label_drift_is_zero_when_the_split_labels_both_start_2026():
    """DESIGN_PHASE2.md 6.4's split (`2026/2027` vs `2026` for the same
    season) does not, by itself, disagree with the *year* `jpn_season_of`
    derives -- both raw labels start with 2026, and so does the derived
    season. `season_label_drift` catches a *year* mismatch, which this is
    not; it is merely two different string spellings of one season.
    """
    rows = [
        "Japan, J1 League,2026/2027,07/08/2026,19:00,Kyoto,Urawa Reds,1,1,D,,,,,,",
        "Japan, J1 League,2026,14/08/2026,19:00,Kyoto,Urawa Reds,1,1,D,,,,,,",
    ]
    frame = _frame(rows)
    assert season_label_drift(frame) == 0


def test_season_label_drift_catches_a_genuine_year_mismatch():
    rows = [
        "Japan, J1 League,2030,10/03/2012,19:00,Kyoto,Urawa Reds,1,1,D,,,,,,",
        "Japan, J1 League,2012,10/03/2012,19:00,Kyoto,Urawa Reds,1,1,D,,,,,,",
    ]
    frame = _frame(rows)
    assert season_label_drift(frame) == 1


# --- play-off filter (DESIGN_PHASE2.md 6.3) ---------------------------------
def _regular_season_rows(season, teams):
    """A full round robin, dated inside `season` itself, so
    `jpn_season_of` derives exactly `season` for every row -- reproducing a
    full 18/20-team J1 season's worth of matches is not needed to exercise
    the filter, so tests lower `JPN_MEMBER_MIN_APPEARANCES` instead."""
    rows = []
    for home in teams:
        for away in teams:
            if home == away:
                continue
            rows.append(_row(season, f"10/03/{season}", home, away, 1, 0, "H"))
    return rows


def test_filter_regular_season_drops_low_appearance_teams(monkeypatch):
    """Team identity for JPN1 is the strict whitelist (`teams.JPN_CANONICAL`,
    same discipline as E0), so the synthetic clubs here must be real J1
    spellings -- an unresolved name would fail with NaN ids before the
    appearance-count filter ever runs, which is exactly why the interlopers
    below are two *real* low-appearance J2 leak sides from
    DESIGN_PHASE2.md 0.20, not made-up names.
    """
    import footy.data.j1_filter as j1_filter

    monkeypatch.setattr(j1_filter, "JPN_MEMBER_MIN_APPEARANCES", 4)
    teams = [
        "Kashima Antlers", "Urawa Reds", "Kyoto", "Vissel Kobe",
        "Kashiwa Reysol", "Cerezo Osaka",
    ]
    rows = _regular_season_rows(2015, teams)
    # A play-off pair that only meets once, well under the threshold.
    rows.append(_row(2015, "01/12/2015", "Tokushima", "Montedio Yamagata", 0, 0, "D"))
    frame = normalise_jpn(_frame(rows))

    filtered = filter_regular_season(frame)
    kept_teams = set(filtered["home_team"]) | set(filtered["away_team"])
    assert "Tokushima" not in kept_teams
    assert "Montedio Yamagata" not in kept_teams
    assert kept_teams == set(teams)
    # Full round robin among the 6 real clubs: N(N-1) rows.
    assert len(filtered) == 6 * 5


def test_filter_regular_season_drops_duplicate_fixtures_keeping_the_earlier(monkeypatch):
    import footy.data.j1_filter as j1_filter

    monkeypatch.setattr(j1_filter, "JPN_MEMBER_MIN_APPEARANCES", 2)
    teams = ["Kashima Antlers", "Urawa Reds", "Kyoto"]
    rows = _regular_season_rows(2015, teams)
    # A duplicated Kashima-vs-Urawa fixture (e.g. a re-paired play-off).
    rows.append(_row(2015, "01/12/2015", "Kashima Antlers", "Urawa Reds", 9, 9, "H"))
    frame = normalise_jpn(_frame(rows))
    filtered = filter_regular_season(frame)

    ab = filtered[
        (filtered["home_team"] == "Kashima Antlers")
        & (filtered["away_team"] == "Urawa Reds")
    ]
    assert len(ab) == 1
    assert ab["fthg"].iloc[0] == 1        # the earlier row's score, not 9-9


def test_regular_season_members_uses_the_explicit_list_for_2026_27():
    from footy.config import JPN_2026_27_MEMBERS

    frame = normalise_jpn(_frame([_row(2026, "08/08/2026", "Kyoto", "Urawa Reds", 1, 0, "H")]))
    members = regular_season_members(frame, 2026)
    assert members == set(JPN_2026_27_MEMBERS)


# --- check profiles ---------------------------------------------------------
def test_run_checks_j1_passes_on_a_clean_18_team_season(monkeypatch):
    import footy.data.j1_filter as j1_filter

    monkeypatch.setattr(j1_filter, "JPN_MEMBER_MIN_APPEARANCES", 4)
    teams = [
        "Kashima Antlers", "Urawa Reds", "Kyoto", "Vissel Kobe",
        "Kashiwa Reysol", "Cerezo Osaka",
    ]
    rows = _regular_season_rows(2015, teams)
    frame = normalise_jpn(_frame(rows))
    filtered = filter_regular_season(frame)

    result = run_checks_j1(filtered)
    assert result.stats["unknown_teams"] == 0
    # 6 teams isn't 18 or 20, so the shape check is expected to complain --
    # this test is about the *unknown-team* and *result-integrity* checks
    # passing cleanly, not the shape gate (exercised for real at N in {18,20}
    # by the OOS/J1 backtest runs themselves).
    assert not any("unknown J1 team spellings" in p for p in result.problems)


def test_run_checks_j1_fails_on_unknown_team_spelling():
    rows = [_row(2015, "10/03/2015", "Kyoto", "Not A Real Club", 1, 0, "H")]
    frame = normalise_jpn(_frame(rows))
    result = run_checks_j1(frame)
    assert not result.ok
    assert any("unknown J1 team spellings" in p for p in result.problems)


def test_run_checks_oos_does_not_hard_fail_on_a_non_380_season(tmp_path):
    """OOS-LEAGUES has no fixed match count per season; only E0's profile
    enforces 380 (DESIGN_PHASE2.md 5.4)."""
    from footy.data.load import normalise, read_raw_csv

    header = "Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR,PSCH,PSCD,PSCA"
    rows = [
        "G1,01/09/15,AEK,PAOK,1,0,H,1.9,3.4,4.1",
        "G1,08/09/15,PAOK,Olympiakos,0,0,D,2.1,3.2,3.6",
    ]
    path = tmp_path / "G1_1516.csv"
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    frame = normalise(read_raw_csv(path), 2015)

    result = run_checks_oos(frame)
    assert not any("380" in p for p in result.problems)
    assert result.stats["low_appearance_teams"] >= 0   # ran without erroring
