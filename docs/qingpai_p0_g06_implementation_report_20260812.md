# 青派 P0 / G06 实施与重跑报告（2026-08-12）

## 结论摘要

P0 三项契约、审计 Schema 与 G06 工程修复已经落地。日亏门控现在由 RB
交易日 session 推进，不再依赖下一次成交触发；回测、CTA 与轻量 live 统一使用
`RBTradingCalendar`；账户权益统一为账户货币单位并逐 bar 观察；Setup candidate
已进入决策、执行和交易审计，但仍为 `observed_only`，不改变交易行为。

2018-03-23 至 2026-08-03 的完整连续回放表明：G06 日切修复没有改变交易集合。
策略仍在 2018 年达到连续 5 笔亏损后进入吸收态，之后 173 条规则接受信号全部被
`max_consecutive_losses: 5 >= 5` 拒绝。因此下一项阻断不是继续修改日切，而是人工
裁决 `OPEN-005` 中连续亏损门控的恢复语义。

## 已实施内容

### 风险 Session

- 新增 `RiskManager.advance_session(session_key, observed_at, source)`。
- 首次初始化、同 session 幂等、后续 session 仅清零 `daily_realized`。
- 更早 session 失败关闭；正式 RB 路径缺失 session key 时失败关闭。
- 周五夜盘映射到下周一交易日；bar 与 fill 使用同一 resolver。
- `current_session_key`、最后观察时间和来源进入持久化风险状态。

### 账户权益

- 回测：`capital + mark_to_market_points * contract_multiplier`。
- 回测和 CTA 每根可执行 bar 调用 `observe_equity()`。
- CTA/live 记录 `oms_balance` 或 `config_fallback` 来源和账户作用域。
- 首个有效 OMS 权益会替换配置 fallback 的初始峰值基准。
- 轻量 live 明确标记 `max_drawdown=disabled_by_design`。

### Setup 身份与审计

- `raw_signal_identity` 记录现有 `(bi_idx, klu_idx, type2str, is_buy)`。
- `setup_candidate_id` 使用实际合约、周期、方向、根笔和未裁决 BSP 家族生成。
- 同一候选跨 event revision 稳定，换月后不同。
- `decision_trace.csv`、`execution_decisions.csv` 和 `trades.csv` 已写入身份字段。
- `trades.csv` 增加 `attempt_sequence` 和 `prior_exit_reason`。
- Setup candidate 不参与消费键、释放、风险审批或成交。

## 完整回放结果

固定配置：`configs/rb_15m_qingpai_strict.yaml`；未做参数搜索。

| 口径 | 规则 accepted | 成交 | 风控拒绝 | 净点数 |
| --- | ---: | ---: | ---: | ---: |
| 连续全历史 | 183 | 10 | 173 | -133 |
| 连续标准 OOS | 104 | 0 | 104 | 0 |
| Fold-reset 标准 OOS | 103 | 86 | 17 | +618 |

连续流的 173 次拒绝与连续 OOS 的 104 次拒绝，原因均为
`max_consecutive_losses: 5 >= 5`。首次拒绝发生在 2018-08-24 夜盘，风险 session
为 2018-08-27；当时 `daily_realized=0`，证明日亏状态已经按新交易日恢复，阻断来自
连续亏损计数。

与 2026-08-10 旧连续基线逐键比较：3658 条决策、183 条执行决策和 10 笔交易
完全对齐；accepted、reason、方向、仓位和执行状态均为 0 差异。因此 G06 修复没有
混入策略规则变化。

Fold-reset 与连续 OOS 之间仍有 2 条 continuous-only 决策，其中 1 条 accepted。
这是不同 warmup/重放边界下的既有决策流差异，所以 +618 点只能作为风险状态敏感性
诊断，不能解释为纯风险复位收益或未来收益预期。

## 产物

- 连续机制审计：`reports/rule_mechanism_audit/20260812_g06_session_fixed/`
- 双口径报告：`reports/risk_state_policy_compare/20260812_g06_session_fixed/`
- 风险 session 契约：`docs/qingpai_p0_risk_session_contract_v1.md`
- 权益契约：`docs/qingpai_p0_equity_contract_v1.md`
- Setup 身份契约：`docs/qingpai_p0_setup_identity_contract_v1.md`
- JSON Schema：`configs/qingpai_p0_audit_schema_v1.json`

## 下一步门槛

### 先裁决 OPEN-005

必须明确 `max_consecutive_losses` 达到阈值后的恢复事件：

1. 人工复位停机：最保守，连续运行会在 2018 年后保持停机。
2. 按交易日自动复位：恢复最快，但改变了当前全局连续亏损语义。
3. 冷却 N 个交易日或满足明确恢复条件后复位：需要同时裁决 N、恢复条件和状态持久化。

`max_loss_points` 与 `max_drawdown_pct` 的复位/停机语义也应一并确认，但本次历史的
直接吸收态由连续亏损门控造成。

### 再实施 P1（G03/G08）

待 OPEN-005 冻结后，再把 Setup candidate 升级为最小状态机，明确保护止损后的
rearm、有效期和最大尝试次数。该阶段必须另做第二次因果隔离回放，不能与 G06
结果合并解释。

### 最后研究策略经济性

风险状态语义和 Setup 状态机稳定后，才重新审查 G01/G02/G05/G07，包括方向矩阵、
动量覆盖、止损成本和仓位 sizing。正式报告继续采用连续状态主口径加 fold-reset
敏感性口径，并保留 decision parity 与拒绝原因。
