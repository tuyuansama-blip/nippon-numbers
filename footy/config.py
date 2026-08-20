"""Project-wide paths, period boundaries and frozen constants.

Every number that the protocol in docs/DESIGN.md pins down lives here and
nowhere else. The point is that the TUNE/TEST boundary, the pass thresholds
and the bootstrap seed cannot drift between modules -- if a threshold is
edited it is edited once, in the open.
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

# --- paths -------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("FOOTY_DATA_DIR", ROOT / "data"))
RAW_DIR = DATA_DIR / "raw"
MATCHES_PATH = DATA_DIR / "matches.parquet"
FROZEN_PARAMS_PATH = DATA_DIR / "frozen_params.json"
REPORTS_DIR = Path(os.environ.get("FOOTY_REPORTS_DIR", ROOT / "reports"))

# --- data source -------------------------------------------------------------
# https://www.football-data.co.uk/mmz4281/1213/E0.csv  == season 2012/13, E0.
BASE_URL = "https://www.football-data.co.uk/mmz4281"
DEFAULT_DIVS = ("E0",)
# Hard floor between two HTTP requests. Serial, always.
MIN_INTERVAL_SEC = 1.0
USER_AGENT = "footy-ev/0.1 (personal research tool; serial, rate-limited, cached)"
REQUEST_TIMEOUT_SEC = 30
MAX_RETRIES = 3
BACKOFF_BASE_SEC = 2.0

# --- period split (DESIGN.md 2.1; once decided, never moved) ------------------
# Seasons are named by their starting year: 2012 == the 2012/13 season.
WARMUP_START = 1995
TUNE_START = 2000
TUNE_END = 2011           # inclusive -- 2011/12, the last season without PSC*
TEST_START = 2012         # first season with PSCH/PSCD/PSCA
TEST_END = 2024           # 2024/25

# --- market ------------------------------------------------------------------
# Pinnacle closing 1X2 only. Max*/Avg* are cross-book synthetics and are not
# tradeable prices (DESIGN.md 2.4, "不採用").
ODDS_COLS = ("PSCH", "PSCD", "PSCA")
OVERROUND_MIN = 1.00
OVERROUND_MAX = 1.10
SHIN_Z_BOUNDS = (0.0, 0.4)

# --- model defaults (used when data/frozen_params.json is absent) -------------
DEFAULT_HALF_LIFE_DAYS = 365.0
DEFAULT_SIGMA = 0.35
DEFAULT_PI = (0.0, 0.0)          # (pi_a, pi_d) prior centre for promoted sides
RHO_BOUNDS = (-0.20, 0.20)
TAU_FLOOR = 1e-10
SCORE_K = 12                     # score matrix truncation, P(X>12 | lam~1.6) < 1e-8
WINDOW_SEASONS = 6               # training window truncation (DESIGN.md 1.3)
MAX_ITER = 500

# --- tuning grids (DESIGN.md 1.3; coarse-to-fine, run once, then frozen) ------
HALF_LIFE_GRID = (60.0, 90.0, 120.0, 180.0, 250.0, 365.0, 550.0, 800.0, float("inf"))
SIGMA_GRID = (0.15, 0.25, 0.35, 0.5, 0.8)

# --- evaluation --------------------------------------------------------------
BOOT_N = 2000
BOOT_SEED = 42
BOOT_ALPHA = 0.05

# Climatology reference rates quoted in DESIGN.md 0 (H/D/A).
CLIMATOLOGY_PRIOR = (0.46, 0.25, 0.29)

# --- pass thresholds (DESIGN.md 3; frozen before the run) --------------------
GAP_STRONG = 0.75
GAP_PASS = 0.60
GAP_WARN = 0.40
GAP_LL_PASS = 0.55
CI_UPPER_MAX = 0.022             # 95% upper bound of the paired RPS difference
AUDIT_MARGIN = 0.005             # mean(RPS_market - RPS_model) above this = audit
CALIB_MIN_BIN_N = 150
CALIB_MAX_DEV = 0.05
DRAW_MAX_DEV = 0.02
SE_MAX = 0.003

# --- COVID (DESIGN.md 6.2) ---------------------------------------------------
# Behind closed doors: reported both ways, judged with them included.
EMPTY_CROWD_START = "2020-06-17"
EMPTY_CROWD_END = "2021-05-23"


def season_label(start_year: int) -> str:
    """1995 -> '1995-96'."""
    return f"{start_year}-{(start_year + 1) % 100:02d}"


def season_code(start_year: int) -> str:
    """1995 -> '9596' (the football-data.co.uk directory name)."""
    return f"{start_year % 100:02d}{(start_year + 1) % 100:02d}"


def parse_season(text: str) -> int:
    """'2012-13' / '2012/13' / '2012' -> 2012."""
    cleaned = str(text).strip().replace("/", "-")
    head = cleaned.split("-")[0]
    if not head.isdigit():
        raise ValueError(f"not a season: {text!r}")
    year = int(head)
    if year < 100:                      # '95-96' style
        year += 1900 if year >= 90 else 2000
    return year


def csv_url(start_year: int, div: str = "E0") -> str:
    return f"{BASE_URL}/{season_code(start_year)}/{div}.csv"


def raw_path(start_year: int, div: str = "E0") -> Path:
    return RAW_DIR / f"{div}_{season_code(start_year)}.csv"


# ==============================================================================
# Phase 2 (docs/DESIGN_PHASE2.md). Nothing above this line is touched by phase 2
# -- ROOT, the TUNE/TEST split and the frozen model constants stay exactly as
# TUNE decided them (DESIGN_PHASE2.md 5.3-1).
# ==============================================================================

# --- OOS-LEAGUES: the 15 unseen divisions used once to confirm the acceptance
# conditions are reachable at all before J1 spends its only holdout run
# (DESIGN_PHASE2.md 5.2-[1]). Same mmz4281 source and schema as E0, so the
# existing fetch/build path is reused unchanged; only team identity and the
# strictness of `check` differ (DESIGN_PHASE2.md 5.4).
OOS_DIVS = (
    "E1", "SC0", "D1", "D2", "I1", "I2", "SP1", "SP2",
    "F1", "F2", "N1", "B1", "P1", "T1", "G1",
)
OOS_FETCH_FROM = 2005          # warm-up headroom ahead of the 2012 test start
OOS_MIN_APPEARANCES = 15       # below this a club is a `check` warning, not a fail

# --- J1 (football-data.co.uk `new/JPN.csv`, DESIGN_PHASE2.md 6) --------------
JPN_DIV = "JPN1"
JPN_LEAGUE_ID = "jpn_1"
JPN_URL = "https://www.football-data.co.uk/new/JPN.csv"
JPN_RAW_FILENAME = "JPN_new.csv"

JPN_WARMUP_SEASONS = (2012, 2013)         # training data only (DESIGN_PHASE2.md 7.1)
JPN_TEST_START = 2014
JPN_TEST_END = 2025                        # 12 seasons, n~3,880 with a PSC close

JPN_MEMBER_MIN_APPEARANCES = 25            # regular-season filter (6.3)
JPN_SEASON_CUTOVER = "2026-07-01"          # spring-autumn -> autumn-spring (6.4)
JPN_BENCHMARK_CUTOVER_SEASON = 2026        # season >= this benchmarks on BFEC, not PSC

# In-progress 2026-27 membership: the appearance-count filter above needs a
# full season of matches to work, so the first season under the new calendar
# is pinned explicitly instead (DESIGN_PHASE2.md 6.3, 6.6's caveat about it
# being the weakest-covered season in the project applies to this list too).
JPN_2026_27_MEMBERS = (
    "jpn_1:gamba_osaka", "jpn_1:urawa_reds", "jpn_1:avispa_fukuoka",
    "jpn_1:vissel_kobe", "jpn_1:cerezo_osaka", "jpn_1:okayama",
    "jpn_1:fc_tokyo", "jpn_1:machida", "jpn_1:kashiwa_reysol", "jpn_1:mito",
    "jpn_1:nagoya_grampus", "jpn_1:shimizu_s_pulse",
    "jpn_1:sanfrecce_hiroshima", "jpn_1:chiba", "jpn_1:verdy",
    "jpn_1:kawasaki_frontale", "jpn_1:v_varen_nagasaki", "jpn_1:kyoto",
    "jpn_1:yokohama_f_marinos", "jpn_1:kashima_antlers",
)

JPN_OVERROUND_PSC_MIN, JPN_OVERROUND_PSC_MAX = 1.00, 1.10     # check.py 6.7
JPN_OVERROUND_BFEC_MIN, JPN_OVERROUND_BFEC_MAX = 0.98, 1.05
JPN_STALENESS_MAX_SEC = 6 * 3600            # odds close_quality gate (8.2-4)

# --- J1 verdict: absolute paired-difference thresholds, not gap_closed -------
# (DESIGN_PHASE2.md 7.3). The ratios are the same numbers as EPL's gap_closed
# table (0.75/0.60/... in DESIGN.md 3) mapped onto J1's own measured span;
# only the span differs between the two leagues, never the ratio.
J1_STRONG_FRAC = 0.25
J1_PASS_FRAC = 0.40
J1_PASS_CI_FRAC = 0.55
J1_WARN_FRAC = 0.60
J1_LL_FRAC = 0.45

# --- calibration v2 (DESIGN_PHASE2.md 3) --------------------------------------
# CAL-1/CAL-2 replace the market-decile condition; CAL-3/CAL-4 are the
# unchanged draw-rate and bootstrap-SE checks from DESIGN.md 3, renamed only
# for the table in DESIGN_PHASE2.md 3.1.
CAL1_SPAN_FRACTION = 0.02       # reliability(debiased) <= max(this*Brier span, null p99)
CAL2_MAX_DEV = 0.05             # own-probability-decile worst |model - observed|
CAL2_MIN_BIN_N = 150
MURPHY_BINS = 20                # equal-frequency bins, 3-outcome pooled (0.1)
NULL_BOOT_N = 200                # parametric bootstrap draws for the null band (0.5)
NULL_BOOT_SEED = 43              # deliberately not BOOT_SEED: a different generator

# --- calibration layer Tb (DESIGN_PHASE2.md 4) --------------------------------
CALIBRATE_WARMUP_MATCHES = 600
CALIBRATE_LAMBDA = 0.003         # sum-NLL ridge back to phi_identity
CALIBRATE_REFIT_DAYS = 30        # "monthly"; exact-day cadence, asof-scoped
CL1_MAX_RPS_REGRESSION = 0.0002  # Do-No-Harm gate (4.4)


def jpn_season_of(ts) -> int:
    """Season start year for J1, derived from the date alone.

    2012-2025 are spring-autumn seasons (one calendar year each); 2026-
    onward is autumn-spring. `Season` in the source CSV is not used as the
    derivation: it is measurably inconsistent right at the cutover
    (DESIGN_PHASE2.md 6.4 -- the first week of 2026-27 is split between the
    labels `2026` and `2026/2027` in the raw file), the same kind of trap
    `load.py` already treats `Date` as authoritative over for the mmz4281
    files.
    """
    stamp = pd.Timestamp(ts)
    cutover = pd.Timestamp(JPN_SEASON_CUTOVER)
    if stamp < cutover:
        return int(stamp.year)
    return int(stamp.year) if stamp.month >= 7 else int(stamp.year) - 1


def jpn_season_label(start_year: int) -> str:
    """2012 -> '2012'; 2026 -> '2026-27' (DESIGN_PHASE2.md 6.4)."""
    if start_year < JPN_BENCHMARK_CUTOVER_SEASON:
        return str(start_year)
    return f"{start_year}-{(start_year + 1) % 100:02d}"
