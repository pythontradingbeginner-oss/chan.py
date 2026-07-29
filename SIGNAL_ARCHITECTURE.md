# 信号规范化架构文档

> **版本**: 0.1.0
> **最后更新**: 2026-07-07
> **状态**: 第一阶段 — 信号提取+评分已完成并验证

---

## 一、概述

本文档描述 chan.py 的信号规范化架构。该架构将缠论 CBS_Point（买卖点对象）转换为**可存储、可回测、可审计的扁平化信号记录**，并通过独立评分器对信号质量进行量化分级。它是从「主观缠论交易」迈向「程序化量化策略」的核心桥梁。

### 设计目标

1. **解耦** — 回测、数据库、出场模块不再直接依赖 `CBS_Point` / `CBi` / `CChan`
2. **可审计** — 每条信号、每个评分、每个决策都有完整记录，可追溯「为什么买 / 为什么不买」
3. **防未来函数** — 通过 `available_at` 时间字段确保策略不会使用尚未可见的信息
4. **可扩展** — 评分器可独立迭代，同一信号可被多个评分器并行评估，不互相覆盖

---

## 二、四层信号管线

```
 CChan / CBS_Point              ← 缠论计算层 (已有)
        │
        ▼
 SignalEvent                    ← 客观事实: 何时、看到了什么形态
        │
        ▼
 SignalAssessment               ← 评分解释: 某版本评分器的独立判断
        │
        ▼
 SignalDecision                 ← 策略决策: 是否接受 + 入场计划
        │
        ▼
 Fill / Order (已有)             ← 执行结果 (SimulatedExecutionEngine)
```

每层都是独立的 `frozen dataclass`，构造后不可变，各自可单独存储为 CSV/Parquet。

---

## 三、核心数据结构

### 3.1 枚举

`signal_core/models.py`

```python
class SignalState(StrEnum):
    CANDIDATE   = "candidate"     # 笔/段未确认
    CONFIRMED   = "confirmed"     # 确认定稿
    INVALIDATED = "invalidated"   # 后续被否定
    EXPIRED     = "expired"       # 超时未确认

class SignalDirection(StrEnum):
    LONG  = "long"
    SHORT = "short"

class ScoreGrade(StrEnum):
    IDEAL    = "ideal"       # structural_score ≥ 0.80
    STANDARD = "standard"    # 0.55 ≤ score < 0.80
    WEAK     = "weak"        # score < 0.55
```

### 3.2 SignalEvent — 客观事实层

`signal_core/models.py`

```python
@dataclass(frozen=True, slots=True)
class SignalEvent:
    # 身份 (revision 递增, 不覆盖旧记录)
    event_id:    str           # 全局唯一, 每次 revision 新生成
    signal_key:  str           # 同生命周期内稳定不变
    revision:    int           # 0=首次出现, 1+=状态变更

    # 品种与周期
    symbol:      str           # "RB"
    contract:    str           # "RB_MAIN"
    timeframe:   str           # "15m"

    # 时间 (未来函数防护)
    bar_end_time: datetime     # 信号形态所在K线收盘时刻
    available_at: datetime     # 策略最早可见此刻 = bar_end_time

    # 生命周期
    state:       SignalState   # candidate → confirmed → invalidated

    # 类型与方向
    direction:   SignalDirection
    primary_bsp: str           # "1" / "1p" / "2" / "2s" / "3a" / "3b"
    bsp_types:   tuple[str, ...]  # 若同时是T2+T3B则为 ("2","3b")

    # 价格
    reference_price: float     # 信号笔尾K线 close

    # 结构锚点 (出场止损/止盈定位)
    bi_idx:         int
    seg_idx:        int | None
    bi_begin_price: float | None   # 笔起始价 = 前底/顶分型价格
    zs_high:        float | None   # 最近中枢上沿
    zs_low:         float | None   # 最近中枢下沿

    # 多级别关联
    parent_event_id: str | None

    # 特征快照
    feature_schema_version: str    # "v0"
    features:     dict[str, Any]
    chan_version: str
    data_run_id:  str
```

### 3.3 SignalAssessment — 评分层

`signal_core/models.py`

```python
@dataclass(frozen=True, slots=True)
class SignalAssessment:
    assessment_id:  str
    event_id:       str           # 对应 SignalEvent.event_id

    scorer_id:      str           # "chan_t1" / "chan_t2" / "chan_t3"
    scorer_version: str           # "0.1.0"

    structural_score: float | None  # 结构质量 0~1
    predictive_score: float | None  # 历史胜率 (后续 ML 填充)

    grade:          ScoreGrade
    hard_blockers:  tuple[str, ...]   # 硬阻断原因码
    component_scores: dict[str, float] # 子项得分明细
    computed_at:    datetime
```

一个 `SignalEvent` 可对应多条 `SignalAssessment`：
```text
同一一买信号:
  - chan_t1_v0: structural=0.68, grade=standard
  - chan_t1_v1: structural=0.74, grade=standard    (评分器迭代后重新评估)
  - ml_continue_v0: predictive=0.61                (ML 模型)
```

### 3.4 SignalDecision — 策略决策层

`signal_core/models.py`

```python
@dataclass(frozen=True, slots=True)
class SignalDecision:
    decision_id:  str
    event_id:     str
    policy_id:    str            # "rb_15m_bsp_filter_v1"

    accepted:     bool
    reason_codes: tuple[str, ...]  # 原因码 ("parent_not_confirmed", "grade_below_standard")

    # 以下仅 accepted=True 时有意义
    entry_price_hint:    float | None
    invalidation_price:  float | None   # 跌破/突破此价则信号作废
    initial_stop_price:  float | None
    position_size_hint:  float | None

    decided_at: datetime
```

### 3.5 SignalRelation — 多级别关系

`signal_core/models.py`

```python
@dataclass(frozen=True, slots=True)
class SignalRelation:
    child_event_id:  str
    parent_event_id: str
    relation_type:   str            # "resonance" / "confirms"
    known_at:        datetime       # 此关系已知时刻
```

---

## 四、提取器：CBS_Point → SignalEvent

`signal_core/extractor.py`

`SignalExtractor` 是整个架构中**唯一**直接接触 `CBS_Point` / `CBi` / `CChan` 的模块。

### 核心能力

| 功能 | 说明 |
|---|---|
| Feature Key 映射 | `bsp2_retrace_rate` → `retrace_rate`，标准化评分器输入 |
| 去重 | `_seen` dict 追踪 `(bi_idx, dir, primary)` 的 last_state，仅在首次出现或状态变更时产出事件 |
| 生命周期跟踪 | CANDIDATE → CONFIRMED: `revision` 递增，不覆盖 |
| 多级别关联 | 通过 `sub_kl_list` 查找上级别 BSP 覆盖关系 |
| 安全取值 | 所有 feature 缺失时置 None，不抛异常 |

### 关键修复记录

| Bug | 发现 | 修复 |
|---|---|---|
| 去重缺失: 45K 事件/2000 bar | 第一次运行时 | 加入 `_seen` 状态追踪字典 |
| Feature key 覆盖: 多个源 key 映射到同一目标key时 `None` 覆盖了已提取值 | T2 100% weak | 只在 `val is not None` 或 key 不存在时写入 |

### 用法

```python
extractor = SignalExtractor(symbol="RB", timeframe="15m")
for bsp in chan[lv_idx].bs_point_lst.bsp_iter():
    event = extractor.extract(bsp, chan=chan, bar_end_time=bar_dt, lv_idx=0)
    if event is not None:
        events.append(event)
```

---

## 五、评分器

`signal_scoring/`

### 设计原则

- **评分在构造 `SignalAssessment` 时一次完成** — 不使用 `object.__setattr__`，真正 immutable
- **每类 BSP 独立评分器** — T1/T2/T3 关注维度完全不同
- **`structural_score` 与 `predictive_score` 分离** — 前者衡量结构是否符合定义，后者衡量历史盈利概率（后续 ML）

### T1Scorer — 一类买卖点 / 盘整背驰

`signal_scoring/t1.py`

| 维度 | 满分条件 | 扣分项 |
|---|---|---|
| 背驰度 | divergence_rate ≤ 0.4 → 1.0 | >1.2 → 硬阻断 no_divergence; 缺失 → 硬阻断 divergence_unavailable |
| 中枢数 | ≥2 → 1.0; =1 → 0.8 | =0 且非盘背 → -0.2 |
| 笔确认 | CONFIRMED → 1.0 | CANDIDATE → 0.3 (-0.3) |
| 笔振幅 | ≥0.008 → 1.0 | <0.001 → 0.0 (-0.15) |

### T2Scorer — 二类买卖点 / 类二买

`signal_scoring/t2.py`

| 维度 | 满分条件 | 扣分项 |
|---|---|---|
| 回撤幅度 | 0.15~0.382 → 1.0 (强势回撤) | >0.80 → 0.2 (-0.35); 缺失 → 硬阻断 retrace_rate_unavailable |
| 一买锚点 | bi_begin_price 未突破 | 突破但仅降分 -0.15 (不硬阻断) |
| 笔确认 | CONFIRMED → 1.0 | CANDIDATE → 0.3 (-0.3) |

### T3Scorer — 三类买卖点

`signal_scoring/t3.py`

| 维度 | 满分条件 | 扣分项 |
|---|---|---|
| 中枢突破 | 离开中枢≥3% → 1.0 | <0.5% → 0.3; zs_range 缺失但有 zs_height → 0.5 (-0.15) |
| 回抽力度 | bi_amp ≥ 0.008 → 1.0 | <0.001 → 0.2 (-0.2) |
| 再启动确认 | CONFIRMED → 1.0 | CANDIDATE → 0.4 (-0.25) |

### 评分分档

```
IDEAL:    score ≥ 0.80
STANDARD: 0.55 ≤ score < 0.80
WEAK:     score < 0.55
```

### 加入新评分器

```python
# 1. 实现子类
class MyNewScorer(SignalScorer):
    def _score_structural(self, event) -> tuple[float, dict, tuple]:
        ...

# 2. 在 signal_scoring/base.py 的 _SCORER_REGISTRY 中注册
_SCORER_REGISTRY["new_type"] = MyNewScorer()
```

---

## 六、全量验证数据

### 数据范围

- 品种: RB (螺纹钢) 连续主力
- 周期: 15 分钟
- 时间: 2018-03-23 → 2025-04-03
- K 线数: 37,860

### 信号提取

| 指标 | 数值 |
|---|---|
| 总信号事件 | 6,398 |
| 确认事件 | 3,082 (48.2%) |
| 候选事件 | 3,316 (51.8%) |
| 每 bar 平均事件 | 0.17 |

### 评分分档分布

#### 全量信号

| BSP 类型 | ideal | standard | weak | 总计 |
|---|---|---|---|---|
| 1 (趋势背驰) | 438 (36.8%) | 600 (50.4%) | 153 (12.8%) | 1,191 |
| 1p (盘整背驰) | 608 (39.7%) | 753 (49.2%) | 171 (11.2%) | 1,532 |
| 2 (二买) | 379 (22.9%) | 678 (40.9%) | 601 (36.2%) | 1,658 |
| 2s (类二买) | 418 (26.8%) | 664 (42.6%) | 478 (30.6%) | 1,560 |
| 3a (三买) | 209 (46.5%) | 238 (53.0%) | 2 (0.4%) | 449 |
| **合计** | **2,058 (32.2%)** | **2,933 (45.8%)** | **1,407 (22.0%)** | **6,398** |

#### 仅确认信号 (state=confirmed)

| BSP 类型 | ideal | standard | weak |
|---|---|---|---|
| 1 | 438 | 158 | 0 |
| 1p | 608 | 157 | 0 |
| 2 | 379 | 201 | 189 |
| 2s | 418 | 212 | 103 |
| 3a | 209 | 0 | 2 |

### 结构评分统计（仅确认信号）

| 统计量 | 值 |
|---|---|
| 均值 | 0.808 |
| 中位数 | 0.850 |
| 标准差 | 0.170 |
| 10% 分位 | 0.600 |
| 25% 分位 | 0.650 |
| 50% 分位 | 0.850 |
| 75% 分位 | 1.000 |
| 90% 分位 | 1.000 |

---

## 七、回测验证：Grade Filter 对比

在 5000 bar 上比较原始策略 vs grade 过滤策略：

| 实验 | 成交笔数 | 总收益 (pts) | 最大回撤 (pts) | 胜率 |
|---|---|---|---|---|
| 原始 (无过滤) | 58 | +210 | -533 | 51.7% |
| min_grade=standard | 56 | +170 | -533 | 50.0% |
| **min_grade=ideal** | **50** | **+628** | **-447** | **52.0%** |

**结论**: `min_grade=ideal` 在测试区间上显著改善风险收益比。但仅基于 5000 bar，需在更长区间上做 walk-forward 验证。

---

## 八、目录结构

```
chan.py/
├── signal_core/              ← 信号规范化核心
│   ├── __init__.py           # 公共 API 导出 (16 项)
│   ├── models.py             # SignalEvent/SignalAssessment/SignalDecision/SignalRelation/
│   │                         #   SignalOutcome/HumanAnnotation + 枚举
│   ├── extractor.py          # SignalExtractor: CBS_Point → SignalEvent (唯一接触 CChan 的模块)
│   ├── lifecycle.py          # SignalLifecycleTracker + SignalJournal (CSV/Parquet 持久化)
│   └── relations.py          # RelationBuilder + check_time_honesty (多级别关联/防未来函数)
│
├── signal_scoring/           ← 评分器
│   ├── __init__.py           # 公共 API 导出 + 评分器注册表
│   ├── base.py               # SignalScorer 基类 + assess_event() 便捷函数
│   ├── t1.py                 # T1Scorer — 一类买卖点 + 盘整背驰
│   ├── t2.py                 # T2Scorer — 二类买卖点 + 类二买
│   ├── t3.py                 # T3Scorer — 三类买卖点
│   └── configs/
│       └── chan_score_v0.yaml  # 评分器维度/阈值配置骨架
│
├── strategy_policy/          ← 策略决策层
│   ├── __init__.py           # 公共 API 导出
│   └── entry_policy.py       # EntryPolicy + MultiPolicyEvaluator
│                              #   消费 SignalAssessment → 产出 SignalDecision
│
├── chan_futures/             ← 已有 — 扩展而非重写
│   ├── backtest.py           # 现有管线: run_chan_trigger_backtest
│   ├── graded_strategy.py     ← 新增: GradedChanStrategy + GradeFilterConfig
│   ├── execution.py          # 已有 (PnL/fee/slippage 已正确处理)
│   └── ...
│
├── scripts/
│   ├── run_signal_extraction_verify.py   ← 信号提取 + 评分验证脚本
│   ├── run_grade_filter_backtest_compare.py  ← 前/后 grade filter 回测对比
│   └── plot_signal_overlay.py            ← 信号叠加价格图
│
└── reports/
    ├── signal_verify_01/     # 初版提取 (有去重 bug, 保留作为对比)
    ├── signal_verify_02/     # 第一次修复 (去重后)
    ├── signal_verify_03/     # T2/T3 评分修复后
    ├── signal_verify_04/     # 最终验证版 + signal_overlay.png
    ├── signal_verify_full/   # 全量 37,860 bar 提取
    └── grade_filter_compare/ # grade filter 回测对比结果
```

---

## 九、与 vn.py 的适配路径

当前架构产出的 `SignalEvent` / `SignalAssessment` 是纯 dataclass，不依赖任何 vn.py 类型。接入 vn.py 策略的标准模式：

```python
# 在 CtaTemplate.on_15min_bar 内:
def on_15min_bar(self, bar: BarData):
    klu = bar_to_klu(bar, kl_type=KL_TYPE.K_15M)
    self.chan.trigger_load({KL_TYPE.K_15M: [klu]})

    # ── BSP 驱动的信号提取 ──
    for bsp in self.chan[0].bs_point_lst.bsp_iter():
        event = self.extractor.extract(bsp, chan=self.chan, bar_end_time=bar.datetime)
        if event is None:
            continue
        assessment = assess_event(event)

        # ── 策略决策 ──
        if assessment.grade in (ScoreGrade.IDEAL, ScoreGrade.STANDARD):
            self.buy(bar.close_price, self.fixed_size)
            # 记录 signal → fill 审计链
```

同一套 `extract()` + `assess_event()` 可用于：
- **vn.py CtaTemplate 回测** (`on_15min_bar`)
- **vn.py 实盘** (`EVENT_BAR` 订阅)
- **独立回测脚本** (`run_chan_trigger_backtest` + `GradedChanStrategy`)
- **向量化信号研究** (遍历历史 → 全量 SignalEvent DataFrame)

---

## 十、待完成项

| 优先级 | 任务 | 说明 |
|---|---|---|
| P0 | 全量回测验证 | 在 37,860 bar 上跑 `min_grade=ideal` 完整回测，确认收益改善非偶然 |
| P1 | Walk-forward 分段验证 | 按年/按主力合约分段，检测过拟合风险 |
| P1 | SignalDecision 落地 | ✅ 已落地 — `strategy_policy/entry_policy.py` |
| P2 | 信号生命周期可视化 | 画 revision 链：同一 signal_key 的 candidate→confirmed→invalidated 演化 |
| P2 | `predictive_score` | 基于 SignalOutcome 的历史 MF/MAF 做 ML 打分 |
| P3 | HumanAnnotation 表 | ✅ 数据模型已就绪 — `signal_core/models.py:HumanAnnotation` |
| P3 | ExitRule 分级 | 根据入场 signal grade 选不同出场规则实例（WEAK → 更紧的止损） |

---

## 十一、修订历史

| 日期 | 版本 | 变更 |
|---|---|---|
| 2026-07-07 | 0.1.0 | 初始版本: 四层模型 + SignalExtractor + T1/T2/T3 评分器 + 全量验证 + grade filter 回测对比 |
| 2026-07-07 | 0.2.0 | 完整落地: + lifecycle.py (SignalLifecycleTracker/SignalJournal) + relations.py (RelationBuilder) + strategy_policy/entry_policy.py (EntryPolicy/MultiPolicyEvaluator) + SignalOutcome/HumanAnnotation 模型 + 评分器 YAML 配置 + SIGNAL_ARCHITECTURE.md 本文档 |
