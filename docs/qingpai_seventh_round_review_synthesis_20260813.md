# 青派缠论与 RB 交易策略第七轮交叉审查综合裁决

日期：2026-08-13  
审查输入：`审查基线_0812_7A_gemini.md`、`审查基线_0812_7B_deepseek.md`  
审查对象：`docs/qingpai_sixth_round_review_synthesis_20260812.md`  
范围：风险事件契约、B0/B1、Option D 状态与订单生命周期；不修改策略行为

## 一、总裁决

第六轮的三项核心方向通过，但尚不能直接照 Gemini 的伪代码实施：

| 项目 | 裁决 |
| --- | --- |
| 风险事件拆分 | 原则通过；补齐幂等、聚合、舍入、原子持久化后才能冻结 |
| B0/B1 | 通过；B0 更名为“风险门控观察模式”，不得使用 `shadow` 或“纯 Alpha” |
| Option D | 继续作为实验候选；策略状态与订单状态分层，不新增重复订单状态机 |
| `PROBE_PENDING_ENTRY` | 不作为独立策略制度状态；以既有订单 reservation 表达，但必须绑定 probe 身份并持久化 intent |
| A/C | A 保留为账户安全闩；C 维持否决 |
| OPEN-005 | 未关闭；累计亏损和最大回撤的恢复政策仍需正式契约 |

本轮不修改策略代码，也不运行 D。

## 二、对 Gemini 7A 的裁决

### 通过

- 将已实现 PnL 的 fill 级记账与完整持仓的 streak 结算分开。
- 完整持仓费用后聚合净 PnL 使用 `win/loss/flat` 三值结果。
- B0/B1 的实验目的和单变量关系基本正确。
- probe 权限跨 session 保留，是比“每天过期再重新赋予”更简洁的候选。
- 当前一手整仓路径必须保持 0 决策差异。

### 不能直接采用

Gemini 给出的 `RiskAccountingEngine` 只是示意代码，不是可冻结接口：

1. `record_close_fill()` 接收 `fill_id` 却没有幂等检查，重复成交回报会重复记账。
2. `position_id` 没有用于累计部分平仓 PnL，`finalize_position()` 仍依赖调用者提供无法核验的总额。
3. `risk_session` 没有实际参与 session 推进，不能保证跨交易日部分平仓正确归属。
4. 没有保存每个未完成 position 的累计 PnL，重启后无法正确 finalize。
5. `observe_equity()` 丢失了“首次权威 OMS 权益替换配置 fallback 峰值”的现有语义。
6. 没有规定 fill 记账、position finalize、probe 转移和运行时状态落盘的原子顺序。
7. “扣除滑点”不能在 finalize 时再扣一次；当前滑点已经反映在成交价中，finalize 应聚合成交 PnL。

因此可以采纳抽象，不能复制伪代码。

## 三、对 DeepSeek 7B 的裁决

### 通过

- 回测风险 PnL 使用 `round(pnl, 1)`，CTA/live 使用原始浮点，是真实 parity 差异。
- `equity_scope` 当前只保存为审计元数据，不直接参与回撤判定。恢复可达性取决于传入权益数值及 `source` 语义。
- 既有 `shadow_mode` 是“不发真实订单”，与 B0 的“风险门控只观察、不阻断”近乎相反，必须避免同名。
- 当前 `RiskManager` 没有 B0 所需的 observe-only 能力，必须新增并做不改变交易集合的 parity 验证。
- 不应在已有订单状态机旁边再维护一套重复的 probe 订单状态机。

### 需要纠正

#### 1. 现有 reservation 尚未完整满足 probe 契约

CTA 的 `_pending_entry + CtaOrderStatusMachine` 已能阻止同时提交两个开仓意图，并持久化活动订单和成交去重 ID；但结论“已经完整覆盖 probe pending”过强：

- `_pending_entry` 本身没有进入 `RuntimeStatePackage`；
- `PendingEntry` 没有序列化接口；
- 重启恢复活动订单后，后续开仓 fill 可能因缺少 `TradeIntent` 被忽略；
- 活动订单目前只记录通用 `role=open` 和 event reason，没有 `probe_epoch_id`；
- 轻量 `live_engine` 的 `_pending_entries` 仅在内存中，未见与 CTA 等价的持久化和成交去重。

正确裁决是：复用订单机表达 pending 生命周期，同时扩展订单元数据和运行时包，持久化 probe 与 intent 的关联；不是新增平行状态机，也不是宣称现状已经满足。

#### 2. `max_loss_points` 的手数换算结论错误

`PositionContext.pnl_points()` 已乘以 `lots`。对同一 RB 合约，`max_loss_points=600` 是 600 个聚合 point-lots，按合约乘数 10 对应约 6,000 元；增加手数会更快达到阈值，但不会把同一个阈值自动变成 60,000 元。

它真正的问题是：

- 没有随账户权益调整；
- `RiskManager` 不知道合约乘数，跨品种时货币含义不同；
- 名称容易与“每手价格点数”混淆。

同理，初始权益 100,000 元下的 `max_drawdown_pct=3.1%` 是约 3,100 元，不是 3,100 个 RB 点；在乘数 10、峰值未变时约等于 310 个聚合点。因此 B1 中最大回撤可能先于 `max_loss_points=600` 触发。

## 四、冻结后的风险事件模型

建议的最小公共契约为：

```text
record_close_fill(
  fill_id, order_id, position_id, pnl_raw_points,
  risk_session_key, observed_at, source
)
  -> 幂等接收真实平仓 fill
  -> 先推进 session，再更新 realized_points/daily_realized
  -> 累加 position 级 realized PnL

finalize_position(position_id, finalized_at, reason)
  -> 只允许一次
  -> 从内部累计值取得 completed_position_net_pnl
  -> 规范化后判定 win/loss/flat
  -> 更新 true_consecutive_losses
  -> 触发 probe 状态转移

observe_equity(value, observed_at, source, scope)
  -> 保留首次权威 OMS 权益切换语义
  -> 更新 peak 与 drawdown 审计
```

必须同时冻结：

- fill、position 和 finalize 的稳定 ID；
- 重复 fill/finalize 的处理；
- 未完成 position 聚合器的持久化；
- 原始 PnL 与规范化 PnL 两列；
- 舍入规则。兼容阶段建议以当前回测的一位小数作为风险判定基线，同时保存原始值，并验证 CTA/live 是否产生符号差异；未经测试不直接改成另一套舍入法；
- 最终 close fill 记账、position finalize、D 转移和 runtime package 持久化的顺序。

## 五、B0/B1 正式命名与不变量

禁止使用 `shadow-only`，建议使用每个门控独立的：

```text
risk_gate_mode = enforce | observe | disabled
```

### B0：Uninterrupted Strategy-Path Diagnostic

- 四个账户状态门控均为 `observe`；
- 记录全部 breach，不短路，只要没有 enforce breach 就允许执行；
- 结构止损、仓位计算、资金约束、费用、滑点、交易窗口和 `max_abs_position` 保持不变；
- 基础数据/session/equity 缺失属于研究失败，不应静默放行；
- 交易和决策必须与“四门控 disabled”参考运行逐键一致，风险审计列除外。

B0 是“策略路径不被账户状态门控截断的诊断”，不是纯 Alpha；青派结构过滤、Setup、执行和成本仍然存在。

### B1：Consecutive-Loss Single-Gate Ablation

- `max_consecutive_losses=disabled`；
- daily loss、cumulative loss、drawdown 均为 `enforce`；
- 其他输入和规则不变；
- 作为 D 的唯一直接因果对照。

风险评估结果应分别输出 `observed_breaches[]` 与 `enforced_breaches[]`，避免当前首次命中即 return 导致后续 breach 不可见。

## 六、Option D 采用两层状态模型

### 策略制度状态

```text
ACTIVE
PAUSED_UNTIL_NEXT_SESSION
PROBE_AVAILABLE
PROBE_IN_FLIGHT
```

### 订单生命周期

继续由现有订单状态机负责 `SUBMITTING/PARTTRADED/CANCELLED/REJECTED/ALLTRADED`。当 probe 开仓订单在途时，不新增平行的 `PROBE_PENDING_ENTRY` 订单机；由以下关联字段表达：

- `probe_epoch_id`；
- `probe_order_ids`；
- `probe_position_id`；
- `reserved_intent` 或可重建的 intent identity；
- `true_consecutive_losses`；
- `eligible_from_session`；
- `probe_result=win|loss|flat`。

派生审计状态可以显示 `PROBE_PENDING_ENTRY`，但订单机是其唯一事实源。零成交终态释放 reservation；发生任一开仓 fill 后进入 `PROBE_IN_FLIGHT`；部分成交后撤销剩余量时仍保持 in-flight，并以实际持仓最终聚合 PnL 结算。

## 七、两份审查共同遗漏的关键问题

### 1. probe 反手必须拆成平仓腿和新开仓腿

当前回测可在一次目标仓位变更中完成反手；CTA/live 先平仓，再消费 `_pending_reversal` 开新仓。D 下这会产生严重差异：

1. probe 的平仓腿必须始终允许，不能被 `streak >= threshold` 阻断；
2. 平仓完成后先 finalize probe，得到 win/loss/flat；
3. 反向新开仓腿必须重新经过 D 和账户安全门控；
4. probe loss/flat 时取消 pending reversal，probe win 后才可能按新状态批准；
5. 不能复用平仓前的风险审批结果。

因此 D 实验前必须把反手定义为两个有序动作，并让 backtest/CTA/live 共享同一转移顺序。否则 D vs B1 没有 parity 基础。

### 2. 风控只能阻断增加风险暴露的腿

当前 `RiskManager.approve()` 对信号统一检查门控，没有通用的“减仓/平仓永远放行”接口。D 的 probe 持仓本身处于高 streak 状态，若把平仓信号送入同一硬门控，可能出现无法退出的错误。正式契约必须先按 `quantity_delta` 拆分：

- 降低绝对暴露或纯平仓：不受状态型开仓门控阻断；
- 增加暴露或反手的新开仓腿：完整审批。

### 3. 持久化必须覆盖业务上下文，不只是订单编号

成交去重 ID、活动订单、pending intent、position PnL 聚合器、probe epoch 和 D 状态必须在同一版本化 runtime package 中恢复。仅恢复订单列表不足以正确处理重启后的成交。

## 八、下一步

不再进行第八轮宽泛讨论。执行前只需要一次针对机器契约和测试矩阵的定向盲审，然后按以下顺序实施：

1. 冻结风险事件 Schema、PnL 规范化、两层 D 状态和反手原子性；在看到 B0/B1 新收益前冻结 D。
2. 实现风险 fill 记账与 position finalize 拆分，补幂等和持久化；当前一手路径必须 exact parity。
3. 实现 `risk_gate_mode=observe|enforce|disabled`，不得改动既有 `shadow_mode`。
4. 运行 2018-2026 B0/B1 连续流、fold 诊断，并记录所有 observed/enforced breaches。
5. 只实现已冻结的 D，比较 D vs B1；B0 只作完整策略路径参照。
6. 并列报告收益、回撤、尾损、恢复时间、probe 结果、门控覆盖率、年度与走势分层稳定性。
7. 连亏、累计亏损、最大回撤三类政策全部裁决后关闭 OPEN-005，再进入 P1 Setup 生命周期。

