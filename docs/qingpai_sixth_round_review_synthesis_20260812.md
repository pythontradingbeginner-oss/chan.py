# 青派缠论与 RB 交易策略第六轮交叉审查综合裁决

日期：2026-08-12  
审查输入：`审查基线_0812_6A_gemini.md`、`审查基线_0812_6B_deepseek.md`  
审查对象：`docs/qingpai_fifth_round_review_synthesis_20260812.md`  
范围：OPEN-005、风险事件口径、B/D 实验前置条件；不修改策略行为

## 一、结论

两份审查均对准第五轮综合裁决，核心事实和方向已形成可靠共识。第五轮裁决继续有效：

- B 是必须运行的对照实验，但必须更正“纯净 Alpha 基线”的命名。
- D 只进入实验契约设计，不成为生产默认。
- A 保留为账户级人工安全闩。
- C 不作为正式候选继续推进。
- 完整逻辑持仓是连续亏损的观察单位，部分平仓 fill 不是。
- `PROBE_IN_FLIGHT` 必不可少，但四状态仍不足以覆盖未成交订单生命周期。
- OPEN-005 原始范围还包括累计亏损和最大回撤；不能只解决连续亏损就宣告关闭。

本轮仍不实施 D，不修改策略代码。下一步也不应直接把 Gemini 给出的 D 边界值写入规则。

## 二、对 Gemini 6A 的裁决

### 通过

1. 接受 `completed_position_net_pnl` 作为连续亏损观察单位。
2. 接受首个真实开仓成交后 probe 已不可再次使用；拒单、撤单和零成交不能算作一次已完成的 probe。
3. 接受 `PROBE_IN_FLIGHT` 状态。
4. 接受 B 先于 D、D 先于 P1、P1 先于经济性实验的顺序。

### 只作为候选，不通过为正式规则

1. “probe 权限在 session 结束时过期”没有理论或数据依据，而且与“下一 session 重新赋权”组合后语义近似空转。更简洁的候选是：达到可用日期后，权限持续到首个真实 probe 成交。
2. “费用后净收益小于等于零都算失败”混合了亏损与持平。建议审计上保留三值结果 `win/loss/flat`；`flat` 不重置连亏，也不增加连亏，并回到暂停状态。该建议仍需人工裁决。
3. probe 亏损后“保留连亏计数”不够精确。实际亏损序列应继续从 5 增至 6、7；策略门控状态与真实 streak 计数应分字段保存，不能把计数饱和在阈值。
4. 四状态只覆盖已成交持仓，没有覆盖订单已提交、尚未成交的窗口。CTA/live 至少需要 `PROBE_PENDING_ENTRY`，或等价的持久化 reservation/order identity。

## 三、对 DeepSeek 6B 的裁决

### 通过

1. 第五轮三项契约空白均为真实代码问题。
2. OPEN-005 的原始范围确实同时包含 `max_consecutive_losses`、`max_loss_points` 和 `max_drawdown_pct`。
3. 当前 `max_loss_points` 没有显式恢复事件。在严格配置的一手整仓路径中，达限后已空仓且禁止新开仓，因而形成实际吸收态；通用多手路径若仍有剩余仓位，后续不受门控的平仓 PnL 理论上仍可能使累计值回到阈值内。
4. 当前 `max_drawdown_pct` 只有在观测权益回升到阈值内时才可能恢复；当策略已空仓且禁止新开仓时，单策略回测中的恢复通常不可达。未平仓头寸或实盘 OMS 账户中其他策略带来的权益变化则可能恢复，因此其行为还依赖 `equity_scope`。

### 已消除的疑点

DeepSeek 提出的 CTA `_calc_pnl()` 与 `PositionContext.pnl_points()` 公式差异疑点，经源码复核目前不存在：

- 回测直接调用 `PositionContext.pnl_points()`；
- CTA 直接调用 `PositionContext.pnl_points()`；
- live `_calc_pnl()` 也委托给同一个 `PositionContext.pnl_points()`。

三端当前的主要差异是调用时机和聚合粒度，不是 PnL 公式。仍需测试多次开仓成交的平均成本、部分平仓和费用归集，但不能把它表述为已经确认的公式错误。

## 四、必须纠正的风险事件模型

Gemini 建议“中间 fill 只更新持仓，不触发风险计数”还不够精确。当前 `RiskManager.on_fill()` 同时承担四项职责：

1. 累加全局已实现 PnL；
2. 累加当前交易日已实现 PnL；
3. 更新连续亏损次数；
4. 观测权益。

这些职责的正确事件粒度不同。若把整个 `on_fill()` 延迟到持仓完全平仓，会导致跨 session 部分平仓的日内亏损被记到错误交易日，也会延迟累计亏损安全门控。

建议冻结为以下事件模型：

```text
record_close_fill(fill_id, position_id, pnl, risk_session)
  -> 每个真实平仓 fill 更新 realized_points 和 daily_realized

finalize_position(position_id, completed_position_net_pnl)
  -> 仅在逻辑持仓归零时更新 consecutive_losses

observe_equity(account_equity, source, scope)
  -> 按 bar/OMS 观测更新 peak equity 和 drawdown
```

这是一项 parity 修复，不应改变当前一手、整仓退出路径的历史决策。实施时必须用旧结果做 exact parity 回归。

## 五、B 基线必须拆成两种

两份附件都把 B 称为“纯净 Alpha 基线”，这个说法不准确。第五轮定义的 B 只关闭 `max_consecutive_losses`，仍保留：

- `max_loss_points=600`；
- `daily_loss_limit=300`；
- `max_drawdown_pct=3.1%`。

当前连续结果只走了 10 笔交易，累计 -133 点、权益最低 98,670，尚未触发另外两个长期门控。但关闭连亏门控后，新的交易路径可能先触发累计亏损或最大回撤并再次进入吸收态。

因此必须拆分：

### B1：连续亏损单变量消融

只关闭 `max_consecutive_losses`，其他规则完全不变。D 与 B1 比较，回答：

> 在相同其他风险条件下，Next-Session Probe 相对关闭连亏门控产生了什么增量效果？

B1 是 OPEN-005/D 的因果对照，但不是纯净 Alpha。

### B0：信号链无状态门控诊断

将 daily loss、consecutive loss、cumulative loss 和 drawdown 设为 shadow-only：继续计算并记录 `would_reject`，但不阻断入场。保留结构止损、仓位计算、资金约束、费用、滑点和 `max_abs_position`。

B0 回答：

> 当前信号、入场、退出链在不被账户状态门控截断时，完整历史路径的经济性如何？

B0 才接近“纯净 Alpha 诊断”。它是研究报告，不是可直接实盘部署的配置。

## 六、OPEN-005 的三类门控裁决

### 1. 连续亏损

- 策略级过滤器候选：D。
- 因果基线：B1。
- 信号链诊断：B0。
- 正式默认：仍未裁决。

### 2. 累计亏损

`max_loss_points` 是账户级长期底线，不应依赖“碰巧仍有剩余仓位并随后盈利”作为恢复机制。生产候选语义应是人工复位安全闩，并记录触发时间、触发权益、授权人/原因和新 epoch。研究中使用 shadow-only B0，另报告硬停止路径。

此外，账户安全底线长期使用“点数”会随手数和合约暴露改变含义。正式生产契约应优先使用账户货币或权益比例；现有 points 规则在迁移前保持兼容。

### 3. 最大回撤

`max_drawdown_pct` 也应视为账户级安全闩。生产环境是否允许因其他策略带来的 OMS 权益回升而自动解锁，必须明确；默认建议不自动解锁，采用人工复位和新 peak epoch。研究中同时报告 shadow breach 和 hard-latch 结果。

### 4. 日内亏损

已完成 RB 交易日日切，继续作为 session 级账户保护。其阈值有效性以后单独研究，本轮不改参数。

## 七、D 的候选状态机

进入盲审的候选模型应至少包含：

```text
ACTIVE
PAUSED_UNTIL_NEXT_SESSION
PROBE_AVAILABLE
PROBE_PENDING_ENTRY
PROBE_IN_FLIGHT
```

推荐送审但尚未批准的转移原则：

- 达到 streak 阈值后，禁止本 session 后续新开仓；
- 后续 session 首个符合原策略的候选可申请 probe；
- 订单提交后建立 reservation，撤单且零成交时释放 reservation；
- 首个开仓 fill 后进入 `PROBE_IN_FLIGHT`；
- 逻辑持仓完整平仓后，以聚合费用后净 PnL 解析 `win/loss/flat`；
- win 清零 streak 并恢复 ACTIVE；
- loss 增加真实 streak，flat 保持 streak，两者均回到暂停；
- probe 权限无信号时是否跨 session 保留，仍由人工裁决。

## 八、最终推进顺序

1. 不再做宽泛 AI 复审；下一轮只盲审风险事件契约、B0/B1 定义和 D 状态转移表。
2. 先实现风险记账事件与连续亏损观察单位的分离；一手旧路径必须 exact parity。
3. 实现 shadow-only 审计模式，分别运行 B0 与 B1 的 2018-2026 连续流和 fold 诊断。
4. 在看到 D 收益前冻结 D 的状态机、Schema 和测试矩阵。
5. 只实施 D，比较 D vs B1；B0 仅作为信号链参照。
6. 同一报告并列展示 shadow breach、hard-latch、连续流和 fold 诊断，避免把被截断样本包装成完整策略表现。
7. OPEN-005 三类门控全部关闭后，再进入 P1 Setup 生命周期。
