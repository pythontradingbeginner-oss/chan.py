from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from chan_futures.risk import CloseFillRecord, RiskManager


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "configs" / "qingpai_open005_machine_contract_v1.json"
MATRIX_PATH = ROOT / "configs" / "qingpai_open005_test_matrix_v1.json"
RUNTIME_SCHEMA_PATH = ROOT / "configs" / "qingpai_open005_runtime_schema_v1.json"
RUNTIME_SCHEMA_V2_PATH = ROOT / "configs" / "qingpai_open005_runtime_schema_v2.json"
RULE_CONTRACT_PATH = ROOT / "configs" / "qingpai_rule_contract_v1.json"
OPTION_D_EVALUATION_PATH = (
    ROOT / "configs" / "qingpai_open005_option_d_evaluation_v1.json"
)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _collect_clause_ids(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        clause_id = value.get("clause_id")
        if clause_id is not None:
            found.append(str(clause_id))
        transition_id = value.get("id")
        if isinstance(transition_id, str) and transition_id.startswith("OPEN005-D-T"):
            found.append(transition_id)
        for child in value.values():
            found.extend(_collect_clause_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_collect_clause_ids(child))
    return found


def test_open005_contract_is_linked_and_has_unique_clauses() -> None:
    contract = _load(CONTRACT_PATH)
    rule_contract = _load(RULE_CONTRACT_PATH)
    open_decisions = {item["id"] for item in rule_contract["open_decisions"]}
    clause_ids = _collect_clause_ids(contract)

    assert contract["contract_id"] == "qingpai_open005_machine_contract_v1"
    assert contract["status"] == "frozen_for_research_implementation"
    assert contract["parent_decision"] == "OPEN-005"
    assert contract["parent_decision"] in open_decisions
    assert contract["production_policy_status"] == "unresolved"
    assert contract["runtime_schema_path"] == str(
        RUNTIME_SCHEMA_PATH.relative_to(ROOT)
    ).replace("\\", "/")
    assert clause_ids
    assert len(clause_ids) == len(set(clause_ids))


def test_open005_runtime_schema_has_one_streak_source_of_truth() -> None:
    schema = _load(RUNTIME_SCHEMA_PATH)
    required = set(schema["required"])
    option_d = schema["$defs"]["optionDState"]

    assert schema["properties"]["schema_version"]["const"] == (
        "open005-risk-runtime-v1"
    )
    assert "true_consecutive_losses" in required
    assert "true_consecutive_losses" not in option_d["required"]
    assert "true_consecutive_losses" not in option_d["properties"]
    assert "has_authoritative_oms_equity" in required
    assert "finalized_position_results" in required
    assert "option_d" not in required
    assert "reserved_intents" not in required
    intent_snapshot = schema["$defs"]["reservedIntent"]["properties"][
        "intent_snapshot"
    ]
    assert set(intent_snapshot["required"]) == {
        "schema_version",
        "signal",
        "decision",
    }
    assert set(option_d["properties"]["regime_state"]["enum"]) == {
        "ACTIVE",
        "PAUSED_UNTIL_NEXT_SESSION",
        "PROBE_AVAILABLE",
        "PROBE_IN_FLIGHT",
    }


def test_option_d_is_implemented_for_research_with_runtime_schema_v2() -> None:
    contract = _load(CONTRACT_PATH)
    option_d = contract["option_d"]

    assert option_d["runtime_implementation_status"] == (
        "implemented_for_research_evaluation"
    )
    assert option_d["runtime_schema_when_implemented"] == (
        "open005-risk-runtime-v2"
    )
    assert "option_d" not in contract["runtime_state"]["required_fields"]
    assert "reserved_intents" not in contract["runtime_state"]["required_fields"]
    assert "finalized_position_results" in contract["runtime_state"][
        "required_fields"
    ]


def test_option_d_risk_state_conforms_to_runtime_schema_v2() -> None:
    from chan_futures.risk import RiskConfig
    from chan_futures.risk_policy import RiskProfile
    from chan_futures.trade_intent import TradeIntent
    from signal_core.models import SignalDecision
    from chan_futures.strategy import StrategySignal
    from datetime import datetime

    schema = _load(RUNTIME_SCHEMA_V2_PATH)
    risk = RiskManager(
        RiskConfig(
            max_consecutive_losses=1,
            profile=RiskProfile.D_EXPERIMENT,
        ),
        next_session_resolver=lambda value: "2026-08-14",
    )
    risk.advance_session("2026-08-13")
    risk.on_fill(pnl_points=-1, session_key="2026-08-13")
    risk.advance_session("2026-08-14")
    intent = TradeIntent(
        signal=StrategySignal(
            timestamp=datetime(2026, 8, 14, 9, 15),
            action="open_long",
            target_position=1,
            price=3500,
            reason="test",
            bsp_type="1",
            bsp_bi_idx=1,
            bsp_klu_idx=1,
        ),
        decision=SignalDecision(
            decision_id="decision-1",
            event_id="event-1",
            policy_id="test",
            accepted=True,
            reason_codes=(),
            entry_price_hint=3500,
            invalidation_price=3450,
            initial_stop_price=3450,
            position_size_hint=1,
            decided_at=datetime(2026, 8, 14, 9, 15),
        ),
    )
    epoch = risk.get_state()["option_d"]["probe_epoch_id"]
    risk.reserve_probe_intent(
        intent_id="probe-intent-1",
        decision_id="decision-1",
        event_id="event-1",
        target_position=1,
        planned_volume=1,
        intent_snapshot=intent.to_runtime_snapshot(),
    )

    state = risk.get_state()
    jsonschema.validate(state, schema)
    assert state["schema_version"] == "open005-risk-runtime-v2"
    assert state["option_d"]["probe_epoch_id"] == epoch


def test_risk_manager_persisted_state_conforms_to_open005_schema() -> None:
    schema = _load(RUNTIME_SCHEMA_PATH)
    risk = RiskManager()
    risk.record_close_fill(
        CloseFillRecord(
            fill_id="fill-1",
            order_id="order-1",
            position_id="position-1",
            pnl_raw_points=-1.04,
            risk_session_key="2026-08-13",
            observed_at="2026-08-13T10:00:00",
            source="contract_test",
        ),
        remaining_quantity=1,
    )

    state = risk.get_state()
    jsonschema.validate(state, schema)
    assert "option_d" not in state
    assert "reserved_intents" not in state
    risk.record_close_fill(
        CloseFillRecord(
            fill_id="fill-2",
            order_id="order-1",
            position_id="position-1",
            pnl_raw_points=2.04,
            risk_session_key="2026-08-13",
            observed_at="2026-08-13T10:01:00",
            source="contract_test",
        ),
        remaining_quantity=0,
    )
    risk.finalize_position(
        "position-1",
        finalized_at="2026-08-13T10:01:00",
        reason="contract_test",
    )
    jsonschema.validate(risk.get_state(), schema)


def test_open005_profiles_use_declared_modes_and_keep_hard_prerequisites() -> None:
    contract = _load(CONTRACT_PATH)
    modes = set(contract["risk_gate_evaluation"]["gate_mode_enum"])
    profiles = contract["experiment_profiles"]

    for profile in profiles.values():
        assert set(profile["gate_modes"].values()) <= modes

    assert set(profiles["B0_UNINTERRUPTED_STRATEGY_PATH"]["gate_modes"].values()) == {
        "observe"
    }
    assert profiles["B0_UNINTERRUPTED_STRATEGY_PATH"][
        "hard_prerequisites_remain_enforced"
    ]
    assert profiles["B1_CONSECUTIVE_LOSS_ABLATION"]["gate_modes"] == {
        "max_loss_points": "enforce",
        "daily_loss_limit": "enforce",
        "max_consecutive_losses": "disabled",
        "max_drawdown_pct": "enforce",
    }
    assert profiles["D_EXPERIMENT"]["gate_modes"] == profiles[
        "B1_CONSECUTIVE_LOSS_ABLATION"
    ]["gate_modes"]
    assert profiles["D_EXPERIMENT"]["direct_control_profile"] == (
        "B1_CONSECUTIVE_LOSS_ABLATION"
    )
    assert profiles["D_EXPERIMENT"]["consecutive_loss_policy"] == (
        contract["option_d"]["clause_id"]
    )
    assert contract["risk_gate_evaluation"]["reserved_name"] == "shadow_mode"


def test_option_d_uses_regime_states_and_existing_order_lifecycle() -> None:
    contract = _load(CONTRACT_PATH)
    option_d = contract["option_d"]

    assert option_d["status"] == "experimental_frozen_for_evaluation"
    assert not option_d["production_default"]
    assert option_d["regime_states"] == [
        "ACTIVE",
        "PAUSED_UNTIL_NEXT_SESSION",
        "PROBE_AVAILABLE",
        "PROBE_IN_FLIGHT",
    ]
    assert option_d["derived_audit_states"] == ["PROBE_PENDING_ENTRY"]
    assert option_d["order_lifecycle_source_of_truth"] == (
        "existing runtime order state machine"
    )
    assert "reported_fill_order_ids" in CONTRACT_PATH.read_text(encoding="utf-8")
    assert {item["id"] for item in option_d["transitions"]} == {
        f"OPEN005-D-T{index:02d}" for index in range(1, 9)
    }


def test_test_matrix_ids_and_contract_references_are_complete() -> None:
    contract = _load(CONTRACT_PATH)
    matrix = _load(MATRIX_PATH)
    clause_ids = set(_collect_clause_ids(contract))
    tests = matrix["tests"]
    test_ids = [item["id"] for item in tests]

    assert matrix["contract_id"] == contract["contract_id"]
    assert matrix["status"] == "frozen_for_research_implementation"
    assert len(test_ids) == len(set(test_ids))
    assert all(item["contract_refs"] for item in tests)
    assert all(set(item["contract_refs"]) <= clause_ids for item in tests)
    assert all(
        item["current_status"] in matrix["result_status_enum"] for item in tests
    )

    required_phases = {
        "contract",
        "risk_accounting",
        "gate_modes",
        "position_transition",
        "option_d",
        "parity",
        "B0_B1",
    }
    assert {item["phase"] for item in tests} >= required_phases


def test_matrix_freezes_the_known_high_risk_edges() -> None:
    matrix = _load(MATRIX_PATH)
    tests = {item["id"]: item for item in matrix["tests"]}

    assert "post-fill quantity reaches zero" in " ".join(
        tests["OPEN005-RA-002"]["assertions"]
    )
    assert "same four state gates disabled" in tests["OPEN005-PARITY-001"][
        "scenario"
    ] or "zero decision differences" in tests["OPEN005-PARITY-001"][
        "assertions"
    ]
    assert "close leg is never blocked" in " ".join(
        tests["OPEN005-D-004"]["assertions"]
    )
    assert "pre-close approval is never reused" in " ".join(
        tests["OPEN005-D-005"]["assertions"]
    )


def test_option_d_evaluation_keeps_b1_control_and_rejects_production_adoption() -> None:
    evaluation = _load(OPTION_D_EVALUATION_PATH)

    assert evaluation["direct_control"] == "B1_CONSECUTIVE_LOSS_ABLATION"
    assert evaluation["experimental_profile"] == "D_EXPERIMENT"
    assert evaluation["gate_modes_exact"]
    assert evaluation["structural_trace_exact"]
    assert evaluation["identity_replay_exact"]
    assert evaluation["folds_complete_without_window_end_of_data"]
    assert evaluation["continuous"]["delta_net_pnl_points"] == 20.0
    assert evaluation["fold_reset_oos"]["delta_net_pnl_points"] == -1.0
    assert evaluation["production_adoption"] == "not_approved"
