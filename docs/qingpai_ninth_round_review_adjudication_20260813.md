# 青派 OPEN-005 第九轮交叉审查裁决

日期：2026-08-13  
审查输入：`审查基线_0813_9A_gemini.md`、`审查基线_0813_9B_deepseek.md`  
审查对象：OPEN-005 风险成交记账与仓位终结第一实施阶段

## 一、独立结论

核心拆分成立：`record_close_fill` 的成交幂等、`finalize_position` 的仓位级一次终结、
双舍入口径和旧一手回测行为 parity 均可以采信。

DeepSeek 指出的三个中等级问题成立：回测身份曾依赖随机 `decision_id`、Runtime Schema
必填字段方向颠倒、PARITY-001 未覆盖 CTA 部分平仓路径。本轮已修正身份与 Schema，
并补 CTA 集成测试。Gemini 对 Option D 已实现、RA-003 至 RA-005 的含义、CTA 已实现
原子持久化三项判断不成立。

OPEN-005 第一阶段现在可以关闭，但关闭的是“风险记账事件拆分”，不是生产恢复能力，
也不是 Option D。

## 二、逐项裁决

| 检查点 | Gemini | DeepSeek | Codex 裁决 |
| --- | --- | --- | --- |
| 重复 close fill 零副作用 | 接受 | 接受 | 通过；订单机与 RiskManager 两层幂等 |
| 部分平仓只在归零时更新 streak | 接受 | 接受 | 通过；新增 CTA 两笔部分平仓集成测试 |
| fill/position 双舍入 | 接受 | 接受并提示不对称 | 通过；不对称是冻结契约，不是数值错误 |
| position_id 生命周期 | 认为完备 | 指出回测随机与字符串敏感 | 接受 DeepSeek；本轮改为稳定回测输入或首笔 runtime fill ID |
| CTA 崩溃与重放 | 认为原子安全 | 有条件通过 | 部分通过；新增反手前同步检查点，但 vn.py JSON 写入不是原子替换 |
| 旧快照与 Schema | 认为完全一致 | 指出 required 颠倒 | 接受 DeepSeek；已要求 `finalized_position_results`，D 字段后移至 v2 |
| 三运行时事件顺序 | 认为无分叉 | 指出回测与 CTA 覆盖不同 | 接受 DeepSeek；记账语义对齐，不宣称全路径回放等价 |
| 3658/183/10 parity | 完全通过 | 行为通过、身份未证明 | 行为通过；旧报告不作为身份 parity 证据 |
| RA-001 至 RA-005 | 错误解释 RA-003/4/5 | 正确映射 | 接受 DeepSeek 映射 |
| Option D | 误称 finalize 已有 D 转移 | 指出 v1/v2 冲突 | D 尚未实现；实施时使用 Runtime Schema v2 |

正确的测试矩阵语义为：

- RA-001：重复 close fill 幂等；
- RA-002：多 fill 仓位归零后一次终结；
- RA-003：部分平仓跨 RB risk session；
- RA-004：舍入边界与先后顺序；
- RA-005：部分平仓后重启、重放与继续终结；
- PARITY-001：旧一手完整回测行为一致性。

## 三、本轮修正

### 1. 可复现身份

- 回测 `position_id` 由 event、Setup、合约、入场时间、bar、实际成交价和方向派生，
  明确排除随机 `decision_id`；时间和价格使用规范化表示。
- CTA 与轻量 live 由第一笔已接受开仓成交身份创建 `position_id`，后续加仓、减仓、
  反手前旧仓终结和恢复均保留该 ID。
- 回测 `trades` 新增 `position_id`、`close_fill_id`、`close_order_id`，供后续跨次完整
  回放做身份 parity。

### 2. Runtime Schema 边界

- `finalized_position_results` 改为 v1 必填字段。
- `option_d` 与 `reserved_intents` 不再是 accounting-only v1 的必填和输出字段。
- D 的结构定义仍作为契约草案保留，但真实 D 状态和 reservation 必须使用
  `open005-risk-runtime-v2`，避免“Schema 允许、当前加载器拒绝”的版本错位。

### 3. CTA 检查点与覆盖

- 反手平仓归零后，先 finalize 并构建 RuntimeStatePackage，再显式调用 vn.py
  `sync_data()`，最后才提交反向开仓腿。
- 集成测试覆盖：两笔部分平仓、第一笔不更新 streak、第二笔只更新一次、先同步再提交
  反手开仓、重复第二笔成交零副作用。

## 四、仍未解决的边界

1. vn.py 当前 `save_json()` 使用 `w+` 直接覆盖，不是临时文件加原子替换。新增检查点
   保证调用顺序，不等于断电级原子持久化。该项在生产启用前仍需独立 durable journal
   或上游原子存储方案。
2. OMS 权益目前是 `get_all_accounts()` 汇总，scope 为 `oms_all_accounts`。单账户单策略
   研究可用；多账户、多策略或手工交易部署前必须冻结账户筛选和资金归属契约。
3. 轻量 `LiveTradingEngine` 仍只有进程内幂等，没有 CTA 等价磁盘恢复。
4. fill 级舍入账本与 position 级胜负分类可出现不同符号，例如两笔 `-0.04` 的 fill
   账本合计为 `0.0`，仓位 raw 合计舍入为 `-0.1` 并计一次 loss。这是有意冻结的两种
   风险统计口径，后续不得把两者混为同一 PnL。

这些边界不阻塞离线 B0/B1 研究，但阻塞 Option D 的生产启用。

## 五、证据边界

- 既有 2018-2026 回放证明：3658 条决策、183 条执行决策、10 笔交易，行为字段
  0 差异。
- 上述既有回放不证明身份 parity，因为它产生于身份修正和身份列导出之前。
- 本轮单测证明稳定身份规则；CTA 集成测试证明部分平仓和反手同步顺序。
- 下一次 B0/B1 完整回放应同时保存身份列，并执行第二次相同输入回放的身份逐列比较。

## 六、下一步

不再进行第四轮宽泛讨论。按 OPEN-005 第二阶段实施：

1. 实现 `risk_gate_mode=enforce|observe|disabled`；
2. 一次评估全部启用门控，输出稳定顺序的 `hard_failures`、`enforced_breaches`、
   `observed_breaches`；
3. 实现共享 `decompose_position_transition(before, target)`，保证平仓腿不被状态门控阻断，
   反手新开腿使用 finalize 后的新风险状态重新审批；
4. 先验证 CURRENT_ENFORCED 行为 parity，再运行 B0/B1 连续状态和 fold-reset 诊断；
5. 人工审查 B0/B1 后才实施 D，并且只比较 D 与 B1。
