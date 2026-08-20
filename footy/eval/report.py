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
    CALIB_MAX_DEV,
    CALIB_MIN_BIN_N,
    CI_UPPER_MAX,
    DRAW_MAX_DEV,
    GAP_LL_PASS,
    GAP_PASS,
    GAP_STRONG,
    GAP_WARN,
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
    market_decile_table,
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
