# 基线全局审查文档独立审核

审查对象：`docs/qingpai_chan_core_and_strategy_global_review_20260810.md`（青派缠论底层代码与 RB 交易策略全局审查基线）
审查日期：2026-08-12
审查者：程序化交易专家（独立审核）
代码基线：`qing_chan` 分支 HEAD `b122917` + 当前未提交工作区修订
验证方法：4 路并行核验代理 + 独立源码直读，共 108 次工具调用、约 389k tokens

---

## 0. 审查结论（先行）

**判定：代码事实层高度可靠，审计数据层完全可靠，但 §4.5 存在一处与当前工作区代码不符的时效性缺陷，§4.2 漏列一层实际生效的过滤管线。**

- §2 核心结构：10 项断言，8 confirmed / 2 partial（均为措辞精度问题，非事实错误）
- §4 策略链：13 项断言，13 confirmed（含 §4.7 同 bar 时序、§4.8 三端共享内核）
- §5 审计数据：8 项断言，7 confirmed / 1 partial（partial 为方法论陈述，非代码事实）
- §7 不变量：10 项，7 项由代码真正强制 / 2 项 partial / 1 项不可验证
- §8 影响矩阵：结构正确，2 处爆炸半径低估、1 行缺失

**关键发现（按严重度）：**
1. **[P1] §4.5 时效性缺陷**：文档第 4 行声明"包含当前未提交的机制审计修订"，但 §4.5 正文（L485-487）描述的是修订前的状态——日切吸收态、`on_fill` 依赖、缺少 session 初始化校验这三个被后两轮列为 P0 的问题，在当前工作区 `risk.py` 中已经修复。
2. **[P2] §4.2 漏列 FilterPipeline**：DC+OBV+成交量过滤管线在 `risk.approve` 之后、成交之前实际生效，能独立否决已通过风控的开仓/反转信号，但 13 步顺序中仅在步骤 12 以"外围过滤器"一笔带过。
3. **[P3] INV-9 一致性不变量措辞需收窄**：不变量本身成立，但核验代理指出的"`price_adjustment` 参数不对称"经独立源码核读后判定为**设计性正确**，非缺陷。

下文逐项展开。

---

## 1. 核心结构层（§2）核验结果

| 断言 | 核验结论 | 依据 |
|---|---|---|
| §2.2 包含合并方向取值（UP 取 max/max，DOWN 取 min/min） | confirmed | `KLine_Combiner.py:94-101` 与断言逐字一致 |
| §2.3 分型只在第 3 根合并 KLC 到来后更新倒数第 2 根 | confirmed | `KLine_List.py:122-133`，`len>=3` 门控 + `lst[-2].update_fx(lst[-3], lst[-1])` |
| §2.4 `can_make_bi` 三检查顺序 | confirmed | `BiList.py:178-186`，跨度/分型有效性/尾部极值三检查存在且顺序一致 |
| §2.4 MACD FULL_AREA 同色绝对面积无代数抵消、PEAK 取同色最大绝对高度 | confirmed | `Bi.py:225-247`，`abs()` + 同向 guard，无抵消 |
| §2.5 CEigenFX 反向笔包含处理 + 三特征序列分型 | confirmed | `EigenFX.py`，`bi.dir != self.dir` 断言、三 ele 槽、`treat_third_ele` 调 `update_fx` |
| §2.5 `cal_seg_sure` "从最后稳定边界向后计算" | **partial** | 见下 |
| §2.6 ZD/ZG 定义、有效重叠 `ZG>ZD`、peak_low/peak_high 为全波动极值 | confirmed | `ZS.py:89-92,99-106`、`ZSList.py:87-89`，注释明确区分核心范围与波动范围 |
| §2.6 背驰先要求出中枢笔有效突破再比较力度 | confirmed | `ZS.py:162-174`，`end_bi_break` 前置 |
| §2.7 `bsp3_back2zs` 三买回踩破 ZG / 三卖反弹破 ZD 即拒绝 | confirmed | `BSPointList.py:419-420` |
| §2.7 严格配置使核心"松"（`.inf` 背驰率实际关闭背驰检查） | confirmed | YAML `.inf` → `float('inf')`，`ZS.py:171` `divergence_rate>100` 自动通过 |

**§2.5 partial 说明**：文档称 `cal_seg_sure` "从最后稳定边界向后计算"，但实际机制是 `do_init()` 先弹出未确认尾段（向后清理），再从 `begin_idx = self[-1].end_bi.idx+1` **向前**扫描（`SegListChan.py:28-34`）。净效果（"只重算未确认尾部"）表述正确，但"向后计算"的方向性描述不准确。属措辞精度问题，不影响系统行为判断。

---

## 2. 策略链层（§4）核验结果

13 项断言全部 confirmed，关键项佐证：

| 断言 | 核验依据 |
|---|---|
| §4.2 13 步顺序 | `runtime_kernel.py:60-130` + `decision_pipeline.py` + `backtest.py:612-827` 逐段对应 |
| §4.3 双止损语义表 | `entry_policy.py:181-200` `_calc_qingpai_levels`；`structural_price = _safe_bi_end(bsp)`（`extractor.py:123`）统一取笔端极值，三类 BSP 的 `execution_stop` 均解析为同一值——与表格一致 |
| §4.4 调整连续价/实际成交价分离 | `production.py:33-50` 同源聚合；`backtest.py:1064-1086` `_execution_ohlc` 用 raw 价；`graded_strategy.py:286-306` `_translate_event_prices` 统一映射 6 个锚点 |
| §4.5 仓位公式 + 不足一手拒绝 | `sizing.py:98-105` `max(0, floor(lots))` 返回 0 不强制 1；`decision_pipeline.py:212-224` 拒绝 |
| §4.6 ExitManager 6 规则顺序 + 小转折触发 + 192 时间止损 | `exit_rules/rules.py` + `base.py:533-535` |
| §4.7 同 bar 决策时序 | `backtest.py:718-762` 出场先于入场；新开仓用 close 价；同 bar high/low 不检查新仓止损；`blocked_by_exit_priority`（L762-773） |
| §4.8 三端共享 `RuntimeDecisionKernel` + 800 预热 + 恢复期禁开允平 | `runtime_kernel.py:1` docstring；`chan_bsp_strategy.py:964-968, 1442-1453`；`hard_risk.py:59-60` |

**§4.3 双止损表独立复核结论**：核验代理初报"execution_stop 命名不精确"。经我独立读 `extractor.py:123` + `entry_policy.py:183-187`，确认 `structural_price` 对所有 BSP 类型统一取 `bsp.bi.get_end_val()`，而 `_calc_qingpai_levels` 对所有类型令 `execution_stop = structural_price`。因此三类 BSP 的 `execution_stop` 在代码中确实都解析为笔端极值，与表格三行表述一致。**代理初报的"不精确"判定不成立，此处撤销。**

---

## 3. 审计数据层（§5）核验结果

8 项断言中 7 项 confirmed、1 项 partial（方法论陈述）。所有数字与报告产物**逐项精确匹配**：

| §5 断言 | 核验依据 |
|---|---|
| 44,826 bar / 3,658 决策 / 183 接受 / 10 成交 / 173 未成交 | `audit_summary.md` L10-11 + `run_meta.json` + `decision_mechanism_audit.csv`（3658 行） |
| 173 全部被 `max_consecutive_losses: 5 >= 5` 拒绝 | `audit_summary.md` L28 + CSV Counter 精确 173 |
| 10 笔全空头 / 9 结构止损 / 8 笔 2+2s / 动量全 not_applicable / 5 笔止损后重入 / 0 可见性失败 | `accepted_trades_audit.csv`（10 行）逐字段 |
| 连续 104 接受 0 成交 vs fold 103 接受 86 成交 +618 点 / 17 风控拒绝 | `risk_state_policy_report.md` L14-15 |
| Fold 年度净点 -68/+397/+297/+4/+7/-19 | 同上 L29-34 |
| 86 笔分解表内部算术自洽（38+48=86；65+21=86；42+31+11+1+1=86；净点三 partition 均和 618） | `fold_reset/all_trades.csv` 聚合 |
| holdout 11 笔全多头 / -48 点 / 胜率 27.27% / PF 0.40 / 1.5x -81 / 2x -92 / 8 结构止损 3 反向 | `holdout_r10_baseline.json` 三场景 + `run_p7_holdout.py:221-225` |
| +618 "不能作纯因果收益" 解释 | `run_meta.json` `decision_parity: continuous_only_count=2` + 报告 L42,47 自述结论——文档解释与报告自述一致 |

**partial 项**：§5.3 "该 holdout 已被人工和多个 AI 反复查看，不再是未见样本"——这是审查流程陈述，非代码事实，无法从源码确认或证伪。文档自身已将其框定为方法论告诫（L596, L628），与 INV-10 呼应。

**结论：基线文档的审计数据层完全可靠。** 后两轮所有经济判断都建立在这些数字上，它们经得起逐项核对。这是本文档最重要的资产。

---

## 4. 不变量层（§7）核验结果

| 不变量 | 结论 | 关键依据 |
|---|---|---|
| INV-1 时点 | confirmed | `multi_level.py:631-646` + `decision_pipeline.py:258-261` 双层强制 |
| INV-2 生命周期 | **partial** | 见下 |
| INV-3 身份 | confirmed | `runtime_kernel.py:165-186` `reset_contract_state` + signal_key 内嵌合约（`models.py:298-306`） |
| INV-4 价格域 | confirmed | `_translate_event_prices` 统一映射 6 锚点；执行用 raw 价 |
| INV-5 风险 | confirmed | `decision_pipeline.py:162-224` 先止损后仓位 + 不足一手拒绝 |
| INV-6 执行 | confirmed | 入场用 close；保护止损盘中触及（`rules.py:59-79`）；跳空用 `ctx.open`（`rules.py:122-128`） |
| INV-7 减风险 | confirmed | `chan_bsp_strategy.py:1442-1453` + `hard_risk.py:59-60` + `risk.py:132`（平仓 target=0 恒过） |
| INV-8 证据五态 | confirmed | 规则接受/风控/提交/成交/建仓五态在审计链可分离 |
| INV-9 一致性 | **partial（需收窄）** | 见下 |
| INV-10 研究 | unverifiable | 流程规则，无代码强制机制 |

### INV-2 partial：决策轨迹跨重启未锁定

文档称"历史决策不可重写"。分解状态机和信号事件确实 append-only（`qingpai_decomposition.py:235-236` 闭态不更新；`SignalEvent` frozen + 每次修订新 event_id）。但 `DecisionTraceRecord` 跨重启**未持久化锁定**：`runtime_state.py` 只恢复仓位/风控/订单，不恢复 `decision_trace`。重启后对同一 bar 重新求值可能产生与已落盘轨迹不同的新轨迹。建议将"历史决策不可变"收窄为"分解与信号不可变；决策轨迹在单次进程内 append-only，跨重启不保证一致"。

### INV-9 partial（收窄）：一致性不变量成立，代理指出的"不对称"实为设计性正确

核验代理标记 INV-9 为"aspirational"——理由是回测向 `evaluate_bar` 传 `price_adjustment`（`backtest.py:669`），而实盘 CTA 不传（`chan_bsp_strategy.py:569-591`，默认 `0.0`），导致结构价翻译在两端发散。

经我独立源码核读，**此判定不成立**：

- 回测 chan 由**调整连续 1m 源**聚合（`production.py:35` `adjusted_1m_path`），结构价处于调整域，必须经 `_translate_event_prices` 减去 adjustment 映射回合约域。
- 实盘 CTA chan 由 **VeighNa SQLite 真实合约 1m bar** 聚合（`chan_bsp_strategy.py:826-839` `_load_warmup_bars` → `cta_engine.load_bar`），结构价本就在合约域，无需翻译，故 `price_adjustment=0.0` **正确**。
- 两端 `DecisionTraceRecord` 中的价格最终都落在**实际合约域**，parity 工具（`parity.py:49-97`）比较的是归一化载荷。

**真正的 parity 风险**不在价格翻译，而在结构几何：回测 chan 建立在调整连续序列上，实盘 chan 建立在单合约原始序列上，换月点的笔/中枢/段结构本身可能不同。但这一点已被 **QP-RB-001** 明确承认（"研究结构使用调整后的连续序列；真实执行使用具体合约……不继承原始价差形成的笔和中枢"）。

**建议**：将 INV-9 措辞收窄为"三端共享 `RuntimeDecisionKernel` 与 `TradeIntent`；价格域经翻译后统一到合约域；结构几何在换月点存在调整连续 vs 单合约的已知差异，由 QP-RB-001 承认"。当前 INV-9 的"same normalized DecisionTraceRecord for same structural snapshot"在严格字面意义上不成立（snapshot 本身可能不同），但在工程 parity 目标上成立。

---

## 5. §4.5 时效性缺陷（关键发现 P1）

### 问题

文档第 4 行声明代码基线"包含当前未提交的机制审计修订"。但 §4.5 正文（L483-488）描述的三个"必须全局处理的状态问题"中，前两个在当前工作区 `chan_futures/risk.py` 中**已经修复**：

| 文档 §4.5 原文 | 当前工作区 `risk.py` 实际 | 状态 |
|---|---|---|
| L486 "日亏损只在 `on_fill()` 看到新日期时清零；一旦日亏门控阻止后续开仓，第二天没有 fill 触发日期切换" | `advance_session(session_key, observed_at, source)`（L236-274）独立于 fill，新交易日清零 `_daily_realized`（L273）；`backtest.py:615-619` 每 bar 调用 | **已修复** |
| L485 "连续亏损达到 5 后……连续回放会进入吸收态" | `advance_session` 不触碰 `consecutive_losses`，仅盈利 fill 清零（L226-227）——**此条仍成立** | 仍准确 |
| （文档未提及）session 未初始化校验 | `require_session_key` 配置（L38）+ `approve()` 返回 `risk_session_uninitialized`（L118-123） | **新增，未反映** |
| （文档未提及）OMS 权威权益源 | `observe_equity(current_equity, *, source, scope, max_drawdown_capability)`（L276-304），`oms_balance` 首次观测重置峰值（L301-302） | **新增，未反映** |
| （文档未提及）session 回归检测 | `advance_session` 检测 `candidate < current` 抛 `risk_session_regression`（L255-259） | **新增，未反映** |

### 影响

这是后两轮 G01-G10 审查反复聚焦的 P0 区。如果后两轮审查者以本文档 §4.5 为基线，会把"日切吸收态""trading-day 接入""逐 bar 权益观测"当作未解决问题提出——而这些在当前工作区已解决。**文档 §4.5 与其第 4 行的基线声明自相矛盾**：要么第 4 行的"包含未提交修订"应删除（即文档如实描述 HEAD b122917 状态），要么 §4.5 正文应更新为反映 `advance_session`/`require_session_key`/`observe_equity` 后的状态。

### 建议

更新 §4.5 正文：
1. 删除或改写 L486 关于日亏 `on_fill` 依赖的描述，改为"`advance_session` 每 bar 推进风险时钟，新交易日清零日内亏损，不依赖 fill 触发"。
2. 保留 L485 关于连续亏损吸收态的描述（仍成立）。
3. 新增 `require_session_key` / `risk_session_uninitialized` / OMS 权威权益源 / session 回归检测四项机制的说明。
4. 明确：`max_consecutive_losses` 仍无恢复事件（吸收态问题未解决），`max_loss_points` 600 点仍为永久停机——这两条与文档原意一致。

---

## 6. §4.2 FilterPipeline 漏列（关键发现 P2）

### 问题

`backtest.py:793-827` 在 `risk.approve` 通过之后、实际成交之前，对开仓/反转信号施加 DC 结构 + OBV + 成交量三层过滤（`chan_futures/filters.py::FilterPipeline`，`check_long`/`check_short`）。不通过则记录 execution_decision 并将 signal 置空，即**能独立否决已通过风控的信号**。

§4.2 的 13 步顺序中，步骤 12 仅以"外围过滤器和 `RiskManager` 再决定是否允许执行"一笔带过，未区分：
- `RiskManager.approve`（风控维度：仓位/亏损/回撤）
- `FilterPipeline`（市场结构维度：DC/OBV/成交量）

二者语义不同、顺序相邻但可分别触发拒绝原因。当前 `execution_decisions` 中应能区分 `risk_rejected` 与 filter 拒绝两类 reason。

### 影响

任何修改入场顺序的审查者若仅依据 §4.2 的 13 步，会漏掉 FilterPipeline 这一独立否决层，对"为何 risk.approve 通过但仍未成交"的归因产生偏差。§5.1 的 173 笔 `risk_rejected` 全部归因于 `max_consecutive_losses`——这提示在本次审计区间内 FilterPipeline 未触发拒绝（或触发已被并入其他 reason），但该层存在性应在文档中显式列出。

### 建议

将 §4.2 步骤 12 拆分为：
- 12a. `FilterPipeline`（DC+OBV+成交量）对开仓/反转信号施加市场结构过滤；
- 12b. `RiskManager.approve` 施加仓位/亏损/回撤风控。

并在 §4.6 出场规则表后补一行说明 FilterPipeline 仅作用于开仓/反转，平仓信号不拦截（`backtest.py:796` 注释已明确）。

---

## 7. §8 影响矩阵评估

矩阵结构正确，但 2 处爆炸半径低估、1 行缺失：

| 问题 | 现状 | 建议 |
|---|---|---|
| "风控恢复"行爆炸半径低估 | 仅列 `RiskManager.approve` 路径 | 补充 `hard_risk.evaluate_open_guard`（`hard_risk.py:113-123`）读同一 `daily_loss`/`drawdown` 状态，以及实盘三路对账（`chan_bsp_strategy.py:1891-1966` self.pos vs PositionContext vs OMS）。改恢复语义会同时影响软 `approve` 与硬 `open_guard`。 |
| "execution stop"行爆炸半径低估 | 未列 setup_candidate_id / attempt_sequence 追踪 | 补充 `backtest.py:463-497` 的 attempt 序号记账依赖止损触发的 exit_reason 分类。改 execution stop 语义会改重入记账。 |
| 缺失行：signal_key 组成 / setup_candidate_id | 未列 | signal_key 内嵌合约（`models.py:298-306` `new_signal_key`）是 INV-3 身份隔离的载体；改 signal_key 组成会同时影响身份、`consumed_keys` 释放、`setup_candidate_id`、跨合约重入。应单列一行。 |

另：核验代理指出 `ExitManager.check()` 的 `check_with_directive`（TIGHTEN_STOP/REDUCE）在当前配置下是死路径（`base.py:533-535` 只调 `rule.check(ctx)`，返回首个 ExitSignal）。文档 §4.6 已注明"保本移动和分批止盈默认关闭"——与死路径一致，但建议在 §8 矩阵注明这两条 directive 当前不触发，避免审查者误以为存在分批止盈执行路径。

---

## 8. 对后两轮 G01-G10 审查的回溯印证

本次基线核读澄清了后两轮审查中的若干争议：

1. **G06（风险门控恢复）**：后两轮将"日切吸收态""trading-day 接入""逐 bar 权益观测"列为 P0。本次确认这三项在当前工作区**已修复**（`advance_session`/`require_session_key`/`observe_equity`）。后两轮若以本文档 §4.5 为基线提出这些问题，其发现对 HEAD b122917 有效，对当前工作区**已过时**。唯一未解决的是 `max_consecutive_losses` 的吸收态（无恢复事件）——这一条后两轮的关切仍然成立。
2. **G09（动态仓位是否需要）**：后两轮曾有"50000 资本推导"的争议。本次确认 `max_abs_position=1` 使动态仓位只能得到 0/1，与文档 §4.1 表格一致；config 中无 50000 资本项。文档表述准确。
3. **G08（多级别拒绝后重评）**：本次确认 `release_signal` 匹配 `key[0],key[1],key[2]` 三元组，**遗漏 `key[3]`（is_buy）**（`strategy.py:92-101`）。若同一 `bi_idx+klu_idx+bsp_type` 存在相反方向（理论上 BSP 不会同时出现买卖，但合约换月或级别切换边界可能），释放会误删异向键。这是 G08 的真实缺陷，建议补全四元组匹配。
4. **release_signal 争议**：第二轮曾有"type2str 演化导致释放失败"的指控，后被证伪。本次独立读 `strategy.py:75`（`bsp_type=bsp.type2str()` 在消费时冻结入 StrategySignal）+ `runtime_kernel.py:118`（释放传原始 signal）确认：两处都用冻结值，匹配成功。第二轮证伪成立。

---

## 9. 最终判定

**作为"整套系统现在究竟怎样工作"的基线文档，本文档在代码事实、审计数据、不变量三个层面整体可靠，可作为后续审查的事实地基。** §5 审计数据层完全可靠，是最高价值资产。

需修正的三项按优先级：
1. **[P1] §4.5 时效性**：更新 L485-487 以反映 `advance_session`/`require_session_key`/`observe_equity` 后的状态；或删除第 4 行"包含未提交修订"声明。此项直接影响后两轮 G06 判断的有效性。
2. **[P2] §4.2 漏列 FilterPipeline**：拆分步骤 12 为 12a（市场结构过滤）/12b（风控），显式列出独立否决层。
3. **[P3] INV-2/INV-9 措辞收窄**：INV-2 决策轨迹跨重启未锁定；INV-9 结构几何在换月点存在已知差异（QP-RB-001 承认），"same structural snapshot"在字面意义上不成立。

次要项：§2.5 "向后计算"方向性措辞；§8 补 `hard_risk.open_guard`/`setup_candidate_id` 爆炸半径 + signal_key 组成行 + TIGHTEN_STOP/REDUCE 死路径标注；G08 release_signal 补全 is_buy 四元组匹配。

**程序化交易专家视角**：这是一个工程纪律显著高于典型研究型策略代码库的系统——五态证据分离、三端共享内核、时点不变量双层强制、审计数据可逐项复核。当前最需要补的不是更多功能，而是**把已落地的机制审计修订同步进基线文档**，避免后续审查者基于过时描述反复提出已解决问题。
