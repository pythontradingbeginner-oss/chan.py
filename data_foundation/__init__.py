from .data import (
    CleaningLog,
    aggregate_continuous_1m_to_5m,
    aggregate_continuous_1m_to_Nm,
    build_continuous_contract,
    build_continuous_products,
    CalendarCoverageError,
    RBTradingCalendar,
    clean_rb_1m_bars,
    generate_quality_report,
    load_cleaned_1m,
    load_continuous_1m,
    load_raw_from_vnpy,
    load_switch_log,
)
from .features import add_dc_macd_comparison, add_dc_structure, add_trend_macd
from .pivots import (
    DCEvent,
    DCSnapshot,
    DirectionalChangeDetector,
    add_dc_pivots,
    detect_dc_pivots,
)


def load_replay_bars_jsonl(*args, **kwargs):
    from .charts import load_replay_bars_jsonl as implementation

    return implementation(*args, **kwargs)


def plot_dc_peak_valley_chart(*args, **kwargs):
    from .charts import plot_dc_peak_valley_chart as implementation

    return implementation(*args, **kwargs)


def plot_strategy_trade_year_charts(*args, **kwargs):
    from .charts import plot_strategy_trade_year_charts as implementation

    return implementation(*args, **kwargs)

__all__ = [
    "CleaningLog",
    "DCEvent",
    "DCSnapshot",
    "DirectionalChangeDetector",
    "add_dc_macd_comparison",
    "add_dc_pivots",
    "add_dc_structure",
    "add_trend_macd",
    "aggregate_continuous_1m_to_5m",
    "aggregate_continuous_1m_to_Nm",
    "build_continuous_contract",
    "build_continuous_products",
    "CalendarCoverageError",
    "RBTradingCalendar",
    "clean_rb_1m_bars",
    "generate_quality_report",
    "load_cleaned_1m",
    "load_continuous_1m",
    "load_replay_bars_jsonl",
    "load_raw_from_vnpy",
    "load_switch_log",
    "plot_dc_peak_valley_chart",
    "plot_strategy_trade_year_charts",
    "detect_dc_pivots",
]
