from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from chan_futures.config_loader import load_config
from vnpy_chan.chan_bsp_strategy import ChanBspStrategy
from vnpy_chan.production_guard import (
    evaluate_production_gate,
    inspect_risk_manager_setting,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STRICT_CONFIG = PROJECT_ROOT / "configs" / "rb_15m_qingpai_strict.yaml"
RELEASE_MANIFEST = PROJECT_ROOT / "configs" / "p7_release_manifest.json"
RISK_PROFILE = PROJECT_ROOT / "configs" / "veighna_risk_manager_setting.p7.json"


def test_shadow_mode_still_requires_core_initialization(tmp_path) -> None:
    result = evaluate_production_gate(
        config=None,
        kl_window=15,
        production_ready=False,
        operator_confirmed=False,
        shadow_mode=True,
        forward_confirmed=False,
        risk_manager_confirmed=False,
        risk_manager_setting_path=tmp_path / "missing.json",
    )

    assert not result.ready
    assert result.mode == "blocked"
    assert "production_ready_false" in result.reasons
    assert "strategy_config_unavailable" in result.reasons


def test_live_gate_blocks_when_veighna_risk_manager_rules_are_disabled(
    tmp_path,
) -> None:
    disabled = tmp_path / "risk_manager_setting.json"
    disabled.write_text(
        json.dumps({"活动委托检查": {"active": False}}, ensure_ascii=False),
        encoding="utf-8",
    )
    config = load_config(STRICT_CONFIG)

    result = evaluate_production_gate(
        config=config,
        kl_window=15,
        production_ready=True,
        operator_confirmed=True,
        shadow_mode=False,
        forward_confirmed=True,
        risk_manager_confirmed=True,
        risk_manager_setting_path=disabled,
    )

    assert not result.ready
    assert "veighna_risk_manager_setting_inactive" in result.reasons


def test_live_gate_accepts_release_risk_manager_setting(tmp_path) -> None:
    enabled = tmp_path / "risk_manager_setting.json"
    enabled.write_text(
        json.dumps({"活动委托检查": {"active": True}, "缠论开仓守卫": {"active": True}}, ensure_ascii=False),
        encoding="utf-8",
    )
    config = load_config(STRICT_CONFIG)

    # P7-R10: live gate needs both custom rule in JSON AND in live_risk_status
    result = evaluate_production_gate(
        config=config,
        kl_window=15,
        production_ready=True,
        operator_confirmed=True,
        shadow_mode=False,
        forward_confirmed=True,
        risk_manager_confirmed=True,
        risk_manager_setting_path=enabled,
        live_risk_status=SimpleNamespace(
            risk_manager_app_loaded=True,
            risk_engine_present=True,
            send_order_patched=True,
            loaded_rules=("活动委托检查", "缠论开仓守卫"),
            active_rules=("活动委托检查", "缠论开仓守卫"),
            error="",
        ),
    )

    assert result.ready
    assert result.mode == "production"


def test_live_gate_blocks_until_forward_simulation_is_confirmed(tmp_path) -> None:
    enabled = tmp_path / "risk_manager_setting.json"
    enabled.write_text(
        json.dumps({"活动委托检查": {"active": True}, "缠论开仓守卫": {"active": True}}, ensure_ascii=False),
        encoding="utf-8",
    )
    config = load_config(STRICT_CONFIG)

    result = evaluate_production_gate(
        config=config,
        kl_window=15,
        production_ready=True,
        operator_confirmed=True,
        shadow_mode=False,
        forward_confirmed=False,
        risk_manager_confirmed=True,
        risk_manager_setting_path=enabled,
        live_risk_status=SimpleNamespace(
            risk_manager_app_loaded=True,
            risk_engine_present=True,
            send_order_patched=True,
            loaded_rules=("活动委托检查", "缠论开仓守卫"),
            active_rules=("活动委托检查", "缠论开仓守卫"),
            error="",
        ),
    )

    assert not result.ready
    assert "forward_confirmed_false" in result.reasons


def test_p7_risk_manager_release_profile_enables_rules() -> None:
    status = inspect_risk_manager_setting(
        RISK_PROFILE
    )

    assert status.active
    # P7-R10: the release profile must include the custom rule active
    enabled_names = list(status.enabled_rules)
    assert "活动委托检查" in enabled_names
    assert "委托规模检查" in enabled_names
    assert "缠论开仓守卫" in enabled_names, (
        f"缠论开仓守卫 must be in enabled_rules; got {enabled_names}"
    )


def test_p7_release_manifest_matches_strategy_defaults() -> None:
    manifest = json.loads(
        RELEASE_MANIFEST.read_text(encoding="utf-8")
    )

    baseline = manifest["baseline_commit"]
    impl_base = manifest["implementation_base_commit"]
    candidate_worktree = manifest["candidate_worktree"]
    assert baseline.startswith("ab31edc")
    # implementation_base_commit is a real full-length commit hash
    assert len(impl_base) == 40 and all(c in "0123456789abcdef" for c in impl_base)
    assert candidate_worktree
    assert "repository_head" not in manifest
    assert manifest["release_status"] == "repair_candidate_uncommitted"
    assert manifest["strategy_config"] == ChanBspStrategy.config_yaml
    assert manifest["default_runtime_mode"] == "shadow"
    assert set(manifest["supported_realtime_windows"]) == {5, 15, 60}
    assert (PROJECT_ROOT / manifest["risk_manager_setting"]).exists()
    artifacts = manifest["artifacts"]
    for name in [
        "strategy_source",
        "strategy_config",
        "risk_manager_profile",
        "rb_trading_days",
        "session_rules",
        "calendar_metadata",
        "risk_manager_custom_rule_source",
    ]:
        if name == "strategy_deployed":
            continue
        artifact = artifacts[name]
        payload = (PROJECT_ROOT / artifact["path"]).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == artifact["sha256"]
    # P7-R10: strategy_source and strategy_deployed differ (deployment pending).
    # The deployed SHA is documented but differs from source; this is expected.
    src_sha = artifacts["strategy_source"]["sha256"]
    dep_sha = artifacts["strategy_deployed"]["sha256"]
    assert src_sha != dep_sha, (
        "strategy_deployed must differ from strategy_source "
        "(deployment is pending, not yet copied to ~/strategies/)"
    )
    # Source sha must match the actual file bytes
    payload = (PROJECT_ROOT / artifacts["strategy_source"]["path"]).read_bytes()
    assert hashlib.sha256(payload).hexdigest() == artifacts["strategy_source"]["sha256"]
    rule_source = artifacts["risk_manager_custom_rule_source"]
    rule_deployed = artifacts["risk_manager_custom_rule_deployed"]
    rule_payload = (PROJECT_ROOT / rule_source["path"]).read_bytes()
    assert hashlib.sha256(rule_payload).hexdigest() == rule_source["sha256"]
    deployed_path = Path(rule_deployed["path"]).expanduser()
    assert hashlib.sha256(deployed_path.read_bytes()).hexdigest() == rule_deployed["sha256"]
    assert rule_source["sha256"] != rule_deployed["sha256"]
    assert "production_ready" not in ChanBspStrategy.parameters
    assert "operator_confirmed" in ChanBspStrategy.parameters
    assert ChanBspStrategy.load_days == 100


def test_strategy_shadow_sender_does_not_call_cta_order_function() -> None:
    strategy = ChanBspStrategy.__new__(ChanBspStrategy)
    strategy.shadow_mode = True
    strategy.production_ready = True
    strategy.order_status = "idle"
    strategy.vt_symbol = "RB2505.SHFE"
    strategy.write_log = lambda message: None
    strategy._refresh_production_gate = lambda: SimpleNamespace(
        ready=True,
        reason_text="shadow_mode_enabled",
    )
    called = False

    def sender():
        nonlocal called
        called = True
        return ["SIM.1"]

    result = strategy._send_live_or_shadow(
        "open_long",
        sender,
        price=3500,
        volume=1,
        closing=False,
        reason="test",
    )

    assert result == []
    assert not called
    assert strategy.order_status == "shadow"


def test_strategy_rejects_missing_release_config(tmp_path) -> None:
    strategy = ChanBspStrategy.__new__(ChanBspStrategy)
    strategy.chan_project_path = str(tmp_path)
    strategy.config_yaml = "configs/missing.yaml"
    strategy.write_log = lambda message: None

    with pytest.raises(FileNotFoundError, match="P7 strategy config not found"):
        strategy._init_chan_pipeline()
