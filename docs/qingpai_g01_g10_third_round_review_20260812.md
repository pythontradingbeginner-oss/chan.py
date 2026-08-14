# 青派 G01-G10 第三轮交叉审查裁决

更新时间：2026-08-12

审查对象：

- `审查基线_0812_3A_gemini.md`
- `审查基线_0812_3B_deepseek.md`
- `审查基线_0812_3C_glm5.2.md`

被复审文件：`docs/qingpai_g01_g10_second_round_review_20260812.md`

## 1. 结论

第三轮没有推翻第二轮裁决，反而进一步确认：现在可以进入 P0 工程设计，但还不能直接照任一 AI 的伪代码实施。

三份复审共同确认：

1. G06 必须实现 RB `trading_day` 驱动的风险 session 推进，不能使用自然日。
2. 回测已逐 bar 观察账户权益，CTA `ChanBspStrategy` 尚未逐 bar 观察，最大回撤峰值会被低估。
3. 轻量 `live_engine` 仍使用日亏和连续亏损门控，因此同样需要交易日日切；其最大回撤当前是 `disabled_by_design`。
4. G08 的九个 reason code 已完整列出，当前没有任何 reason 可以通过直接删除 `_consumed_keys` 来形成完整、可审计的恢复语义。
5. `parent_structure_unconfirmed` 在当前严格配置下不可达，但在 `require_confirmed_parent=False` 的其他配置下可达。
6. `release_signal()` 当前调用传入原始 `StrategySignal` 快照，所以后续 BSP 的 `type2str()` 演变不会让现有两个释放调用失配。
7. P0 必须先冻结审计 schema；只修 G06 后重跑，再实施 G03/G08 最小状态机并第二次重跑。

新增且必须吸收的实施事实：`trading_day` 虽然已存在于数据管线和生产 parquet 中，`prepare_ohlc_frame()` 也会保留该列，但 `chan_futures/backtest.py` 当前完全不读取它。因此 P0 不只是修改 `RiskManager`，还必须打通数据行、回测主循环、CTA/live 适配器到风险状态机的 session-key 传递。

## 2. 对第三轮三份意见的裁决

### Gemini

Gemini 的总体实施顺序正确，但不能原样采用，原因如下：

1. 它一边要求契约状态名标准化，一边又发明 `P0_PARITY_FIX` 和 `P1_SPEC_IMPLEMENTED`。当前机器契约只允许 `fixed`、`engineering_default`、`research_candidate`、`deferred`；P0/P1 应是实施优先级，不是规则状态。
2. `P1_SPEC_IMPLEMENTED` 还把尚未实施的状态机提前标记成 implemented，事实状态错误。
3. `canonical_setup_id` 仅用“根结构 BSP 时间戳与级别”生成过于粗糙，无法保证合约换月、方向、BSP 规则族和同时间多结构不碰撞。
4. CTA 初始化峰值不能无条件使用配置中的 initial capital。正式 CTA 应优先采用首个有效、作用域明确的账户权益快照；配置资金只能作为回测、shadow 或账户不可用时的显式 fallback。
5. 伪代码中的 `_daily_realized_pnl`、`_daily_paused`、`_logger` 都不是当前 `RiskManager` 字段，不能当作最小补丁直接复制。
6. 文档中的 `[cite: 4]` 是残留占位符，不是有效证据引用。
7. G07 不能同时模糊标成 `fixed / research_candidate`。当前“确认反向 BSP 可平仓”是版本默认，grade-aware 变体才是 research candidate；假开关清理属于工程一致性。

### DeepSeek

DeepSeek 对第二轮文档的核验整体准确，以下意见应吸收：

- backtest 主循环必须显式消费 `trading_day`。
- 轻量 live engine 虽关闭最大回撤，仍需修复日亏 session 吸收态。
- 双层身份应先形成 schema，明确写入哪些 CSV/记录对象。
- 基于结构事件的重新武装与固定 bar 冷却不是同一种研究假设，应分开。

一处措辞仍需修正：当前 release 匹配的保证不依赖“同一根 bar”，而依赖调用方保存并传回消费时的原始 `StrategySignal` 快照。即使保护止损发生在多根 bar 以后，只要保存的是原 entry signal，类型快照仍可匹配；若未来从最新 BSP 重建 signal，才可能失配。

“新的 5m 同向确认”具有多级别结构依据，但“保护止损后必须以此重新武装”仍未被当前规则契约定义，不能直接升级为 fixed。

### GLM

GLM 的三项补充成立：

- backtest 尚未消费已有的 `trading_day` 列。
- 轻量 live engine 的最大回撤是能力禁用，不应伪装成 parity 已通过。
- reason 白名单释放并非第一轮综合稿的主张，后续引用应标明建议来源。

GLM 对 `release_signal()` 忽略 `is_buy` 的代码观察准确，但严重性需要降级。合法结构下同一 `bi_idx/klu_idx/type2str` 通常不应同时出现相反 `is_buy`；因此这首先是键定义与查询不一致、缺少不变量测试的问题，不足以在没有反例前断言已经发生双向误释放。P0 schema 应消除这种歧义，并增加结构不变量测试。

## 3. P0 前必须冻结的三个契约

### 3.1 风险 session 推进契约

建议接口概念为：

```text
advance_session(session_key, observed_at, source)
```

其中 `session_key` 对 RB 为规范化 `trading_day`，但接口本身不应把 RiskManager 与 RB 日历实现耦死。

必须定义：

- 同一 session 重复调用为幂等操作。
- 只允许时间向前推进；旧 bar、迟到事件不得把风险状态回滚到旧交易日。
- session 前进时只重置明确属于 session 的状态，例如 `_daily_realized`。
- 恢复持久化状态后，首次同 session 事件不得再次清零。
- session key 缺失、日历越界或时间倒退时失败关闭并记录原因。
- 审批前必须推进 session；平仓 fill 更新风险状态前同样必须解析 fill 所属交易日，不能假设一定先收到 bar。

最后一项很重要：实盘订单/成交回报与 bar 是异步事件。若只在 `on_bar()` 调 `advance_session()`，新交易日首个 fill 可能先于首根策略 bar 到达，日内 PnL 会归错 session。

### 3.2 账户权益观测契约

必须先定义“账户权益”的作用域和单位：

- 回测使用 `initial_capital + mark_to_market_points × contract_multiplier`。
- CTA 当前 `_account_equity()` 会汇总 OMS 的全部账户 balance；多账户、多 gateway 或账户内其他策略是否应纳入 RB 风控，需要明确。
- 正式 CTA 初始峰值应来自首个有效账户权益快照，而不是无条件使用研究配置 capital。
- 账户权益不可用时，应明确 `fail_closed`、沿用最近快照或 shadow fallback，不能静默改变基准。
- 最大回撤使用货币权益；`daily_loss_limit`、`max_loss_points` 当前使用点数。两类单位必须在状态和报告字段中显式区分。

逐 bar `observe_equity()` 只有在权益来源、作用域和单位确定后才具有 parity 意义。

### 3.3 Setup 身份契约

Gemini 的“时间戳 + 级别”不足以构成稳定 SetupID。候选 schema 至少必须表达：

- 实际合约或 contract epoch，禁止跨换月复用。
- 本级 timeframe。
- 方向。
- 根结构锚点，例如稳定的笔起止身份或经过版本化的结构 anchor。
- setup 规则族，明确多个 BSP 类型是同一 setup 的 revision 还是不同 setup。
- schema version，保证未来升级可追溯。

`canonical_setup_id` 的具体字段仍需人工裁决。当前 `signal_key=(contract,timeframe,bi_idx,direction,primary_bsp)` 可以作为候选输入，但不能直接等同 setup ID，因为 `primary_bsp` 演变可能把同一结构试错拆成不同生命周期。

## 4. 审计 schema 应落在哪里

P0 schema 不应只写一段文字，建议形成版本化 JSON Schema 和字段字典，并落入以下审计链：

| 记录 | 应新增的最小字段 | 用途 |
|---|---|---|
| `DecisionTraceRecord` / `decision_trace.csv` | raw identity 各维、canonical setup candidate、setup schema version、risk session key | 三端决策 parity 与跨 revision 归并 |
| `execution_decisions.csv` | canonical setup candidate、risk state、risk session key、审批前后状态 | 区分规则接受、风险拒绝和成交 |
| `trades.csv` | canonical setup candidate、entry attempt sequence、prior exit reason | G03 重入审计 |
| 风险状态持久化 | current session key、last observed_at、peak equity、equity source/scope | 重启与乱序事件一致性 |

`SignalDecision` 是通用规则决策对象，不宜为了 RB 特有 session 字段无限扩张。运行时和执行审计字段优先放在 `DecisionTraceRecord`、execution audit 和风险状态中。

## 5. P0 的准确改动边界

### P0-A：只定义与记录，不改变交易集合

1. 新增审计 schema/字段字典。
2. 为旧消费键生成 `raw_signal_identity`。
3. 生成 `canonical_setup_candidate`，但只记录，不参与去重、重评或成交。
4. 记录 `risk_session_key`、权益来源、权益单位和运行端能力状态。

### P0-B：只修 G06 与 parity

1. `RiskManager` 增加幂等、前进式 session 推进 API。
2. backtest 从 bar 行读取 `trading_day`；对不含该列的通用输入，使用同一 calendar adapter 映射，映射失败则按配置失败关闭。
3. CTA/live 在 bar、审批和 fill 风险更新前使用同一 adapter 推进 session。
4. CTA 每根可交易 bar 观察作用域明确的账户权益，并正确初始化峰值。
5. 轻量 live engine 明确记录 `max_drawdown=disabled_by_design`。
6. 不在本阶段改变 consecutive-loss、max-loss、max-drawdown 的恢复策略。

### P0-C：验证与第一次重跑

必须新增：

- 周五夜盘归入下周一交易日。
- 同 session 幂等、跨 session 清零、旧事件不回滚。
- 重启恢复后不重复清零。
- fill 先于首根 bar 到达时归入正确交易日。
- 日历越界和缺失 session key 失败关闭。
- 回测/CTA 对同一权益序列得到相同 peak/drawdown。
- 只修 G06 前后 decision parity 不变；变化只能出现在风险审批与后续成交集合。

第一次重跑只用于回答：修复 G06 后，连续状态为何恢复或仍停止、各风险门控触发多少次、交易集合如何变化。不能据此开始调 G01/G02/G05/G07 参数。

## 6. P1 与后续研究边界

P1 才开始改变 setup 资格：

1. `PENDING_CONTEXT -> ELIGIBLE/EXPIRED`。
2. `CONSUMED -> STOPPED_REARMABLE/INVALIDATED`。
3. 明确 parent conflict、子级窗口结束、结构失效和换月的终止事件。
4. 第二次重跑只测量 G03/G08 的增量影响。

G01/G02/G05/G07 继续停留在只读研究；G04=`deferred`、G09=`engineering_default`、G10=`default_until_resolved` 维持当前版本行为。

## 7. 最终判断

第三轮审查已足以结束“是否先修 G06”的讨论：应当先修。但开始编码前，还需要把 session、equity、setup identity 三份小契约和审计 schema 写清楚。

尤其不能直接采用以下内容：

- Gemini 新造的契约状态名。
- “时间戳 + 级别”的 SetupID。
- 直接用配置 capital 初始化正式 CTA 峰值。
- 只在 bar 到达时推进风险 session。
- 以 reason-code 白名单替代显式 pending 状态。

完成 P0 schema 后即可实施 G06，不需要再进行第四轮泛化讨论。下一次审查应针对具体 schema、接口和测试用例，而不是再次讨论 G01-G10 的方向。
