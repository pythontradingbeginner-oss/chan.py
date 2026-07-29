# 第三阶段——完整回测评估：新 session 上下文

> **生成时间**: 2026-07-07
> **前置阶段**: 第一阶段「信号规范化」✅ | 第二阶段「出场规则体系」✅
> **本阶段目标**: 基于前两阶段的产物，建立标准化的完整回测评估框架

---

## 一、前两阶段已完成的所有资产

### 1.1 信号规范化 (Stage 1)

**数据模型** (`signal_core/models.py`):
- `SignalEvent` —— 客观事实层。BSP 信号的扁平化快照
- `SignalAssessment` —— 评分层。structural_score + predictive_score
- `SignalDecision` —— 策略决策层。accepted/rejected + 入场计划
- `SignalRelation` —— 多级别共振关系
- `SignalOutcome` —— 信号历史结果标签 (forward_return, MFE/MAE, hit_1R/2R)
- `HumanAnnotation` —— 主观交易标注
- 枚举: `SignalState` (candidate/confirmed/invalidated/expired), `SignalDirection`, `ScoreGrade`

**提取器** (`signal_core/extractor.py`):
- `SignalExtractor` — CBS_Point → SignalEvent (带去重 + feature key 映射)

**生命周期与持久化** (`signal_core/lifecycle.py`):
- `SignalLifecycleTracker` — 按 signal_key 分组构建 revision 链
- `SignalJournal` — CSV/Parquet 增量持久化

**多级别关系** (`signal_core/relations.py`):
- `RelationBuilder` — 构建 SignalRelation, 检测未来函数泄漏 (`check_time_honesty`)

**评分器** (`signal_scoring/`):
- `T1Scorer` — 一买/盘背: 背驰度+中枢数+笔确认+振幅
- `T2Scorer` — 二买/类二买: 回撤幅度+一买锚点+笔确认
- `T3Scorer` — 三买: 中枢突破+回抽力度+再启动确认
- `assess_event(event)` — 便捷函数, 自动选评分器

**入口策略** (`strategy_policy/entry_policy.py`):
- `EntryPolicy` — 消费 SignalEvent+SignalAssessment, 产出 SignalDecision
- `EntryPolicyConfig` — 可配置: min_grade / accepted_bsp_types / require_confirmed / long_only / extra_conditions
- `MultiPolicyEvaluator` — 多策略并行评估

**Grade Filter** (`chan_futures/graded_strategy.py`):
- `GradedChanStrategy` — 包装 MinimalChanTrendStrategy + grade filter
- `GradeFilterConfig` / `GradedSignal`

### 1.2 出场规则体系 (Stage 2)

**数据模型** (`strategy_policy/exit_rules/base.py`):
- `ExitContext` —— 每根 bar 的不可变快照, 被 ExitRule.check() 消费
  - 包含: bar OHLCV / position info / 入场结构锚点 / 入场评分 / 衍生指标 (mfe/mae/bars_since_entry) / ChanExitSnapshot (optional)
- `ExitSignal` —— 单条规则的出场决策 (全平)
- `ExitDirective(V2)` —— 分级出场指令 (HOLD / TIGHTEN_STOP / REDUCE / CLOSE_ALL)
- `ChanExitSnapshot` —— 缠论结构字段容器 (segment_complete/macd_areas/adjacent_bidong/fractal_break_price 等)
- `ExitAction` 枚举
- `TrailingState` —— ExitManager 内部可变跟踪状态

**规则框架** (`strategy_policy/exit_rules/base.py`):
- `ExitRule` 抽象基类 —— `check(ExitContext) → ExitSignal | None`
- `ExitManager` —— 多规则调度 + 状态管理 + strict 模式 + 异常审计
  - `on_entry()` / `on_close()` / `check()`
  - 规则按 category+priority 排序

**通用规则** (`strategy_policy/exit_rules/rules.py`):
| 规则 | category | 说明 |
|---|---|---|
| `StructureStopRule` | protection | 结构锚点止损 (invalidation/bi_begin/zs, grade_tighten) |
| `FixedStopRule` | protection | 固定点数止损 (grade_adjust: WEAK→0.5×) |
| `TrailingStopRule` | profit | MFE-based 跟踪止损 (trigger_points+giveback_ratio) |
| `TimeStopRule` | time | 持仓时间上限 (max_bars) |
| `OppositeSignalRule` | signal | 反向缠论信号出场 |
| `MACDCrossRule` | auxiliary | MACD 死叉/金叉出场 |

**缠论结构规则** (`strategy_policy/exit_rules/rules.py`):
| 规则 | category | priority | V2 动作 | 说明 |
|---|---|---|---|---|
| `ChanDivergenceExitRule` | chan_structure | 10 | TIGHTEN_STOP→CLOSE_ALL | 走势力度背驰 |
| `ChanSegmentCompleteExitRule` | chan_structure | 15 | REDUCE→CLOSE_ALL | 线段终结 |
| `ChanSmallTurnExitRule` | chan_structure | 5 | CLOSE_ALL | 小转大保护 |

**回测脚本** (`scripts/run_chan_exit_backtest.py`):
- `run_chan_exit_backtest()` —— ExitManager-aware backtest variant
- `ChanExitBacktestConfig` / `ChanExitBacktestResult`
- 支持两种模式: vanilla (通用规则) / chan-native (缠论结构规则 + `_ChanLoopState`)

### 1.3 已有脚本

| 脚本 | 用途 |
|---|---|
| `scripts/run_signal_extraction_verify.py` | BSP→SignalEvent 提取 + 评分验证, 支持 --limit 0 全量 |
| `scripts/run_grade_filter_backtest_compare.py` | before/after grade filter 回测对比 |
| `scripts/plot_signal_overlay.py` | 信号叠加价格图 |
| `scripts/run_chan_exit_backtest.py` | ExitManager backtest |

### 1.4 已有回测数据

| 目录 | 内容 |
|---|---|
| `reports/signal_verify_full/` | 全量 37,860 bar (2018-2025) 信号提取 + 评分 |
| `reports/grade_filter_compare/` | min_grade=standard 的 before/after 对比 |
| `reports/grade_filter_compare_ideal/` | min_grade=ideal 的 before/after 对比 |

---

## 二、可用的回测基础设施

### 2.1 现有回测引擎 (不改)

`chan_futures/backtest.py` — `run_chan_trigger_backtest()` 是经过验证的回测引擎:
- 逐 bar replay: `chan.trigger_load()` → `strategy.on_bar()` → `risk.approve()` → `execution.execute()`
- PnL/手续费/滑点已正确处理 (by `SimulatedExecutionEngine`)
- 输出: bars (逐 bar 权益), fills (逐笔成交), summary

### 2.2 现有数据管线

- `data_foundation` 包: `load_continuous_1m()` / `clean_rb_1m_bars()` / `build_continuous_contract()` / `aggregate_continuous_1m_to_Nm()`
- 预聚合 RB 15m 连续数据: `data/processed/RB_15m_continuous_raw.parquet` (37,860 bar, 2018-03-23 → 2025-04-03)

### 2.3 Entrance Pipeline

```
bar → row_to_klu → chan.trigger_load → strategy.on_bar
  → (if GradedChanStrategy) extract + assess + grade filter
  → risk.approve → execution.execute → entry
  → ExitManager.on_entry()
```

### 2.4 Exit Pipeline

```
each bar: ExitManager.check(ExitContext)
  → 更新 tracking state (mfe/mae/bars_since_entry)
  → 组装 ExitContext (含 ChanExitSnapshot if enable_chan_fields)
  → 依次询问 rules (按 category+priority 排序)
  → 返回第一个触发的 ExitSignal
  → _force_close via SimulatedExecutionEngine
  → ExitManager.on_close()
```

---

## 三、已有实验结果

### 3.1 信号分布 (全量 37,860 bar)

| BSP 类型 | ideal | standard | weak | 总计 |
|---|---|---|---|---|
| 1 (趋势背驰) | 438 (36.8%) | 600 (50.4%) | 153 (12.8%) | 1,191 |
| 1p (盘整背驰) | 608 (39.7%) | 753 (49.2%) | 171 (11.2%) | 1,532 |
| 2 (二买) | 379 (22.9%) | 678 (40.9%) | 601 (36.2%) | 1,658 |
| 2s (类二买) | 418 (26.8%) | 664 (42.6%) | 478 (30.6%) | 1,560 |
| 3a (三买) | 209 (46.5%) | 238 (53.0%) | 2 (0.4%) | 449 |

总信号事件: 6,398 (确认 3,082, 候选 3,316)

### 3.2 Grade Filter 回测对比 (5000 bar, 15m)

| 实验 | fills | return | max_dd | win_rate |
|---|---|---|---|---|
| 原始 (无过滤) | 58 | +210 pts | -533 pts | 51.7% |
| min_grade=standard | 56 | +170 pts | -533 pts | 50.0% |
| **min_grade=ideal** | **50** | **+628 pts** | **-447 pts** | **52.0%** |

### 3.3 Exit Rule 回测结果

尚未运行。第二阶段搭建了完整的出场规则框架(+586 行 base, +739 行 rules, +555 行 backtest script)，但 exit rule 回测对比实验尚未执行。这是第三阶段的核心任务之一。

---

## 四、第三阶段——建议的执行计划

### Phase A: Exit Rule 回测验证 (优先级最高)

1. **运行 ExitManager 回测** — 在 5000 bar 上用 `run_chan_exit_backtest()` 跑一组 baseline (只用 StructureStop+FixStop+TrailingStop)，确认管线完整
2. **对比 exit 规则组合** — 在 `reports/exit_rule_comparison/` 下跑多组对比:
   - Baseline: FixStop(180) + TimeStop(192)
   - Baseline + TrailingStop(500/0.4)
   - Baseline + TrailingStop(500/0.5)
   - Baseline + TrailingStop(800/0.3)
   - Full: StructureStop + TrailingStop + TimeStop
3. **全量 exit 回测** — 在 37,860 bar 上跑获胜组合

### Phase B: 回测报告标准化

4. **统一报告格式** — `reports/{strategy_name}/{variant}/`:
   - `bars.csv` — 逐 bar 权益曲线
   - `fills.csv` — 逐笔成交
   - `exit_events.csv` — 逐笔出场事件 (rule_id + reason_code)
   - `summary.csv` — 汇总指标
   - `config.json` — 完整策略配置
5. **标准绩效指标**: total_return / max_drawdown / sharpe / win_rate / profit_factor / avg_hold_bars / max_consecutive_losses / return_by_year

### Phase C: 入场+出场联合优化

6. **固定入场 (ideal T1+T1P), 遍历出场组合** — 在 `reports/exit_sweep/` 下
7. **固定出场 (最佳组合), 遍历入场 grade** — 在 `reports/entry_sweep/` 下
8. **输出 Pareto 前沿** — 收益 vs 回撤 scatter

### Phase D: Walk-forward 稳健性检验

9. **按年分段** — 2018/2019/2020/2021/2022/2023/2024 各自跑一次, 检查 OOS 表现
10. **按主力合约分段** — 确保换月点不引发异常信号
11. **成本压力测试** — fee_points=2/3, slippage_points=2/3, 观察策略剩余利润

---

## 五、关键 API 速查表

### 入场端 (已有, 可直接导入)

```python
from signal_core import SignalExtractor, SignalEvent, SignalAssessment, SignalState, ScoreGrade, SignalDirection
from signal_scoring import assess_event, T1Scorer, T2Scorer, T3Scorer
from strategy_policy.entry_policy import EntryPolicy, EntryPolicyConfig

# 提取 + 评分
extractor = SignalExtractor(symbol="RB", timeframe="15m")
event = extractor.extract(bsp, chan=chan, bar_end_time=dt, lv_idx=0)
assessment = assess_event(event)

# 入场决策
policy = EntryPolicy(EntryPolicyConfig(min_grade=ScoreGrade.STANDARD))
decision = policy.evaluate(event, assessment)
if decision.accepted:
    # 获取入场计划
    entry_price = decision.entry_price_hint
    stop_price = decision.initial_stop_price
    invalidation = decision.invalidation_price
```

### 出场端 (已有, 可直接导入)

```python
from strategy_policy.exit_rules import (
    ExitManager, ExitContext, ExitSignal, ExitRule,
    StructureStopRule, FixedStopRule, TrailingStopRule, TimeStopRule,
    OppositeSignalRule, MACDCrossRule,
    ChanDivergenceExitRule, ChanSegmentCompleteExitRule, ChanSmallTurnExitRule,
    ChanExitSnapshot,
)

# 构建 ExitManager
manager = ExitManager([
    StructureStopRule(grade_tighten=True),
    FixedStopRule(stop_points=180.0, grade_adjust=True),
    TrailingStopRule(trigger_points=500.0, giveback_ratio=0.5),
    TimeStopRule(max_bars=192),
])

# 开仓时
manager.on_entry(
    direction=SignalDirection.LONG,
    entry_price=fill_price,
    bi_begin_price=decision.invalidation_price,  # 传入 SignalDecision 锚点
    zs_high=None, zs_low=None,
    initial_stop_price=decision.initial_stop_price,
    invalidation_price=decision.invalidation_price,
    entry_grade=assessment.grade.value,
)

# 每根 bar 检查出场
exit_sig = manager.check(
    bar_end_time=dt, open=o, high=h, low=l, close=c,
    # optional:
    opposite_signal_triggered=...,
    macd_cross_down=...,
    chan_snapshot=...,  # if enable_chan_fields
)
if exit_sig is not None:
    # close position at exit_sig.exit_price
    manager.on_close()
```

### 回测脚本 (已有, 可直接运行)

```python
from scripts.run_chan_exit_backtest import run_chan_exit_backtest, ChanExitBacktestConfig

result = run_chan_exit_backtest(frame, config=ChanExitBacktestConfig(
    kl_type=KL_TYPE.K_15M,
    fee_points=1.0, slippage_points=1.0,
    exit_rules=[
        StructureStopRule(),
        FixedStopRule(stop_points=180, grade_adjust=True),
        TrailingStopRule(trigger_points=500, giveback_ratio=0.5),
        TimeStopRule(max_bars=192),
    ],
))
# result.bars / result.fills / result.summary / result.exit_events / result.exit_error_summary
```

---

## 六、不该重建的内容

以下模块在第三阶段**直接使用，不应重写**:

| 模块 | 原因 |
|---|---|
| `signal_core/models.py` | 四层数据模型已完成, 所有字段已验证 |
| `signal_core/extractor.py` | CBS_Point→SignalEvent 已验证通过 (37,860 bar 全量) |
| `signal_scoring/*` | T1/T2/T3 评分器已完成, 分档合理 |
| `chan_futures/execution.py` | PnL/fee/slippage 已正确处理 |
| `chan_futures/backtest.py` | 逐 bar 回放管线成熟 |
| `strategy_policy/exit_rules/base.py` | ExitManager/ExitContext/ExitSignal 框架稳定 |
| `strategy_policy/exit_rules/rules.py` | 6 条通用规则 + 3 条缠论规则已完成 |
| `scripts/run_chan_exit_backtest.py` | ExitManager 回测管线可运行 |

---

## 七、文件索引

```
chan.py/
├── signal_core/                    # [Stage 1] 信号规范化
│   ├── __init__.py
│   ├── models.py                   #   7 dataclass + 3 enum
│   ├── extractor.py                #   SignalExtractor
│   ├── lifecycle.py                #   LifecycleTracker + Journal
│   └── relations.py                #   RelationBuilder
│
├── signal_scoring/                 # [Stage 1] 评分器
│   ├── __init__.py
│   ├── base.py                     #   SignalScorer + assess_event()
│   ├── t1.py / t2.py / t3.py
│   └── configs/chan_score_v0.yaml
│
├── strategy_policy/                # [Stage 1+2] 策略决策层
│   ├── __init__.py
│   ├── entry_policy.py             #   EntryPolicy (Stage 1)
│   └── exit_rules/                 #   ExitManager + Rules (Stage 2)
│       ├── __init__.py
│       ├── base.py                 #     ExitContext/Signal/Manager (586 lines)
│       └── rules.py                #     9 rules total (739 lines)
│
├── chan_futures/                   # [Stage 1] 回测适配
│   ├── __init__.py
│   ├── backtest.py
│   ├── execution.py
│   ├── feed.py
│   ├── risk.py
│   ├── strategy.py
│   └── graded_strategy.py          #   GradedChanStrategy
│
├── scripts/                        # 运行脚本
│   ├── run_signal_extraction_verify.py    # [Stage 1]
│   ├── run_grade_filter_backtest_compare.py  # [Stage 1]
│   ├── plot_signal_overlay.py             # [Stage 1]
│   └── run_chan_exit_backtest.py          # [Stage 2]
│
├── data/processed/
│   └── RB_15m_continuous_raw.parquet   #   37,860 bars
│
├── reports/
│   ├── signal_verify_full/              #   全量信号提取
│   └── grade_filter_compare*/            #   Grade filter 对比结果
│
├── SIGNAL_ARCHITECTURE.md              #   完整架构文档 v0.2.0
└── CLAUDE.md                           #   项目级说明
```
