"""ChanBspStrategy open-guard rule for VeighNa RiskManager.

Second hard-risk layer: a real VeighNa RuleTemplate loaded from the user
`rules/` directory.  It intercepts every MainEngine.send_order() and refuses
OPEN orders that violate the RB main-contract / single-lot / manual-halt /
minimum-equity / tick-heartbeat / account-heartbeat / rollover-authorization
guard.  Risk-reducing CLOSE orders always pass.

Important defaults:
  - active defaults to False.  RuleTemplate.__init__ sets active=True, so the
    rule deliberately turns itself OFF unless the operator explicitly enables
    it in risk_manager_setting.json.  This is the safe-by-default contract for
    this release.
  - EVENT_ACCOUNT is explicitly registered in on_init (P7-R10 fix).
  - Tick heartbeat is per-contract: only ticks matching the order's vt_symbol
    keep the heartbeat alive.
  - Account heartbeat is per-gateway: only account events for the order's
    gateway keep the heartbeat alive.
  - Empty whitelist → refuse ALL OPEN (fail-closed).
  - CtaStrategy_Rollover OPEN is always refused.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from vnpy.event import Event, EventEngine
from vnpy.trader.event import EVENT_ACCOUNT, EVENT_TICK
from vnpy.trader.object import (
    AccountData,
    OrderRequest,
    TickData,
)

from vnpy_riskmanager.template import RuleTemplate

from vnpy_chan.hard_risk import HardRiskState, evaluate_open_guard


class ChanOpenGuardRule(RuleTemplate):
    """禁止违规开仓；合法平仓永远放行。"""

    name: str = "缠论开仓守卫"

    parameters: dict[str, str] = {
        "rb_whitelist": "RB主力白名单(逗号分隔)",
        "single_lot_limit": "单手限制",
        "minimum_equity": "最低权益",
        "tick_timeout_seconds": "行情心跳超时(秒)",
        "account_timeout_seconds": "账户心跳超时(秒)",
        "manual_halt": "人工停机",
        "rollover_authorized_symbols": "换月授权合约(逗号分隔)",
        "rollover_armed": "换月授权开关",
        "rollover_valid_until": "换月授权截止(ISO)",
    }

    variables: dict[str, str] = {
        "last_tick_time": "最近行情时间",
        "last_account_time": "最近账户时间",
    }

    def __init__(self, risk_engine: Any, setting: dict) -> None:
        # Disable by default BEFORE super().__init__ runs on_init + setting.
        # The base class flips active=True, so we force it back to False and
        # only re-enable when the setting explicitly says active: true.
        super().__init__(risk_engine, setting)
        self.active = False
        if isinstance(setting, dict) and setting.get("active", False):
            self.active = True

        # P7-R10: explicitly register EVENT_ACCOUNT (RuleTemplate has no
        # standard on_account callback, so RiskEngine.register_events cannot
        # auto-detect it).
        event_engine = getattr(risk_engine, "event_engine", None)
        if event_engine is not None:
            self._register_account_handler(event_engine)

    def _register_account_handler(self, event_engine: EventEngine) -> None:
        """幂等注册 EVENT_ACCOUNT handler。"""
        if hasattr(self, "_account_handler") and self._account_handler is not None:
            return
        self._account_handler = self._on_account_event
        event_engine.register(EVENT_ACCOUNT, self._on_account_event)

    def on_init(self) -> None:
        """初始化参数与心跳状态。"""
        self.rb_whitelist: str = ""
        self.single_lot_limit: int = 1
        self.minimum_equity: float = 0.0
        self.tick_timeout_seconds: int = 120
        self.account_timeout_seconds: int = 30
        self.manual_halt: bool = False
        self.rollover_authorized_symbols: str = ""
        self.rollover_armed: bool = False
        self.rollover_valid_until: str = ""

        self.last_tick_time: datetime | None = None
        self.last_account_time: datetime | None = None
        self._account_handler = None

        # P7-R10: per-contract tick tracking — "last_tick_time" is now a
        # dict keyed by vt_symbol so that a tick for one contract cannot
        # satisfy the heartbeat requirement for a different contract.
        self._tick_times: dict[str, datetime] = {}
        # P7-R10: per-gateway account tracking — same rationale.
        self._account_times: dict[str, datetime] = {}

    def update_setting(self, rule_setting: dict) -> None:
        """参数更新；缺省保持 active=false（安全默认）。"""
        super().update_setting(rule_setting)
        if isinstance(rule_setting, dict):
            self.active = bool(rule_setting.get("active", False))

    # ── 标准事件回调（用标准名，RiskEngine.register_events 自动检测）──

    def on_tick(self, tick: TickData) -> None:
        """标准行情推送回调 — RiskEngine 自动注册 EVENT_TICK。

        P7-R10: track heartbeat per-contract (vt_symbol) so that a tick for
        one contract cannot satisfy the heartbeat for another.
        """
        if not self.active:
            return
        if tick is None:
            return
        vt_symbol = f"{tick.symbol}.{tick.exchange.value if hasattr(tick.exchange, 'value') else tick.exchange}"
        now = datetime.now()
        self._tick_times[vt_symbol.upper()] = now
        # Also maintain the legacy field for backward compat display
        self.last_tick_time = now
        self.put_event()

    def _on_account_event(self, event: Event) -> None:
        """账户事件回调（EVENT_ACCOUNT 手动注册）。

        P7-R10: track heartbeat per-gateway so that an account event from
        a different gateway cannot satisfy the heartbeat.
        """
        if not self.active:
            return
        account: object = event.data
        if account is None:
            return
        gateway = str(getattr(account, "gateway_name", "") or "")
        now = datetime.now()
        if gateway:
            self._account_times[gateway.upper()] = now
        self.last_account_time = now
        self.put_event()

    # ── 风控入口 ──

    def check_allowed(self, req: OrderRequest, gateway_name: str) -> bool:
        """仅拦截开仓；所有平仓委托无条件放行。

        P7-R10 fail-closed defaults:
          - Empty whitelist → refuse ALL OPEN
          - Whiplist must be exact main-contract symbols, not RB prefix
          - tick_timeout > 0 and no tick for THIS contract → refuse
          - account_timeout > 0 and no account event for THIS gateway → refuse
          - minimum_equity > 0 and account equity unreachable → refuse
          - CtaStrategy_Rollover OPEN → ALWAYS refuse
        """
        from vnpy.trader.constant import Offset

        offset = getattr(req, "offset", None)
        if offset in {
            Offset.CLOSE,
            Offset.CLOSETODAY,
            Offset.CLOSEYESTERDAY,
        }:
            return True

        # P7-R10: CtaStrategy_Rollover OPEN is ALWAYS refused.
        reference = str(getattr(req, "reference", "") or "")
        if reference.upper().endswith("_ROLLOVER"):
            self.write_log(f"开仓被拒：Rollover OPEN 一律禁止 (ref={reference})")
            return False

        symbol = str(getattr(req, "symbol", ""))
        exchange = getattr(req, "exchange", None)
        exchange_str = exchange.value if hasattr(exchange, "value") else str(exchange or "")
        vt_symbol = f"{symbol}.{exchange_str}" if exchange_str else symbol

        whitelist_raw = [
            s.strip().upper()
            for s in str(self.rb_whitelist or "").split(",")
            if s.strip()
        ]

        # P7-R10: empty whitelist → fail-closed.
        if not whitelist_raw:
            self.write_log(f"开仓被拒：白名单为空")
            return False

        # P7-R10: exact main-contract match required (not RB prefix).
        if vt_symbol.upper() not in whitelist_raw:
            # Also try matching just the base symbol (e.g. "RB2610" matches "RB2610.SHFE")
            base = vt_symbol.split(".")[0].upper()
            if base not in whitelist_raw:
                self.write_log(f"开仓被拒：合约{vt_symbol}不在RB白名单 {','.join(whitelist_raw)}")
                return False

        # P7-R10 per-contract tick heartbeat
        tick_timeout = float(self.tick_timeout_seconds or 0)
        if tick_timeout > 0:
            tick_time = self._tick_times.get(vt_symbol.upper())
            if tick_time is None:
                self.write_log(f"开仓被拒：合约{vt_symbol}无行情心跳")
                return False
            if (datetime.now() - tick_time).total_seconds() > tick_timeout:
                self.write_log(f"开仓被拒：合约{vt_symbol}行情心跳超时")
                return False

        # P7-R10 per-gateway account heartbeat
        account_timeout = float(self.account_timeout_seconds or 0)
        if account_timeout > 0:
            gw_key = str(gateway_name or "").upper()
            if not gw_key:
                self.write_log("开仓被拒：无法确定gateway")
                return False
            account_time = self._account_times.get(gw_key)
            if account_time is None:
                self.write_log(f"开仓被拒：网关{gw_key}无账户心跳")
                return False
            if (datetime.now() - account_time).total_seconds() > account_timeout:
                self.write_log(f"开仓被拒：网关{gw_key}账户心跳超时")
                return False

        # P7-R10: minimum_equity > 0 and unreachable → fail-closed
        min_eq = float(self.minimum_equity or 0.0)
        if min_eq > 0:
            eq = self._account_equity()
            if eq is None:
                self.write_log("开仓被拒：最低权益要求但无法查询账户权益")
                return False
            if eq < min_eq:
                self.write_log(f"开仓被拒：权益{eq:.0f}<最低{min_eq:.0f}")
                return False

        # single_lot_limit enforcement
        planned_lots = int(getattr(req, "volume", 1) or 1)
        limit = int(self.single_lot_limit or 0)
        if limit > 0 and planned_lots > limit:
            self.write_log(f"开仓被拒：手数{planned_lots}>限制{limit}")
            return False

        if bool(self.manual_halt):
            self.write_log("开仓被拒：人工停机")
            return False

        return True

    def _account_equity(self) -> float | None:
        engine = getattr(self, "risk_engine", None)
        if engine is None:
            return None
        try:
            main_engine = getattr(engine, "main_engine", None)
            if main_engine is None:
                return None
            oms = main_engine.get_engine("oms")
            accounts = oms.get_all_accounts() if oms is not None else []
            balances = [float(a.balance) for a in accounts]
            return sum(balances) if balances else None
        except Exception:
            return None
