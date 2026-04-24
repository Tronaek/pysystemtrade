"""Build roll calendars for all instruments from database prices to CSV.

Iterates every instrument code found in the contract price database and calls
build_and_write_roll_calendar for each one, without interactive prompts.
"""

from sysdata.config.private_config import get_private_config_as_dict
from sysproduction.data.prices import diagPrices
from sysinit.futures.rollcalendars_from_db_prices_to_csv import build_and_write_roll_calendar


def build_all_roll_calendars():
    price_data = diagPrices().db_futures_contract_price_data
    instrument_list = get_instrument_list(price_data)
    output_datapath = get_csv_directory()

    print(f"Building roll calendars for {len(instrument_list)} instruments")
    print(f"Writing roll calendars to {output_datapath}")

    failures = []
    for instrument_code in instrument_list:
        print(f"\n--- {instrument_code} ---")
        try:
            build_and_write_roll_calendar(
                instrument_code,
                output_datapath=output_datapath,
                input_prices=price_data,
                check_before_writing=False,
            )
        except Exception as exc:
            print(f"Failed for {instrument_code}: {exc}")
            failures.append(instrument_code)

    if failures:
        print(f"\nFailed instruments: {', '.join(failures)}")
    else:
        print("\nAll roll calendars built successfully.")


def get_csv_directory() -> str:
    private_config = get_private_config_as_dict()
    csv_directory = private_config.get("csv_directory")
    if not csv_directory:
        raise ValueError("Missing csv_directory in private config")
    return str(csv_directory)


def get_instrument_list(price_data) -> list[str]:
    method = getattr(price_data, "get_list_of_instrument_codes_with_merged_price_data", None)
    if callable(method):
        return method()

    fallback = getattr(price_data, "get_list_of_instruments", None)
    if callable(fallback):
        return fallback()

    raise AttributeError(
        "Price data object does not expose an instrument listing method"
    )


if __name__ == "__main__":
    build_all_roll_calendars()
