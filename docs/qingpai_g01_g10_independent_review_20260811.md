# 青派 G01-G10 独立审核意见

更新时间：2026-08-11
审核基线：[docs/qingpai_chan_core_and_strategy_global_review_20260810.md](docs/qingpai_chan_core_and_strategy_global_review_20260810.md)
代码基线：`qing_chan` 分支，HEAD `b122917f7ab3632c1a73e87b21c30c6bceb487ba`
审核者：Claude (Sonnet 5) 独立审核
审核方法：10 路并行调查 → 10 路对抗验证（20 agent，约 1.23M tokens，293 次工具调用）；每条 finding 的 `当前行为` 经独立重读代码二次核实，对抗验证者可修正调查者声明。

## 0. 头部结论

1. **代码事实层全部成立**：10/10 问题的 `当前行为` 声明经对抗验证全部准确（含若干行号微调与因果降级，见各条修正注）。基线文档的描述没有夸大。
2. **裁决分布**：8 项 CONFIRMED + 2 项 PARTIALLY_CONFIRMED（G03、G08）。两项 PARTIALLY_CONFIRMED 的"部分"不在代码事实层，而在修改方案的最小性（G03）与不变量归因的过度延伸（G08）——其核心问题依然成立。
3. **唯一实现层 bug**：G06 的 `daily_loss_limit` 吸收态。对抗验证将其从"可能锁死"升级为"必然锁死"（`on_fill` 在回测中仅于 `_close_trade` 内调用，日亏触发时持仓必为 None，后续无 fill 可触发日切重置）。这是应立即修复的缺陷，不需要人工裁决。
4. **最清晰的代码修复**：G08 为多级别拒绝补 `release_signal`，镜像动量拒绝模式（`runtime_kernel.py:96-102` 缺少与 `:111-112` 对称的释放）。
5. **其余 7 项**（G01/G02/G04/G05/G07/G09/G10）当前实现是正确的 chosen default / engineering_default，建议维持现状，将"是否改进"列为 research_candidate 在未见 holdout 上证伪，而非现在用人工裁决推翻已有默认。
6. **时间诚实（不变量 1）未被任何问题违反**：审计 §2 已证 multi_level / event / parent / momentum 的 available-after-entry 失败均为 0。10 个 G-questions 均不触及时间不变量违反。

## 1. 裁决汇总表

| ID | 全局问题 | 所属层 | 裁决 | 核心建议 | 人工裁决 |
|---|---|---|---|---|---|
| G01 | 未分类能否入场 | 青派语义/入口 | CONFIRMED | 维持现状（架构隔离 + Rulebook §11 默认） | 否 |
| G02 | 2/2s 力度条件 | 青派语义 | CONFIRMED | 维持现状（§12 限 1/1p；2/2s 力度由 T2Scorer 结构评分表达） | 部分（2/2s 力度定义） |
| G03 | 保护止损后同 setup 重入 | 青派语义/退出/执行 | PARTIALLY_CONFIRMED | 同意"允许重入"方向，反对"粒度不匹配隐式实现"方式 | 是（重入节奏） |
| G04 | 父级转折滞后如何处理 | 青派语义 | CONFIRMED | 维持严格拒绝（QP-LEVEL-002 deferred）；清理 ParentTrendFilter 死代码 | 部分（研究分支对照） |
| G05 | 结构止损成本/波动下限 | 仓位/退出/执行 | CONFIRMED | 反对扩大 execution_stop；支持 min_stop_distance 入场过滤 | 部分（阈值选择） |
| G06 | 风险门控何时恢复 | 风控 | CONFIRMED | daily_loss_limit 日切恢复 = bug，立即修；max_consecutive_losses 恢复语义需裁决 | 部分（连续亏损恢复策略） |
| G07 | 反向 BSP 平仓是否要求入口规则通过 | 退出 | CONFIRMED | 维持现状（非对称：入场严、出场宽） | 否 |
| G08 | 多级别拒绝后能否重评同一 BSP | 青派语义 | PARTIALLY_CONFIRMED | 为多级别拒绝补 release_signal（镜像动量拒绝） | 部分（parent_direction_conflict 重评） |
| G09 | 动态仓位是否真的需要 | 风控 | CONFIRMED | 维持现状（max_abs_position=1 合法默认；基础设施已就绪） | 否 |
| G10 | 分级退出是否进入第一版 | 退出 | CONFIRMED | 维持现状（OPEN-004 关闭；框架正确休眠） | 否 |

## 2. 跨问题依赖

- **G03 ↔ G08（共享 `_consumed_keys` / `release_signal`）**：两者都触及 BSP 身份消费与释放机制。G08 的修复（多级别拒绝后释放）会改变 G03 所讨论的"同 setup 重入"的实际行为——释放后父级/子级稍后对齐时同一 BSP 可重评，这本身就是一种重入路径。两者应一并裁决：先确定"哪些拒绝类型应释放"（G08），再确定"释放后重入的节奏"（G03 的冷却/次数上限）。若先单独实施 G08 而不定 G03 节奏，会引入无门禁的隐式重入（正是 G03 警告的脆弱模式）。
- **G05 ↔ G09（共享 sizing 链路）**：G05 的 `min_stop_distance` 入场过滤与 G09 的"不足一手拒绝"（`risk_budget_below_one_lot`）都在 `_size_decision` 中。G05 填补的是"lots=1 但成本占比过高"的中间地带，G09 是"lots<1"的极端地带。两者独立但相邻，实施 G05 时不应触碰 G09 的不足一手拒绝逻辑。
- **G06 是 173 条 accepted-not-executed 的根因**：审计 §3 显示 173 条规则接受但未成交，全部被 `max_consecutive_losses: 5 >= 5` 拒绝。在 G06 的吸收态修复前，长回放的经济证据（10 笔全空头、9 笔结构止损等）被吸收态污染，**不能用于评估其他 G-questions 的经济有效性**。G06 应优先于任何依赖长回放经济证据的裁决。
- **G04 的 240 条多头父级冲突拒绝 + G08 的多级别不释放**：G04 说明父级方向冲突是设计权衡（deferred 默认），G08 说明多级别拒绝不释放是遗漏。若实施 G08，被 G04 拒绝的多头信号将可重评——但只要父级仍冲突，重评仍会被拒；只有父级方向实际转变后才会重新入场。G08 修复不绕过 G04 门控，两者正交。

## 3. 按审核优先级排列的独立意见

依基线第 9 节审核优先级：1. 结构与时间 → 2. 规则契约完整性 → 3. 入口/退出/风险闭环 → 4. 参数与经济效果。

### 优先级 1：结构与时间正确性

10 个 G-questions 均无时间不变量违反（审计 §2：0 failures）。G08 涉及父子级时序对齐（子级确认未就绪时的重评），但其本质是信号消费闭环（优先级 3），非时间诚实问题。本优先级无独立 finding。

### 优先级 2：规则契约完整性

#### G01 — 未分类能否入场

```
Finding ID: G01
所属层: 青派语义/入口
证据类型: 代码事实 + 规则契约（Rulebook §11 chosen default）
当前行为: 走势分解只记录、不硬拒绝。对抗验证强化了这一结论：decomposition 不仅"不被消费"，而且在架构上不可达——GradedChanStrategy.evaluate_bar() 的方法签名（chan_futures/graded_strategy.py:119-165）完全不接收 decomposition 参数。runtime_kernel.py:76-80 先计算 decomposition，但仅在 :95（strategy.evaluate_bar() 返回后）通过 with_decomposition 附加到已决策的 intent 上（trade_intent.py:173-177，仅 replace，不改 accepted）。DecisionPipeline.evaluate()（decision_pipeline.py:90-148）和 EntryPolicy.evaluate()（entry_policy.py:70-138）的入场判定链路零引用 decomposition/regime（grep 确认）。classify_observation() 在无中枢/中枢失效/同向不推进三种情况返回 UNCLASSIFIED（qingpai_decomposition.py:485-503, 545-553），但这是分类结果，不被任何入口逻辑消费为拒绝条件。regime 仅作为 DecisionTraceRecord 的审计字段（trade_intent.py:35, 219-223），全仓库无 regime.*reject/gate/block 模式。
期望行为: Rulebook §11 显式规定"P4 暂不按走势类型新增入场硬门槛"，原因是 BSP 允许集合按走势类型分别固化前，粗糙的方向一致条件会误杀一买/一卖反转。当前"不硬拒绝"是正确的 chosen default，代码应忠实执行——实际代码不仅忠实执行，还通过架构隔离提供了更强的保证。
违反的规则或不变量: 未违反。QP-DECOMP-001(fixed)、QP-REGIME-001(fixed) 已实现；不变量2（生命周期：关闭后不可回写，qingpai_decomposition.py:235-236）、8（证据：regime 记入 DecisionTraceRecord）、9（一致性：回测与实盘走同一 RuntimeDecisionKernel）均满足。
上游原因: 非缺陷。Rulebook §11 有意选择，代码忠实实现。分解分类器与入场判定之间是松耦合的"记录-审计"关系，而非"判定-阻断"关系。
受影响的下游模块: 仅报告层（DecisionTraceRecord 记录 regime 供审计）和样本标签层（holdout 标签可按 regime 分层统计）。不影响入口、仓位、风控、退出、执行层。
最小可证伪修改: 维持现状（零改动）。未来若要证伪"不硬拒绝是否最优"，应在 BSP 类型 × regime 允许集合固化后，以 research_candidate 契约设计 A/B 比较（reject_unclassified=true vs false），而非现在用粗糙方向一致条件阻塞。
必须新增的测试: 无需修复缺陷（无缺陷）。建议补一个防护性回归测试：构造 decomposition=None 或 regime=UNCLASSIFIED 的 TradeIntent，断言 DecisionPipeline.evaluate() 仍可正常接受/拒绝，确保未来不会意外引入隐式 regime 门槛破坏 §11 默认。
可能的反作用: 若未来加粗糙 regime 门槛：一买/一卖（T1/T1P）反转信号必然在前一走势反向产生，方向一致条件会系统性误杀反转入场——正是 §11 所担忧的。维持现状的反面：极端无结构样本（如数据管线故障致无中枢）下仍可能入场，但 BSP 信号生成本身要求结构上下文，无中枢时通常不产 BSP 信号，构成天然结构性约束。
是否需要人工裁决: 否。
```

#### G02 — 2/2s 的力度条件

```
Finding ID: G02
所属层: 青派语义
证据类型: 代码事实 + 规则契约 + 研究观察
当前行为: QingpaiMomentumAnalyzer.analyze_intent() 仅对 {"1","1p"} 做组合动量确认；对其余类型返回 not_applicable 并放行（qingpai_momentum.py:122-127）。_not_applicable() 返回 status=NOT_APPLICABLE、accepted=True、reason_codes=()（:292-301）——显式记录，非静默放行，不违反不变量8。apply() 在 confirmation.accepted=True 时直接返回不改 decision（:98-99）。T2Scorer（管辖 2/2s，signal_scoring/base.py:104-105）的 _score_structural 仅有回撤幅度/一买锚点/笔确认三个纯结构维度（t2.py:21-83），是三者中唯一无力度的评分器（T1Scorer 有 divergence_rate + bi_amp，T3Scorer 有 return_strength）。EntryPolicy 对 2/2s 的 setup_invalidation 用 related_bsp1_price（结构锚点），无力度门禁（entry_policy.py:190-191）。
期望行为: QP-MACD-001(fixed) 规定 MACD 只辅助判断力度，不替代中枢方向和价格结构。Rulebook §12 明确将组合动量限定于 1/1p。青派教学对二类点的力度未明确固化——二类点的力度本质上由回撤结构表达（回撤深度、是否破一买锚点），而非由 MACD 背驰表达（背驰是一类点的定义特征）。因此对 2/2s 不施加 1/1p 式组合动量确认是符合规则的。
违反的规则或不变量: 未违反。QP-MACD-001 被尊重（MACD 未替代 2/2s 结构判断）；QP-MACD-003(research_candidate) 当前默认（不对 2/2s 施加价格力度裁决）是合法工程默认；不变量8 未违反（NOT_APPLICABLE 显式记录，五状态未合并）。
上游原因: Rulebook §12 显式将组合动量限定于 1/1p，代码忠实执行。2/2s 的力度评估在设计上委托给 T2Scorer 的结构评分，而非动量分析器。这是规则分工，不是实现遗漏。
受影响的下游模块: 2/2s 信号接受率（相对偏高，但仍受 T2Scorer 结构评分和 min_grade 约束，非无条件放行）、交易频率、评分维度完整性、退出端负担（结构止损 -927pts 的研究观察表明部分低质量 2/2s 依赖退出规则兜底）。
最小可证伪修改: 维持现状作为默认。若要证伪"2/2s 不需要力度确认"，最小可证伪修改是 research_candidate 路径（不是将 1/1p 组合动量套用到 2/2s——那在结构上错误，因为 2/2s 不是背驰点）：在 T2Scorer 新增 reversal_bi_force 结构力度维度（反转笔振幅/斜率相对前段离开笔的比值，纯结构指标），通过 config flag 开关，在 holdout 上 A/B 对比。位于评分层，不触及 chan.py 核心，不影响 1/1p 动量逻辑。
必须新增的测试: (1) 现状回归：验证 2/2s 的 momentum_status 恒为 NOT_APPLICABLE 且 accepted=True。(2) 若实施 research_candidate：holdout A/B 对比 T2Scorer 加 reversal_bi_force vs 不加的 OOS Sharpe/PF/胜率。(3) 证据链测试：NOT_APPLICABLE 的 MomentumConfirmation 在审计日志中可独立检索（不变量8）。
可能的反作用: 给 2/2s 加力度门槛可能降低交易频率（2/2s 占比高），可能过滤掉部分盈利交易（反向信号退出 +1047pts 说明部分 2/2s 盈利）；错误使用 1/1p 的 MACD 面积比框架会违反 QP-MACD-001 且结构上无意义（2/2s 不是背驰点）。
是否需要人工裁决: 部分（2/2s 的力度定义是否需要专属结构维度，待 holdout 样本足够后评估；当前标记为 deferred）。
```

#### G04 — 父级转折滞后如何处理

```
Finding ID: G04
所属层: 青派语义
证据类型: 代码事实 + 规则契约 + 研究观察
当前行为: 严格执行链通过 MultiLevelDecisionEngine.assess() 实现父级门控，对入场信号是纯二进制 pass/reject（multi_level.py:571-590）：父级缺失→reject、未确认→reject、方向冲突→reject；最终 accepted = not reasons_tuple（:650-652），无中间状态。非入场（平仓）信号 bypass（:570），满足不变量7。apply() 若 intent 原本 accepted 但 context.accepted=False，直接覆盖为 accepted=False，无减仓/降级分支（:526-554）。ParentTrendFilter（parent_filter.py:36-182）定义了 DOWNGRADE 动作，但对抗验证澄清：DOWNGRADE 仅在 _unknown() 方法中返回（父级不可用/decision_time 缺失，:163-182），方向冲突时返回 REJECT（:105）；且该类全仓库无任何模块导入——是死代码/孤立脚手架。parent_action 仅作审计元数据（trade_intent.py:44），从不参与非二进制决策。时点诚实：as_of(cutoff) 严格按 available_at <= decision_time 过滤（:238-243）；回测与实盘共用 RuntimeDecisionKernel.evaluate_bar()（backtest.py:565 / live_engine.py:291 / chan_bsp_strategy.py:550 均调用同一入口）。
期望行为: QP-LEVEL-002(deferred) 明确"qingpai_strict 执行链固定拒绝逆向开仓；减仓试错不进入严格执行链"；OPEN-003(default_until_resolved) 规定 qingpai_strict 固定拒绝，仅研究分支允许对照。当前严格执行链行为符合契约期望。
违反的规则或不变量: 未违反。QP-LEVEL-002(deferred) 和 OPEN-003 的 chosen default 被忠实执行。不变量1（时点）、9（一致性）满足；不变量7（减风险）通过非入场 bypass 满足。
上游原因: 60m 线段确认有天然滞后（特征序列算法需反向笔突破确认），转折点旧方向线段仍为最近已确认线段，导致新方向信号被系统性拒绝。这是结构确认机制的固有特性，非未来函数。基线 §2.5 已识别。
受影响的下游模块: 多空分布失衡（审计：10 笔全空头）、转折点新方向信号系统性被拒、趋势反转期入场机会。减仓/禁多状态缺失意味着持仓在父级反转后只能通过独立退出规则平仓。
最小可证伪修改: 维持严格执行链现状。结构层面的最小可证伪修改仅在研究分支（与 qingpai_strict 物理隔离）进行：父级方向冲突时不拒绝而转入"禁多/禁空观察态"（仅允许减仓/平仓、禁止新开逆向仓），在未见 holdout 上对照严格拒绝 vs 观察态的 OOS Sharpe 和最大回撤。注意：现有 DOWNGRADE 仅处理父级缺失，方向冲突→观察态需新增动作映射（如 OBSERVE），不能直接复用 DOWNGRADE。同时清理 parent_filter.py 死代码（标注 research-only 或删除）。
必须新增的测试: (1) 研究分支对照：未见 holdout 上 strict-reject vs 禁多观察态的 OOS Sharpe/maxDD/转折点信号捕获率。(2) 死代码验证：确认 ParentTrendFilter 未被任何执行路径引用（import graph 测试）。(3) 若实现研究分支：平仓不被禁多态阻止的回归测试（不变量7）。
可能的反作用: 若在严格链中引入减仓/禁多态：可能违反不变量7 边界（需确保观察态不阻止合法平仓）；可能引入回测/实盘不一致风险；增加状态机复杂度导致三端 parity 难维持。仅在研究分支引入则无上述副作用。
是否需要人工裁决: 部分（是否在研究分支投入对照实验；结论不得直接回写严格链）。
```

#### G10 — 分级退出是否进入第一版策略

```
Finding ID: G10
所属层: 退出
证据类型: 代码事实 + 规则契约（qingpai_rule_contract_v1.json）+ 研究观察
当前行为: ExitManager.check 只消费 CLOSE_ALL（base.py:535 调用 rule.check(ctx)，从不调用 check_with_directive）。框架已定义 ExitAction{HOLD,TIGHTEN_STOP,REDUCE,CLOSE_ALL}（base.py:32-43）和 ExitDirective（:46-64），但 grep 确认 check_with_directive 仅被规则自身的 check() 内部和测试调用，ExitManager 从不调度。ChanDivergenceExitRule 的 check_with_directive 可返回 TIGHTEN_STOP（rules.py:490-530），但 check()（:478-488）仅当 action==CLOSE_ALL 才返回 ExitSignal，否则 None——TIGHTEN_STOP 被静默丢弃。对抗验证进一步指出这是"双层死代码"：配置 require_second_level_confirm=false（yaml:82）使 sub_confirmed 恒 True，TIGHTEN_STOP 分支（:521-530）本就不可达；即使可达，ExitManager 也不调度。ChanSegmentCompleteExitRule 的 REDUCE 路径（:669-675）同理，且该规则根本未出现在配置中。回测消费侧 backtest.py:604-627 接到 ExitSignal 即 target=0 强制全平；三端一致（chan_bsp_strategy.py:1239 / live_engine.py:573 同接口）。PositionContext 有 merge_open_fill 支持分批开仓（position.py:128-160）和 pnl_points 的 volume 参数（:167-173），但无 partial_close 方法。
期望行为: QP-EXIT-001(engineering_default, contract:193-198) 明确"保本移动和分批止盈默认关闭"；OPEN-004(default_until_resolved: 关闭, contract:228-231) 明确保本止损/分批止盈/对立中枢目标位暂不启用。基线 §4.6 要求启用分级出场前必须先补齐指令执行、剩余仓位、费用和三端一致性。因此第一版应仅用 CLOSE_ALL——这正是当前实现。
违反的规则或不变量: 未违反。QP-EXIT-001/OPEN-004 的"默认关闭"被遵守；不变量7（CLOSE_ALL 本身是合法平仓）、9（三端同接口）满足。
上游原因: 分级退出框架在 V2 设计阶段预埋，但执行层（部分平仓、止损价动态收紧、费用分摊）和三端 dispatch 尚未实现，故规则契约标记为 default_until_resolved: 关闭。这是有意的阶段性冻结，非缺陷。
受影响的下游模块: 当前不受影响（分级退出未启用）。若将来启用，影响面：仓位状态机需 partial_close、费用引擎需按比例计费、OMS 需区分减仓/全平单、三端 check() 调用点需统一改用 check_with_directive。
最小可证伪修改: 维持现状。任何"现在就开分级退出"的修改都违反 OPEN-004。未来若 OPEN-004 解决（有 holdout 证据），按基线 §4.6 清单依次实现：指令执行分发、PositionContext partial_close、部分平仓费用、三端 check_with_directive 调用点统一，再开启 YAML 开关。建议为 TIGHTEN_STOP/REDUCE 死代码路径加注释或 lint 标记说明当前未被调度。
必须新增的测试: 当前无需新增（维持现状）。若未来准备启用，需先补齐：TIGHTEN_STOP 更新保护止损端到端测试、REDUCE 部分平仓剩余仓位记账一致性测试、部分平仓按比例计费测试、三端同指令序列同仓位同费用 parity 测试。这些测试在当前状态下不应编写（执行路径尚不存在）。
可能的反作用: 维持现状的代价：放弃 TIGHTEN_STOP 和 REDUCE 在未见样本的潜在正期望。但这正是 OPEN-004 标记 research_candidate 的原因——需 holdout 验证净收益后再决定，当前无证据支持开启。
是否需要人工裁决: 否。
```

### 优先级 3：入口、退出和风险状态闭环

#### G06 — 风险门控何时恢复（最严重，优先处理）

```
Finding ID: G06
所属层: 风控
证据类型: 代码事实 + 研究观察（对照报告数据）
当前行为:
1. 连续亏损恢复=吸收态：risk.py:178-181 中 on_fill 仅在 pnl_points>0 时清零 _consecutive_losses；approve 在 _consecutive_losses>=max_consecutive_losses 时拒绝开仓（:130-138）。on_fill 只在平仓时被调用（backtest.py:439，位于 _close_trade 内，grep 确认是唯一调用点），第 5 笔连续亏损平仓后 position_context 必为 None（backtest.py:444），持仓为零；开仓又被 approve 阻断→永远不会再有 fill→永远不会出现盈利 fill→_consecutive_losses 永远不会清零。这是真正的吸收态，无任何可达恢复事件。
2. 日亏恢复=必然锁死（对抗验证从"可能"升级为"必然"）：risk.py:170-175 中 _daily_realized 仅在 on_fill 收到新日期时清零。代码无任何独立的日切/session-rollover 钩子（grep 确认 backtest.py/risk.py/chan_bsp_strategy.py 无 on_new_day/daily_reset/session 调用）。关键：on_fill 在回测中仅在 _close_trade 内被调用（开仓不调用 on_fill），因此 daily_loss_limit 触发时（发生在平仓 fill 后）持仓必为 None。后续无 fill → _daily_realized 永不清零 → 日亏变为永久阻断。实盘侧同样依赖 on_fill（chan_bsp_strategy.py:685-689），_risk_day_loss() 直接读 get_state()["daily_realized"]（:1469-1474），无外部日切重置。
3. max_loss_points=600 为永久停机：risk.py:109-117，_realized_points 单调累加（:167），无恢复路径。与基线"600 点本来就是永久型停机门槛"一致，设计意图明确。
4. 研究证据印证：risk_state_policy_report.md L14 连续状态 OOS trades=0, accepted=104, risk_rejected=104, reasons={"max_consecutive_losses: 5 >= 5": 104}；L15 fold-reset trades=86, net=+618pts；L42 shared=2684, accepted_mismatch=0（另有 continuous_only=2, accepted_continuous_only=1，但 shared 事件中无 accepted 分歧），差异主要来自风控状态口径。
5. 平仓路径不受风控阻断：backtest.py:616-635 exit_manager 触发的平仓直接调用 execution.execute(sig)，不经过 risk.approve()；换月平仓 backtest.py:486 同样绕过。risk.approve() 只在 backtest.py:715 对新开仓/反转调用。不变量7 未违反。
期望行为: QP-RISK-002(eng_default) 的 daily_loss_limit 应有明确日切恢复事件（每个新交易日/session 开始时 _daily_realized 清零，不依赖 fill 到达）。QP-RISK-003(eng_default) 的 max_consecutive_losses 需规则层明确定义恢复语义（按日重置/按冷却期/确认为永久停机）。基线 §4.5 明确要求"为每个风险状态明确定义恢复事件"。
违反的规则或不变量: 未违反不变量 7/8/9（平仓路径绕过 approve，状态分离正确，回测与实盘同 RiskManager 故 parity 成立）。违反 QP-RISK-002 的隐式契约：risk.py:6 注释"当日不再开仓"承诺"日内"生命周期，但代码无日切恢复事件。违反 QP-RISK-003 的隐式契约：risk.py:7 注释"直到出现盈利"承诺存在恢复路径，但吸收态使其不可达。两者为规则契约定义不完整，非不变量违反。
上游原因: RiskManager 状态机设计不完整：日切恢复与连续亏损恢复都没有独立的恢复事件入口，而是隐式依赖 on_fill 的副作用。on_fill 只在平仓时触发，当风控阻断开仓且持仓为零时，on_fill 永远不会再被调用，形成吸收态。daily_loss_limit 的"日内"语义本应与交易日历绑定，而非与成交事件绑定。
受影响的下游模块: 长回放在 consecutive_losses 达 5 后永久停机（0 trades）；fold-reset 对照（+618pts）证明吸收态导致全量 OOS 机会丧失；daily_loss_limit 触发后必然锁死；实盘 chan_bsp_strategy.py:685 同接口相同缺陷；compare_risk_state_policies.py 仅诊断不修复。G06 是 173 条 accepted-not-executed 的根因，污染长回放经济证据。
最小可证伪修改: 两处独立修改：
(1) daily_loss_limit 日切恢复（实现层 bug，不需人工裁决）：在 RiskManager 新增 on_new_day(date) 方法清零 _daily_realized 并更新 _current_date；在 backtest.py 主循环检测跨日时调用 risk.on_new_day()；在 chan_bsp_strategy.py 的 on_bar/session 回调中同样调用。这是补上缺失的状态转移，不是调参。可证伪：修改后重跑 compare_risk_state_policies.py，连续流与 fold-reset 的 daily_loss_limit 拒绝数应收敛。仅在风控执行层内部补全，不破坏不变量7。
(2) max_consecutive_losses 恢复语义（需规则层裁决）：三选一——(a) 确认为永久停机门槛（与 max_loss_points 同生命周期，修正 risk.py:7 注释移除"直到出现盈利"暗示）；(b) 按交易日重置（与 daily_loss_limit 共用 on_new_day）；(c) 按冷却期（N 根 K 线或 N 日）重置。选 (a) 则维持现状只改注释；选 (b)/(c) 需加重置逻辑。可证伪：任选一项后重跑双口径对照，连续流 trades 应从 0 变非零且与 fold-reset 方向一致。
必须新增的测试: (1) test_risk_daily_reset_on_new_day：日亏触发后持仓为零，调用 on_new_day 后 approve 不再因 daily_loss_limit 拒绝。(2) test_risk_consecutive_absorption：连续 5 亏平仓后 approve 拒绝，且无 API 可恢复（记录当前行为为 baseline）。(3) test_risk_consecutive_recovery_policy：针对选定恢复策略验证恢复事件可达且 approve 恢复。(4) test_backtest_calls_on_new_day：跨日 K 线插入验证 risk.on_new_day 被调用且 _daily_realized 清零。(5) test_live_engine_daily_reset_parity：vnpy_chan 侧 session 切换调用 on_new_day，与 backtest 一致（不变量9）。
可能的反作用: 若 consecutive_losses 选按日重置 (b)，可能放松风控导致连续亏损期内次日恢复开仓，增加尾部风险暴露；若选永久停机 (a)，则连续长回放持续 0 trades，需接受"连续状态口径下策略在某点后永久停机"作为真实结论。daily_loss_limit 日切重置是纯 bug 修复，无副作用。反对将 fold-reset 的 +618pts 解释为"修复后收益"——报告 L46-48 已正确声明这只是状态敏感性诊断。
是否需要人工裁决: 部分。daily_loss_limit 日切恢复判定为实现层 bug，不需裁决，应立即修。max_consecutive_losses 恢复语义（永久/按日/冷却）是规则契约设计选择，需裁决；裁决前维持现状，但将连续状态口径的 0-trade 结果如实标注为"吸收态预期行为"。
```

#### G08 — 多级别拒绝后能否重评同一 BSP

```
Finding ID: G08
所属层: 青派语义
证据类型: 代码事实 + 规则契约 + 研究观察
当前行为: 三类拒绝对 _consumed_keys 的不对称处理：
1. BSP 身份消费：MinimalChanTrendStrategy.on_bar() 在 strategy.py:60-65 将 key=(bi.idx, klu.idx, type2str(), is_buy) 加入 _consumed_keys（:65 在 target_position 检查 :66 之前），此后同一 BSP 在 :61-62 被跳过。
2. 信号未确认释放：GradedChanStrategy._build_result() 在 graded_strategy.py:265-266 当 reason 含 "signal_not_confirmed" 时调用 release_signal。有测试覆盖（test_decision_pipeline.py:103-117）。
3. 动量拒绝释放：runtime_kernel.py:103-112，当 momentum.apply 将 intent 从 accepted 变为 not accepted 时调用 release_signal（:112）。
4. 多级别拒绝不释放（关键不对称）：runtime_kernel.py:96-102，multi_level.apply 将 intent 从 accepted 变为 not accepted 时无任何 release_signal 调用。对抗验证补充关键细节：:104 的 was_accepted = intent.accepted 捕获的是 multi_level.apply() 之后的状态，因此当 multi_level 拒绝时 was_accepted 已为 False，momentum 块的 release guard（:111 if was_accepted and not intent.accepted）永远不会为 multi_level 拒绝触发——这确认不对称是 multi_level 块自身缺少对称释放，而非 momentum 块的遗漏。多级别拒绝原因包括 parent_direction_conflict、parent_structure_unconfirmed、sublevel_confirmation_not_available_at_decision 等（multi_level.py:572-629），无论哪种，BSP 身份均被永久消费。
5. 重评的自然边界：on_bar() 只看 chan.get_latest_bsp(number=1)（strategy.py:51），仅评估最新 BSP。即使释放 key，旧 BSP 在不再是最新时自然不再被评估。
6. 动量拒绝不区分 CANDIDATE/REJECTED 就一律释放（qingpai_momentum.py:98-112），已树立"外部确认未通过一律释放"先例。
期望行为: 时序延迟类多级别拒绝（parent_structure_unconfirmed、sublevel_confirmation_not_available_at_decision 等）应与动量拒绝和信号未确认一致：结构性有效但外部确认未到达时，释放 BSP 身份允许后续重评，直到 BSP 不再是最新或被结构性证伪。结构性冲突类拒绝（parent_direction_conflict）的重评是否允许属青派理论未定问题，需人工裁决。
违反的规则或不变量: 对抗验证修正了不变量归因：不变量2（生命周期）描述的是结构信号生命周期（SignalLifecycleTracker 的 CANDIDATE→CONFIRMED→INVALIDATED 链），_consumed_keys 是策略层消费去重守卫，不是结构生命周期管理器——两者是不同概念，_consumed_keys 永久消费不等于关闭结构 revision 通道。不变量8 描述的是执行管线五状态分离，_consumed_keys 不属这五状态。原意见将策略层消费与结构层生命周期混为一是过度延伸。实际存在的是规则契约层面的一致性原则缺失（三类外部确认拒绝释放行为不统一），非第7节硬不变量违反。QP-LEVEL-002 为 deferred，不构成硬违反。
上游原因: runtime_kernel.py:96-102 在 multi_level.apply() 拒绝后缺少 release_signal 调用。:104 的 was_accepted 捕获 post-multi_level 状态的设计使 momentum release 仅在 multi_level 接受时触发，进一步确认不对称。大概率是遗漏而非有意设计——没有测试或注释锁定多级别拒绝不释放的行为，且拒绝原因中包含与时序延迟直接对应的 sublevel_confirmation_not_available_at_decision。
受影响的下游模块: 系统性漏掉转折后入场机会：父级/子级在 BSP 形成后 1-N 根 K 线才就绪时，该 BSP 已被永久消费。影响多级别共振场景入场时效。严重程度取决于实际拒绝类型分布——若绝大多数为 parent_direction_conflict（结构性冲突），释放后重评获益有限。
最小可证伪修改: 在 runtime_kernel.py:96-102 为多级别拒绝增加 release_signal 调用，镜像动量拒绝模式：
    if self.multi_level is not None:
        was_accepted = intent.accepted
        intent = self.multi_level.apply(intent, current_chan=chan, decision_time=timestamp, lv_idx=lv_idx)
        if was_accepted and not intent.accepted:
            self.strategy.release_signal(intent.signal)
结构层面最小修改（不是调参），使多级别拒绝与动量拒绝行为一致。不跨越架构边界（仅改 runtime_kernel.py 青派外围层），不破坏不变量7（multi_level.apply 在 :544 对非 accepted intent 直接返回，平仓不受影响）、不变量1（释放仅移除消费键，不改时点检查）。可在 holdout A/B 对比释放 vs 不释放的入场数、入场时延、盈亏比，若恶化则恢复现状。
必须新增的测试: (1) 多级别拒绝后 BSP 释放，下一根 K 线父级方向对齐时同一 BSP 可重评入场。(2) 多级别拒绝后 BSP 释放，但 BSP 不再是最新时不被重评（天然边界）。(3) 拒绝→释放→重评→仍拒绝→再释放循环，验证不产生重复成交且 decision_trace 正确记录多次评估。(4) 多级别拒绝释放后 momentum 仍拒绝时的两层释放交互。(5) holdout A/B 研究脚本。
可能的反作用: (1) 迟到入场风险：BSP 形成后父级/子级数根 K 线后才对齐，入场价已偏离，盈亏比下降。(2) 重复评估循环：每根 K 线产生一条 decision_trace 记录，审计链变长但不影响执行。(3) parent_direction_conflict 释放后重评可能在一个已过时 BSP 上入场——但由"仅最新 BSP 被评估"的天然边界控制。
是否需要人工裁决: 部分。时序延迟类拒绝（parent_structure_unconfirmed、sublevel_confirmation_not_available_at_decision）的重评代码层面已有明确答案（应释放），无需裁决。parent_direction_conflict（结构性方向冲突）后重评是否青派教学允许，属理论未定，需裁决。建议先用 blanket release（镜像动量先例），在 holdout 观察 parent_direction_conflict 释放后重评入场绩效，再决定是否按 reason_codes 精细过滤。
```

#### G03 — 保护止损后同 setup 能否重入

```
Finding ID: G03
所属层: 青派语义/退出/执行
证据类型: 代码事实 + 规则契约边界 + 研究观察（机制因果链为推断）
当前行为:
1. MinimalChanTrendStrategy 用 _consumed_keys 去重，key=(bi.idx, klu.idx, type2str(), is_buy)（strategy.py:60）；key 命中返回 None（:61-62）；key 在 target_position 检查前加入（:65-67），即使不产生交易也消费。
2. signal_key 定义 models.py:306：f"{contract}_{timeframe}_bi{bi_idx}_{direction}_{primary_bsp}"，粒度 (contract, timeframe, bi_idx, direction, primary_bsp)，跨 revision 不变（extractor.py:113）。
3. 粒度不匹配确实存在：_consumed_keys 含 klu_idx 和完整 type2str()，signal_key 只含 primary_bsp。同一 bi_idx 的 BSP 若 type2str() 或 klu_idx 演变，_consumed_keys 产生新 key，signal_key 不变。
4. release_signal 仅两条路径：(a) graded_strategy.py:265-266 "signal_not_confirmed" 拒绝时；(b) runtime_kernel.py:111-112 momentum 从 accepted 变 rejected 时。均不覆盖 execution_stop 退出——backtest.py:616-643 触发平仓后只调用 exit_manager.on_close()，不调用 release_signal。
5. 保护止损退出后 _consumed_keys 不释放。若 BSP type2str()/klu_idx 随后演变，策略产生新 key 允许入场。审计脚本（audit_qingpai_mechanisms.py:725-750）按 signal_key 分组统计 reentry_after_stop。
【因果降级】原调查声明"5 笔止损后重入即由此机制产生"是因果推断而非已验证代码事实。代码事实是：(a) 粒度不匹配机制存在；(b) execution_stop 退出不释放 key；(c) 审计按 signal_key 检测到重复入场。但"5 笔重入由 type2str()/klu_idx 演变导致"未经逐笔验证，应降级为"最可能的机制"。
6. setup_invalidation 收盘穿越逻辑不在策略层实现。setup_invalidation_price 在 entry_policy.py:118-119/181-200 计算，存入 SignalDecision，但 MinimalChanTrendStrategy.on_bar 不引用它做重入决策。
【补充】release_signal 匹配逻辑（strategy.py:94-100）按 (bi_idx, klu_idx, type2str()) 匹配——若 type2str() 已演变，release_signal 释放的是旧 key 而非当前 BSP 的 key，可能导致释放失效。
期望行为: 青派双止损设计意图（基线 §4.3）：execution_stop 盘中触及仅本次订单退出，整套结构未必失效；已收盘 close 穿越 setup_invalidation 才是分析前提失效。逻辑上 setup 未失效应允许重入，但节奏（冷却/次数上限）未被契约定义。基线 §4.3 明确"是否允许同一 setup 在保护止损后重入，当前契约没有定义"。
违反的规则或不变量: 未违反硬性不变量。不变量2——_consumed_keys 是策略层本地去重集，非信号生命周期状态，不涉及对已关闭 signal_key 的回写。不变量8——重入产生新 SignalDecision/TradeIntent，不混淆五状态。不变量7——execution_stop 退出不被重入逻辑阻止。但重入节奏未被 QP-EXIT-001 或任何契约显式覆盖，属于规则定义缺口而非违反。
上游原因: _consumed_keys 的 key 粒度（bi_idx, klu_idx, type2str(), is_buy）与 signal_key 粒度（bi_idx, direction, primary_bsp）不一致。策略层没有显式"保护止损后重入"策略，依赖 _consumed_keys 隐式去重——但由于粒度更细，type2str() 或 klu_idx 演变产生新 key 绕过去重。
受影响的下游模块: 审计层（signal_key 级别重复入场统计）、成本统计（重复试错费用累积）、风控连续亏损计数（止损后重入再止损叠加 consecutive_losses，backtest.py:440 调用 risk.on_fill）、回测净值（重入 PnL 已计入但无独立风控门禁）。
最小可证伪修改: 对抗验证修正了原方案的跨层问题：
1. on_position_closed 钩子应放在 GradedChanStrategy（而非 MinimalChanTrendStrategy），因为 GradedChanStrategy 已持有 SignalDecision（含 setup_invalidation_price），不引入跨层依赖：
   - close_reason == execution_stop 且 last_close 未穿越 setup_invalidation → 调用 _inner.release_signal（显式表达"setup 未失效，允许重入"）
   - close_reason 涉及 setup_invalidation 收盘穿越 → 不释放（永久阻止该 key）
2. 将 _consumed_keys key 粒度从 (bi_idx, klu_idx, type2str(), is_buy) 对齐到 (bi_idx, primary_bsp, is_buy)，消除 type2str() 演变隐性重入。同步修改 release_signal 匹配逻辑（strategy.py:94-100），否则释放失效。
3. 冷却 bar 数和最大重入次数作为 engineering_default（默认 cooldown=0, max_reentries=3）。
可证伪：walk-forward OOS 比较显式释放+粒度对齐 vs 当前隐性行为的重入笔数和 PnL 分布。若维持现状，需在契约中显式记录"重入由 type2str/klu_idx 演变隐式允许，非显式策略"。
【裁决注】对抗验证判定 modification_is_minimal_and_falsifiable=false，因为原方案含三项相互关联修改（钩子+粒度对齐+节奏参数），非严格"最小"。但三项相互关联（钩子需粒度对齐才能正确工作，节奏参数需钩子才能强制执行），作为 engineering_default 可接受。这是 PARTIALLY_CONFIRMED 的原因。
必须新增的测试: (1) BSP type2str() 演变场景：构造 BSP 从 {"1"} 演变为 {"1","1p"} 的回放序列，验证当前实现是否产生同 signal_key 第二次入场（验证因果推断的关键测试）。(2) execution_stop 退出后同 key 重入：构造保护止损触发但 setup_invalidation 未被收盘穿越，验证 _consumed_keys 是否阻止重入（预期：type2str/klu_idx 不变则阻止，变化则允许）。(3) setup_invalidation 收盘穿越后重入：验证 setup 失效后是否永久阻止同 signal_key 重入。(4) 比较测试：显式 release_on_stop vs 当前隐性，walk-forward OOS 比较重入笔数和 PnL。(5) 若实施粒度对齐，测试 release_signal 匹配逻辑在新粒度下是否正确释放目标 key。
可能的反作用: 显式释放 key 后重入可能更频繁（当前 type2str() 演变概率不确定，显式释放则每次 execution_stop 后都允许重入），若不加冷却可能增加成本损耗。粒度对齐后 type2str() 演变不再产生新 key，可能减少某些合法重复试错机会。粒度对齐后需同步修改 release_signal 匹配逻辑，否则释放失效。
是否需要人工裁决: 是。青派逻辑层面 execution_stop 后 setup 未失效时允许重入是正确的，不应阻止。但当前通过粒度不匹配"隐式允许"重入是脆弱的——若有人将 _consumed_keys 对齐到 signal_key 粒度（合理重构），重入会被意外阻止，与青派意图矛盾。建议将重入策略从隐性改为显式（钩子置于 GradedChanStrategy + 粒度对齐 + release_signal 同步修改），节奏参数（冷却/次数上限）需人工裁决后才能设为 engineering_default。裁决前维持现状，但需在契约 provisional 文档显式记录"重入由 type2str/klu_idx 演变隐式允许"这一实现事实。
```

#### G05 — 结构止损能否有成本/波动下限

```
Finding ID: G05
所属层: 仓位/退出/执行
证据类型: 代码事实 + 规则契约 + 研究观察
当前行为:
1. execution_stop 完全取结构价：_calc_qingpai_levels 中 execution_stop = float(event.structural_price)（entry_policy.py:183-187），无任何成本/波动下限。
2. 对 BSP 1/1p，setup_invalidation = execution_stop（entry_policy.py:190-191），二者同值——对 1/1p 扩大 execution_stop 等于改变 setup_invalidation。
3. 仓位计算按 execution_stop 距离：_size_decision 中 stop = decision.execution_stop_price（decision_pipeline.py:162），self._sizer.calculate(initial_stop_price=float(stop), entry_price=entry)（:193-198）。
4. FixedFractionalSizer 纯用结构距离 stop_distance = abs(entry - stop)（sizing.py:98），无下限；ATRSizer 已有 stop_distance = max(structural_distance, atr*atr_multiplier)（sizing.py:163），但仅作用于 sizing，execution_stop 本身不变。
5. 不足一手拒绝：if lots < 1: rejected("risk_budget_below_one_lot")（decision_pipeline.py:212-224），但 lots=1 且成本占风险 31% 时不拒绝。
6. StructureStopRule._resolve_stop 直接用 ctx.initial_stop_price（=execution_stop）作触发价（rules.py:85-90），无成本下限；跳空时 _executable_stop_price 用 open 价（rules.py:122-128）。
7. RiskManager.approve（risk.py:90-150）不检查止损距离或成本/风险比。
期望行为: QP-RISK-001(fixed)：没有清晰结构止损的信号不交易；止损越宽仓位越小。基线 §4.3：execution_stop 是"本次订单的小止损"，setup_invalidation 是"整套分析前提失效"，二者应分离。不变量5（先止损后仓位，不足一手拒绝）和6（执行）要求 execution_stop 忠实反映结构止损位。问题核心：当结构止损过近导致成本/初始风险比例过高时，是否允许加入执行层下限。
违反的规则或不变量: 未违反。当前实现忠实执行 QP-RISK-001 和不变量5/6。无成本/波动下限不构成不变量违反——它是经济有效性问题，不是实现正确性或青派符合度问题。
上游原因: 结构止损完全取笔端极值（structural_price），无执行层成本/波动约束；当 BSP 入场价距笔端极值过近（如小级别背驰后入场），止损距离可小至个位数点数，导致 2*(fee+slippage) 占初始风险比例过高。
受影响的下游模块: StructureStopRule 触发价、FixedFractionalSizer 的 lots 计算、实际交易频率与成本占比、1/1p 信号的 setup_invalidation 语义（若选择扩大 execution_stop）。
最小可证伪修改: 维持 execution_stop = structural_price 不变；在 DecisionPipeline._size_decision 中新增可选的 min_stop_distance 入场过滤（engineering_default）：
    stop_distance = abs(entry - float(stop))
    if stop_distance < min_stop_distance:
        return replace(decision, accepted=False, reason_codes=...+("stop_distance_below_min",))
该检查在 stop 确认非 None（:166 后）且 sizing 计算（:193 前）之间插入。保持结构止损不变、setup_invalidation 不变、ATRSizer 已有下限不变，仅在入场门禁增加成本/波动下限检查。min_stop_distance 可基于 2*(fee+slippage) 倍数或 ATR 分数设定，在研究配置比较。
反对直接扩大 execution_stop 到 max(structural, entry±min_distance)——因为对 1/1p 这会改变 setup_invalidation（:190-191 二者同值），违反"不改变 setup_invalidation"前提；且对其他类型也会使保护止损脱离结构锚点，违反 QP-RISK-001 的结构止损语义。
必须新增的测试: (1) min_stop_distance 过滤：构造 stop_distance < min_stop_distance 的信号，断言 decision.accepted == False 且 reason_codes 含 "stop_distance_below_min"。(2) 1/1p 信号 execution_stop == setup_invalidation 不变（回归保护）。(3) ATRSizer 的 max(structural, atr*mult) 仍生效且 execution_stop 不被修改。(4) close-only 信号不被 min_stop_distance 过滤阻止（不变量7保护，close-only 在 _size_decision 之前处理 decision_pipeline.py:119-126）。(5) holdout 比较 min_stop_distance=0（现状）vs 2*round_trip_cost vs 0.5*ATR 三组的交易频率、成本/风险比中位数、总期望。
可能的反作用: 加入 min_stop_distance 过滤会减少交易频率（过滤窄止损信号），可能错过部分小级别 BSP 交易；阈值需在未见样本验证不降低总期望。错误地扩大 execution_stop：对 1/1p 破坏 setup_invalidation 语义；对 lots=1 固定仓位会直接增加单笔绝对风险。
是否需要人工裁决: 部分（min_stop_distance 的阈值选择，以及是否启用此过滤；当前标记为 engineering_default 候选）。
```

#### G07 — 反向 BSP 平仓是否要求入口规则也通过

```
Finding ID: G07
所属层: 退出
证据类型: 代码事实 + 规则契约
当前行为: OppositeSignalRule.check() 只读取 ctx.opposite_signal_triggered 布尔标志，不检查反向信号是否通过入口管线（rules.py:369-379）。该标志由三处调用方设置，逻辑一致：_is_confirmed_opposite / _confirmed_opposite 只检查 intent.event.state == SignalState.CONFIRMED 且方向与持仓相反，不检查 intent.accepted（backtest.py:1015-1024; live_engine.py:748-755; chan_bsp_strategy.py:1994-2002）。intent 是 decision_kernel.evaluate_bar() 的返回值，经 strategy.evaluate_bar → decomposition → multi_level.apply → momentum.apply 完整入口管线（runtime_kernel.py:81-112）。对抗验证补充：multi_level.apply（multi_level.py:547-554）与 momentum.apply（qingpai_momentum.py:103-112）在拒绝时均用 replace(intent.decision, accepted=False)，只替换 decision 字段，不修改 intent.event，因此 event.state 仍可为 CONFIRMED。故一个被多级别/动量拒绝开仓（accepted=False）的反向信号，仍可触发原仓平仓。三端实现一致，满足不变量9。另注：OppositeSignalRule.only_high_grade 参数在 __init__ 和 rule_id 中声明/使用（rules.py:358,362,366），影响规则命名和审计可追溯性，但 check() 不用它做出场决策——小瑕疵。
期望行为: QP-EXIT-001 只规定出场优先级，未规定反向信号的准入门槛。减风险不变量7 倾向于让出场更容易而非更难。因此反向 BSP 只需结构确认（CONFIRMED）即可触发平仓，无需通过入场全链（accepted），是符合规则的非对称设计。
违反的规则或不变量: 未违反。非对称设计（入场严、出场宽）符合不变量7 精神。QP-EXIT-001 未规定反向信号需通过入口准入门槛。
上游原因: 非对称设计是有意为之：入场需通过完整 DecisionPipeline 才能 accepted=True 并开仓；出场只需 SignalEvent 到达 CONFIRMED。multi_level.apply 和 momentum.apply 在拒绝时只 replace decision.accepted=False 不修改 intent.event，证明结构反转语义与开仓准入语义有意解耦。
受影响的下游模块: 出场频率、盈利持仓时长、反手语义。若收紧门槛要求 accepted=True，出场减少，持仓时长延长，可能增加盈利但也可能让本该锁定的利润回吐；反手语义从"结构反转即切换"变为"结构反转且通过全链才切换"。研究证据（若可信）显示反向信号退出是最大正贡献来源（86 笔中 31 笔 +1047 点）。
最小可证伪修改: 维持现状。理由：(1) 入场与出场服务不同目的——入场过滤链精选高质量建仓点，出场规则管理风险和锁定利润；将入场门槛强加于出场会违反不变量7 精神。(2) 反向 BSP 的"结构确认(CONFIRMED)"本身就是缠论层面的反转事件，其语义独立于多级别/动量过滤——这些过滤判断"是否值得开新仓"，而非判断"结构是否已反转"。(3) 若要证伪，可作为 research_candidate 在研究配置增加 require_opposite_accepted=True 变体进行样本外比较，但不应作为默认或 fixed 契约。另建议清理 only_high_grade 死代码（独立小瑕疵）。若未来实施该变体，需注意它读取 intent.accepted 而非 event.state，且需在 ExitContext 暴露该字段（当前不携带 accepted 信息）。
必须新增的测试: 不需要为维持现状新增测试。建议补一个回归测试验证三端 _is_confirmed_opposite / _confirmed_opposite 逻辑一致性（三处代码重复，存在漂移风险）。另建议补一个测试验证 only_high_grade=True 时行为是否符合预期（当前为死代码，测试会暴露问题）。
可能的反作用: 维持现状的反面风险：某些被动量/多级别拒绝的反向信号可能是噪声（假反转），在这些时刻平仓会过早离场、错失后续利润。但研究证据（若可信）表明净效果为正。若未来市场体制变化导致假反转增多，可通过 research_candidate 变体在样本外验证后切换，无需现在改变默认。
是否需要人工裁决: 否。
```

### 优先级 4：参数与经济效果

#### G09 — 动态仓位是否真的需要

```
Finding ID: G09
所属层: 风控
证据类型: 代码事实 + 规则契约核对
当前行为: 代码已实现完整的动态仓位计算链路，但被 max_abs_position=1 截断为 0/1 门控。
1. FixedFractionalSizer.calculate（sizing.py:90-105）：lots = floor(equity * risk_pct / (stop_distance * multiplier))。以配置 capital=100000、risk_pct=0.01、multiplier=10 计算：lots = floor(1000 / (stop_distance * 10))。stop_distance<=100pts→>=1 手；>100pts→0 手。
2. DecisionPipeline._size_decision（decision_pipeline.py:193-200）：先调 sizer.calculate 得动态手数，再 min(lots, max_abs_position)。max_abs_position=1 → 结果只能是 0 或 1。
3. 不足一手拒绝（decision_pipeline.py:212-224）：lots<1 时以 "risk_budget_below_one_lot" 或 "margin_budget_below_one_lot" 拒绝，忠实执行不变量5。
4. 保证金上限（decision_pipeline.py:202-211）：margin_lots = floor(available_funds * max_margin_utilization / (entry * multiplier * margin_rate))，再 min(lots, margin_lots)。以 entry~3500, multiplier=10, margin_rate=0.13, available_funds~100000, max_margin_utilization=0.50 估算：margin_lots=10，在 max_abs_position=1 下不构成约束。
5. 手数回写信号（graded_strategy.py:268-271）：lots = int(decision.position_size_hint)，signed_target = ±lots。
6. 配置确认（rb_15m_qingpai_strict.yaml:88,95-97,102）：max_abs_position=1, method=fixed_fractional, capital=100000, risk_pct=0.01, multiplier=10。
7. 执行引擎（execution.py:53-78）使用单一 fill_price 处理整笔 quantity_delta，无部分成交建模；slippage_points 固定不随手数缩放。当前 max_abs_position=1 下此简化无影响。
ATRSizer（sizing.py:113-169）同样已实现，:163 stop_distance = max(structural_distance, atr*atr_multiplier)，但同样会被 max_abs_position 钳制。
期望行为: QP-RISK-002(eng_default)：单笔风险预算以决策时账户总权益计算；可用资金、保证金和 max_abs_position 只作为最终可执行手数上限。规则并未要求动态仓位必须产出 >1 手数；它只要求风险预算公式驱动计算、max_abs_position 作为上限。当前实现符合该契约：公式先算、上限后裁。max_abs_position=1 是合法上限选择，导致公式退化为 0/1 门控，是上限选择的后果，非规则违反。
违反的规则或不变量: 未违反。QP-RISK-002 被忠实执行（公式驱动计算 sizing.py:103-104，account_equity 传入 decision_pipeline.py:197，max_abs_position 作上限 :199-200，不足一手拒绝 :212-224 符合不变量5）。
上游原因: max_abs_position=1 是配置选择（yaml:88），不是代码缺陷。sizing 公式本身是动态的，只是上限设为 1。
受影响的下游模块: 收益波动率、单笔风险敞口、风险预算利用率。max_abs_position=1 下每笔实际风险随 stop_distance 线性变化（止损宽则风险=1% 权益，止损窄则风险<1% 权益），无法实现真正等风险加仓；但不影响信号接受/风控审批/执行撮合的任何下游状态机。
最小可证伪修改: 维持现状。理由：(a) 动态仓位基础设施（FixedFractionalSizer、ATRSizer、make_sizer 工厂）已完整且正确实现，将 max_abs_position 调高即可激活，零代码改动；(b) max_abs_position=1 是 v1 的合法工程默认——固定 1 手隔离信号质量评估与仓位噪声，是 CTA 研究管线标准做法；(c) 当前 0/1 退化不是结构缺陷，是上限选择的预期后果。若要证伪"动态仓位能改善未见样本绩效"，最小可证伪修改是在研究配置层面创建 max_abs_position=3 的变体（config_loader 已支持），在同一 holdout 上对比 Sharpe/PF/maxDD——这是 research_candidate 比较而非结构修复。不应为此修改 sizing.py 或 decision_pipeline.py 的任何结构。不破坏第7节任何不变量，不跨越架构边界。
必须新增的测试: 无需新增测试以维持现状。现有 test_p2_risk_pipeline.py:106-120 已覆盖 FixedFractionalSizer 边界（10 手、5 手在 equity=50000 下、0 手、零止损距）。若未来以 max_abs_position>1 变体做研究对比，需新增测试验证：手数>1 时 risk.approve 的 max_abs_position 上限、margin_cap_applied 标记、多手 position_size_hint 回写信号后的 execution quantity_delta 正确性。
可能的反作用: 若未来将 max_abs_position 调高以激活动态仓位，需注意：(1) 加仓会放大止损跳空时的实际亏损，max_loss_points/daily_loss_limit 的点数预算需同步评估；(2) 保证金占用随手数线性增长，max_margin_utilization 可能更早触顶；(3) execution.py:53-78 按单一 fill_price 处理整笔 quantity_delta，未建模部分成交，且 slippage_points 固定不随手数缩放——多手订单的市场冲击未反映在回测中，需复核成交价假设。当前 max_abs_position=1 下这些均不触发。唯一建议（非阻塞）：在配置注释或研究文档明确说明"max_abs_position=1 时 fixed_fractional 退化为风险预算门控，非真实等风险加仓"，避免后续研究者误解 risk_pct 语义。
是否需要人工裁决: 否。
```

## 4. 建议的下一步顺序

1. **立即修 G06(1)**：daily_loss_limit 日切恢复是实现层 bug，不需裁决。新增 `RiskManager.on_new_day(date)`，在 backtest 主循环和 live engine 按 日调用。这是阻断长回放产生有效经济证据的根本障碍。
2. **裁决 G06(2)**：max_consecutive_losses 恢复语义三选一（永久/按日/冷却）。裁决后写入规则契约，明确恢复事件。在此之前维持现状，但将连续状态口径的 0-trade 如实标注为"吸收态预期行为"。
3. **实施 G08 代码修复**：在 `runtime_kernel.py:96-102` 为多级别拒绝补 `release_signal`，镜像动量拒绝。先用 blanket release（不区分拒绝类型），在未见 holdout 观察 parent_direction_conflict 释放后重评入场绩效，再裁决是否按 reason_codes 精细过滤。
4. **一并裁决 G03**：G08 修复会改变 G03 的重入实际行为。两者应一并裁决：先确定哪些拒绝类型应释放（G08），再确定释放后重入节奏（G03 的冷却/次数上限）。若实施 G03 显式化，钩子置于 GradedChanStrategy + 粒度对齐 + release_signal 同步修改。
5. **G05 入场过滤**：作为 engineering_default 加入 DecisionPipelineConfig，在未见 holdout 比较 min_stop_distance 阈值。不扩大 execution_stop。
6. **维持现状项（G01/G02/G04/G07/G09/G10）**：当前实现是正确的 chosen default / engineering_default。将"是否改进"列为 research_candidate，待 BSP×regime 矩阵固化（G01）、2/2s 样本足够（G02）、研究分支对照（G04）、require_opposite_accepted 变体（G07）、max_abs_position>1 变体（G09）、OPEN-004 解决（G10）后，在**新的未见时间区间**证伪——当前 holdout 已被反复查看，不再是未见数据（不变量10）。
7. **清理死代码**：ParentTrendFilter（G04）、OppositeSignalRule.only_high_grade（G07）、ChanDivergenceExitRule 的 TIGHTEN_STOP 双层死代码（G10）——标注 research-only 或删除，避免误导维护者认为已生效。

所有修改须保持第7节 10 个不变量不破，并按基线第8节修改影响矩阵重验相关链路。任何依赖长回放经济证据的裁决，必须在 G06 吸收态修复后重新评估。
