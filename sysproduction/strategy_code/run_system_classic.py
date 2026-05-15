"""
this:

- gets capital from the database (earmarked with a strategy name)
- runs a backtest using that capital level, and mongodb data
- gets the final positions and position buffers
- writes these into a table (earmarked with a strategy name)


"""
import datetime
from syscore.constants import arg_not_supplied
from syscore.exceptions import missingData

from sysdata.config.configdata import Config
from sysdata.data_blob import dataBlob

from sysobjects.production.optimal_positions import (
    bufferedOptimalPositions,
)
from sysobjects.production.tradeable_object import instrumentStrategy

from sysproduction.data.currency_data import dataCurrency
from sysproduction.data.capital import dataCapital
from sysproduction.data.contracts import dataContracts
from sysproduction.data.optimal_positions import dataOptimalPositions
from sysproduction.data.sim_data import get_sim_data_object_for_production

from sysproduction.data.backtest import store_backtest_state

from syslogging.logger import *

from systems.provided.futures_chapter15.basesystem import futures_system
from systems.basesystem import System


class runSystemClassic(object):
    def __init__(
        self,
        data: dataBlob,
        strategy_name: str,
        backtest_config_filename=arg_not_supplied,
    ):
        if backtest_config_filename is arg_not_supplied:
            raise Exception("Need to supply config filename")

        self.data = data
        self.strategy_name = strategy_name
        self.backtest_config_filename = backtest_config_filename

    ## DO NOT CHANGE THE NAME OF THIS FUNCTION
    def run_backtest(self):
        strategy_name = self.strategy_name
        data = self.data

        base_currency, notional_trading_capital = self._get_currency_and_capital()

        system = self.system_method(
            notional_trading_capital=notional_trading_capital,
            base_currency=base_currency,
        )

        function_to_call_on_update = self.function_to_call_on_update
        function_to_call_on_update(
            data=data, strategy_name=strategy_name, system=system
        )

        store_backtest_state(data, system, strategy_name=strategy_name)

    ## MODIFY THIS WHEN INHERITING FOR A DIFFERENT STRATEGY
    ## ARGUMENTS MUST BE: data: dataBlob, strategy_name: str, system: System
    @property
    def function_to_call_on_update(self):
        return updated_buffered_positions

    def _get_currency_and_capital(self):
        data = self.data
        strategy_name = self.strategy_name

        capital_data = dataCapital(data)
        try:
            notional_trading_capital = capital_data.get_current_capital_for_strategy(
                strategy_name
            )
        except missingData:
            # critical log will send email
            error_msg = (
                "Capital data is missing for %s: can't run backtest" % strategy_name
            )
            data.log.critical(error_msg)
            raise Exception(error_msg)

        currency_data = dataCurrency(data)
        base_currency = currency_data.get_base_currency()

        self.data.log.debug(
            "Using capital of %s %.2f" % (base_currency, notional_trading_capital)
        )

        return base_currency, notional_trading_capital

    # DO NOT CHANGE THE NAME OF THIS FUNCTION; IT IS HARDCODED INTO CONFIGURATION FILES
    # BECAUSE IT IS ALSO USED TO LOAD BACKTESTS
    def system_method(
        self,
        notional_trading_capital: float = arg_not_supplied,
        base_currency: str = arg_not_supplied,
    ) -> System:
        data = self.data
        backtest_config_filename = self.backtest_config_filename

        system = production_classic_futures_system(
            data,
            backtest_config_filename,
            log=data.log,
            notional_trading_capital=notional_trading_capital,
            base_currency=base_currency,
        )

        return system


def production_classic_futures_system(
    data: dataBlob,
    config_filename: str,
    log=get_logger("futures_system"),
    notional_trading_capital: float = arg_not_supplied,
    base_currency: str = arg_not_supplied,
) -> System:
    sim_data = get_sim_data_object_for_production(data)
    config = Config(config_filename)

    # Overwrite capital and base currency
    if notional_trading_capital is not arg_not_supplied:
        config.notional_trading_capital = notional_trading_capital

    if base_currency is not arg_not_supplied:
        config.base_currency = base_currency

    # Dynamically set start_date if not already configured: use the minimum across all instruments
    _apply_minimum_instrument_start_date_to_config(config, sim_data, log)

    system = futures_system(data=sim_data, config=config)
    system._log = log

    return system


def _apply_minimum_instrument_start_date_to_config(
    config: Config, sim_data, log
) -> None:
    """
    Compute the earliest available start date across all instruments in config,
    and set config.start_date if not already explicitly configured.
    
    This ensures analysis only runs over periods where all instruments have data,
    avoiding early-window biases from staggered instrument start dates.
    """
    # If start_date is already explicitly set in config, respect it
    if hasattr(config, "start_date"):
        existing_start_date = getattr(config, "start_date", None)
        if existing_start_date is not None:
            log.debug(
                "Keeping explicitly configured start_date: %s" % existing_start_date
            )
            return

    try:
        instrument_list = config.instrument_weights.keys()
    except (AttributeError, KeyError):
        # No instruments configured, skip dynamic start_date
        log.debug(
            "No instrument_weights in config; skipping dynamic start_date computation"
        )
        return

    if not instrument_list:
        return

    min_start_date = None
    for instrument_code in instrument_list:
        try:
            prices = sim_data.get_raw_price(instrument_code)
            if len(prices) > 0:
                instrument_start = prices.index[0]
                if min_start_date is None or instrument_start > min_start_date:
                    min_start_date = instrument_start
                log.debug(
                    "Instrument %s available from %s"
                    % (instrument_code, instrument_start)
                )
            else:
                log.warning(
                    "Instrument %s has no price data available" % instrument_code
                )
        except Exception as e:
            log.warning(
                "Could not determine start date for instrument %s: %s"
                % (instrument_code, str(e))
            )

    if min_start_date is not None:
        # Convert to midnight datetime if it's a date object
        if not isinstance(min_start_date, datetime.datetime):
            min_start_date = datetime.datetime.combine(min_start_date, datetime.time())
        
        config.start_date = min_start_date
        log.info(
            "Dynamically set analysis start_date to %s (latest earliest instrument start)"
            % min_start_date
        )
    else:
        log.warning(
            "Could not determine minimum instrument start date; start_date not set"
        )


def updated_buffered_positions(data: dataBlob, strategy_name: str, system: System):
    log = data.log

    data_optimal_positions = dataOptimalPositions(data)

    list_of_instruments = system.get_instrument_list()
    for instrument_code in list_of_instruments:
        lower_buffer, upper_buffer = get_position_buffers_from_system(
            system, instrument_code
        )
        position_entry = construct_position_entry(
            data=data,
            system=system,
            instrument_code=instrument_code,
            lower_position=lower_buffer,
            upper_position=upper_buffer,
        )
        instrument_strategy = instrumentStrategy(
            instrument_code=instrument_code, strategy_name=strategy_name
        )
        data_optimal_positions.update_optimal_position_for_instrument_strategy(
            instrument_strategy=instrument_strategy, position_entry=position_entry
        )
        log.debug(
            "New buffered positions %.3f %.3f"
            % (position_entry.lower_position, position_entry.upper_position),
            instrument_code=instrument_code,
        )


def get_position_buffers_from_system(system: System, instrument_code: str):
    buffers = system.portfolio.get_buffers_for_position(
        instrument_code
    )  # get the upper and lower edges of the buffer
    lower_buffer = buffers.iloc[-1].bot_pos
    upper_buffer = buffers.iloc[-1].top_pos

    return lower_buffer, upper_buffer


def construct_position_entry(
    data: dataBlob,
    system: System,
    instrument_code: str,
    lower_position: float,
    upper_position: float,
) -> bufferedOptimalPositions:
    diag_contracts = dataContracts(data)
    reference_price = system.rawdata.get_daily_prices(instrument_code).iloc[-1]
    reference_contract = diag_contracts.get_priced_contract_id(instrument_code)
    position_entry = bufferedOptimalPositions(
        date=datetime.datetime.now(),
        lower_position=lower_position,
        upper_position=upper_position,
        reference_price=reference_price,
        reference_contract=reference_contract,
    )

    return position_entry
