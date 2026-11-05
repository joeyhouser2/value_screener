"""Settings and paths. Thresholds come from ``config/settings.yaml``."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"


@dataclass(frozen=True)
class Settings:
    sec_user_agent: str = ""
    sec_max_rps: float = 8
    sec_workers: int = 8
    cache_ttl_days: float = 7
    exchanges: tuple[str, ...] = ("NASDAQ", "NYSE")
    stale_months: int = 15
    history_years: int = 15
    price_chunk: int = 200
    price_pause: float = 1.0
    trend_retries: int = 2
    trend_retry_pause: float = 30.0
    trend_batch_timeout: float = 120.0
    track_cohort_days: int = 91
    track_baseline_pct: float = 0.10
    backtest_start: str = "2012-01-01"
    past_cap_min_eps: float = 0.50
    past_cap_max_multiple: float = 1000.0
    yf_industry_fallback: bool = True
    min_peers: int = 8
    undervalued_pct: float = 0.20
    overvalued_pct: float = 0.80
    min_metrics: int = 2
    quality_max_de_pct: float = 0.80
    normalized_window: int = 10
    normalized_min_years: int = 5
    pe10_min_years: int = 3
    cyclical_margin_vol: float = 0.10
    min_interest_coverage: float = 2.0
    altman_z_distress: float = 1.81
    altman_z_safe: float = 2.99
    altman_z2_distress: float = 1.10
    altman_z2_safe: float = 2.60
    screen_min_market_cap: float = 50e6
    screen_min_f_tests: int = 6
    max_share_cagr: float = 0.05
    deep_value_pct: float = 0.20
    deep_value_min_f: float = 6
    deep_value_max_p_tbv: float = 3.0
    min_p_ncav: float = 0.1
    trough_min_years: int = 7
    trough_max_margin_position: float = 0.33
    trough_pct: float = 0.30
    trough_min_f: float = 4
    data_dir: Path = field(default=REPO_ROOT / "data")
    config_dir: Path = field(default=CONFIG_DIR)

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"


def load_settings(path: Path | None = None, **overrides) -> Settings:
    """settings.yaml, then the git-ignored settings.local.yaml beside it, then the
    ``SEC_USER_AGENT`` env var — so personal values (your SEC contact) stay out of git."""
    path = path or (CONFIG_DIR / "settings.yaml")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    local = path.with_name("settings.local.yaml")
    if local.exists():
        raw.update(yaml.safe_load(local.read_text(encoding="utf-8")) or {})
    if os.environ.get("SEC_USER_AGENT"):
        raw["sec_user_agent"] = os.environ["SEC_USER_AGENT"]
    known = {f.name for f in fields(Settings)}
    kwargs = {k: v for k, v in raw.items() if k in known}
    if "exchanges" in kwargs:
        kwargs["exchanges"] = tuple(kwargs["exchanges"])
    return replace(Settings(**kwargs), **overrides)
