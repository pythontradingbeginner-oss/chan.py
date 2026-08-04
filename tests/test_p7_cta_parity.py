from __future__ import annotations

import pandas as pd

from chan_futures.backtest import _calculate_atr, _new_chan, _resolve_kl_type
from chan_futures.config_loader import load_config, make_runtime_decision_kernel
from chan_futures.feed import row_to_klu
from chan_futures.parity import compare_runtime_traces
from data_foundation import RBTradingCalendar, aggregate_continuous_1m_to_Nm
from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.object import BarData
from vnpy_chan.chan_bsp_strategy import ChanBspStrategy


class _Engine:
    main_engine = None

    def write_log(self, message, strategy=None):
        pass

    def put_strategy_event(self, strategy):
        pass


def test_same_1m_bars_produce_identical_p6_and_cta_decision_traces() -> None:
    config = load_config("configs/rb_15m_qingpai_strict.yaml")
    calendar = RBTradingCalendar.load_default()
    source = pd.read_parquet("data/processed/RB_1m_continuous_raw.parquet")
    source = source[source["active_symbol"] == "RB2410"].copy()
    mapped = calendar.map_datetimes(source["datetime"])
    source["trading_day"] = pd.to_datetime(mapped["trading_day"]).to_numpy()
    day_counts = source.groupby("trading_day").size()
    complete_days = [
        day
        for day, count in day_counts.items()
        if count == len(calendar.expected_minutes(day))
    ]
    selected_days = pd.Series(complete_days[-25:])
    source = source[source["trading_day"].isin(selected_days)].reset_index(drop=True)
    assert len(source) >= 8_000

    strategy = ChanBspStrategy(
        _Engine(),
        "p7_cta_parity",
        "RB2410.SHFE",
        {
            "load_days": 100,
            "operator_confirmed": False,
            "shadow_mode": True,
        },
    )
    strategy._reset_initialization_state()
    strategy._init_chan_pipeline()
    strategy._replay_warmup([_to_bar(row) for row in source.itertuples(index=False)])

    main = aggregate_continuous_1m_to_Nm(source, 15, calendar=calendar)
    child = aggregate_continuous_1m_to_Nm(source, 5, calendar=calendar)
    parent = aggregate_continuous_1m_to_Nm(source, 60, calendar=calendar)
    kl_type = _resolve_kl_type(config.kl_type)
    chan = _new_chan(config, kl_type)
    kernel = make_runtime_decision_kernel(config, symbol="RB", timeframe="15m")
    atr_values = _calculate_atr(main, config.sizing.atr_period)
    child_cursor = 0
    parent_cursor = 0

    for row_number, row in main.iterrows():
        timestamp = pd.Timestamp(row["datetime"])
        while child_cursor < len(child) and child.iloc[child_cursor]["datetime"] <= timestamp:
            child_row = child.iloc[child_cursor]
            kernel.observe_child_bar(
                row_to_klu(child_row, kl_type=kernel.multi_level.child_kl_type),
                available_at=child_row["datetime"],
            )
            child_cursor += 1
        while parent_cursor < len(parent) and parent.iloc[parent_cursor]["datetime"] <= timestamp:
            parent_row = parent.iloc[parent_cursor]
            kernel.observe_parent_bar(
                row_to_klu(parent_row, kl_type=kernel.multi_level.parent_kl_type),
                available_at=parent_row["datetime"],
            )
            parent_cursor += 1

        klu = row_to_klu(row, kl_type=kl_type)
        chan.trigger_load({kl_type: [klu]})
        atr = atr_values.iloc[row_number]
        kernel.evaluate_bar(
            chan=chan,
            current_position=0,
            price=float(row["close"]),
            timestamp=timestamp,
            active_symbol=str(row["active_symbol"]),
            account_equity=config.sizing.capital,
            available_funds=config.sizing.capital,
            atr=None if pd.isna(atr) else float(atr),
        )

    report = compare_runtime_traces(
        {"p6_replay": kernel.decision_trace, "cta": strategy.decision_trace}
    )
    assert report.trace_lengths["p6_replay"] > 0
    report.assert_consistent()


def _to_bar(row) -> BarData:
    return BarData(
        symbol=str(row.active_symbol),
        exchange=Exchange.SHFE,
        datetime=pd.Timestamp(row.datetime).to_pydatetime(),
        interval=Interval.MINUTE,
        volume=float(row.volume),
        turnover=float(getattr(row, "turnover", 0) or 0),
        open_interest=float(row.open_interest),
        open_price=float(row.open),
        high_price=float(row.high),
        low_price=float(row.low),
        close_price=float(row.close),
        gateway_name="p7-parity",
    )
