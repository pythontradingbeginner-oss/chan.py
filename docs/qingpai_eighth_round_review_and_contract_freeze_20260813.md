# 青派缠论与 RB 交易策略第八轮裁决及 OPEN-005 契约冻结说明

日期：2026-08-13  
审查输入：`审查基线_0813_8A_gemini.md`、`审查基线_0813_8B_deepseek.md`  
上游裁决：`docs/qingpai_seventh_round_review_synthesis_20260813.md`

## 一、独立结论

DeepSeek 指出的流程缺口成立：审查前不存在独立的 OPEN-005 机器契约、Runtime Schema 和测试矩阵；第七轮只有自然语言内嵌草案，不能称为已经冻结。

Gemini 提供了有用的接口、Schema 和测试骨架，但不能直接复制：

- `record_close_fill` 缺少实际幂等和 position 聚合语义；
- 将每个 fill 分别舍入后再相加会与完整 position 先聚合后舍入产生不同的 win/loss/flat；
- 仅按订单数量方向判断 risk reducing，会把跨零反手整单错误地视作减仓；
- reservation 只存 ID，无法在重启后重建 `TradeIntent`；
- D profile 没有明确其余门控必须与 B1 相同。

本轮裁决：第七轮方向通过，并将其修正为正式、机器可读的研究实施契约；Option D 仍不是生产默认，累计亏损和最大回撤的生产恢复政策仍保持 OPEN-005 未决。

## 二、已创建的正式产物

1. `configs/qingpai_open005_machine_contract_v1.json`
   - 冻结事件单位、PnL 规范化、风险门控模式、B0/B1/D profile、两层 D 状态、反手顺序、持久化和 parity 不变量。
2. `configs/qingpai_open005_runtime_schema_v1.json`
   - 冻结风险运行时状态、fill 去重、position 聚合、D 状态和可恢复 intent reservation 的 JSON Schema。
3. `configs/qingpai_open005_test_matrix_v1.json`
   - 20 项机器可读测试，覆盖契约、幂等、跨 session、舍入、重启、门控模式、仓位腿拆分、D、反手和全历史 parity。
4. `tests/test_qingpai_open005_contract.py`
   - 自动验证契约关联、唯一 ID、枚举、单一 streak 事实源、D/B1 正交关系和高风险测试锚点。

## 三、冻结后的关键裁决

### 1. 舍入口径

- 每个 close fill 使用 `round(raw, 1)` 更新 `realized_points` 与 `daily_realized`，以保持当前回测记账兼容。
- 完整 position 的 `win/loss/flat` 先汇总所有 raw fill PnL，再 `round(sum(raw), 1)` 判定。
- 原始值和规范化值必须同时保存。
- 滑点已反映在实际成交价中，finalize 不得重复扣除。

### 2. 风险门控模式

正式枚举为：

```text
enforce | observe | disabled
```

不得使用现有 `shadow_mode` 名称。所有启用的状态门控完整评估，不在首次命中时短路；输出 `enforced_breaches` 和 `observed_breaches`。session、equity、仓位转换合法性和 `max_abs_position` 等硬前提不受 B0 observe 模式放宽。

### 3. B0/B1/D

- B0：四个账户状态门控均 observe，用于不中断策略路径的诊断，不称为纯 Alpha。
- B1：只 disabled 连亏门控，其他三项 enforce，是 D 的直接对照。
- D：门控配置与 B1 完全相同，只增加冻结的实验性 probe 恢复政策。

### 4. D 两层状态

策略制度层只有：

```text
ACTIVE
PAUSED_UNTIL_NEXT_SESSION
PROBE_AVAILABLE
PROBE_IN_FLIGHT
```

`PROBE_PENDING_ENTRY` 仅为派生审计状态。订单生命周期继续由既有订单机管理，但必须绑定并持久化 `probe_epoch_id`、订单 ID、position ID 和可无损重建的 `TradeIntent` 快照。

### 5. 平仓、减仓和反手

统一共享 `decompose_position_transition(before, target)`：

- 降低绝对暴露的腿不受账户状态型开仓门控阻断；
- 跨零反手拆成 close-to-zero 和 open-from-zero 两腿；
- 先完成旧仓平仓、记账、finalize 和 D 转移；
- 再对新开仓腿做全新审批，不复用平仓前的批准。

## 四、测试状态

- JSON 文件语法：通过。
- Runtime Schema：通过 JSON Schema Draft 2020-12 校验。
- 契约与既有 P0 契约测试：9 项通过。
- 测试矩阵共 20 项：2 项契约检查为 `passing`，18 项实现与回测项目为 `not_implemented`。

这意味着“规则和验收标准已经冻结”，不意味着“策略功能已经实现”。

## 五、实施进度更新（2026-08-13）

第一实施阶段已经完成：`record_close_fill`、`finalize_position`、仓位聚合、成交幂等和
CTA 风险状态恢复已落地。完整历史回放通过行为 parity：3658 条决策、183 条执行决策、
10 笔交易，行为字段 0 差异。详见
`docs/qingpai_open005_risk_accounting_implementation_report_20260813.md`。

## 六、下一实施阶段

按冻结顺序推进：

1. 已完成：实现 `record_close_fill`、`finalize_position`、position 聚合和幂等持久化。
2. 已完成：当前一手整仓路径 2018-2026 行为 parity，保持 3658/183/10 且行为 0 差异。
3. 下一步：实现 `risk_gate_mode=enforce|observe|disabled` 和多 breach 审计。
4. 随后运行 B0/B1 连续流与 fold 诊断。
5. 最后实现冻结的 D，并仅用 D vs B1 做直接因果比较。

本轮只完成注释所列第一步“冻结机器契约”，未修改交易策略行为，未运行 B0/B1，也未实施 Option D。
