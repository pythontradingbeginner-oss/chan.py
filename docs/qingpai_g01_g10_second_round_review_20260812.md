# 青派 G01-G10 第二轮交叉审查裁决

更新时间：2026-08-12

本轮审查对象：

- `审查基线_0811_2A_gemini.md`
- `审查基线_0811_2B_deepseek.md`
- `审查基线_0811_2C_glm5.2.md`

被复审文件：`docs/qingpai_g01_g10_cross_review_synthesis_20260811.md`

本轮目的不是再次投票，而是判断三份复审提出的新增事实、修正意见和实施建议能否成立。与上一轮五份文本相比，本轮是 Gemini、DeepSeek、GLM 三个模型家族各一份，独立性更高。

## 1. 总体结论

三份复审对上一轮综合稿的核心结论形成了真实收敛：

1. G06 日亏日切是 P0 实现缺陷，必须先修复，且不能再依赖成交触发恢复。
2. G03/G08 应从隐式 `_consumed_keys` 增删升级为显式 setup 生命周期。
3. G01/G02/G05/G07 暂不改变正式交易行为，只补契约或研究审计。
4. G04/G09/G10 维持当前版本默认，不进入当前正式策略修改。
5. 任意 ATR、MACD 比例、冷却根数和重入次数仍无 OOS 证据，不能写入正式规则。
6. 当前 holdout 已经被反复查看，不能再承担最终参数选择后的未见验证。

这说明第一轮综合稿的总体架构无需推翻。但三份复审新增了四个必须吸收的工程问题：

- CTA 路径缺少逐 bar `risk.observe_equity()`，最大回撤峰值与回测可能不一致。
- 风险日切必须按 RB `trading_day/session`，不能简单使用自然日 `timestamp.date()`。
- G08 必须映射实际 `reason_codes`，不能只写概念分类。
- G07 的反向退出谓词在回测、CTA、轻量 live engine 三处重复，存在长期漂移风险。

## 2. 对三份复审的评价

### Gemini

**可接受部分**：

- 完整接受了“源码事实 > 契约 > 研究假设”的证据顺序。
- G01/G02/G04/G05/G07/G09/G10 的正式行为裁决与综合稿一致。
- G03/G08 合并为 setup 状态机、G06 合并为风险状态机的方向正确。

**需要纠正部分**：

- 文首明确反对没有 OOS 依据的冷却参数，但风险状态表又写入“冷却 20 根 bar + 影子盈利”，内部自相矛盾。该数值和影子恢复机制均不得进入契约。
- `STOPPED_REARMABLE` 必须等待“新的 5m 同向确认”是合理候选，但仍是规则主张，不是已被青派理论或历史证据确认的事实。
- 把累计亏损和最大回撤直接定义为“永久熔断”同样是待裁决政策，不能因当前代码处于吸收态就反推这是理论期望。
- 状态表里的 300 点、5 次、600 点、3.1% 只是当前配置值，不应写入状态机定义本身。

### DeepSeek

**可接受部分**：

- 精确指出“BSP 被评估但目标仓位未变化时仍已消费 key”，比“生成信号即消费”更准确。
- 建议把 G08 概念分类映射到真实 `reason_codes`，可操作性更强。
- 建议把连续亏损恢复缺失提升为机器可读契约中的 OPEN 项，正确。
- 指出 setup 身份变化会改变后续审计归并口径，因此重跑前应先冻结审计身份字段，正确。
- 建议把“规则/工程方向错误”和“可研究但证据不足”分开，正确。

**需要纠正或降级部分**：

- “`type2str()` 演变后 `release_signal()` 必然找不到旧 key”并不普遍成立。当前拒绝路径传入的是原始 `StrategySignal`，其 `bsp_type` 与消费时一致，通常可以匹配旧 key。只有未来从最新 BSP 重建 signal，或释放 API 改为使用演变后的类型时，才会出现该风险。真正已确认的问题是三套身份粒度不一致，以及 `release_signal()` 匹配时忽略 `is_buy`。
- `parent_direction_conflict` 不是时间意义上的永久事实；新的已确认父级线段可能改变方向。它应归为“当前规则冲突”，是否允许同 setup 等待到父级翻转需要人工裁决。
- DeepSeek 称综合稿把 G07 列入“必须人工裁决”并不准确。综合稿最终将 G07 grade-aware 变体列在研究分支，正式默认维持现状。
- “未分类一律拒绝”在当前规则版本中不可实施，但未来修改契约并完成 regime/BSP 矩阵后仍可研究；不应描述为永远错误。

### GLM

**可接受部分**：

- 代码事实核验最完整，并确认了机器契约中 `QP-RISK-003` 实为“不足一手拒绝”。
- “冻结”不是契约状态名的批评成立。后续应使用 `fixed`、`engineering_default`、`research_candidate`、`deferred` 或 `default_until_resolved`，避免把阶段默认误写成永久禁止。
- 发现 CTA 逐 bar 权益峰值观测缺口，成立且应进入 P0 parity 修复。
- 发现三端反向退出谓词重复，成立。

**需要纠正或降级部分**：

- “只释放时序类 reason”比 blanket release 好，但仍不能只靠一个 reason-code 白名单直接进入正式行为。部分 reason 的可恢复性取决于当前配置、数据是否就绪、子级窗口是否结束和 setup 是否过期。
- “只看最新 BSP”只是当前实现的天然限幅，不是规则契约。不能依赖这个偶然行为替代显式 setup 有效期。
- `parent_structure_unavailable` 和 `sublevel_data_unavailable` 可能是暂时 warmup，也可能是数据管线故障，不能无条件视为可释放等待。
- 轻量 `vnpy_chan.live_engine` 当前显式关闭 `max_drawdown_pct`，所以“逐 bar 最大回撤 parity”主要直接影响 CTA `ChanBspStrategy`；轻量 live engine 的问题是风险能力本身与正式配置不同，需要单列说明，而不是笼统称三端同一缺陷。

## 3. G08 全量拒绝码裁决

当前 `MultiLevelDecisionEngine.assess()` 实际可能产生以下 reason。仅看 reason 字符串还不够，必须结合配置、数据 readiness、子级时间窗口和 setup 有效期。

| reason code | 当前含义 | 初步类别 | 是否可直接 `release_signal()` |
|---|---|---|---|
| `parent_not_available_at_decision` | 已有父级快照首次可见时间晚于决策时点 | 时间可见性等待 | 否；进入显式 pending，并受 setup expiry 约束 |
| `parent_structure_unavailable` | 截止当前没有可用父级结构快照 | readiness/结构不足 | 否；先区分 warmup 与数据故障 |
| `parent_structure_unconfirmed` | 父级快照存在但未确认 | 时间等待候选 | 否；严格配置要求 confirmed 时该分支通常不可达，需按配置测试 |
| `parent_direction_conflict` | 已确认父级方向与入场方向冲突 | 当前规则冲突 | 否；默认消费/阻断，是否允许等父级翻转需人工裁决 |
| `current_bi_time_window_unavailable` | 无法确定当前笔的子级匹配窗口 | 结构/完整性失败 | 否；终止并报警 |
| `sublevel_confirmation_not_available_at_decision` | 匹配确认存在，但决策时尚不可见 | 时间等待 | 否；进入显式 pending，证据到达后重评 |
| `sublevel_data_unavailable` | 截止当前没有子级观测，且未找到未来匹配 | readiness/数据不足 | 否；先判定数据 readiness，不得用重试掩盖断流 |
| `sublevel_confirmation_missing` | 有子级数据但当前窗口内无匹配确认 | 等待或到期 | 否；窗口未结束时 pending，结束后 expired |
| `multi_level_future_leak` | 可见时间关系违反时点不变量 | 完整性错误 | 绝不释放；终止并报警 |

结论：三份复审都支持 reason 分类，但“分类后直接释放”仍然把状态隐藏在 set 副作用中。可以分阶段实现一个最小状态机，只先覆盖 `PENDING_CONTEXT -> ELIGIBLE/EXPIRED`，不必等全部重入规则完成；但不建议先提交 reason 白名单式 blanket release。

## 4. G06 的新增全局问题

### 4.1 日切必须使用交易日

当前 `RiskManager._resolve_date()` 使用自然日期。RB 夜盘属于后续交易日，且周五夜盘可能属于下周一交易日。项目已经有 `data_foundation.data.calendar.RBTradingCalendar.map_datetime()`，数据管线和 session aggregator 也已经携带 `trading_day`。

因此正确接口不应只是：

```text
on_new_day(timestamp.date())
```

而应是：

```text
advance_session(trading_day, observed_at)
```

它必须在每根 bar 的风险审批前调用，与是否成交完全解耦；回测、CTA 和正式 live 路径必须使用同一个 trading-day 解析口径。状态持久化还要保存当前 `trading_day`，重启后不能重复或漏掉日切。

### 4.2 最大回撤逐 bar 权益观测

当前工作区的回测已经在每根 bar 调用 `risk.observe_equity(equity_cash)`，并以初始资金初始化峰值。CTA `ChanBspStrategy` 只在产生信号并调用 `risk.approve()`、或者平仓 `on_fill()` 时观察权益；如果持仓期间连续多根 bar 没有信号，真实权益峰值不会进入 `_peak_equity`，后续回撤会被低估。

P0 修复必须包括：

- CTA 在每根可交易 bar 上调用 `observe_equity(_account_equity())`。
- CTA 初始化 RiskManager 时使用同一账户权益单位和明确初始峰值。
- 回测/CTA 对同一权益序列产生相同 peak/drawdown 状态测试。
- 轻量 live engine 当前关闭 max drawdown，应在 parity 报告中明确为“能力禁用”，不能伪装为已对齐。

### 4.3 风控契约状态

应新增机器可读 OPEN 项，分别定义：

- 日亏暂停的 session key 与自动恢复事件。
- 连续亏损门控的恢复方式。
- `max_loss_points` 的恢复或人工复位语义。
- `max_drawdown_pct` 的恢复、人工复位或 hysteresis 语义。

只有日亏的“新交易日自动恢复”已经由现有注释和配置名给出足够语义，可作为实现缺陷直接修复。其他三项仍需人工裁决。

## 5. 身份、审计与重跑顺序

DeepSeek 指出阶段 A/B 可能重复重跑，这个提醒成立，但不能为了省一次回放把风险修复和 setup 行为重构混在同一实验。

建议采用两层身份：

- `raw_signal_identity`：完整保存当前 `(bi_idx, klu_idx, type2str, is_buy)`，用于复现旧行为。
- `canonical_setup_id`：新定义的稳定 setup 身份，用于跨 revision、重入和状态机归并。

实施顺序：

1. 先只增加两层身份和风险状态审计字段，不改变交易行为。
2. 只修 G06 日切与权益观测，重跑一次，用于测量 G06 的纯因果影响。
3. 再实施 setup 最小状态机，第二次重跑，用于测量 G03/G08 的增量影响。

两次重跑是有意的因果隔离，不是浪费。若把 G06 与身份/重评行为一起改，无法解释新增交易究竟来自风控恢复还是 setup 重评。

## 6. 对“剔除建议”的重新分层

### 当前版本不可实施

- 未定义 regime/BSP 权限矩阵前，`unclassified` 一律拒绝。
- 不区分 reason、有效期和数据 readiness 的 blanket release。
- 直接扩大结构止损，导致 execution stop 脱离结构锚点。
- 在 OMS/仓位/费用未闭环前启用部分退出。

### 可以研究，但证据不足

- 任何 ATR、成本比例或 T2 力度阈值。
- 固定冷却 bar 数、最大重入次数和 5m 重新武装条件。
- opposite exit 的最低 grade。
- `max_abs_position > 1` 的多手版本。

## 7. 修订后的优先级

### P0：恢复风控与证据口径

1. 冻结审计字段定义：raw identity、canonical setup candidate、risk state/reason/session key。
2. 实现基于 `trading_day` 的风险 session 推进，消除日亏吸收态。
3. 补 CTA 逐 bar 权益峰值观测和初始权益，生成三端能力差异报告。
4. 只改 G06 后重跑连续状态/fold-reset，核对 decision parity 与执行差异。

### P1：闭合 setup 资格

1. 人工裁决 canonical `SetupID`、有效期、保护止损后 rearm 条件和次数政策。
2. 先实现最小 `PENDING_CONTEXT -> ELIGIBLE/EXPIRED`，覆盖明确时序类拒绝。
3. 再实现 `CONSUMED -> STOPPED_REARMABLE/INVALIDATED`，不要把两个阶段混成一次大改。
4. 重跑并逐事件解释新增加的评估、接受、成交和重入。

### P2：只读研究与单机制实验

- G01 unclassified reason 与 regime/BSP 矩阵。
- G02 T2 专属结构力度。
- G05 cost/risk 与 ATR 窄止损过滤。
- G07 opposite exit grade/反事实 MFE/MAE。

### 维持当前版本默认

- G04：`QP-LEVEL-002=deferred`，严格配置继续拒绝父级冲突。
- G09：一手上限继续作为 v1 engineering default。
- G10：`OPEN-004` 未解决前继续仅 `CLOSE_ALL`。

## 8. 最终裁决

本轮三份复审没有推翻第一轮综合结论，但把实施路径推进了一步：

- G06 不再只是“补一个 on_new_day”，而是“交易日 session 推进 + 逐 bar 权益观测 + 三端能力说明”。
- G03/G08 不再是“完整状态机或 blanket release”二选一，可以先实现显式的最小 pending 状态，再扩展重入状态。
- 审计身份字段必须先冻结，但策略身份行为重构应在 G06 单独重跑之后实施，以保留因果可解释性。
- 所有契约状态名应与机器可读文件一致，避免用“冻结”暗示永久不可变。

下一步仍应先做 P0，不应跳到 G01/G02/G05/G07 的收益实验。
