"""`footy site build` (DESIGN_SITE.md 2.1, MVP slice of P1/P4/P6).

Reads whatever is already committed to `predictions/j1_*.json` and renders a
handful of static HTML pages: this round's predictions (`index.html`), a
plain-English methodology summary (`methodology.html`), and the track-record
page (`record.html`). Nothing here fits a matches.parquet or reports/ into
the picture -- the round page is rendered from the frozen prediction JSON
only (DESIGN_SITE.md 1.4: "予測ブロックのレンダリング元は必ず凍結 JSON の
み"), and the track record is rebuilt from those same JSON files via
`footy.pipeline.reconcile.build_track_record`, which already treats them as
the single source of truth.

Output is pure static HTML + one CSS file, no build step needed once
written -- point Cloudflare Pages (or any static host) at the output
directory directly (DESIGN_SITE.md 2.3).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
from jinja2 import Environment, FileSystemLoader, select_autoescape

from footy.config import PREDICTIONS_DIR
from footy.pipeline.reconcile import build_track_record
from footy.site import facts

SITE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = SITE_DIR / "templates"
STATIC_DIR = SITE_DIR / "static"
DEFAULT_OUTPUT_DIR = SITE_DIR.parent.parent / "site"


def _fmt_utc(value: str | None) -> str:
    """`"2026-08-21T10:00:00+00:00"` -> `"2026-08-21 10:00 UTC"`. Templates
    only ever display timestamps, never compute with them, so this stays a
    plain string formatter rather than a timezone-aware helper elsewhere."""
    if not value:
        return "-"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _fmt_kickoff(value: str | None) -> str:
    """Compact per-row kickoff for the fixtures table: `"Aug 21 10:00"`.
    The year and the UTC marker live once in the table caption instead of
    being repeated on every row, so the probability columns keep their room
    on narrow screens."""
    if not value:
        return "-"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    return dt.strftime("%b %d %H:%M")


def _fmt_pct(value, digits: int = 1) -> str:
    if value is None:
        return "-"
    return f"{float(value) * 100:.{digits}f}%"


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["fmt_utc"] = _fmt_utc
    env.filters["fmt_kickoff"] = _fmt_kickoff
    env.filters["fmt_pct"] = _fmt_pct
    return env


def file_sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _load_payload(path: Path) -> dict | None:
    """A prediction JSON, or `None` for anything in the directory that is
    not one (e.g. a future `*.ots.json` stamp stub -- `stamp.py` names those
    `<round>.ots.json`, which still matches the `j1_*.json` glob)."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict) or "matches" not in payload or "round_id" not in payload:
        return None
    return payload


def list_predictions(predictions_dir=None) -> list[tuple[dict, Path]]:
    """All valid round files, newest `generated_at` first."""
    root = Path(predictions_dir) if predictions_dir else PREDICTIONS_DIR
    if not root.exists():
        return []
    found = []
    for path in sorted(root.glob("j1_*.json")):
        payload = _load_payload(path)
        if payload is not None:
            found.append((payload, path))
    found.sort(key=lambda item: item[0].get("generated_at", ""), reverse=True)
    return found


def latest_prediction(predictions_dir=None) -> tuple[dict, Path] | None:
    predictions = list_predictions(predictions_dir)
    return predictions[0] if predictions else None


def round_label(payload: dict) -> str:
    """`"J1 2026-27 -- round of 2026-08-21"` -- matches the header already
    written by `footy.pipeline.predict.render_markdown` for the same file,
    so the same round reads identically on the site and in the repo."""
    return f"J1 {payload['season_label']} -- round of {payload['round_id']}"


def render_index(
    payload: dict, file_hash: str, *, source_filename: str = "", env: Environment | None = None
) -> str:
    env = env or _env()
    template = env.get_template("index.html.j2")
    return template.render(
        site_name=facts.SITE_NAME,
        disclaimer=facts.DISCLAIMER,
        attribution=facts.SOURCE_ATTRIBUTION,
        payload=payload,
        round_label=round_label(payload),
        file_sha256=file_hash,
        source_filename=source_filename,
        run=facts.J1_CONFIRMATION_RUN,
        active_page="index",
    )


def render_methodology(*, env: Environment | None = None) -> str:
    env = env or _env()
    template = env.get_template("methodology.html.j2")
    return template.render(
        site_name=facts.SITE_NAME,
        disclaimer=facts.DISCLAIMER,
        attribution=facts.SOURCE_ATTRIBUTION,
        run=facts.J1_CONFIRMATION_RUN,
        active_page="methodology",
    )


def _live_record_summary(track: pd.DataFrame) -> dict:
    """Counts a same-night provisional result (`source:
    "odds_api_provisional"`) toward the live total same as a confirmed one
    -- the point of the two-stage pipeline is that the site doesn't have to
    wait for football-data to say "not yet played" -- but keeps
    `n_provisional` alongside it so the record page can say so rather than
    quietly blend the two (DESIGN_PHASE2.md 9: warn, don't hide). Tolerates
    a `track` with no `source` column (older callers, and this module's own
    pre-two-stage tests): that means everything in it is confirmed."""
    if track.empty:
        return {"n": 0, "rounds": 0, "mean_rps_raw": None, "mean_rps_cal": None, "n_provisional": 0}
    source = track["source"] if "source" in track.columns else pd.Series("football_data", index=track.index)
    return {
        "n": int(len(track)),
        "rounds": int(track["round_id"].nunique()),
        "mean_rps_raw": float(track["rps_raw"].mean()),
        "mean_rps_cal": float(track["rps_cal"].mean()),
        "n_provisional": int((source == "odds_api_provisional").sum()),
    }


def render_record(
    track: pd.DataFrame, *, first_round_id: str | None = None, env: Environment | None = None
) -> str:
    env = env or _env()
    template = env.get_template("record.html.j2")
    return template.render(
        site_name=facts.SITE_NAME,
        disclaimer=facts.DISCLAIMER,
        attribution=facts.SOURCE_ATTRIBUTION,
        run=facts.J1_CONFIRMATION_RUN,
        live=_live_record_summary(track),
        live_min_n=facts.LIVE_RECORD_MIN_N,
        first_round_id=first_round_id,
        active_page="record",
    )


def build_site(*, predictions_dir=None, output_dir=None) -> dict:
    """Writes `index.html`, `methodology.html`, `record.html` and
    `style.css` into `output_dir` (default `site/`, DESIGN_SITE.md 2.1/2.6).
    Returns `{"ok": False, "reason": ...}` instead of writing anything if
    there is no prediction to render -- an empty or stale site must never
    go out (DESIGN_SITE.md 2.4's publish-gate spirit applied to the site
    step itself)."""
    out_root = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
    latest = latest_prediction(predictions_dir)
    if latest is None:
        return {"ok": False, "reason": "no prediction files found in predictions/"}
    payload, path = latest

    env = _env()
    out_root.mkdir(parents=True, exist_ok=True)

    index_html = render_index(payload, file_sha256(path), source_filename=path.name, env=env)
    methodology_html = render_methodology(env=env)

    predictions = list_predictions(predictions_dir)
    root_dir = path.parent
    track = build_track_record(root_dir)
    first_round_id = predictions[-1][0]["round_id"] if predictions else None
    record_html = render_record(track, first_round_id=first_round_id, env=env)

    written = {}
    for name, content in (
        ("index.html", index_html),
        ("methodology.html", methodology_html),
        ("record.html", record_html),
    ):
        target = out_root / name
        target.write_text(content, encoding="utf-8")
        written[name] = str(target)

    style_target = out_root / "style.css"
    style_target.write_text((STATIC_DIR / "style.css").read_text(encoding="utf-8"), encoding="utf-8")
    written["style.css"] = str(style_target)

    return {
        "ok": True,
        "output_dir": str(out_root),
        "written": written,
        "round_id": payload["round_id"],
        "n_fixtures": len(payload["matches"]),
        "track_record_rows": int(len(track)),
    }
