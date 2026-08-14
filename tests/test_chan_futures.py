from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from Common.CEnum import BSP_TYPE, DATA_FIELD, KL_TYPE
from chan_futures import MinimalChanTrendStrategy, row_to_klu, run_chan_trigger_backtest
from chan_futures.backtest import ChanBacktestConfig, _account_equity
from chan_futures.execution import SimulatedExecutionEngine


def test_row_to_klu_maps_futures_minute_bar():
    row = {
        "datetime": pd.Timestamp("2025-01-02 21:01", tz="Asia/Shanghai"),
        "open": 3300.0,
        "high": 3310.0,
        "low": 3290.0,
        "close": 3305.0,
        "volume": 100.0,
        "turnover": 330500.0,
    }

    klu = row_to_klu(row, kl_type=KL_TYPE.K_1M)

    assert klu.kl_type == KL_TYPE.K_1M
    assert klu.time.to_str() == "2025/01/02 21:01"
    assert klu.open == 3300.0
    assert klu.trade_info.metric[DATA_FIELD.FIELD_VOLUME] == 100.0


def test_minimal_strategy_generates_signal_from_latest_bsp():
    strategy = MinimalChanTrendStrategy()
    signal = strategy.on_bar(
        chan=_FakeChan(_FakeBsp(is_buy=True)),
        current_position=0,
        price=3310.0,
        timestamp=pd.Timestamp("2025-01-02 21:02"),
        active_symbol="RB2505",
    )

    assert signal is not None
    assert signal.action == "open_long"
    assert signal.target_position == 1
    assert signal.bsp_type == "1"


def test_execution_engine_reverses_position_and_realizes_points():
    engine = SimulatedExecutionEngine(fee_points=1.0, slippage_points=0.0)
    long_signal = _signal(target_position=1, price=100.0)
    short_signal = _signal(target_position=-1, price=110.0)

    engine.execute(long_signal)
    engine.execute(short_signal)

    assert engine.state.position == -1
    assert engine.state.avg_price == 110.0
    assert engine.state.realized_points == 7.0


def test_account_equity_converts_point_pnl_with_contract_multiplier():
    engine = SimulatedExecutionEngine(fee_points=0.0, slippage_points=0.0)
    engine.state.realized_points = -100.0
    config = SimpleNamespace(
        sizing=SimpleNamespace(capital=100_000.0),
        execution=SimpleNamespace(contract_multiplier=10.0),
    )

    assert _account_equity(config, engine, 3300.0) == 99_000.0


def test_backtest_replays_bars_through_trigger_load():
    frame = _sample_bars(120)

    result = run_chan_trigger_backtest(
        frame,
        config=ChanBacktestConfig(fee_points=0.0, slippage_points=0.0),
    )

    assert len(result.bars) == 120
    assert result.summary.iloc[0]["bar_count"] == 120
    assert "equity_points" in result.bars


def _sample_bars(count: int) -> pd.DataFrame:
    rows = []
    price = 3300.0
    for idx in range(count):
        wave = 20 if (idx // 15) % 2 == 0 else -20
        open_price = price
        close = price + wave / 15
        high = max(open_price, close) + 2
        low = min(open_price, close) - 2
        rows.append(
            {
                "datetime": pd.Timestamp("2025-01-02 09:00") + pd.Timedelta(minutes=idx),
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": 100,
                "open_interest": 1000,
                "active_symbol": "RB2505",
                "flags": 0,
            }
        )
        price = close
    return pd.DataFrame(rows)


def _signal(target_position: int, price: float):
    from chan_futures.strategy import StrategySignal

    return StrategySignal(
        timestamp=pd.Timestamp("2025-01-02 09:00"),
        action="test",
        target_position=target_position,
        price=price,
        reason="test",
        bsp_type="1",
        bsp_bi_idx=1,
        bsp_klu_idx=1,
    )


class _FakeChan:
    def __init__(self, bsp):
        self.bsp = bsp
        self.kl_list = [_FakeKlc(0), _FakeKlc(1), _FakeKlc(2)]

    def get_latest_bsp(self, idx=0, number=1):
        return [self.bsp]

    def __getitem__(self, idx):
        return self.kl_list


class _FakeBsp:
    def __init__(self, is_buy: bool):
        self.is_buy = is_buy
        self.type = [BSP_TYPE.T1]
        self.bi = _FakeBi(3)
        self.klu = _FakeKlu(9, _FakeKlc(1))

    def type2str(self):
        return "1"


class _FakeBi:
    def __init__(self, idx: int):
        self.idx = idx


class _FakeKlu:
    def __init__(self, idx: int, klc):
        self.idx = idx
        self.klc = klc


class _FakeKlc:
    def __init__(self, idx: int):
        self.idx = idx
