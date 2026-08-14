"""Build roll calendars for all instruments from database prices to CSV.

Iterates every instrument code found in the contract price database and calls
build_and_write_roll_calendar for each one, without interactive prompts.
"""

from sysdata.config.configdata import get_csv_step_directory
from sysdata.config.private_config import get_private_config_as_dict
from sysproduction.data.prices import diagPrices
from sysinit.futures.rollcalendars_from_db_prices_to_csv import build_and_write_roll_calendar


def build_all_roll_calendars():
    price_data = diagPrices().db_futures_contract_price_data
    instrument_list = get_instrument_list(price_data)
    output_datapath = get_roll_calendar_directory()

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
        raise Exception(f"Roll calendar build failed for: {', '.join(failures)}")

    print("\nAll roll calendars built successfully.")

def get_roll_calendar_directory() -> str:
    return get_csv_step_directory("roll_calendars")


def get_instrument_list(price_data) -> list[str]:
    method = getattr(price_data, "get_list_of_instrument_codes_with_merged_price_data", None)
    if callable(method):
        instrument_list = method()
        return _filter_instrument_list_to_config(instrument_list)

    raise AttributeError(
        "Price data object does not expose an instrument listing method"
    )


def _filter_instrument_list_to_config(instrument_list: list[str]) -> list[str]:
    private_config = get_private_config_as_dict()
    include_instrument_lists = private_config.get("include_instrument_lists", {})
    reporting_instruments = include_instrument_lists.get("reporting_instruments", [])

    if not isinstance(reporting_instruments, list) or not reporting_instruments:
        return instrument_list

    reporting_set = {str(instrument).strip() for instrument in reporting_instruments if str(instrument).strip()}
    return [instrument for instrument in instrument_list if instrument in reporting_set]


if __name__ == "__main__":
    build_all_roll_calendars()
