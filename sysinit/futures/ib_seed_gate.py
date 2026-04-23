"""Lightweight gate shared by seed-ib and deep-ib.

Checks whether an instrument already has seeded parquet files so the
seeding modules can skip it without connecting to IB.
"""
from pathlib import Path


def _parquet_store() -> Path:
    from sysdata.config.production_config import get_production_config

    config = get_production_config()
    return Path(config.get_element("parquet_store"))


def _contract_prices_dir() -> Path:
    return _parquet_store() / "futures_contract_prices"


def _marker_path(instrument: str, step: str) -> Path:
    marker_dir = _parquet_store().parent / "reports" / "ib_seed_progress"
    marker_dir.mkdir(parents=True, exist_ok=True)
    return marker_dir / f"{step}__{instrument}.done"


def _has_seed_parquet_files(instrument: str) -> bool:
    """Return True if both Day@ and Hour@ files exist for this instrument."""
    prices_dir = _contract_prices_dir()
    has_day = any(prices_dir.glob(f"Day@{instrument}#*.parquet"))
    has_hour = any(prices_dir.glob(f"Hour@{instrument}#*.parquet"))
    return has_day and has_hour


def should_skip_instrument(instrument: str, step: str = "seed-ib") -> bool:
    """Return True if this instrument has already been seeded."""
    if _marker_path(instrument, step).exists():
        return True
    return _has_seed_parquet_files(instrument)


def mark_instrument_completed(instrument: str, step: str = "seed-ib") -> None:
    """Write a completion marker so future runs skip this instrument."""
    _marker_path(instrument, step).touch()
