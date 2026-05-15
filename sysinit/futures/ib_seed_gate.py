"""Lightweight gate shared by seed-ib and deep-ib.

Skip logic is based on whether stored prices already meet a lookback target,
not just whether files exist.
"""
from __future__ import annotations

import datetime
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


def _as_utc(dt: datetime.datetime) -> datetime.datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.UTC)
    return dt.astimezone(datetime.UTC)


def _lookback_targets_by_step(step: str) -> dict[str, datetime.datetime]:
    now = datetime.datetime.now(datetime.UTC)

    if step == "deep-ib":
        # Deep seed walks up to 10y of daily and 12m of hourly history.
        return {
            "hourly": now - datetime.timedelta(days=30 * 12),
            "daily": now - datetime.timedelta(days=365 * 10),
        }

    # Seed pulls one month of hourly and one year of daily history.
    return {
        "hourly": now - datetime.timedelta(days=30),
        "daily": now - datetime.timedelta(days=365),
    }


def _get_earliest_by_frequency(
    *,
    data,
    instrument: str,
    list_of_contracts: list[str],
) -> dict[str, datetime.datetime | None]:
    from sysbrokers.IB.ib_futures_contract_price_data import futuresContract
    from syscore.dateutils import DAILY_PRICE_FREQ, HOURLY_FREQ
    from sysproduction.data.prices import updatePrices

    earliest = {"hourly": None, "daily": None}
    update_prices = updatePrices(data)

    for contract_date in list_of_contracts:
        contract = futuresContract(instrument, contract_date[:6])
        for name, frequency in (("hourly", HOURLY_FREQ), ("daily", DAILY_PRICE_FREQ)):
            try:
                prices = update_prices.get_prices_for_contract_object_at_frequency(
                    contract_object=contract, frequency=frequency
                )
            except Exception:
                continue

            if len(prices) == 0:
                continue

            freq_earliest = _as_utc(prices.index.min().to_pydatetime())
            if earliest[name] is None or freq_earliest < earliest[name]:
                earliest[name] = freq_earliest

    return earliest


def _meets_lookback_targets(
    *,
    data,
    instrument: str,
    list_of_contracts: list[str],
    step: str,
) -> bool:
    earliest = _get_earliest_by_frequency(
        data=data,
        instrument=instrument,
        list_of_contracts=list_of_contracts,
    )
    targets = _lookback_targets_by_step(step)

    # Both frequencies need to satisfy the target for the step.
    return (
        earliest["hourly"] is not None
        and earliest["daily"] is not None
        and earliest["hourly"] <= targets["hourly"]
        and earliest["daily"] <= targets["daily"]
    )


def should_skip_instrument(
    instrument: str,
    step: str = "seed-ib",
    *,
    data=None,
    list_of_contracts: list[str] | None = None,
) -> bool:
    """Return True if this instrument already meets the step lookback target."""
    if data is None or list_of_contracts is None:
        # Conservative fallback if caller didn't provide context.
        if _marker_path(instrument, step).exists():
            return True
        return _has_seed_parquet_files(instrument)

    return _meets_lookback_targets(
        data=data,
        instrument=instrument,
        list_of_contracts=list_of_contracts,
        step=step,
    )


def mark_instrument_completed(instrument: str, step: str = "seed-ib") -> None:
    """Write a completion marker so future runs skip this instrument."""
    _marker_path(instrument, step).touch()
