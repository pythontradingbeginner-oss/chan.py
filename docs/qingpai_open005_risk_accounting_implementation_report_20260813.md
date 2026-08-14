# 青派 OPEN-005 风险记账拆分实施报告

日期：2026-08-13  
上游契约：`configs/qingpai_open005_machine_contract_v1.json`  
Runtime Schema：`configs/qingpai_open005_runtime_schema_v1.json`  
测试矩阵：`configs/qingpai_open005_test_matrix_v1.json`

## 一、结论

OPEN005 第一实施阶段已经完成：平仓成交记账与仓位结果结算不再是同一事件。
`record_close_fill` 以 `fill_id` 幂等更新成交级累计 PnL 和仓位聚合器；
`finalize_position` 只在仓位数量归零后按完整仓位 raw PnL 更新一次真实连续亏损。

旧的一手整仓策略行为保持不变。2018-03-23 至 2026-08-03 完整回放仍为
3658 条决策、183 条执行决策、10 笔交易、20 条 fill，净点数 -133，最大回撤
133 点。与 2026-08-12 G06 基线相比，所有行为字段为 0 差异。

## 二、实现范围

### 风险记账核心

- 新增 `CloseFillRecord`、`PositionPnlAggregator`、`PositionFinalizeResult`。
- `record_close_fill` 先拒绝重复 `fill_id`，再推进风险 session、逐 fill 舍入并更新
  `realized_points`、`daily_realized` 和仓位聚合器。
- `finalize_position` 只接受 `remaining_quantity=0` 的聚合器；完整仓位先汇总 raw PnL，
  再舍入一次判定 win/loss/flat，并且只更新一次 `true_consecutive_losses`。
- 旧 `on_fill` 保留为兼容包装器，一次调用仍代表一个完整的一手仓位结果。

### 身份与运行时接线

- `PositionContext` 新增稳定 `position_id`，首次开仓生成，部分加仓/减仓和 CTA 恢复均保留。
- 回测身份由稳定事件、Setup、合约和 fill 数据派生，排除随机 `decision_id`；CTA/live
  由第一笔已接受开仓成交身份派生。
- 回测使用确定性的 synthetic fill/order ID，并在 `trades` 导出
  `position_id`、`close_fill_id`、`close_order_id` 供后续身份 parity。
- CTA 和轻量 live 使用 vn.py `vt_tradeid`、`vt_orderid`；缺失身份失败关闭。
- CTA 与轻量 live 的部分平仓均先记账，重复成交在 PnL 和持仓数量变化前退出，仓位归零后再 finalize。

### 持久化

- 风险快照保存 `processed_close_fill_ids`、`active_position_aggregators`、
  `finalized_position_ids`、`true_consecutive_losses` 和旧兼容字段。
- CTA 的既有 `RuntimeStatePackage.risk` 已承载上述状态；部分平仓后重启可恢复聚合器并拒绝成交重放。
- `RiskManager.get_state()` 已通过冻结 JSON Schema 的实例校验。
- accounting-only v1 不再输出 D 占位；D 目前只有契约定义，不包含运行时行为。
  真正实施 D 时必须升级到 `open005-risk-runtime-v2`。
- CTA 反手归零后显式调用 vn.py `sync_data()`，再提交反向开仓。该顺序检查点不等于
  断电级原子持久化：当前 vn.py `save_json()` 仍是 `w+` 直接覆盖。

## 三、测试和回放证据

- 风险记账、契约、session、CTA 恢复和订单接线定向集合：62 passed。
- 第九轮修正后的扩大回归（身份、Schema、CTA 部分平仓、恢复、订单机、runtime
  parity、session、机制审计）：104 passed。
- OPEN005 测试矩阵：RA-001 至 RA-005、PARITY-001 已从
  `not_implemented` 更新为 `passing`。
- 完整回放产物：
  `reports/rule_mechanism_audit/20260813_open005_risk_accounting/`。
- 机器可读 parity：
  `reports/rule_mechanism_audit/20260813_open005_risk_accounting/open005_parity.json`。

逐字段结果：

| 文件 | 行数 | 行为差异行 | 随机身份差异 |
| --- | ---: | ---: | ---: |
| decision_trace.csv | 3658 | 0 | 0 |
| execution_decisions.csv | 183 | 0 | decision_id 183 行 |
| trades.csv | 10 | 0 | decision_id 10 行 |
| fills.csv | 20 | 0 | 0 |

`decision_id` 由现有策略评估代码每次运行生成随机 UUID，因此不可能跨独立回放逐值相同；
对应 `event_id`、`signal_key`、时间、接受理由、执行状态、成交与盈亏全部相同。这里将其
单列为随机身份元数据，不计入行为差异。

这份完整回放发生在稳定身份修正及身份列导出之前，因此只证明行为 parity，不证明
`position_id`/`fill_id` 的跨运行一致。稳定身份目前由定向单测证明；下一次 B0/B1
完整回放必须增加两次相同输入的身份逐列比较。

另需明确：fill 级规范化账本与 position 级胜负分类是两个有意不同的口径。多笔极小
PnL 可能在 fill 账本合计为 0，却在 position raw 合计舍入后分类为 win/loss；两类门控
不得互相替代。

完整 `pytest -q tests` 结果为 509 passed、2 failed、44 warnings；风险记账相关均通过，
P7 发布清单中的策略源码 SHA256 已随本轮正式修改同步。两个失败均与本次改动无关，
分别位于 `tests/test_dc_peak_valley_chart.py` 和 `tests/test_strategy_trade_year_chart.py`，
都要求同一个图表函数经顶层与 `charts` 子包导入后保持对象身份相同。

## 四、明确未实施

- 尚未实现 `risk_gate_mode=enforce|observe|disabled` 和多 breach 审计。
- 尚未实现统一的 `decompose_position_transition`。
- 尚未运行 B0/B1 连续流和 fold 诊断。
- 尚未实现或启用 Option D。
- 轻量 `LiveTradingEngine` 仍没有 CTA 等价的磁盘运行时快照设施；本轮实现其内存幂等
  接线，但不能把它宣称为跨进程持久化完成。正式可恢复运行路径仍是 CTA adapter。
- CTA 当前虽有反手前同步检查点，但 vn.py 策略 JSON 文件不是原子替换写入；生产级
  durable journal 仍未完成。
- OMS 权益 scope 仍是 `oms_all_accounts`；多账户、多策略生产部署前必须冻结账户筛选。

## 五、下一步

按冻结顺序进入 OPEN005 第二实施阶段：实现风险门控模式和全量 breach 审计，先验证
CURRENT_ENFORCED 行为零差异，再生成 B0/B1 profile；随后运行 B0/B1 连续流与 fold
诊断。Option D 继续后置，并且未来只与 B1 做直接比较。
