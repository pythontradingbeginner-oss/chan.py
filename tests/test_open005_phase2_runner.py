from __future__ import annotations

import pandas as pd

from chan_futures.backtest import _in_decision_window
from scripts.run_open005_phase2 import (
    IDENTITY_COLUMNS,
    compare_d_b1_paths,
    compare_d_b1_fold_paths,
    compare_identity_sources,
    compare_fold_paths,
    compare_profile_paths,
    filter_fold_windows,
    _fold_source_complete,
)


def test_zero_trade_identity_comparison_is_exact_but_insufficient(tmp_path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "trades.csv").write_text("", encoding="utf-8")
    (right / "trades.csv").write_text("", encoding="utf-8")

    result = compare_identity_sources(left, right)

    assert result["exact"]
    assert not result["evidence_sufficient"]
    assert result["reason"] == "zero_trade_identity_comparison"


def test_nonempty_identity_comparison_checks_all_frozen_columns(tmp_path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    frame = pd.DataFrame(
        [
            {
                "position_id": "position-1",
                "close_fill_id": "fill-1",
                "close_order_id": "order-1",
            }
        ]
    )
    frame.to_csv(left / "trades.csv", index=False)
    frame.to_csv(right / "trades.csv", index=False)

    result = compare_identity_sources(left, right)

    assert result["exact"]
    assert result["evidence_sufficient"]
    assert result["columns"] == list(IDENTITY_COLUMNS)


def test_fold_summary_excludes_warmup_rows() -> None:
    frames = {
        "decision_trace": pd.DataFrame(
            {
                "timestamp": pd.to_datetime(
                    [
                        "2020-12-31 23:45",
                        "2021-01-01 00:00",
                        "2021-12-31 23:45",
                    ]
                )
            }
        ),
        "execution_decisions": pd.DataFrame(),
        "trades": pd.DataFrame(),
        "fills": pd.DataFrame(),
    }
    fold = {
        "test_start": pd.Timestamp("2021-01-01"),
        "test_end": pd.Timestamp("2022-01-01"),
    }

    filtered = filter_fold_windows(frames, [fold])

    assert filtered["decision_trace"]["timestamp"].tolist() == [
        pd.Timestamp("2021-01-01"),
        pd.Timestamp("2021-12-31 23:45"),
    ]


def test_current_b1_delta_proves_one_gate_variable_and_path_change(tmp_path) -> None:
    left = tmp_path / "current"
    right = tmp_path / "b1"
    left.mkdir()
    right.mkdir()
    for directory, status, trades in (
        (left, "risk_rejected", []),
        (right, "executed_open", [{"pnl_points": 5.0}]),
    ):
        pd.DataFrame([{"timestamp": "2021-01-01", "event_id": "event-1", "accepted": True}]).to_csv(
            directory / "decision_trace.csv", index=False
        )
        pd.DataFrame(
            [
                {
                    "timestamp": "2021-01-01",
                    "event_id": "event-1",
                    "status": status,
                    "rejection_reason": "max_consecutive_losses" if directory == left else "",
                }
            ]
        ).to_csv(directory / "execution_decisions.csv", index=False)
        pd.DataFrame(trades).to_csv(directory / "trades.csv", index=False)
        (directory / "fills.csv").write_text("", encoding="utf-8")

    result = compare_profile_paths(
        left,
        right,
        left_modes={
            "max_loss_points": "enforce",
            "daily_loss_limit": "enforce",
            "max_consecutive_losses": "enforce",
            "max_drawdown_pct": "enforce",
        },
        right_modes={
            "max_loss_points": "enforce",
            "daily_loss_limit": "enforce",
            "max_consecutive_losses": "disabled",
            "max_drawdown_pct": "enforce",
        },
    )

    assert result["experimental_variable_isolated"]
    assert result["gate_mode_differences"] == {
        "max_consecutive_losses": {"left": "enforce", "right": "disabled"}
    }
    assert result["structural_trace"]["exact"]
    assert result["outcome_delta"]["risk_rejections"] == -1
    assert result["first_execution_divergence"]["row"] == 0


def test_option_d_uses_b1_as_its_only_direct_control(tmp_path) -> None:
    b1 = tmp_path / "b1"
    option_d = tmp_path / "d"
    b1.mkdir()
    option_d.mkdir()
    gate_modes = {
        "max_loss_points": "enforce",
        "daily_loss_limit": "enforce",
        "max_consecutive_losses": "disabled",
        "max_drawdown_pct": "enforce",
    }
    decision = pd.DataFrame(
        [{"timestamp": "2021-01-01", "event_id": "event-1", "accepted": True}]
    )
    decision.to_csv(b1 / "decision_trace.csv", index=False)
    decision.to_csv(option_d / "decision_trace.csv", index=False)
    pd.DataFrame(
        [{"timestamp": "2021-01-01", "event_id": "event-1", "status": "executed_open"}]
    ).to_csv(b1 / "execution_decisions.csv", index=False)
    pd.DataFrame(
        [
            {
                "timestamp": "2021-01-01",
                "event_id": "event-1",
                "status": "risk_rejected",
                "rejection_reason": "option_d_paused_until_next_session",
                "option_d_regime_state": "PAUSED_UNTIL_NEXT_SESSION",
            }
        ]
    ).to_csv(option_d / "execution_decisions.csv", index=False)
    pd.DataFrame([{"pnl_points": -5.0, "is_option_d_probe": False}]).to_csv(
        b1 / "trades.csv", index=False
    )
    pd.DataFrame([{"pnl_points": 2.0, "is_option_d_probe": True}]).to_csv(
        option_d / "trades.csv", index=False
    )
    for directory in (b1, option_d):
        pd.DataFrame([{"fill_id": f"fill-{directory.name}"}]).to_csv(
            directory / "fills.csv", index=False
        )

    result = compare_d_b1_paths(
        b1,
        option_d,
        b1_modes=gate_modes,
        d_modes=gate_modes.copy(),
    )

    assert result["direct_control"] == "B1_CONSECUTIVE_LOSS_ABLATION"
    assert result["experimental_profile"] == "D_EXPERIMENT"
    assert result["experimental_variable_isolated"]
    assert result["structural_trace"]["exact"]
    assert result["probe_trade_count"] == 1
    assert result["outcome_delta"]["net_pnl_points"] == 7.0

    for source, destination in ((b1, tmp_path / "b1-fold"), (option_d, tmp_path / "d-fold")):
        destination.mkdir()
        for name in ("decision_trace", "execution_decisions", "trades", "fills"):
            frame = pd.read_csv(source / f"{name}.csv")
            frame.to_csv(destination / f"all_{name}.csv", index=False)

    fold_result = compare_d_b1_fold_paths(
        tmp_path / "b1-fold",
        tmp_path / "d-fold",
        b1_modes=gate_modes,
        d_modes=gate_modes.copy(),
    )

    assert fold_result["comparison_scope"] == "fold_reset_oos"
    assert fold_result["experimental_variable_isolated"]
    assert fold_result["probe_trade_count"] == 1


def test_fold_path_comparison_includes_execution_and_trade_layers() -> None:
    base = {
        "decision_trace": pd.DataFrame(
            [{"timestamp": "2021-01-01", "event_id": "e1", "accepted": True}]
        ),
        "execution_decisions": pd.DataFrame(
            [{"timestamp": "2021-01-01", "event_id": "e1", "status": "risk_rejected"}]
        ),
        "trades": pd.DataFrame(),
        "fills": pd.DataFrame(),
    }
    reset = {name: frame.copy() for name, frame in base.items()}
    reset["execution_decisions"].loc[0, "status"] = "executed_open"

    result = compare_fold_paths(base, reset)

    assert result["decision_trace"]["exact"]
    assert not result["execution_decisions"]["exact"]
    assert not result["exact"]


def test_fold_path_comparison_aligns_rows_by_event_key() -> None:
    continuous = {
        "decision_trace": pd.DataFrame(
            [
                {"timestamp": "2021-01-01", "event_id": "e1", "accepted": True},
                {"timestamp": "2021-01-02", "event_id": "e2", "accepted": True},
            ]
        ),
        "execution_decisions": pd.DataFrame(),
        "trades": pd.DataFrame(),
        "fills": pd.DataFrame(),
    }
    reset = {name: frame.copy() for name, frame in continuous.items()}
    reset["decision_trace"] = reset["decision_trace"].iloc[1:].reset_index(drop=True)

    result = compare_fold_paths(continuous, reset)["decision_trace"]

    assert result["continuous_only"] == 1
    assert result["fold_only"] == 0
    assert result["differing_rows"] == 0


def test_fold_fill_filter_keeps_natural_close_after_test_end() -> None:
    frames = {
        "decision_trace": pd.DataFrame(),
        "execution_decisions": pd.DataFrame(),
        "trades": pd.DataFrame(),
        "fills": pd.DataFrame(
            [
                {
                    "timestamp": "2021-12-31 23:45",
                    "previous_position": 0,
                    "target_position": 1,
                },
                {
                    "timestamp": "2022-01-01 00:15",
                    "previous_position": 1,
                    "target_position": 0,
                },
            ]
        ),
    }
    fold = {
        "test_start": pd.Timestamp("2021-01-01"),
        "test_end": pd.Timestamp("2022-01-01"),
    }

    filtered = filter_fold_windows(frames, [fold])

    assert len(filtered["fills"]) == 2


def test_legacy_fold_marker_replays_only_when_window_trade_was_forced(tmp_path) -> None:
    marker = tmp_path / "_complete.json"
    marker.write_text("{}", encoding="utf-8")
    pd.DataFrame(
        [
            {
                "entry_time": "2021-06-01",
                "exit_reason": "end_of_data",
            }
        ]
    ).to_csv(tmp_path / "trades.csv", index=False)
    fold = {
        "test_start": pd.Timestamp("2021-01-01"),
        "test_end": pd.Timestamp("2022-01-01"),
    }

    assert not _fold_source_complete(tmp_path, fold)


def test_natural_exit_marker_still_replays_when_window_trade_was_forced(tmp_path) -> None:
    marker = tmp_path / "_complete.json"
    marker.write_text(
        '{"warmdown_policy":"natural_exit_v1","warmdown_bars":800}',
        encoding="utf-8",
    )
    pd.DataFrame(
        [
            {
                "entry_time": "2021-06-01",
                "exit_reason": "end_of_data",
            }
        ]
    ).to_csv(tmp_path / "trades.csv", index=False)
    fold = {
        "test_start": pd.Timestamp("2021-01-01"),
        "test_end": pd.Timestamp("2022-01-01"),
    }

    assert not _fold_source_complete(tmp_path, fold)


def test_natural_exit_marker_accepts_fold_without_forced_window_trade(tmp_path) -> None:
    marker = tmp_path / "_complete.json"
    marker.write_text(
        '{"warmdown_policy":"natural_exit_v1","warmdown_bars":800}',
        encoding="utf-8",
    )
    pd.DataFrame(
        [
            {
                "entry_time": "2021-06-01",
                "exit_reason": "structure_stop",
            }
        ]
    ).to_csv(tmp_path / "trades.csv", index=False)
    fold = {
        "test_start": pd.Timestamp("2021-01-01"),
        "test_end": pd.Timestamp("2022-01-01"),
    }

    assert _fold_source_complete(tmp_path, fold)


def test_decision_window_is_left_closed_and_right_open() -> None:
    start = pd.Timestamp("2021-01-01")
    end = pd.Timestamp("2022-01-01")

    assert _in_decision_window(start, start=start, end=end)
    assert _in_decision_window(end - pd.Timedelta(minutes=1), start=start, end=end)
    assert not _in_decision_window(end, start=start, end=end)
