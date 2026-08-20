"""Fetch season CSVs from football-data.co.uk into data/raw/.

The whole input to this project is ~30 small CSVs, one per season. They never
change once a season is over, so the cache is write-once: a file that already
exists is never re-fetched. That keeps `footy fetch` cheap to re-run and keeps
the request count off the site's back.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from footy.config import (
    BACKOFF_BASE_SEC,
    DEFAULT_DIVS,
    MAX_RETRIES,
    MIN_INTERVAL_SEC,
    RAW_DIR,
    REQUEST_TIMEOUT_SEC,
    USER_AGENT,
    csv_url,
    raw_path,
    season_label,
)


@dataclass
class FetchReport:
    downloaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"downloaded={len(self.downloaded)} "
            f"cached={len(self.skipped)} missing={len(self.missing)}"
        )


class Fetcher:
    """Serial, rate-limited HTTP client. There is no parallel mode on purpose."""

    def __init__(self, *, min_interval: float = MIN_INTERVAL_SEC, session=None):
        # A caller may raise the interval but never lower the floor.
        self.min_interval = max(float(min_interval), MIN_INTERVAL_SEC)
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self._last_request = 0.0

    def _wait(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if self._last_request and elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

    def get(self, url: str) -> bytes | None:
        """Return the body, or None on a 404. Retries transient failures."""
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            self._wait()
            try:
                response = self.session.get(url, timeout=REQUEST_TIMEOUT_SEC)
            except requests.RequestException as exc:      # pragma: no cover - network
                last_error = exc
                self._last_request = time.monotonic()
                time.sleep(BACKOFF_BASE_SEC * (2**attempt))
                continue
            self._last_request = time.monotonic()
            if response.status_code == 404:
                return None
            if response.status_code == 200:
                return response.content
            last_error = RuntimeError(f"HTTP {response.status_code} for {url}")
            time.sleep(BACKOFF_BASE_SEC * (2**attempt))
        raise RuntimeError(f"failed to fetch {url}: {last_error}")


def fetch_seasons(
    year_from: int,
    year_to: int,
    *,
    divs=DEFAULT_DIVS,
    raw_dir: Path | str | None = None,
    fetcher: Fetcher | None = None,
    progress=None,
) -> FetchReport:
    """Download `divs` for every season start year in [year_from, year_to].

    Seasons already present in the cache are skipped without a request; a 404
    (a season that does not exist yet) is recorded and is not an error.
    """
    root = Path(raw_dir) if raw_dir else RAW_DIR
    root.mkdir(parents=True, exist_ok=True)
    report = FetchReport()
    client = fetcher

    for year in range(int(year_from), int(year_to) + 1):
        for div in divs:
            target = root / raw_path(year, div).name
            tag = f"{div} {season_label(year)}"
            if target.exists() and target.stat().st_size > 0:
                report.skipped.append(tag)
                if progress:
                    progress(tag, "cached")
                continue
            if client is None:
                client = Fetcher()
            body = client.get(csv_url(year, div))
            if body is None:
                report.missing.append(tag)
                if progress:
                    progress(tag, "missing")
                continue
            target.write_bytes(body)
            report.downloaded.append(tag)
            if progress:
                progress(tag, f"{len(body):,} bytes")
    return report
