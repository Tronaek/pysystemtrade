from __future__ import annotations

from statistics import median

from syscore.constants import arg_not_supplied

from systems.basesystem import System

from sysproduction.strategy_code.run_system_classic import (
    production_classic_futures_system,
    runSystemClassic,
    updated_buffered_positions,
)


REGIME_TREND = "trend"
REGIME_CARRY = "carry"
REGIME_RISK_OFF = "risk_off"


# All weights sum to 1.0 and only use rules available in futuresconfig.yaml.
REGIME_FORECAST_WEIGHTS = {
    REGIME_TREND: {
        "ewmac2_8": 0.18,
        "ewmac4_16": 0.16,
        "ewmac8_32": 0.14,
        "ewmac16_64": 0.12,
        "ewmac32_128": 0.08,
        "ewmac64_256": 0.06,
        "carry": 0.10,
        "breakout40": 0.08,
        "relmomentum40": 0.06,
        "relcarry": 0.02,
    },
    REGIME_CARRY: {
        "carry": 0.28,
        "relcarry": 0.22,
        "ewmac8_32": 0.08,
        "ewmac16_64": 0.12,
        "ewmac32_128": 0.10,
        "ewmac64_256": 0.08,
        "breakout40": 0.06,
        "relmomentum40": 0.06,
    },
    REGIME_RISK_OFF: {
        "ewmac8_32": 0.06,
        "ewmac16_64": 0.16,
        "ewmac32_128": 0.14,
        "ewmac64_256": 0.12,
        "carry": 0.18,
        "relcarry": 0.14,
        "breakout40": 0.08,
        "relmomentum40": 0.12,
    },
}


class runSystemRegimeAware(runSystemClassic):
    # Keep classic buffered writeback semantics.
    @property
    def function_to_call_on_update(self):
        return updated_buffered_positions

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

        regime, trend_strength, vol_percent, carry_strength = _detect_regime(system)
        system.config.forecast_weights = dict(REGIME_FORECAST_WEIGHTS[regime])

        data.log.debug(
            "Regime-aware strategy selected regime=%s (trend_strength=%.4f, vol_percent=%.4f, carry_strength=%.4f)"
            % (regime, trend_strength, vol_percent, carry_strength)
        )

        return system


def _detect_regime(system: System) -> tuple[str, float, float, float]:
    instrument_list = list(system.get_instrument_list())
    if len(instrument_list) == 0:
        return REGIME_TREND, 0.0, 0.0, 0.0

    trend_samples: list[float] = []
    vol_samples: list[float] = []
    carry_samples: list[float] = []

    for instrument_code in instrument_list:
        trend_value = _latest_float_from_series(
            system.combForecast.get_combined_forecast(instrument_code)
        )
        if trend_value is not None:
            trend_samples.append(abs(trend_value))

        vol_value = _latest_float_from_series(
            system.rawdata.get_daily_percentage_volatility(instrument_code)
        )
        if vol_value is not None:
            vol_samples.append(abs(vol_value))

        carry_value = _latest_float_from_series(system.rawdata.raw_carry(instrument_code))
        if carry_value is not None:
            carry_samples.append(abs(carry_value))

    trend_strength = median(trend_samples) if trend_samples else 0.0
    vol_percent = median(vol_samples) if vol_samples else 0.0
    carry_strength = median(carry_samples) if carry_samples else 0.0

    # Simple, deterministic happy-path thresholds.
    if vol_percent >= 2.0:
        return REGIME_RISK_OFF, trend_strength, vol_percent, carry_strength
    if trend_strength >= 8.0:
        return REGIME_TREND, trend_strength, vol_percent, carry_strength
    if carry_strength >= 0.20:
        return REGIME_CARRY, trend_strength, vol_percent, carry_strength

    return REGIME_TREND, trend_strength, vol_percent, carry_strength


def _latest_float_from_series(value) -> float | None:
    try:
        clean = value.dropna()
    except Exception:
        return None

    if len(clean) == 0:
        return None

    last_value = clean.iloc[-1]
    if hasattr(last_value, "iloc"):
        if len(last_value) == 0:
            return None
        last_value = last_value.iloc[0]

    try:
        return float(last_value)
    except Exception:
        return None
