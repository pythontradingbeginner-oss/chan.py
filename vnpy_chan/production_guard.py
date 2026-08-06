from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


P7_BASELINE_COMMIT = "ab31edcdc2fca71c472b3ae7485266adee4de194"
P7_RELEASE_MANIFEST = "configs/p7_release_manifest.json"
DEFAULT_RISK_MANAGER_SETTING_PATH = (
    Path.home() / ".vntrader" / "risk_manager_setting.json"
)
SUPPORTED_PRODUCTION_WINDOWS = {5, 15, 60}


@dataclass(frozen=True, slots=True)
class RiskManagerSettingStatus:
    path: Path
    exists: bool
    enabled_rules: tuple[str, ...] = ()
    disabled_rules: tuple[str, ...] = ()
    error: str = ""

    @property
    def active(self) -> bool:
        return self.exists and bool(self.enabled_rules) and not self.error


@dataclass(frozen=True, slots=True)
class ProductionGateResult:
    ready: bool
    mode: str
    reasons: tuple[str, ...]
    risk_manager_setting: RiskManagerSettingStatus

    @property
    def reason_text(self) -> str:
        return ";".join(self.reasons)


@dataclass(frozen=True, slots=True)
class RiskEngineLiveStatus:
    """Snapshot of the ACTUAL VeighNa RiskEngine state (not just JSON)."""

    risk_manager_app_loaded: bool
    risk_engine_present: bool
    send_order_patched: bool
    loaded_rules: tuple[str, ...]
    active_rules: tuple[str, ...]
    error: str = ""

    @property
    def active(self) -> bool:
        return (
            self.risk_manager_app_loaded
            and self.risk_engine_present
            and self.send_order_patched
            and bool(self.active_rules)
            and not self.error
        )


def inspect_live_risk_engine(main_engine: Any) -> RiskEngineLiveStatus:
    """Query the actual RiskEngine on a MainEngine.

    P7-R10: uses bound-method detection instead of relying on non-existent
    MainEngine._send_order_original.  Checks that main_engine.send_order is
    a bound method whose __self__ is the RiskEngine instance and whose
    __func__ is RiskEngine.send_order.
    """
    if main_engine is None:
        return RiskEngineLiveStatus(
            risk_manager_app_loaded=False,
            risk_engine_present=False,
            send_order_patched=False,
            loaded_rules=(),
            active_rules=(),
            error="main_engine_unavailable",
        )
    try:
        risk_engine = main_engine.get_engine("riskmanager")
    except Exception as exc:
        return RiskEngineLiveStatus(
            risk_manager_app_loaded=False,
            risk_engine_present=False,
            send_order_patched=False,
            loaded_rules=(),
            active_rules=(),
            error=f"risk_engine_query:{type(exc).__name__}",
        )
    if risk_engine is None:
        return RiskEngineLiveStatus(
            risk_manager_app_loaded=False,
            risk_engine_present=False,
            send_order_patched=False,
            loaded_rules=(),
            active_rules=(),
            error="risk_engine_missing",
        )

    # P7-R11: check that send_order is a bound method whose __self__
    # IS the queried risk_engine (identity check), __func__ IS
    # RiskEngine.send_order, and _send_order is present + callable.
    # This guards against other wrappers that might replace send_order
    # with a different bound method.
    patched = False
    try:
        so = getattr(main_engine, "send_order", None)
        if so is not None and hasattr(so, "__self__"):
            so_self = so.__self__
            so_func = so.__func__
            from vnpy_riskmanager.engine import RiskEngine
            if so_self is risk_engine and isinstance(so_self, RiskEngine):
                if so_func is RiskEngine.send_order or so_func is risk_engine.send_order:
                    original = getattr(so_self, "_send_order", None)
                    if original is not None and callable(original):
                        patched = True
    except Exception:
        patched = False

    try:
        names = tuple(risk_engine.get_all_rule_names())
        active = tuple(
            name for name in names if bool(getattr(risk_engine.rules[name], "active", False))
        )
    except Exception as exc:
        return RiskEngineLiveStatus(
            risk_manager_app_loaded=True,
            risk_engine_present=True,
            send_order_patched=patched,
            loaded_rules=(),
            active_rules=(),
            error=f"risk_engine_state:{type(exc).__name__}",
        )

    return RiskEngineLiveStatus(
        risk_manager_app_loaded=True,
        risk_engine_present=True,
        send_order_patched=patched,
        loaded_rules=tuple(sorted(names)),
        active_rules=tuple(sorted(active)),
    )


def evaluate_production_gate(
    *,
    config: Any,
    kl_window: int,
    production_ready: bool,
    operator_confirmed: bool,
    shadow_mode: bool,
    forward_confirmed: bool,
    risk_manager_confirmed: bool,
    risk_manager_setting_path: str | Path | None,
    pending_order_count: int = 0,
    live_risk_status: RiskEngineLiveStatus | None = None,
) -> ProductionGateResult:
    risk_setting = inspect_risk_manager_setting(
        risk_manager_setting_path or DEFAULT_RISK_MANAGER_SETTING_PATH
    )
    reasons: list[str] = []
    if not production_ready:
        reasons.append("production_ready_false")
    if int(kl_window) not in SUPPORTED_PRODUCTION_WINDOWS:
        reasons.append(f"unsupported_kl_window:{kl_window}")
    if config is None:
        reasons.append("strategy_config_unavailable")
    else:
        production = getattr(config, "production", None)
        if production is None or not bool(getattr(production, "enabled", False)):
            reasons.append("config.production.enabled_false")
        risk = getattr(config, "risk", None)
        if risk is None or not _has_hard_risk_limit(risk):
            reasons.append("strategy_hard_risk_limits_missing")
        exits = getattr(config, "exits", ())
        if not exits:
            reasons.append("exit_rules_missing")

    if shadow_mode:
        return ProductionGateResult(
            ready=not reasons,
            mode="shadow" if not reasons else "blocked",
            reasons=tuple(reasons or ["shadow_mode_enabled"]),
            risk_manager_setting=risk_setting,
        )

    if not operator_confirmed:
        reasons.append("operator_confirmed_false")
    if not forward_confirmed:
        reasons.append("forward_confirmed_false")
    if not risk_manager_confirmed:
        reasons.append("risk_manager_confirmed_false")
    if pending_order_count > 0:
        reasons.append(f"pending_order_recovery_required:{pending_order_count}")
    if not risk_setting.active:
        reasons.append("veighna_risk_manager_setting_inactive")

    # P7-R10: JSON must include "缠论开仓守卫" active
    if "缠论开仓守卫" not in risk_setting.enabled_rules:
        reasons.append("risk_manager_setting_no_chan_open_guard")

    # P7-R10: production mode REQUIRES live_risk_status
    if live_risk_status is None:
        reasons.append("live_risk_status_unavailable")
    else:
        if not live_risk_status.risk_manager_app_loaded:
            reasons.append("risk_manager_app_not_loaded")
        if not live_risk_status.risk_engine_present:
            reasons.append("risk_engine_missing")
        if not live_risk_status.send_order_patched:
            reasons.append("risk_engine_not_patching_send_order")
        if "缠论开仓守卫" not in live_risk_status.loaded_rules:
            reasons.append("risk_engine_chan_open_guard_rule_not_loaded")
        if not live_risk_status.active_rules:
            reasons.append("risk_engine_no_active_rules")
        elif "缠论开仓守卫" not in live_risk_status.active_rules:
            reasons.append("risk_engine_chan_open_guard_rule_not_active")
        if live_risk_status.error:
            reasons.append(f"risk_engine_error:{live_risk_status.error}")

    return ProductionGateResult(
        ready=not reasons,
        mode="production" if not reasons else "blocked",
        reasons=tuple(reasons),
        risk_manager_setting=risk_setting,
    )


def inspect_risk_manager_setting(
    path: str | Path,
) -> RiskManagerSettingStatus:
    setting_path = Path(path)
    if not setting_path.exists():
        return RiskManagerSettingStatus(path=setting_path, exists=False)
    try:
        raw = json.loads(setting_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return RiskManagerSettingStatus(
            path=setting_path,
            exists=True,
            error=f"{type(exc).__name__}: {exc}",
        )
    enabled: list[str] = []
    disabled: list[str] = []
    if isinstance(raw, dict):
        for name, value in raw.items():
            active = bool(value.get("active")) if isinstance(value, dict) else False
            (enabled if active else disabled).append(str(name))
    return RiskManagerSettingStatus(
        path=setting_path,
        exists=True,
        enabled_rules=tuple(sorted(enabled)),
        disabled_rules=tuple(sorted(disabled)),
    )


def _has_hard_risk_limit(risk: Any) -> bool:
    fields = (
        "max_loss_points",
        "daily_loss_limit",
        "max_consecutive_losses",
        "max_drawdown_pct",
    )
    return any(getattr(risk, name, None) is not None for name in fields)
