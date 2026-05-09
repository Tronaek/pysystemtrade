from __future__ import annotations

import json
import os
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

TREND_RULES = [
    "ewmac2_8",
    "ewmac4_16",
    "ewmac8_32",
    "ewmac16_64",
    "ewmac32_128",
    "ewmac64_256",
]


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

REGIME_WEIGHT_OVERRIDE_ENV = "DRAGON_REGIME_FORECAST_WEIGHTS_JSON"


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
        all_weights = _resolve_regime_forecast_weights(system)

        # Regime search relies on explicit per-regime fixed weights. If these
        # estimation flags remain enabled, pysystemtrade will ignore
        # config.forecast_weights and re-estimate weights/DM instead.
        system.config.use_forecast_weight_estimates = False
        system.config.use_forecast_div_mult_estimates = False
        system.config.forecast_weights = dict(all_weights[regime])

        # Regime detection may populate combForecast cache; clear it so the
        # selected regime weights are used for the actual backtest calculations.
        system.cache.delete_items_for_stage("combForecast", delete_protected=True)

        data.log.debug(
            "Regime-aware strategy selected regime=%s (trend_strength=%.4f, vol_percent=%.4f, carry_strength=%.4f)"
            % (regime, trend_strength, vol_percent, carry_strength)
        )

        return system


def _resolve_regime_forecast_weights(system: System) -> dict[str, dict[str, float]]:
    raw_override = os.environ.get(REGIME_WEIGHT_OVERRIDE_ENV)
    if raw_override:
        try:
            parsed = json.loads(raw_override)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Invalid JSON in {REGIME_WEIGHT_OVERRIDE_ENV}: {exc}"
            ) from exc
        return _validated_regime_forecast_weights(parsed)

    config_override = system.config.get_element_or_default(
        "regime_forecast_weights", arg_not_supplied
    )
    if config_override is not arg_not_supplied:
        return _validated_regime_forecast_weights(config_override)

    return _validated_regime_forecast_weights(REGIME_FORECAST_WEIGHTS)


def _validated_regime_forecast_weights(
    candidate: dict,
) -> dict[str, dict[str, float]]:
    required = {REGIME_TREND, REGIME_CARRY, REGIME_RISK_OFF}
    if not isinstance(candidate, dict):
        raise ValueError("regime_forecast_weights must be a dict")

    missing = required.difference(candidate.keys())
    if missing:
        raise ValueError(
            "regime_forecast_weights missing regimes: " + ", ".join(sorted(missing))
        )

    normalized: dict[str, dict[str, float]] = {}
    for regime_name in sorted(required):
        regime_weights = candidate.get(regime_name)
        if not isinstance(regime_weights, dict) or not regime_weights:
            raise ValueError(f"weights for regime {regime_name} must be a non-empty dict")

        clean_weights = {
            str(rule_name): float(weight)
            for rule_name, weight in regime_weights.items()
            if float(weight) > 0.0
        }
        if not clean_weights:
            raise ValueError(f"weights for regime {regime_name} must include positive values")

        total = sum(clean_weights.values())
        if total <= 0.0:
            raise ValueError(f"weights for regime {regime_name} must sum to a positive value")

        normalized[regime_name] = {
            rule_name: weight / total
            for rule_name, weight in clean_weights.items()
        }

    return normalized


def _detect_regime(system: System) -> tuple[str, float, float, float]:
    instrument_list = list(system.get_instrument_list())
    if len(instrument_list) == 0:
        return REGIME_TREND, 0.0, 0.0, 0.0

    trend_samples: list[float] = []
    vol_samples: list[float] = []
    carry_samples: list[float] = []

    for instrument_code in instrument_list:
        trend_value = _trend_strength_from_capped_rules(system, instrument_code)
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


def _trend_strength_from_capped_rules(system: System, instrument_code: str) -> float | None:
    samples: list[float] = []
    for rule_name in TREND_RULES:
        try:
            forecast_series = system.forecastScaleCap.get_capped_forecast(
                instrument_code,
                rule_name,
            )
        except Exception:
            continue

        latest_value = _latest_float_from_series(forecast_series)
        if latest_value is not None:
            samples.append(abs(latest_value))

    if not samples:
        return None

    return float(median(samples))


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
