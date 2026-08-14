# P0 账户权益来源与单位契约 v1

状态：`engineering_default`

## 风险量纲

| 字段 | 单位 | 口径 |
|---|---|---|
| `current_equity` / `peak_equity` | 账户货币 | 初始权益加按合约乘数换算的盯市盈亏 |
| `max_drawdown_pct` | 比例 | `(peak_equity-current_equity)/peak_equity` |
| `daily_realized` | 点数 | 已平仓交易的净点数 |
| `realized_points` / `max_loss_points` | 点数 | 全局累计净点数 |
| `consecutive_losses` | 笔数 | 连续亏损平仓次数 |

点数门控和货币权益门控不得混用字段、阈值或报告标签。

## 回测

```text
account_equity = initial_capital
               + mark_to_market_points * contract_multiplier
```

- 初始峰值为 `sizing.capital`。
- 每根 bar 观察一次账户权益。
- 平仓后使用相同货币单位更新风险状态。

## CTA

- 正式运行优先使用 OMS 首个有效账户权益快照作为初始峰值。
- 当前实现汇总 OMS 返回的账户 `balance`；该值在 v1 中定义为策略可见账户作用域。
- 多账户或多 gateway 部署前必须显式限定账户集合，禁止无意汇总无关资金。
- OMS 暂不可用时，`sizing.capital` 只作为显式 fallback，并在审计中标记来源。
- 每根可执行策略 bar 调用 `RiskManager.observe_equity()`，不能只在有信号或有成交时观察。

## 轻量 live engine

- 日亏和连续亏损门控继续启用，并遵守相同 session 契约。
- 当前 `max_drawdown_pct=None`，能力标记为 `disabled_by_design`。
- 不得把该能力差异报告为最大回撤 parity 已通过。

## 审计字段

- `equity_value`
- `equity_unit=account_currency`
- `equity_source=backtest_mark_to_market|oms_balance|config_fallback`
- `equity_scope=simulated_account|oms_all_accounts`
- `max_drawdown_capability=enabled|disabled_by_design`

## 失败策略

- 回测权益不可计算时失败。
- CTA OMS 权益不可用时允许配置 fallback，但必须显式记录。
- 非有限值或非正权益不得更新峰值，并应阻止依赖权益的开仓审批。
