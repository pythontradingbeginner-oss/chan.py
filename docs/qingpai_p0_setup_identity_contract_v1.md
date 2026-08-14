# P0 Setup 身份与审计契约 v1

状态：`research_candidate`

## 目的

在不改变当前交易行为的前提下，同时保留旧消费键和跨 revision 的 setup 候选身份，为 G03/G08 状态机提供可复现的审计基础。

## 双层身份

### Raw Signal Identity

完整保存当前策略消费键：

```text
(bi_idx, klu_idx, type2str, is_buy)
```

该身份只用于复现旧行为，不声明它是正确的 setup 生命周期。

### Canonical Setup Candidate v1

候选字段：

```text
(contract_epoch, timeframe, direction, root_bi_idx, setup_family)
```

- `contract_epoch`：实际合约或明确的结构重置 epoch，禁止跨换月复用。
- `timeframe`：本级别周期。
- `direction`：long/short。
- `root_bi_idx`：当前候选使用的根笔索引。
- `setup_family`：研究字段，决定 1/1p、2/2s 等类型是否属于同一 setup 族。

该 ID 在 P0 只记录为 `canonical_setup_candidate`，不得参与去重、释放、重评或成交。

P0 工程默认采用：`root_bi_idx = related_bsp1_bi_idx`（存在时），否则使用当前
`bi_idx`；`setup_family=unresolved_primary_bsp:<primary_bsp>`，明确表示 BSP 家族
合并尚未裁决。`candidate_id` 是上述字段规范串的 SHA-256，实际合约换月会改变
`contract_epoch` 并生成新 ID。同一字段组合跨 event revision 保持稳定。

## 尚未裁决

- `primary_bsp` 演变是否创建新 setup。
- T1/T1P、T2/T2S 是否分别合并为 setup family。
- 保护止损后的重新武装条件。
- 最大尝试次数和有效期。
- 父级方向冲突后是否允许同 setup 等待父级翻转。

这些事项由 `OPEN-006` 保持未决。

## 审计落点

- `decision_trace.csv`：raw identity、candidate ID、schema version、setup state。
- `execution_decisions.csv`：candidate ID、risk session、风险审批状态。
- `trades.csv`：candidate ID、attempt sequence、前次退出原因。
- 不向通用 `SignalDecision` 强塞 RB 特有 session 字段。

机器 schema：`configs/qingpai_p0_audit_schema_v1.json`。
