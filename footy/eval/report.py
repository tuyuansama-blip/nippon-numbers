"""Scoring a walk-forward run, judging it, and writing it up.

The verdict table in DESIGN.md 3 is frozen before the run, and it is
deliberately not symmetric: beating the market is not a win. Pinnacle's
closing 1X2 carries a 2.9% margin and is the sharpest price generally
available; a model that sees only past scorelines -- no injuries, no line-ups,
no fixture congestion -- beating it significantly is more likely to be a leak
than a discovery. So `FAIL-AUDIT` is checked before `STRONG`.

Everything is judged on paired per-match differences over the matches where a
Pinnacle close exists. Levels are reported but never judged: season difficulty
alone moves RPS from 0.180 to 0.209.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from footy.config import (
    AUDIT_MARGIN,
    CAL1_SPAN_FRACTION,
    CAL2_MAX_DEV,
    CAL2_MIN_BIN_N,
    CALIB_MAX_DEV,
    CALIB_MIN_BIN_N,
    CI_UPPER_MAX,
    DRAW_MAX_DEV,
    GAP_LL_PASS,
    GAP_PASS,
    GAP_STRONG,
    GAP_WARN,
    J1_LL_FRAC,
    J1_PASS_CI_FRAC,
    J1_PASS_FRAC,
    J1_STRONG_FRAC,
    J1_WARN_FRAC,
    REPORTS_DIR,
    SE_MAX,
    season_label,
)
from footy.eval.metrics import (
    block_bootstrap,
    calibration_table,
    draw_check,
    gap_closed,
    logloss_array,
    market_decile_expected_band,
    market_decile_table,
    murphy_decomposition,
    null_calibration_bootstrap,
    own_decile_table,
    rps_array,
)

VERDICTS = ("STRONG", "PASS", "WARN", "FAIL", "FAIL-AUDIT")


def make_run_id(prefix: str = "dc") -> str:
    return f"{_dt.datetime.now():%Y%m%d_%H%M%S}_{prefix}"


def params_hash(params: dict) -> str:
    blob = json.dumps(params, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:12]


@dataclass
class Evaluation:
    run_id: str
    params: dict
    n_total: int
    n_scored: int
    n_market_missing: int
    scores: pd.DataFrame
    paired: pd.DataFrame
    gaps: dict
    calibration: pd.DataFrame
    deciles: pd.DataFrame
    draws: dict
    by_season: pd.DataFrame
    crowd: pd.DataFrame
    fits: pd.DataFrame
    verdict: str = "FAIL"
    reasons: list[str] = field(default_factory=list)
    checks: dict = field(default_factory=dict)
    primary: str = "dc"
    # Phase 2 (DESIGN_PHASE2.md 3): populated only by `evaluate_v2`.
    murphy: dict | None = None
    own_deciles: pd.DataFrame | None = None
    null_band: dict | None = None
    market_band: dict | None = None


def _score_rows(probs: dict, y, blocks, label_map=None) -> pd.DataFrame:
    rows = []
    for name, matrix in probs.items():
        r = rps_array(matrix, y)
        ll = logloss_array(matrix, y)
        rows.append(
            {
                "model": (label_map or {}).get(name, name),
                "n": int(len(y)),
                "rps": float(np.mean(r)),
                "logloss": float(np.mean(ll)),
            }
        )
    return pd.DataFrame(rows)


def evaluate(result, *, primary: str = "dc", run_id: str | None = None) -> Evaluation:
    """Score every leg of a walk-forward run on one identical match set."""
    frame = result.frame
    run_id = run_id or make_run_id(primary)
    has_market = result.has_market
    n_missing = int((~has_market).sum())

    y_all = result.y
    scored = frame[has_market]
    y = y_all[has_market]
    blocks = result.blocks[has_market]

    legs: dict[str, np.ndarray] = {}
    for name, matrix in result.probs.items():
        legs[name] = matrix[has_market]
    legs["market"] = result.market("shin")[has_market]
    legs["market_mult"] = result.market("multiplicative")[has_market]

    labels = {
        "dc": "dc (Dixon-Coles)",
        "poisson": "poisson (no decay, rho=0)",
        "clim": "climatology",
        "market": "market (Pinnacle close, Shin)",
        "market_mult": "market (Pinnacle close, multiplicative)",
    }
    scores = _score_rows(legs, y, blocks, labels)

    # --- paired differences vs the market ------------------------------------
    market_rps = rps_array(legs["market"], y)
    market_ll = logloss_array(legs["market"], y)
    paired_rows = []
    for name, matrix in legs.items():
        if name.startswith("market"):
            continue
        d_rps = rps_array(matrix, y) - market_rps
        d_ll = logloss_array(matrix, y) - market_ll
        ci_rps = block_bootstrap(d_rps, blocks)
        ci_ll = block_bootstrap(d_ll, blocks)
        paired_rows.append(
            {
                "model": name,
                "d_rps": ci_rps["mean"],
                "d_rps_lo": ci_rps["lo"],
                "d_rps_hi": ci_rps["hi"],
                "d_rps_se": ci_rps["se"],
                "d_logloss": ci_ll["mean"],
                "d_logloss_lo": ci_ll["lo"],
                "d_logloss_hi": ci_ll["hi"],
                "n_blocks": ci_rps["n_blocks"],
            }
        )
    paired = pd.DataFrame(paired_rows)

    def level(name: str, column: str) -> float:
        row = scores[scores["model"] == labels.get(name, name)]
        return float(row[column].iloc[0]) if len(row) else float("nan")

    gaps = {}
    if "clim" in legs and primary in legs:
        gaps["rps"] = gap_closed(
            level("clim", "rps"), level(primary, "rps"), level("market", "rps")
        )
        gaps["logloss"] = gap_closed(
            level("clim", "logloss"),
            level(primary, "logloss"),
            level("market", "logloss"),
        )
    gaps["rps_market"] = level("market", "rps")
    gaps["rps_clim"] = level("clim", "rps")
    gaps["rps_model"] = level(primary, "rps")
    gaps["shin_vs_mult_rps"] = level("market", "rps") - level("market_mult", "rps")

    # --- calibration ---------------------------------------------------------
    model_probs = legs.get(primary)
    calibration = calibration_table(model_probs, y)
    deciles = market_decile_table(model_probs, legs["market"], y)
    draws = draw_check(model_probs, y)

    # --- diagnostics (never part of the verdict) -----------------------------
    by_season = []
    for season, group in scored.groupby("season", sort=True):
        mask = (scored["season"] == season).to_numpy()
        row = {
            "season": season_label(int(season)),
            "n": int(mask.sum()),
            "rps_model": float(np.mean(rps_array(model_probs[mask], y[mask]))),
            "rps_market": float(np.mean(rps_array(legs["market"][mask], y[mask]))),
            "rps_clim": float(np.mean(rps_array(legs["clim"][mask], y[mask])))
            if "clim" in legs
            else float("nan"),
        }
        row["gap_closed"] = gap_closed(
            row["rps_clim"], row["rps_model"], row["rps_market"]
        )
        by_season.append(row)
    by_season = pd.DataFrame(by_season)

    crowd_rows = []
    for flag, name in ((False, "crowd present"), (True, "crowd empty (COVID)")):
        mask = (scored["empty_crowd"] == flag).to_numpy()
        if mask.sum() == 0:
            continue
        crowd_rows.append(
            {
                "subset": name,
                "n": int(mask.sum()),
                "rps_model": float(np.mean(rps_array(model_probs[mask], y[mask]))),
                "rps_market": float(
                    np.mean(rps_array(legs["market"][mask], y[mask]))
                ),
            }
        )
    if crowd_rows:
        both = {
            "subset": "all (judged on this row)",
            "n": int(len(y)),
            "rps_model": level(primary, "rps"),
            "rps_market": level("market", "rps"),
        }
        crowd_rows.append(both)
    crowd = pd.DataFrame(crowd_rows)

    evaluation = Evaluation(
        run_id=run_id,
        params={**result.params, "params_hash": params_hash(result.params)},
        n_total=int(len(frame)),
        n_scored=int(len(y)),
        n_market_missing=n_missing,
        scores=scores,
        paired=paired,
        gaps=gaps,
        calibration=calibration,
        deciles=deciles,
        draws=draws,
        by_season=by_season,
        crowd=crowd,
        fits=result.fits,
        primary=primary,
    )
    _judge(evaluation)
    return evaluation


def _judge(ev: Evaluation, *, leak_ok: bool = True) -> None:
    """Apply the frozen table in DESIGN.md 3."""
    row = ev.paired[ev.paired["model"] == ev.primary]
    if row.empty:
        ev.verdict = "FAIL"
        ev.reasons.append(f"model {ev.primary!r} produced no paired differences")
        return
    row = row.iloc[0]

    gap_rps = ev.gaps.get("rps", float("nan"))
    gap_ll = ev.gaps.get("logloss", float("nan"))

    wide = ev.deciles[ev.deciles["n"] >= CALIB_MIN_BIN_N] if len(ev.deciles) else ev.deciles
    worst_decile = float(wide["gap"].abs().max()) if len(wide) else float("nan")
    decile_ok = bool(len(wide)) and worst_decile <= CALIB_MAX_DEV
    draw_ok = abs(ev.draws["gap"]) <= DRAW_MAX_DEV
    se_ok = float(row["d_rps_se"]) <= SE_MAX
    calibration_ok = decile_ok and draw_ok and se_ok

    ev.checks = {
        "gap_closed_rps": gap_rps,
        "gap_closed_logloss": gap_ll,
        "d_rps": float(row["d_rps"]),
        "d_rps_lo": float(row["d_rps_lo"]),
        "d_rps_hi": float(row["d_rps_hi"]),
        "d_rps_se": float(row["d_rps_se"]),
        "decile_worst_gap": worst_decile,
        "decile_bins_used": int(len(wide)),
        "decile_ok": decile_ok,
        "draw_gap": ev.draws["gap"],
        "draw_ok": bool(draw_ok),
        "se_ok": bool(se_ok),
        "calibration_ok": bool(calibration_ok),
        "ci_upper_ok": bool(float(row["d_rps_hi"]) <= CI_UPPER_MAX),
        "leak_ok": bool(leak_ok),
    }

    # 1. Beating the market is audited, not celebrated.
    beats_by = -float(row["d_rps"])
    if beats_by > AUDIT_MARGIN and float(row["d_rps_hi"]) < 0.0:
        ev.verdict = "FAIL-AUDIT"
        ev.reasons.append(
            f"model beats the market by {beats_by:.4f} RPS with a 95% CI "
            f"entirely below zero ([{row['d_rps_lo']:.4f}, "
            f"{row['d_rps_hi']:.4f}]). Treat as a leak until audited, not as "
            "a result."
        )
        return

    # 2. A broken calibration or a red leak test is fatal whatever the gap is.
    if not leak_ok:
        ev.verdict = "FAIL"
        ev.reasons.append("leak test is red")
        return
    if not calibration_ok:
        # The verdict table lists no calibration clause under STRONG, but its
        # FAIL row ("較正条件違反") carries no gap_closed qualifier. Read
        # together, a calibration breach is fatal at any gap, so FAIL wins --
        # and the gap it overrode is printed so nothing is hidden.
        ev.verdict = "FAIL"
        if np.isfinite(gap_rps) and gap_rps >= GAP_WARN:
            ev.reasons.append(
                f"gap_closed(RPS) = {gap_rps:.3f} would have been "
                + ("STRONG" if gap_rps >= GAP_STRONG else
                   "PASS" if gap_rps >= GAP_PASS else "WARN")
                + ", but a calibration condition is mandatory and failed"
            )
        if not decile_ok:
            ev.reasons.append(
                f"market-decile calibration: worst |model - observed| = "
                f"{worst_decile:.4f} > {CALIB_MAX_DEV:.2f} "
                f"(bins with n >= {CALIB_MIN_BIN_N}: {len(wide)})"
            )
        if not draw_ok:
            ev.reasons.append(
                f"draw rate off by {ev.draws['gap']:+.4f} "
                f"(limit {DRAW_MAX_DEV:.2f})"
            )
        if not se_ok:
            ev.reasons.append(
                f"bootstrap SE {row['d_rps_se']:.4f} > {SE_MAX:.3f}: the run "
                "cannot resolve the thresholds it is being judged against"
            )
        return

    # 3. The gap scale.
    if gap_rps >= GAP_STRONG:
        ev.verdict = "STRONG"
        ev.reasons.append(f"gap_closed(RPS) = {gap_rps:.3f} >= {GAP_STRONG}")
        return
    if (
        gap_rps >= GAP_PASS
        and float(row["d_rps_hi"]) <= CI_UPPER_MAX
        and gap_ll >= GAP_LL_PASS
    ):
        ev.verdict = "PASS"
        ev.reasons.append(
            f"gap_closed(RPS) = {gap_rps:.3f} >= {GAP_PASS}, "
            f"CI upper {row['d_rps_hi']:.4f} <= {CI_UPPER_MAX}, "
            f"gap_closed(LL) = {gap_ll:.3f} >= {GAP_LL_PASS}"
        )
        return
    if gap_rps >= GAP_WARN:
        ev.verdict = "WARN"
        ev.reasons.append(
            f"gap_closed(RPS) = {gap_rps:.3f} is in [{GAP_WARN}, {GAP_PASS}): "
            "the model works but is leaving something on the table -- go to "
            "the ablations"
        )
        if gap_rps >= GAP_PASS:
            ev.reasons.append(
                f"gap cleared {GAP_PASS} but a PASS side-condition did not: "
                f"CI upper {row['d_rps_hi']:.4f} (limit {CI_UPPER_MAX}), "
                f"gap_closed(LL) {gap_ll:.3f} (limit {GAP_LL_PASS})"
            )
        return
    ev.verdict = "FAIL"
    ev.reasons.append(
        f"gap_closed(RPS) = {gap_rps:.3f} < {GAP_WARN}. Suspect the "
        "implementation or the data before the threshold (DESIGN.md 6.4)."
    )


# --- rendering ---------------------------------------------------------------
def verdict_block(ev: Evaluation) -> str:
    """The `== 判定 ==` block, printed by the CLI and embedded in the report."""
    checks = ev.checks
    lines = ["== 判定 =="]
    lines.append(f"  verdict         {ev.verdict}")
    lines.append(
        f"  gap_closed RPS  {checks.get('gap_closed_rps', float('nan')):.3f}   "
        f"(STRONG >= {GAP_STRONG}, PASS >= {GAP_PASS}, WARN >= {GAP_WARN})"
    )
    lines.append(
        f"  gap_closed LL   {checks.get('gap_closed_logloss', float('nan')):.3f}"
        f"   (PASS >= {GAP_LL_PASS})"
    )
    lines.append(
        f"  RPS             model {ev.gaps.get('rps_model', float('nan')):.4f} / "
        f"market {ev.gaps.get('rps_market', float('nan')):.4f} / "
        f"clim {ev.gaps.get('rps_clim', float('nan')):.4f}"
    )
    lines.append(
        f"  paired d_RPS    {checks.get('d_rps', float('nan')):+.4f} "
        f"95%CI [{checks.get('d_rps_lo', float('nan')):+.4f}, "
        f"{checks.get('d_rps_hi', float('nan')):+.4f}]  "
        f"SE {checks.get('d_rps_se', float('nan')):.4f}"
    )
    lines.append(
        f"  calibration     deciles {'ok' if checks.get('decile_ok') else 'NG'}"
        f" (worst {checks.get('decile_worst_gap', float('nan')):.4f}, "
        f"{checks.get('decile_bins_used', 0)} bins) / "
        f"draw {'ok' if checks.get('draw_ok') else 'NG'} "
        f"({checks.get('draw_gap', float('nan')):+.4f}) / "
        f"SE {'ok' if checks.get('se_ok') else 'NG'}"
    )
    lines.append(
        f"  matches         {ev.n_scored:,} scored, {ev.n_market_missing:,} "
        "without a Pinnacle close (excluded from head-to-head)"
    )
    for reason in ev.reasons:
        lines.append(f"  -> {reason}")
    return "\n".join(lines)


def _table(frame: pd.DataFrame, floats: int = 4) -> str:
    if frame is None or len(frame) == 0:
        return "_(該当なし)_\n"
    header = "| " + " | ".join(str(c) for c in frame.columns) + " |"
    sep = "| " + " | ".join("---" for _ in frame.columns) + " |"
    lines = [header, sep]
    for row in frame.itertuples(index=False):
        cells = []
        for value in row:
            if isinstance(value, float):
                cells.append("-" if not np.isfinite(value) else f"{value:.{floats}f}")
            else:
                cells.append(str(value))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def plot_calibration(ev: Evaluation, path: Path) -> Path | None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if ev.calibration.empty:
        return None
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 5))
    ax1.plot([0, 1], [0, 1], color="#888", ls="--", lw=0.8, label="perfect")
    for outcome, colour in (("H", "#1f77b4"), ("D", "#ff7f0e"), ("A", "#2ca02c")):
        part = ev.calibration[
            (ev.calibration["outcome"] == outcome) & (ev.calibration["n"] >= 20)
        ]
        if part.empty:
            continue
        sizes = part["n"].to_numpy(dtype="float64")
        ax1.scatter(
            part["p_mean"], part["observed"],
            s=10 + 60 * sizes / max(sizes.max(), 1.0),
            color=colour, alpha=0.75, label=outcome,
        )
        ax1.plot(part["p_mean"], part["observed"], color=colour, lw=1.0, alpha=0.6)
    ax1.set_xlabel("predicted probability")
    ax1.set_ylabel("observed frequency")
    ax1.set_title(f"Model calibration ({ev.primary})")
    ax1.grid(alpha=0.25)
    ax1.legend()

    if not ev.deciles.empty:
        ax2.plot([0, 1], [0, 1], color="#888", ls="--", lw=0.8)
        ax2.plot(ev.deciles["market_mean"], ev.deciles["observed"], "o-",
                 color="#d62728", label="observed")
        ax2.plot(ev.deciles["market_mean"], ev.deciles["model_mean"], "s-",
                 color="#1f77b4", label="model mean")
        ax2.set_xlabel("market implied probability (decile mean)")
        ax2.set_ylabel("probability")
        ax2.set_title("By market decile")
        ax2.grid(alpha=0.25)
        ax2.legend()

    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


def plot_rps_by_season(ev: Evaluation, path: Path) -> Path | None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if ev.by_season.empty:
        return None
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(9, 6), sharex=True, height_ratios=[2, 1]
    )
    x = np.arange(len(ev.by_season))
    ax1.plot(x, ev.by_season["rps_clim"], "o-", color="#888", label="climatology")
    ax1.plot(x, ev.by_season["rps_market"], "o-", color="#d62728", label="market")
    ax1.plot(x, ev.by_season["rps_model"], "o-", color="#1f77b4", label=ev.primary)
    ax1.set_ylabel("mean RPS")
    ax1.set_title(f"RPS by season  ({ev.run_id})  verdict={ev.verdict}")
    ax1.grid(alpha=0.25)
    ax1.legend()

    ax2.bar(x, ev.by_season["gap_closed"], color="#1f77b4", alpha=0.7)
    ax2.axhline(GAP_PASS, color="#2ca02c", ls="--", lw=0.8, label=f"PASS {GAP_PASS}")
    ax2.axhline(GAP_WARN, color="#ff7f0e", ls="--", lw=0.8, label=f"WARN {GAP_WARN}")
    ax2.set_ylabel("gap_closed")
    ax2.set_xticks(x)
    ax2.set_xticklabels(ev.by_season["season"], rotation=45, ha="right")
    ax2.grid(alpha=0.25)
    ax2.legend(fontsize=8)

    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


def write_report(ev: Evaluation, reports_dir: Path | str | None = None) -> Path:
    root = Path(reports_dir) if reports_dir else REPORTS_DIR
    root.mkdir(parents=True, exist_ok=True)
    calib_png = plot_calibration(ev, root / f"calibration_{ev.run_id}.png")
    season_png = plot_rps_by_season(ev, root / f"rps_by_season_{ev.run_id}.png")

    lines: list[str] = []
    lines.append(f"# バックテスト結果 `{ev.run_id}`\n")
    lines.append("## 判定\n")
    lines.append(
        "> 合格条件は「市場に勝つこと」ではなく **`gap_closed ≥ 0.60`**"
        "（気候値と市場の差のうち 60% 以上を埋めること）。\n"
        "> 市場に有意に勝った場合は成功ではなく **FAIL-AUDIT**（リーク疑い）"
        "として扱う。得点結果しか見ないモデルが Pinnacle クローズに勝つ確率は、"
        "バグの確率より低い。\n"
    )
    lines.append("```\n" + verdict_block(ev) + "\n```\n")

    lines.append("## 実行条件\n")
    params = pd.DataFrame(
        [{"key": k, "value": v} for k, v in sorted(ev.params.items())]
    )
    lines.append(_table(params))
    lines.append(
        f"\n- 予測対象 {ev.n_total:,} 試合中 **{ev.n_scored:,}** 試合で"
        f"head-to-head 比較（Pinnacle クローズ欠損 **{ev.n_market_missing:,}** 件は"
        "比較から除外、モデル単独指標には残る）\n"
    )

    lines.append("\n## 水準（判定には使わない）\n")
    lines.append(
        "> RPS の水準はシーズン難易度で 0.180〜0.209 も動く。"
        "判定は必ず同一試合集合上のペア差で行う。\n"
    )
    lines.append(_table(ev.scores))
    lines.append(
        f"\nShin法 − 単純正規化 の RPS 差: **{ev.gaps.get('shin_vs_mult_rps', float('nan')):+.5f}**"
        "（マージン除去方式は結論を動かさない）\n"
    )

    lines.append("\n## ペア差（市場 Shin 基準・節ブロックブートストラップ）\n")
    lines.append(_table(ev.paired))

    lines.append("\n## gap_closed\n")
    gaps = pd.DataFrame(
        [{"metric": k, "value": v} for k, v in ev.gaps.items()]
    )
    lines.append(_table(gaps, floats=5))

    lines.append("\n## 較正\n")
    lines.append("### 市場含意確率デシル別（判定に使う）\n")
    lines.append(_table(ev.deciles))
    lines.append(
        f"\n引き分け専用チェック: 平均 p_draw **{ev.draws['mean_p_draw']:.4f}** / "
        f"実測 **{ev.draws['observed_draw_rate']:.4f}** / "
        f"差 **{ev.draws['gap']:+.4f}**（許容 ±{DRAW_MAX_DEV}）\n"
    )
    lines.append("\n### 予測確率バケット別（アウトカム別）\n")
    lines.append(_table(ev.calibration[ev.calibration["n"] >= 20]))

    lines.append("\n## 診断（判定には使わない）\n")
    lines.append("### シーズン別\n")
    lines.append(_table(ev.by_season))
    if not ev.crowd.empty:
        lines.append("\n### COVID 無観客期間の内訳\n")
        lines.append(
            "> 主結果は「含む」で判定する。除いた結果で判定すると事後選択になる。\n"
        )
        lines.append(_table(ev.crowd))

    if ev.fits is not None and not ev.fits.empty:
        seconds = [c for c in ev.fits.columns if c.endswith("_seconds")]
        if seconds:
            summary = ev.fits[seconds].agg(["mean", "median", "max", "sum"])
            summary = summary.reset_index(names="stat")
            lines.append("\n### fit 所要時間（秒）\n")
            lines.append(_table(summary, floats=4))
            lines.append(f"\nフォールド数: **{len(ev.fits):,}**\n")

    if calib_png:
        lines.append(f"\n![calibration]({calib_png.name})\n")
    if season_png:
        lines.append(f"\n![rps by season]({season_png.name})\n")

    path = root / f"backtest_{ev.run_id}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ==============================================================================
# Phase 2 (DESIGN_PHASE2.md 3, 7.3). `evaluate_v2` reuses every number
# `evaluate` already computed -- scores, paired differences, gap_closed,
# by-season/crowd diagnostics -- and replaces only the two things §2 found
# wrong: the market-decile calibration gate becomes CAL-1..4, and (J1 only)
# the gap_closed verdict scale becomes an absolute paired-difference scale
# measured against J1's own span (§7.3), because gap_closed's denominator is
# too small there for its own CI to resolve the 0.60 threshold (§7.3, §11-2).
#
# `evaluate`/`_judge`/`verdict_block`/`write_report` above are untouched: the
# EPL TEST leg in DESIGN.md is v1's, on the record, unmodified.
# ==============================================================================


def _calibration_v2(ev: "Evaluation", result, primary: str) -> dict:
    """CAL-1..4 (DESIGN_PHASE2.md 3.1): Murphy decomposition + own-decile
    table + the null bootstrap that sets CAL-1's floor, all computed once and
    hung off `ev` so both judging and rendering can reuse them."""
    has_market = result.has_market
    y = result.y[has_market]
    model_probs = result.probs[primary][has_market]
    market_probs = result.market("shin")[has_market]

    murphy_model = murphy_decomposition(model_probs, y)
    murphy_market = murphy_decomposition(market_probs, y)
    murphy_clim = (
        murphy_decomposition(result.probs["clim"][has_market], y)
        if "clim" in result.probs else None
    )
    ev.murphy = {"model": murphy_model, "market": murphy_market, "clim": murphy_clim}

    own_deciles = own_decile_table(model_probs, y)
    ev.own_deciles = own_deciles
    null_band = null_calibration_bootstrap(model_probs)
    ev.null_band = null_band

    brier_span = (
        murphy_clim["brier"] - murphy_market["brier"]
        if murphy_clim is not None else float("nan")
    )
    cal1_threshold = null_band["reliability_debiased_p99"]
    if np.isfinite(brier_span):
        cal1_threshold = max(CAL1_SPAN_FRACTION * brier_span, cal1_threshold)
    cal1_value = murphy_model["reliability_debiased"]
    cal1_ok = bool(np.isfinite(cal1_value) and cal1_value <= cal1_threshold)

    wide = own_deciles[own_deciles["n"] >= CAL2_MIN_BIN_N] if len(own_deciles) else own_deciles
    cal2_worst = float(wide["gap"].abs().max()) if len(wide) else float("nan")
    cal2_ok = bool(len(wide)) and cal2_worst <= CAL2_MAX_DEV

    row = ev.paired[ev.paired["model"] == primary]
    row = row.iloc[0] if len(row) else None
    cal3_ok = bool(np.isfinite(ev.draws["gap"]) and abs(ev.draws["gap"]) <= DRAW_MAX_DEV)
    cal4_ok = bool(row is not None and float(row["d_rps_se"]) <= SE_MAX)

    checks = {
        "brier_span": brier_span,
        "cal1_reliability_debiased": cal1_value,
        "cal1_threshold": cal1_threshold,
        "cal1_ok": cal1_ok,
        "cal2_own_decile_worst_gap": cal2_worst,
        "cal2_bins_used": int(len(wide)),
        "cal2_ok": cal2_ok,
        "cal3_draw_gap": ev.draws["gap"],
        "cal3_ok": cal3_ok,
        "cal4_se": float(row["d_rps_se"]) if row is not None else float("nan"),
        "cal4_ok": cal4_ok,
        "calibration_ok": bool(cal1_ok and cal2_ok and cal3_ok and cal4_ok),
    }

    # Diagnostic only (DESIGN_PHASE2.md 3.2): the band a perfectly-honest
    # forecaster of this run's own resolution would show on the (demoted)
    # market-decile table.
    ev.market_band = None
    gap_rps = ev.gaps.get("rps", float("nan"))
    if np.isfinite(gap_rps) and "clim" in result.probs:
        ev.market_band = market_decile_expected_band(
            market_probs, y,
            clim_score=ev.gaps["rps_clim"], market_score=ev.gaps["rps_market"],
            target_gap_closed=gap_rps,
        )
    return checks


def evaluate_v2(
    result, *, primary: str = "dc", run_id: str | None = None,
    league_mode: str = "gap",
) -> Evaluation:
    """`evaluate` plus the phase-2 calibration axis and (optionally) the J1
    absolute-difference verdict scale (DESIGN_PHASE2.md 3, 7.3).

    `league_mode="gap"` -- EPL TEST re-score and OOS-LEAGUES: gap_closed
    stays the judging scale exactly as DESIGN.md 3 fixed it, only CAL-1..4
    replace the market-decile gate.
    `league_mode="absolute"` -- J1: the verdict scale itself changes to
    absolute paired RPS/LL differences against J1's own measured span,
    because gap_closed's denominator is 42% of EPL's and its CI cannot
    resolve 0.60 there (DESIGN_PHASE2.md 7.3, 11-2).
    """
    ev = evaluate(result, primary=primary, run_id=run_id)
    cal = _calibration_v2(ev, result, primary)
    ev.checks.update(cal)

    row = ev.paired[ev.paired["model"] == primary].iloc[0]
    if league_mode == "absolute":
        _judge_j1(ev, result, primary, row, cal["calibration_ok"])
    else:
        _judge_gap_v2(ev, row, cal["calibration_ok"])
    return ev


def _judge_gap_v2(ev: "Evaluation", row, calibration_ok: bool) -> None:
    """DESIGN.md 3's gap_closed scale, unchanged (DESIGN_PHASE2.md 3.3),
    with the market-decile/draw/SE gate replaced by CAL-1..4."""
    gap_rps = ev.gaps.get("rps", float("nan"))
    gap_ll = ev.gaps.get("logloss", float("nan"))
    ev.checks["gap_closed_rps"] = gap_rps
    ev.checks["gap_closed_logloss"] = gap_ll
    ev.checks["d_rps"] = float(row["d_rps"])
    ev.checks["d_rps_lo"] = float(row["d_rps_lo"])
    ev.checks["d_rps_hi"] = float(row["d_rps_hi"])
    ev.checks["ci_upper_ok"] = bool(float(row["d_rps_hi"]) <= CI_UPPER_MAX)

    beats_by = -float(row["d_rps"])
    if beats_by > AUDIT_MARGIN and float(row["d_rps_hi"]) < 0.0:
        ev.verdict = "FAIL-AUDIT"
        ev.reasons = [
            f"model beats the market by {beats_by:.4f} RPS with a 95% CI "
            f"entirely below zero ([{row['d_rps_lo']:.4f}, "
            f"{row['d_rps_hi']:.4f}]). Treat as a leak until audited, not as "
            "a result (DESIGN.md 3, unchanged by phase 2)."
        ]
        return

    if not calibration_ok:
        ev.verdict = "FAIL"
        ev.reasons = [
            "calibration v2 (CAL-1..4, DESIGN_PHASE2.md 3.1) failed; the "
            f"gap it overrides is gap_closed(RPS)={gap_rps:.3f}"
        ]
        c = ev.checks
        if not c["cal1_ok"]:
            ev.reasons.append(
                f"CAL-1: reliability(debiased)={c['cal1_reliability_debiased']:.6f} "
                f"> threshold {c['cal1_threshold']:.6f}"
            )
        if not c["cal2_ok"]:
            ev.reasons.append(
                f"CAL-2: own-decile worst |gap|={c['cal2_own_decile_worst_gap']:.4f} "
                f"> {CAL2_MAX_DEV} ({c['cal2_bins_used']} bins with n>={CAL2_MIN_BIN_N})"
            )
        if not c["cal3_ok"]:
            ev.reasons.append(
                f"CAL-3: draw gap {c['cal3_draw_gap']:+.4f} outside +/-{DRAW_MAX_DEV}"
            )
        if not c["cal4_ok"]:
            ev.reasons.append(f"CAL-4: bootstrap SE {c['cal4_se']:.4f} > {SE_MAX}")
        return

    if gap_rps >= GAP_STRONG:
        ev.verdict = "STRONG"
        ev.reasons = [f"gap_closed(RPS) = {gap_rps:.3f} >= {GAP_STRONG}"]
        return
    if gap_rps >= GAP_PASS and float(row["d_rps_hi"]) <= CI_UPPER_MAX and gap_ll >= GAP_LL_PASS:
        ev.verdict = "PASS"
        ev.reasons = [
            f"gap_closed(RPS) = {gap_rps:.3f} >= {GAP_PASS}, CI upper "
            f"{row['d_rps_hi']:.4f} <= {CI_UPPER_MAX}, gap_closed(LL) = "
            f"{gap_ll:.3f} >= {GAP_LL_PASS}, calibration v2 ok"
        ]
        return
    if gap_rps >= GAP_WARN:
        ev.verdict = "WARN"
        ev.reasons = [
            f"gap_closed(RPS) = {gap_rps:.3f} is in [{GAP_WARN}, {GAP_PASS})"
        ]
        return
    ev.verdict = "FAIL"
    ev.reasons = [f"gap_closed(RPS) = {gap_rps:.3f} < {GAP_WARN}"]


def _judge_j1(ev: "Evaluation", result, primary: str, row, calibration_ok: bool) -> None:
    """DESIGN_PHASE2.md 7.3: absolute paired differences against J1's own
    measured span, not gap_closed. The ratios (0.25/0.40/0.55/0.60) are the
    same numbers as DESIGN.md 3's gap_closed table (0.75/0.60/.../0.60)
    applied to J1's span instead of EPL's -- fixed in `footy/config.py`
    before this run, never chosen after seeing J1's own result."""
    has_market = result.has_market
    y = result.y[has_market]
    rps_span = ev.gaps.get("rps_clim", float("nan")) - ev.gaps.get("rps_market", float("nan"))

    ll_clim = (
        float(np.mean(logloss_array(result.probs["clim"][has_market], y)))
        if "clim" in result.probs else float("nan")
    )
    ll_market = float(np.mean(logloss_array(result.market("shin")[has_market], y)))
    ll_span = ll_clim - ll_market
    d_ll = float(row["d_logloss"])

    d_rps = float(row["d_rps"])
    d_rps_hi = float(row["d_rps_hi"])
    strong_th = J1_STRONG_FRAC * rps_span
    pass_th = J1_PASS_FRAC * rps_span
    warn_th = J1_WARN_FRAC * rps_span
    ci_th = J1_PASS_CI_FRAC * rps_span
    ll_th = J1_LL_FRAC * ll_span

    ev.checks.update({
        "rps_span": rps_span,
        "ll_span": ll_span,
        "d_rps": d_rps,
        "d_rps_lo": float(row["d_rps_lo"]),
        "d_rps_hi": d_rps_hi,
        "d_logloss": d_ll,
        "j1_strong_threshold": strong_th,
        "j1_pass_threshold": pass_th,
        "j1_warn_threshold": warn_th,
        "j1_ci_threshold": ci_th,
        "j1_ll_threshold": ll_th,
        "gap_closed_rps": ev.gaps.get("rps", float("nan")),
        "gap_closed_logloss": ev.gaps.get("logloss", float("nan")),
    })

    beats_by = -d_rps
    if beats_by > AUDIT_MARGIN and d_rps_hi < 0.0:
        ev.verdict = "FAIL-AUDIT"
        ev.reasons = [
            f"model beats the market by {beats_by:.4f} RPS with a 95% CI "
            f"entirely below zero. Treat as a leak, not a result."
        ]
        return

    if not calibration_ok:
        ev.verdict = "FAIL"
        ev.reasons = ["calibration v2 (CAL-1..4) failed"]
        c = ev.checks
        if not c["cal1_ok"]:
            ev.reasons.append(
                f"CAL-1: reliability(debiased)={c['cal1_reliability_debiased']:.6f} "
                f"> threshold {c['cal1_threshold']:.6f}"
            )
        if not c["cal2_ok"]:
            ev.reasons.append(
                f"CAL-2: own-decile worst |gap|={c['cal2_own_decile_worst_gap']:.4f} "
                f"> {CAL2_MAX_DEV}"
            )
        if not c["cal3_ok"]:
            ev.reasons.append(f"CAL-3: draw gap {c['cal3_draw_gap']:+.4f}")
        if not c["cal4_ok"]:
            ev.reasons.append(f"CAL-4: bootstrap SE {c['cal4_se']:.4f} > {SE_MAX}")
        return

    if d_rps <= strong_th:
        ev.verdict = "STRONG"
        ev.reasons = [f"d_RPS = {d_rps:+.4f} <= STRONG threshold {strong_th:.4f}"]
        return
    if d_rps <= pass_th and d_rps_hi <= ci_th and d_ll <= ll_th:
        ev.verdict = "PASS"
        ev.reasons = [
            f"d_RPS = {d_rps:+.4f} <= {pass_th:.4f}, CI upper {d_rps_hi:.4f} "
            f"<= {ci_th:.4f}, d_LL = {d_ll:+.4f} <= {ll_th:.4f}, "
            "calibration v2 ok"
        ]
        return
    if d_rps <= warn_th:
        ev.verdict = "WARN"
        ev.reasons = [f"d_RPS = {d_rps:+.4f} is in ({pass_th:.4f}, {warn_th:.4f}]"]
        return
    ev.verdict = "FAIL"
    ev.reasons = [f"d_RPS = {d_rps:+.4f} > WARN threshold {warn_th:.4f}"]


def verdict_block_v2(ev: "Evaluation", *, league_mode: str = "gap") -> str:
    """The v2 `== 判定 ==` block: CAL-1..4 replace the market-decile line,
    and (J1) absolute d_RPS/thresholds replace gap_closed as the headline."""
    c = ev.checks
    lines = ["== 判定 (v2) =="]
    lines.append(f"  verdict         {ev.verdict}")
    if league_mode == "absolute":
        lines.append(
            f"  d_RPS           {c.get('d_rps', float('nan')):+.4f} 95%CI "
            f"[{c.get('d_rps_lo', float('nan')):+.4f}, "
            f"{c.get('d_rps_hi', float('nan')):+.4f}]  span={c.get('rps_span', float('nan')):.4f}"
        )
        lines.append(
            f"  thresholds      STRONG<={c.get('j1_strong_threshold', float('nan')):.4f}  "
            f"PASS<={c.get('j1_pass_threshold', float('nan')):.4f}  "
            f"CI<={c.get('j1_ci_threshold', float('nan')):.4f}  "
            f"WARN<={c.get('j1_warn_threshold', float('nan')):.4f}"
        )
        lines.append(
            f"  d_LL            {c.get('d_logloss', float('nan')):+.4f}  "
            f"<= {c.get('j1_ll_threshold', float('nan')):.4f}"
        )
        lines.append(
            f"  gap_closed RPS  {c.get('gap_closed_rps', float('nan')):.3f}  "
            "(reported, not judged -- J1's span is too small for its CI, DESIGN_PHASE2.md 7.3)"
        )
    else:
        lines.append(
            f"  gap_closed RPS  {c.get('gap_closed_rps', float('nan')):.3f}   "
            f"(STRONG >= {GAP_STRONG}, PASS >= {GAP_PASS}, WARN >= {GAP_WARN})"
        )
        lines.append(
            f"  gap_closed LL   {c.get('gap_closed_logloss', float('nan')):.3f}"
            f"   (PASS >= {GAP_LL_PASS})"
        )
        lines.append(
            f"  paired d_RPS    {c.get('d_rps', float('nan')):+.4f} "
            f"95%CI [{c.get('d_rps_lo', float('nan')):+.4f}, "
            f"{c.get('d_rps_hi', float('nan')):+.4f}]"
        )
    lines.append(
        f"  CAL-1 reliability(debiased) {c.get('cal1_reliability_debiased', float('nan')):.6f} "
        f"<= {c.get('cal1_threshold', float('nan')):.6f}  "
        f"[{'ok' if c.get('cal1_ok') else 'NG'}]"
    )
    lines.append(
        f"  CAL-2 own-decile worst|gap| {c.get('cal2_own_decile_worst_gap', float('nan')):.4f} "
        f"<= {CAL2_MAX_DEV}  ({c.get('cal2_bins_used', 0)} bins)  "
        f"[{'ok' if c.get('cal2_ok') else 'NG'}]"
    )
    lines.append(
        f"  CAL-3 draw gap  {c.get('cal3_draw_gap', float('nan')):+.4f} <= {DRAW_MAX_DEV}  "
        f"[{'ok' if c.get('cal3_ok') else 'NG'}]"
    )
    lines.append(
        f"  CAL-4 SE        {c.get('cal4_se', float('nan')):.4f} <= {SE_MAX}  "
        f"[{'ok' if c.get('cal4_ok') else 'NG'}]"
    )
    lines.append(
        f"  matches         {ev.n_scored:,} scored, {ev.n_market_missing:,} "
        "without a close price"
    )
    for reason in ev.reasons:
        lines.append(f"  -> {reason}")
    return "\n".join(lines)


def write_report_v2(
    ev: "Evaluation", reports_dir=None, *, league_mode: str = "gap"
) -> Path:
    """`write_report`, with a Murphy-decomposition section, the CAL-1..4
    table, the market-decile band diagnostic and the calibration-layer phi
    time series appended (DESIGN_PHASE2.md 3.4, 4.4-CL3)."""
    root = Path(reports_dir) if reports_dir else REPORTS_DIR
    root.mkdir(parents=True, exist_ok=True)
    path = write_report(ev, root)          # v1 body, unmodified

    lines: list[str] = ["\n## フェーズ2 追補: 較正 v2\n"]
    lines.append("```\n" + verdict_block_v2(ev, league_mode=league_mode) + "\n```\n")

    lines.append("### Murphy 分解 (3アウトカム プール・等頻度20ビン)\n")
    murphy_rows = []
    for name, m in (ev.murphy or {}).items():
        if m is None:
            continue
        murphy_rows.append({"leg": name, **m})
    lines.append(_table(pd.DataFrame(murphy_rows), floats=6))
    if ev.murphy and ev.murphy.get("model") and ev.murphy.get("market"):
        d_brier = ev.murphy["model"]["brier"] - ev.murphy["market"]["brier"]
        d_rel = ev.murphy["model"]["reliability_raw"] - ev.murphy["market"]["reliability_raw"]
        d_res = ev.murphy["market"]["resolution"] - ev.murphy["model"]["resolution"]
        lines.append(
            f"\nmodel - market Brier差 **{d_brier:+.5f}**: 較正不良ぶん "
            f"**{d_rel:+.5f}**、解像度不足ぶん **{d_res:+.5f}**（DESIGN_PHASE2.md 0.4 と同形式）\n"
        )

    lines.append("\n### 帰無分布ブートストラップ（完全較正な予測器, n_boot="
                  f"{ev.null_band.get('n_boot', 0) if ev.null_band else 0}）\n")
    if ev.null_band:
        lines.append(_table(pd.DataFrame([ev.null_band]), floats=6))

    lines.append("\n### CAL-2 自前確率デシル別\n")
    lines.append(_table(ev.own_deciles))

    lines.append("\n### 市場デシル表（診断のみ・判定に使わない）と期待帯\n")
    lines.append(_table(ev.deciles))
    if ev.market_band:
        lines.append(
            f"\n完全較正な予測器（gap_closed={ev.market_band['target_gap_closed']:.3f} "
            f"相当の解像度, sd={ev.market_band['sd']:.3f}, "
            f"{ev.market_band['n_seeds']} seeds）が示すはずの市場デシル最悪|gap|: "
            f"**{ev.market_band['worst_gap_mean']:.4f} ± "
            f"{ev.market_band['worst_gap_sd']:.4f}**\n\n"
            f"_{ev.market_band['note']}_\n"
        )

    if ev.fits is not None and "cal_temperature" in ev.fits.columns:
        lines.append("\n### 較正層 φ の時系列 (CL-3)\n")
        phi = ev.fits[ev.fits["cal_temperature"].notna()]
        cols = ["fold", "cal_n_history", "cal_warm", "cal_temperature",
                "cal_phi_home", "cal_phi_draw"]
        cols = [c for c in cols if c in phi.columns]
        sample = phi[cols]
        if len(sample) > 40:
            step = max(1, len(sample) // 40)
            sample = sample.iloc[::step]
        lines.append(_table(sample, floats=4))

    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    return path
