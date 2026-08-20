"""Project-wide paths, period boundaries and frozen constants.

Every number that the protocol in docs/DESIGN.md pins down lives here and
nowhere else. The point is that the TUNE/TEST boundary, the pass thresholds
and the bootstrap seed cannot drift between modules -- if a threshold is
edited it is edited once, in the open.
"""

from __future__ import annotations

import os
from pathlib import Path

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
