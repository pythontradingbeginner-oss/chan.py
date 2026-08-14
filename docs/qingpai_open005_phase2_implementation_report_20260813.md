# 青派缠论 RB 策略 OPEN-005 第二阶段实施报告

日期：2026-08-13  
状态：第二阶段实现与 2018-2026 历史诊断完成，等待人工审查

## 1. 本阶段边界

本阶段只实现已冻结的 OPEN-005 风险门控和仓位转换契约：

1. `risk_gate_mode = enforce | observe | disabled`；
2. `CURRENT_ENFORCED`、`B0_UNINTERRUPTED_STRATEGY_PATH`、
   `B1_CONSECUTIVE_LOSS_ABLATION` 三个固定研究 profile；
3. 一次评估所有已启用状态门控，同时输出 `hard_failures`、
   `enforced_breaches`、`observed_breaches`；
4. 将目标仓位变化统一拆成有序的 close leg 和 open leg；
5. 平仓腿绕过入场状态门控，反手必须先平旧仓并 finalize，再从 flat
   状态重新审批开仓腿；
6. 运行 CURRENT parity、B0/B1 连续流、fold-reset 和回放身份诊断。

本阶段没有修改青派缠论结构算法、BSP 识别、入场评分、多周期确认、动量
确认、出场规则、仓位参数或交易成本；没有实现或运行 Option D。

## 2. 机器契约与实现映射

| 契约 | 实现 | 关键保证 |
| --- | --- | --- |
| OPEN005-GATE-001 | `chan_futures/risk_policy.py`、`chan_futures/risk.py` | 不短路地评估全部启用门控；旧 `reason` 保持首项稳定顺序 |
| OPEN005-PROFILE-CURRENT/B0/B1 | `chan_futures/config.py`、严格 YAML | profile 固定映射，允许显式逐门控 override 并严格校验 |
| OPEN005-EXPOSURE-001 | `chan_futures/position_transition.py` | `0→1`、`2→1`、`1→2`、`2→-1` 使用同一分解函数 |
| OPEN005-REV-001 | 回测、CTA、live engine | close-to-zero → 风险记账/finalize → open-from-zero 重新审批 |
| OPEN005-RA-001/002 | 回测、CTA、live engine | 每个平仓 fill 独立记账；只有仓位归零才更新真实连亏状态 |

## 3. 风险 profile

| Profile | 累计亏损 | 日内亏损 | 连续亏损 | 最大回撤 |
| --- | --- | --- | --- | --- |
| CURRENT_ENFORCED | enforce | enforce | enforce | enforce |
| B0_UNINTERRUPTED_STRATEGY_PATH | observe | observe | observe | observe |
| B1_CONSECUTIVE_LOSS_ABLATION | enforce | enforce | disabled | enforce |

B0 不是纯 Alpha，也不是“无风控”：风险 session、权益可用性、仓位转换合法性、
`max_abs_position` 等硬前提仍然强制执行。B1 的实验变量只有连续亏损门控，其他
门控与 CURRENT 一致。

## 4. 实现审计补项

在实现复查中一并补齐两个当前一手正式配置不会触发、但属于统一契约的多手边界：

1. 同向加仓使用增量 fill 数量和加权持仓上下文，不重建既有仓位身份；
2. 部分平仓逐 fill 写入风险账本，仓位级 PnL 跨 fill 聚合，到数量归零时才
   `finalize_position`。

fold 报告只汇总 `[test_start, test_end)`，不把每折 800 根预热 K 线计入 OOS。

## 5. 自动化验证

定向覆盖包括：

- 多门控同时违约及稳定顺序；
- B0 observe 与硬前提失败关闭；
- B1 只关闭连续亏损门控；
- 平仓/减仓绕过状态门控；
- 仓位转换表与反手开仓腿重新审批；
- 多 fill 风险记账、幂等、重启恢复和仓位 finalize；
- CTA 反手 checkpoint 后重新审批；
- 回放稳定身份与零交易证据不足标记；
- fold 预热区间排除；
- CURRENT/B1 单实验变量和首个执行路径分叉。

OPEN-005 及相关执行路径最终定向回归为 72/72 通过，其中第二阶段聚合器专项
测试为 8/8 通过。完整 `pytest tests -q` 在显式接入本机 riskmanager 源码后得到
501 passed、27 failed：其中 25 个失败来自本机 `vnpy_riskmanager` 版本缺少
仓库测试所需的 `RiskEngine.load_rules_from_folder` 等 API，另外 2 个是本轮开始前
已存在的图表函数导入身份失败；本轮 OPEN-005 测试无失败。

## 6. 2018-2026 结果

机器产物目录：`reports/open005_phase2/20260813_full/`。

数据范围为 2018-03-23 21:15 至 2026-08-03 23:00，共 44,826 根 RB 15m
K 线。

### 6.1 连续状态

| Profile | 决策 | 执行决策 | 风险拒绝 | 交易 | 净点数 | 交易曲线最大回撤 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| CURRENT | 3,658 | 183 | 173 | 10 | -133 | 133 |
| B1 | 3,658 | 183 | 120 | 63 | -442 | 444 |
| B0 | 3,655 | 182 | 0 | 182 | -37 | 753 |

CURRENT 的 173 次风险拒绝全部来自 `max_consecutive_losses: 5 >= 5`。B1
只关闭该门控后继续执行 53 笔，但在 2020 年被 `max_drawdown: 4.57% >=
3.10%` 阻断 120 次；相对 CURRENT 多亏 309 点。B0 的终值虽然为 -37 点，
但路径最低约 -751 点、最大交易曲线回撤 753 点，不能把较好的终值解释为可交易。

B0 年度净点数为：2018 -36、2019 -135、2020 -413、2021 +161、2022
+297、2023 +149、2024 +5、2025 -17、2026 -48。较好的终值来自承受
2018-2020 巨大亏损后在 2021-2023 恢复，而不是稳定优势。

### 6.2 六折 fold-reset

六个不重叠测试窗覆盖 2020-03-23 至 2026-03-23。fold 不拟合或调整参数；
每折使用 800 根前置 K 线预热结构，风险状态从空状态开始。测试窗结束后停止新
入场，并继续最多 800 根 K 线让窗口内持仓自然退出。汇总前会检查窗口内开仓交易；
若仍以 `end_of_data` 人工平仓，该 fold 不视为完整并必须延长回放或标记无效。
本次 B0/B1 共 12 份 fold 均无 `end_of_data` 交易。

| Fold | 测试起点 | 交易 | B0 点数 | B1 点数 |
| ---: | --- | ---: | ---: | ---: |
| 1 | 2020-03-23 | 25 | -197 | -197 |
| 2 | 2021-03-23 | 16 | +397 | +397 |
| 3 | 2022-03-23 | 39 | +287 | +287 |
| 4 | 2023-03-23 | 10 | +4 | +4 |
| 5 | 2024-03-23 | 12 | +7 | +7 |
| 6 | 2025-03-23 | 1 | -19 | -19 |
| 合计 |  | 103 | +479 | +479 |

B0 连续 OOS 与 fold-reset 的 2,683 条结构决策、103 笔交易和 206 个 fills
完全一致；62 条执行审计差异只来自 observed breach 状态，不改变成交。B1 连续
OOS 因 2018-2020 累积回撤状态而为 0 笔，fold-reset 则恢复为 103 笔、+479
点。这证明差异来自跨年度风险状态，不是青派结构决策发生变化。B0 与 B1 在六个
年度 fold 中没有执行分叉，因此 fold-reset 只能诊断状态路径依赖，不能识别关闭
连续亏损门控的消融效应；该效应的直接证据来自连续流 CURRENT 与 B1 的比较。

`+479` 是 103 笔历史样本的正收益，属于值得继续验证的优势迹象，不构成统计上已经
证明未来正期望。收益主要集中在 Fold 2 和 Fold 3，后续实验仍需披露分折分布。

### 6.3 Parity 证据

以下证据门槛全部满足：

1. CURRENT 对第一阶段基线的 3,658 决策、183 执行决策、10 交易、20 fills
   逐行零差异；
2. CURRENT 双跑的 10 个非空 `position_id`、`close_fill_id`、
   `close_order_id` 完全一致；
3. B0 与四个状态门控全部 disabled 的参考路径在 3,655 决策、182 执行、
   182 交易、364 fills 上交易行为完全一致；两者的 observed-breach 审计字段按定义
   不同，不属于行为 parity；
4. B1 配置差异只有 `max_consecutive_losses: enforce -> disabled`；
5. CURRENT 与 B1 的结构 trace 在 3,658 行上零差异，首个执行分叉为
   2018-08-24 21:30 的第 5 连亏拒绝；
6. B0/B1 连续状态与 fold-reset 的结构、执行、交易和 fills 差异均已披露。

B0 相对 CURRENT 少 3 条结构决策，是因为 2021-03-16、2021-08-02 和
2022-03-15 对应时点 B0 已有活动持仓；这是成交路径导致的持仓占用，不是数据缺失。

## 7. 当前决策边界

Option D 仍为 `contract_only_not_implemented`。第二阶段结果支持继续研究“跨
session 暂停后受控试探恢复”，但不支持直接删除连续亏损或最大回撤门控。人工审查
本报告及 B0/B1 机器产物之前，不实施 Option D，也不调整任何交易策略参数。
