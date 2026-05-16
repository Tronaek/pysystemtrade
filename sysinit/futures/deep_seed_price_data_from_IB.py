"""Deep-seed: fetch multiple years of historical futures prices from IB.

Unlike seed_price_data_from_IB (which fetches a single 1-year window ending
now), this module walks backward in time from the earliest data already stored,
fetching additional chunks until IB returns no more data.

Daily   : up to DEEP_DAILY_CHUNKS × 1-year chunks  (~10 years)
Hourly  : up to DEEP_HOURLY_CHUNKS × 1-month chunks (~1 year)
"""
from __future__ import annotations

import datetime
import pandas as pd

from syscore.exceptions import missingData, missingContract
from sysbrokers.IB.ib_futures_contract_price_data import futuresContract
from syscore.dateutils import DAILY_PRICE_FREQ, HOURLY_FREQ, Frequency
from sysdata.data_blob import dataBlob
from sysproduction.data.broker import dataBroker
from sysproduction.data.prices import updatePrices, diagPrices
from sysproduction.update_historical_prices import write_merged_prices_for_contract
from sysobjects.futures_per_contract_prices import futuresContractPrices

from sysinit.futures.ib_seed_gate import should_skip_instrument, mark_instrument_completed

DEEP_DAILY_CHUNKS = 10
DEEP_HOURLY_CHUNKS = 120
DEEP_DAILY_OVERLAP = datetime.timedelta(days=2)
DEEP_HOURLY_OVERLAP = datetime.timedelta(hours=6)

# IB endDateTime format expected by reqHistoricalData
_IB_DT_FMT = "%Y%m%d %H:%M:%S"


def _end_datetime_str(dt: datetime.datetime) -> str:
    dt_utc = _as_utc(dt)
    return f"{dt_utc.strftime(_IB_DT_FMT)} UTC"


def _as_utc(dt: datetime.datetime) -> datetime.datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.UTC)
    return dt.astimezone(datetime.UTC)


def _earliest_timestamp(prices: futuresContractPrices) -> datetime.datetime | None:
    if len(prices) == 0:
        return None
    return _as_utc(prices.index.min().to_pydatetime())


def _overlap_for_frequency(frequency: Frequency) -> datetime.timedelta:
    return DEEP_HOURLY_OVERLAP if frequency == HOURLY_FREQ else DEEP_DAILY_OVERLAP


def deep_seed_price_data_from_IB(instrument_code: str) -> None:
    data = dataBlob()
    data_broker = dataBroker(data)

    list_of_contracts = data_broker.get_list_of_contract_dates_for_instrument_code(
        instrument_code, allow_expired=True
    )

    if should_skip_instrument(
        instrument_code,
        step="deep-ib",
        data=data,
        list_of_contracts=list_of_contracts,
    ):
        print(f"Skipping {instrument_code}: deep lookback target already covered")
        return

    for contract_date in list_of_contracts:
        date_str = contract_date[:6]
        contract_object = futuresContract(instrument_code, date_str)
        _deep_seed_contract(data=data, data_broker=data_broker, contract_object=contract_object)

    mark_instrument_completed(instrument_code, step="deep-ib")


def _deep_seed_contract(
    *,
    data: dataBlob,
    data_broker: dataBroker,
    contract_object: futuresContract,
) -> None:
    list_of_frequencies = [HOURLY_FREQ, DAILY_PRICE_FREQ]
    for frequency in list_of_frequencies:
        _deep_seed_contract_at_frequency(
            data=data,
            data_broker=data_broker,
            contract_object=contract_object,
            frequency=frequency,
        )

    write_merged_prices_for_contract(
        data, contract_object=contract_object, list_of_frequencies=list_of_frequencies
    )


def _deep_seed_contract_at_frequency(
    *,
    data: dataBlob,
    data_broker: dataBroker,
    contract_object: futuresContract,
    frequency: Frequency,
) -> None:
    update_prices = updatePrices(data)
    diag_prices = diagPrices(data)
    log_attrs = {**contract_object.log_attributes(), "method": "temp"}

    # Read whatever is already stored so we can anchor the backward walk.
    try:
        existing = diag_prices.get_prices_at_frequency_for_contract_object(
            contract_object=contract_object,
            frequency=frequency,
        )
    except Exception:
        existing = futuresContractPrices.create_empty()

    chunks = DEEP_HOURLY_CHUNKS if frequency == HOURLY_FREQ else DEEP_DAILY_CHUNKS
    # Step size matches the IB duration window for each frequency.
    step_days = 30 if frequency == HOURLY_FREQ else 365

    # Start just before the earliest data we already have; fall back to now.
    anchor = _earliest_timestamp(existing)
    if anchor is None:
        anchor = datetime.datetime.now(datetime.UTC)
        end_dt = anchor
    else:
        # On reruns, fetch only one chunk just older than the earliest point.
        # Include a small overlap to self-heal boundary gaps if a prior run
        # was interrupted during download/write.
        end_dt = anchor + _overlap_for_frequency(frequency)
        chunks = 1

    accumulated = existing
    wrote_any_chunk = False

    ib_client = data_broker.broker_futures_contract_price_data.ib_client

    for chunk_idx in range(chunks):
        end_str = _end_datetime_str(end_dt)
        data.log.debug(
            f"Deep-seed {contract_object} @ {frequency} chunk {chunk_idx + 1}/{chunks} ending {end_str}",
            **log_attrs,
        )

        try:
            contract_with_ib = (
                data_broker.data.broker_futures_contract_price
                .futures_contract_data.get_contract_object_with_IB_data(
                    contract_object, allow_expired=True
                )
            )
            chunk_df = ib_client.broker_get_historical_futures_data_for_contract_ending_at(
                contract_with_ib,
                bar_freq=frequency,
                end_datetime=end_str,
                allow_expired=True,
            )
        except (missingData, missingContract):
            data.log.debug(
                f"No more data from IB for {contract_object} @ {frequency}",
                **log_attrs,
            )
            break

        if chunk_df is None or len(chunk_df) == 0:
            break

        chunk_prices = futuresContractPrices(chunk_df)
        new_earliest = _earliest_timestamp(chunk_prices)
        if new_earliest is None or new_earliest >= end_dt:
            # Window did not advance — stop.
            break

        # Merge: chunk is older data, existing is newer; combine and deduplicate.
        merge_frames = [frame for frame in (chunk_prices, accumulated) if len(frame) > 0]
        if len(merge_frames) == 1:
            combined = futuresContractPrices(merge_frames[0].sort_index())
        else:
            combined = futuresContractPrices(pd.concat(merge_frames).sort_index())
        combined = futuresContractPrices(combined[~combined.index.duplicated(keep="last")])

        if len(combined) <= len(accumulated):
            # Overlap-only chunk added nothing new.
            break

        accumulated = combined

        # Incrementally persist progress to the same parquet target.
        update_prices.overwrite_prices_at_frequency_for_contract(
            contract_object=contract_object,
            frequency=frequency,
            new_prices=accumulated,
        )
        wrote_any_chunk = True

        end_dt = new_earliest - datetime.timedelta(seconds=1)

    if wrote_any_chunk:
        update_prices.overwrite_prices_at_frequency_for_contract(
            contract_object=contract_object,
            frequency=frequency,
            new_prices=accumulated,
        )
        data.log.debug(
            f"Wrote {len(accumulated)} rows for {contract_object} @ {frequency}",
            **log_attrs,
        )


if __name__ == "__main__":
    print("Deep-seed historical price data from IB")
    instrument_code = input("Instrument code? <return to abort> ")
    if instrument_code == "":
        exit()
    deep_seed_price_data_from_IB(instrument_code)
