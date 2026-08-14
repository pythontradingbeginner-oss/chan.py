# 青派缠论 RB 策略 OPEN-005 Option D 实施与实验报告

日期：2026-08-13  
状态：研究实现及定向工程审查完成；工程通过，不批准生产采用

## 1. 结论先行

Option D 已按冻结契约实现，并通过状态机、持久化、成交幂等、部分成交、反手顺序、
回测身份和 D/B1 单变量隔离验证。工程层面可以进入定向交叉审查。

策略效果层面暂不批准用 D 替换现行风控：

- 2018-2026 连续流中，D 相对直接控制组 B1 少做 3 笔交易，净点数从 `-442`
  改善到 `-422`，差值 `+20`；但二者仍然亏损；
- 六折 fold-reset 中，B1 为 `103 笔 / +479`，D 为 `102 笔 / +478`，D 反而
  少 `1` 点；
- D 的改善只出现在特定连续亏损路径，尚无跨 fold 的稳定增益证据。它是风险恢复
  状态机，不是新增 Alpha。

## 2. 前置研究工具修复

在运行 D 前完成并验证两项交叉审查要求：

1. 决策窗统一为左闭右开 `[test_start, test_end)`；
2. fold 写出后立即检查窗口内开仓交易，若仍以 `end_of_data` 强平，则撤销完成标记
   并使运行失败；缓存复用也执行同一检查。

旧 B0/B1 六折在 12 个右端点均无决策，且 12 个 fold 均无 `end_of_data`，所以
原有 `103/+479` 数值不受修复影响。本次 D 六折全部通过新完整性护栏。

## 3. Option D 实现

### 3.1 状态机

实现四个持久化状态：

`ACTIVE -> PAUSED_UNTIL_NEXT_SESSION -> PROBE_AVAILABLE -> PROBE_IN_FLIGHT`

- 普通仓位结算为亏损且真实连亏达到阈值时，记录结算 risk session，并暂停到 RB
  日历中的下一有效交易日；
- 到达可恢复 session 后只开放一个 probe epoch；未使用的资格跨 session 保留；
- 下单前持久化完整 `TradeIntent` 预约，绑定订单 ID；零成交终态释放预约；
- 首笔开仓 fill 才消费 probe 资格并绑定 `position_id`；部分成交后撤余单仍保持
  `PROBE_IN_FLIGHT`；
- 若 broker 先回报终态订单及累计成交、后发送 trade，订单进入
  `TERMINAL_PENDING_TRADES`，预约与完整 intent 保留到 trade 对账完成；不得误按
  零成交释放资格；
- 探针仓位结算为 win 时返回 `ACTIVE`；loss 或 flat 时重新暂停到下一 session。

### 3.2 记账与反手顺序

Option D 复用 OPEN-005 的逐 fill 风险账本和仓位级 finalize：真实连亏只在仓位归零
后更新一次。反手固定执行：先平旧仓、记账并 finalize，再从 flat 状态重新审批开仓腿。
探针 loss/flat 会取消尚未提交的反手开仓腿，平仓腿不受入场状态门控阻断。

### 3.3 持久化和恢复

D 使用 `open005-risk-runtime-v2`，持久化 regime、session、probe epoch、订单、仓位和
完整预约 intent。D 恢复入口只接受完整 v2；旧 v1 或缺字段快照 fail-closed，防止把
未知探针状态错误恢复为 `ACTIVE`。非 D profiles 继续使用 v1，兼容路径不变。

非法或不完整的 D 快照恢复现为调用者可见的原子操作：校验失败时恢复前的
`RiskManager` 状态逐字段保持不变，不暴露“新记账字段 + 旧 Option D 状态”的混合态。
回测、CTA 和预发布 live 路径统一保存实际送审的 post-transition open intent。

### 3.4 已知持久化边界

vn.py 的策略变量同步不是操作系统级事务文件提交。若进程恰好在风险状态、持仓状态和
订单状态写入之间崩溃，重启恢复会 fail-closed，阻止新增敞口并要求 OMS、活动订单和
broker fill ID 对账；系统不会把不完整状态猜测为 `ACTIVE`，也不会自动重发探针订单。
这是已记录的恢复流程，不代表存储层已经具备 ACID 事务。

`vnpy_chan.live_engine.LiveTradingEngine` 是预发布事件引擎，不是当前生产宿主。它已补
`ALLTRADED` 先于 trade 的探针对账，但尚未具备 CTA 路径完整的持久化与 OMS 恢复门禁；
当前生产宿主仍只认 `vnpy_chan.chan_bsp_strategy.ChanBspStrategy`。

## 4. 实验隔离

直接控制组严格限定为 B1：

| Profile | 累计亏损 | 日内亏损 | 连续亏损 | 最大回撤 | 额外规则 |
| --- | --- | --- | --- | --- | --- |
| B1 | enforce | enforce | disabled | enforce | 无 |
| D | enforce | enforce | disabled | enforce | 冻结的暂停/探针恢复状态机 |

严格 YAML SHA-256、数据起止、44,826 根 RB 15m bars 均完全一致；未修改青派结构、
BSP、入场、出场、仓位、成本或阈值。B1 与 D 的 3,658 条结构决策逐行零差异。

## 5. 2018-2026 连续流

| 指标 | B1 | D | D - B1 |
| --- | ---: | ---: | ---: |
| 结构决策 | 3,658 | 3,658 | 0 |
| 执行决策 | 183 | 183 | 0 |
| 风险拒绝 | 120 | 123 | +3 |
| 交易 | 63 | 60 | -3 |
| fills | 126 | 120 | -6 |
| 净点数 | -442 | -422 | +20 |

D 产生 5 笔探针交易。三次同 session 暂停恰好避开 B1 的三笔交易：

| 时间 | 事件 | B1 PnL | 原因 |
| --- | --- | ---: | --- |
| 2019-06-03 11:30 | `RB1910_15m_bi36_long_2s_r7` | -13 | structure_stop |
| 2019-06-26 14:45 | `RB1910_15m_bi57_short_2_r3` | -7 | structure_stop |
| 2019-08-23 14:45 | `RB1910_15m_bi103_short_2s_r5` | 0 | contract_rollover |

共同执行的交易 PnL 无差异，因此连续流 `+20` 完全可归因于冻结的暂停规则，而非
结构或成交计算漂移。D 随后与 B1 一样被最大回撤门控长期阻断：B1 的触发回撤为
`4.57%`，D 因避开 20 点亏损变为 `4.37%`，两者均高于 `3.10%`。

## 6. 六折 fold-reset

| Fold | B1 交易 | D 交易 | B1 点数 | D 点数 | D-B1 | D 探针 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 25 | 24 | -197 | -198 | -1 | 5 |
| 2 | 16 | 16 | +397 | +397 | 0 | 0 |
| 3 | 39 | 39 | +287 | +287 | 0 | 0 |
| 4 | 10 | 10 | +4 | +4 | 0 | 0 |
| 5 | 12 | 12 | +7 | +7 | 0 | 0 |
| 6 | 1 | 1 | -19 | -19 | 0 | 0 |
| 合计 | 103 | 102 | +479 | +478 | -1 | 5 |

只有 Fold 1 触发 D，并出现一次暂停拒绝；其余五折完全相同。fold-reset 是风险状态
敏感性诊断，不是参数拟合，也不证明未来正期望。`+478` 仍主要由 Fold 2、3 贡献。

## 7. 自动化与身份验证

- Option D、风险记账、CTA/live 乱序恢复、订单状态、runner、机器契约和 P7 发布守卫
  本轮扩展定向回归 `127 passed`；
- fold 右端点、缓存完整性、D/B1 连续及 fold 对照器测试通过；
- `compileall` 和 `git diff --check` 通过；
- D 独立全历史复跑与主运行逐文件完全一致：3,658 decisions、183 execution
  decisions、60 trades、120 fills 零差异；`position_id`、close fill/order ID、
  probe epoch 和 probe position 均稳定；
- 完整 `pytest tests -q` 在接入本机 riskmanager 源码后，修复发布清单哈希前为
  `520 passed / 28 failed`。其中唯一新增失败是本轮修改 CTA 源码后清单哈希未同步，
  已更新并定向复测通过；其余 27 项与此前记录一致：25 项来自本机
  `vnpy_riskmanager` API 版本不兼容，2 项是既有图表导入身份失败。未在修复哈希后
  完成一次完整套件；2026-08-14 再次尝试时在 420 秒上限内未结束并被终止，因此
  没有新的全量统计。定向 `127 passed` 与全量状态必须分开解读。

机器总表：`reports/open005_option_d/20260813_d_vs_b1_final/option_d_evaluation.json`。

## 8. DeepSeek 定向审查处置

2026-08-14 定向审查给出“工程正确性 PASS，无 P0/P1；策略不批准生产”。逐项处置：

- P2-1 接受并修复：`RiskManager.load_state` 失败后回滚，新增非法 D 快照原子性测试；
- P3-1 接受并文档化：明确非事务文件写入的崩溃窗口和 fail-closed 对账流程；
- P3-2 部分接受并修复：预发布 live 路径补所有终态（含 `ALLTRADED`）先于 trade 的
  预约保留，同时明确它不是生产宿主；
- P3-3 接受并修复：新增 `REJECTED -> late OPEN fill` 测试，迟到成交不改变业务状态，
  CTA 进入 `RECOVERY_REQUIRED`；
- P3-4 接受并修复：三条路径均保存实际执行审批使用的 open intent 快照。

上述修复不改变青派结构、BSP、入场、出场、风险阈值、历史数据或 D/B1 配置，因此
既有连续流 `+20`、fold-reset `-1` 的策略研究结论不变。

## 9. 审查与后续决策

本轮定向审查已经完成并闭环。后续研究重点仍是：

1. v2 Schema、预约 intent 和重启 fail-closed 是否完整；
2. 首 fill 消费探针、零 fill 释放及部分 fill 语义；
3. 探针反手的 close/finalize/reapprove 顺序；
4. D/B1 只有恢复状态机一个实验变量；
5. `+20` 连续流改善与 `-1` fold 差异的研究解释是否克制。

当前建议：保留 D 为研究 profile，不进入生产，不调整参数。工程修复完成后应回到
交易策略主线：先对 Alpha/退出机制做可归因的机制假设，再决定是否扩展更长历史、
不同品种或周期作为外部验证；不再为 Option D 进行宽泛交叉审查。
