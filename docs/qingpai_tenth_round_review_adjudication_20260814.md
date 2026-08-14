# 青派缠论 RB 策略第十轮定向审查裁决

日期：2026-08-14  
审查来源：DeepSeek v4 Pro 定向审查  
范围：Option D 状态机、持久化、乱序成交、反手顺序与 D-vs-B1 实验隔离

## 1. Codex 独立结论

DeepSeek 的总裁决成立：审查时的 Option D 工程实现无 P0/P1；状态机 T01-T08、反手
先平后审、D-vs-B1 单变量隔离和研究结论均有源码、测试与机器产物相互印证。

其策略裁决也成立：Option D 不批准生产。连续流 D 相对 B1 改善 `+20`，但二者仍为
`-422/-442`；六折 fold-reset 为 `-1`。这些数据只支持“风险恢复状态机可解释”，
不支持“形成稳定 Alpha”或“可替换现行策略”。

## 2. 发现处置

### P2-1 接受并修复：恢复原子性

`RiskManager.load_state` 原先可能先覆盖记账字段，再因 Option D 不变量失败而留下半恢复
对象。现改为失败时回滚到调用前完整快照。新增测试刻意同时篡改 `realized_points` 和
`PROBE_IN_FLIGHT` 身份，确认异常后 `get_state()` 与恢复前逐字段相同。

### P3-1 接受并文档化：文件提交边界

内存恢复原子性不等于磁盘 ACID。vn.py 策略变量同步仍存在进程崩溃窗口。正式契约和
实施报告现明确：重启遇到不完整快照必须 fail-closed，禁止新增敞口，并通过 OMS、
活动订单和 broker fill ID 对账恢复。

### P3-2 部分接受并修复：两条 live 路径

当前生产宿主明确为 `ChanBspStrategy`；`LiveTradingEngine` 标记为预发布事件引擎，
在具备 CTA 等价持久化与 OMS 恢复门禁前不得用于生产。其探针订单路径仍补齐了所有
终态（含 `ALLTRADED`）先于 trade 的预约保留和后续成交对账，避免保留代码继续漂移。

### P3-3 接受并修复：REJECTED 后迟到成交

新增 `REJECTED -> late OPEN fill` 对抗测试。零成交拒单先释放探针预约；迟到成交不
修改持仓或风险状态，CTA 写入 `unreconciled_open_fill` 恢复原因、同步 checkpoint，
并阻断后续新增敞口，等待 broker/OMS 人工对账。

### P3-4 接受并修复：预约意图统一

新增 `TradeIntent.for_open_leg()` 作为共享构造入口。回测、CTA 和预发布 live 均保存
同一 post-transition 开仓腿意图：`action=open_long/open_short`，有符号目标仓位等于
开仓腿数量。该修复对当前一手历史路径等价，但消除了未来加仓和反手时的语义分叉。

## 3. 验证与影响边界

- 新增原子恢复、`CANCELLED/ALLTRADED -> trade`、`REJECTED -> late fill`、统一开仓腿
  意图测试；
- OPEN-005、订单状态、恢复、runner、机器契约和生产守卫定向回归 `127 passed`；
- JSON 机器契约、测试矩阵、v2 Schema 和发布清单均可解析；
- `compileall` 与 `git diff --check` 通过；
- 完整 `pytest -q tests` 在 420 秒命令上限内未完成，已终止残留测试进程，故本轮没有
  新的全量通过/失败统计；不得把定向通过误写为全套通过；
- 未修改青派结构、BSP、入场/出场规则、风险阈值、数据范围或 D/B1 profile；
- 当前 `max_abs_position=1` 下，统一快照前后内容等价，因此既有 D-vs-B1 历史结果不变。

## 4. 后续裁决

本轮定向工程审查已闭环，不需要再进行第十一轮宽泛 AI 交叉审查。保留 Option D 为研究
profile，继续维持 `production_adoption=not_approved`。下一阶段应回到交易策略本身：
围绕 Alpha 来源、退出机制和市场状态适配提出可证伪假设，再用冻结基线做隔离实验；
更长历史和跨品种/周期只作为外部稳健性验证，不应用来继续调 Option D 参数。
