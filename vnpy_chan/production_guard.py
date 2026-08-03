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


def evaluate_production_gate(
    *,
    config: Any,
    kl_window: int,
    production_ready: bool,
    shadow_mode: bool,
    forward_confirmed: bool,
    risk_manager_confirmed: bool,
    risk_manager_setting_path: str | Path | None,
    pending_order_count: int = 0,
) -> ProductionGateResult:
    risk_setting = inspect_risk_manager_setting(
        risk_manager_setting_path or DEFAULT_RISK_MANAGER_SETTING_PATH
    )
    if shadow_mode:
        return ProductionGateResult(
            ready=True,
            mode="shadow",
            reasons=("shadow_mode_enabled",),
            risk_manager_setting=risk_setting,
        )

    reasons: list[str] = []
    if not production_ready:
        reasons.append("production_ready_false")
    if not forward_confirmed:
        reasons.append("forward_confirmed_false")
    if not risk_manager_confirmed:
        reasons.append("risk_manager_confirmed_false")
    if int(kl_window) not in SUPPORTED_PRODUCTION_WINDOWS:
        reasons.append(f"unsupported_kl_window:{kl_window}")
    if pending_order_count > 0:
        reasons.append(f"pending_order_recovery_required:{pending_order_count}")
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
    if not risk_setting.active:
        reasons.append("veighna_risk_manager_setting_inactive")

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
