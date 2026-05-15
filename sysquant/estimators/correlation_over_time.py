import pandas as pd
from syscore.interactive.progress_bar import progressBar
from sysquant.estimators.correlation_estimator import correlationEstimator

from sysquant.fitting_dates import generate_fitting_dates
from sysquant.estimators.correlations import CorrelationList


def _normalise_time_index(data: pd.DataFrame) -> pd.DataFrame:
    index = data.index
    if isinstance(index, (pd.DatetimeIndex, pd.TimedeltaIndex, pd.PeriodIndex)):
        return data

    coerced_index = pd.to_datetime(index, errors="coerce")
    if not coerced_index.isna().all():
        with_datetime_index = data.copy()
        with_datetime_index.index = coerced_index
        with_datetime_index = with_datetime_index.loc[
            ~with_datetime_index.index.isna()
        ]
        return with_datetime_index.sort_index()

    # Last-resort fallback: keep row order and place values on a business-day index.
    fallback_end = pd.Timestamp.now(tz="UTC").tz_convert(None).normalize()
    fallback_index = pd.date_range(end=fallback_end, periods=len(index), freq="B")
    with_fallback_index = data.copy()
    with_fallback_index.index = fallback_index

    return with_fallback_index


def correlation_over_time_for_returns(
    returns_for_correlation: pd.DataFrame,
    frequency="W",
    forward_fill_price_index=True,
    **kwargs,
) -> CorrelationList:
    returns_for_correlation = _normalise_time_index(returns_for_correlation)

    index_prices_for_correlation = returns_for_correlation.cumsum()
    if forward_fill_price_index:
        index_prices_for_correlation = index_prices_for_correlation.ffill()

    index_prices_for_correlation = index_prices_for_correlation.resample(
        frequency
    ).last()
    returns_for_correlation = index_prices_for_correlation.diff()

    correlation_list = correlation_over_time(returns_for_correlation, **kwargs)

    return correlation_list


def correlation_over_time(
    data_for_correlation: pd.DataFrame,
    date_method="expanding",
    rollyears=20,
    interval_frequency: str = "12M",
    **kwargs,
) -> CorrelationList:
    column_names = list(data_for_correlation.columns)

    # Generate time periods
    fit_dates = generate_fitting_dates(
        data_for_correlation,
        date_method=date_method,
        rollyears=rollyears,
        interval_frequency=interval_frequency,
    )

    progress = progressBar(len(fit_dates), "Estimating correlations")

    correlation_estimator_for_one_period = correlationEstimator(
        data_for_correlation, **kwargs
    )

    corr_list = []
    # Now for each time period, estimate correlation
    for fit_period in fit_dates:
        progress.iterate()
        corrmat = correlation_estimator_for_one_period.calculate_estimate_for_period(
            fit_period
        )
        corr_list.append(corrmat)

    correlation_list = CorrelationList(
        corr_list=corr_list, column_names=column_names, fit_dates=fit_dates
    )

    return correlation_list
