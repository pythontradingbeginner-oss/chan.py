# 出场规则体系 v2 — 从模块雏形到生产可用的计划

> **基于四份交叉审查的合成**
> **审查来源**: GPT-5.2 (战术正确性), Gemini 3.0 (缠论语义纵深), GLM-5.0 (工程落地), DeepSeek v4 (系统集成)
> **日期**: 2026-07-07
> **状态**: 计划阶段 — 不进行代码修改

---

## 一、v1 审计结论

v1（当前代码）做了正确的事：

| 维度 | v1 结论 |
|---|---|
| **架构方向** | ✅ 正确 — ExitRule 零依赖 CChan/CBi，ExitManager 集中管理追踪状态，ExitContext 不可变快照 |
| **与信号管线对齐** | ✅ 正确 — 消费 SignalEvent 的结构锚点（bi_begin_price, zs_high/low, invalidation_price），无新耦合 |
| **Grade 联动** | ⚠️ 方向对，实现有问题 — 详见下文 §3.4 |
| **回测集成** | ❌ 未接入 — ExitManager 从未在 `run_chan_trigger_backtest` 中被调用 |
| **真实 Bug** | ❌ 2 个 — MACDCrossRule 开仓即出场; `except Exception: continue` 静默吞止损规则 |

**共识**: 四份审查全部认可 v1 的分层方向。分歧仅在「下一步应该先做什么」。本章综合四份审查给出统一的优先级排序。

---

## 二、需要修复的 Bug（P0 — 必须修，启动任何新工作前先修）

### 2.1 `except Exception: continue` 静默吞止损规则

**位置**: `base.py:360`
**严重性**: 致命 — 止损静默失效 → 回测权益虚高 → 策略上线后爆仓
**所有四份审查均提及**: GPT "最容易被忽略的高估"; GLM "致命隐患" P0; DeepSeek 评为容错机制但方向反了; Gemini 未直接点名但暗示需要 stricter

**修复方案**:

```python
class ExitManager:
    def __init__(self, rules, *, strict: bool = False):
        self._strict = strict
        self._rule_error_counts: dict[str, int] = {}  # rule_id → 异常次数

    def check(self, ...):
        for rule in self._rules:
            try:
                result = rule.check(ctx)
                if result is not None:
                    return result
            except Exception as e:
                self._rule_error_counts[rule.rule_id] = \
                    self._rule_error_counts.get(rule.rule_id, 0) + 1
                if self._strict:
                    raise
                # 非 strict: 首次异常时 log（不是 print — 用 warnings.warn 或 logger）
```

**交付物**: `ExitManager` 增加 `strict` 参数 + `error_summary()` 方法返回 `dict[str, int]`，回测结束时调用。

### 2.2 MACDCrossRule 检查「当前 DIF ≤ DEA」而非「死叉事件」

**位置**: `rules.py:336-348`
**严重性**: 致命 — 只要 DIF 低于 DEA（熊市常态），开多仓第一根 bar 就出场
**GLM 点名**; GPT 建议 MACD 放到实验规则

**根因**: 代码检查的是「状态」（DIF ≤ DEA），不是「事件」（前 bar DIF > DEA 且当前 DIF ≤ DEA）。docstring 说"交叉检测在策略层完成"，但代码自己做了 `<=`。

**修复方案**: `ExitContext` 增加两个布尔字段 `macd_cross_down: bool` / `macd_cross_up: bool`，由策略层用前后两根 bar 计算后传入。规则只消费布尔值：

```python
class MACDCrossRule(ExitRule):
    def check(self, ctx: ExitContext) -> ExitSignal | None:
        if ctx.is_long and ctx.macd_cross_down:
            return ExitSignal(self.rule_id, "macd_death_cross", ctx.close, "MACD 死叉出场")
        if not ctx.is_long and ctx.macd_cross_up:
            return ExitSignal(self.rule_id, "macd_golden_cross", ctx.close, "MACD 金叉出场")
        return None
```

---

## 三、架构重构（P0–P1 — 设计层面的结构性改动）

### 3.1 ExitSignal ≠ 成交价：拆开触发价与成交价

**GPT 核心关切**。当前 `ExitSignal.exit_price` 同时承担「建议出场价」和「实际成交价」两种语义。在跳空/滑点场景下这会导致系统性高估。

**拆分方案**:

```
ExitSignal          ← 规则产出: "我建议在这个价格出场"
     ↓
ExecutionModel      ← 模拟成交: 根据触发价 + 当前 bar 开盘 + 滑点 → 真实成交价
     ↓
ExitFill            ← 最终记录: 实际成交价 + 执行详情
```

`ExitSignal` 增加字段:
- `trigger_price: float` — 规则计算的触发价（如结构止损价位）
- `execution_mode: str` — "this_bar" | "next_open" | "next_bar_close"

`ExitFill`（新 dataclass）:
- `trigger: ExitSignal` — 来源规则
- `fill_price: float` — 实际成交价
- `slippage: float` — 滑点
- `filled_at: datetime` — 成交时间

**执行时序**（GPT 建议的核心框架）:

| 阶段 | 执行的规则类型 | 成交价 |
|---|---|---|
| 开盘 | 跳空止损（open < stop → fill=open） | bar.open |
| 盘中 | 固定止损、结构止损、已激活的追踪止损 | 止损价（下一跳） |
| 收盘 | MACD 出场、反向信号出场、时间止损 | 下一根 bar 的 open |
| 下一根开盘 | 执行上一根收盘阶段生成的平仓指令 | bar.open |

**注意**: 当前 v1 只有 bar-level 回测（无 tick 数据），盘中阶段只能用 low/high 判断"是否触及"，无法精确到 bar 内时序。bar-level 近似足以用于策略评估，但需文档化这一假设。

### 3.2 OHLC 内的时序歧义：追踪止损的反向路径问题

**GPT + GLM + DeepSeek 均提及**。同一根 bar OHLC 无法区分「先涨到 high 再跌回 close」还是「先跌到 low 再涨回 close」。这导致追踪止损可能用 bar 内高点制造虚假的 MFE 回撤触发。

**当前 v1 的选择**: close-based tracking（保守，避免了未来函数）。代价是 trailing stop 对 bar 内大幅冲高回落反应迟钝。

**v2 方案**: 增加 `high_low_based: bool` 选项到 `TrailingState`:

| 模式 | MFE 来源 | 适用场景 |
|---|---|---|
| `close_only`（默认） | 收盘价 | 保守，无 bar 内时序歧义 |
| `bar_extreme` | bar 内 high/low | 激进，可能引入路径假设 |

`bar_extreme` 模式在 `TrailingState` 增加 `peak_favorable_price: float`（多仓=high 最大值，空仓=low 最小值），`TrailingStopRule` 可选用此模式。默认为 `close_only`。

**测试要求**（GPT 列出的关键测试）:
> 追踪止损不能用本 bar 高点制造未来 MFE

### 3.3 多规则优先级：从列表顺序到分类优先级

**GPT 核心关切**。列表顺序决定优先级太脆弱——一条规则的插入位置改变可能彻底改变策略行为。

**v2 方案**: 每条规则声明自己的 `category` 和 `priority`，`ExitManager` 内部排序：

```python
class ExitRule(ABC):
    category: str = "protection"   # "risk" | "protection" | "profit" | "signal" | "time" | "auxiliary"
    priority: int = 50             # 同 category 内的排序，数字越小越优先

# 默认分类:
#   risk:       强制风控（跳空、保证金、最大亏损）
#   protection: 止损保护（结构止损、固定止损）
#   profit:     利润保护（跟踪止损、ATR trailing）
#   signal:     结构信号（反向缠论信号）
#   time:       时间止损
#   auxiliary:  辅助指标（MACD、成交量等）
```

`ExitManager.check()` 内部按 `(category_order, priority)` 排序后依次询问。

`ExitManager.check_all()`（DeepSeek P3 建议）返回所有触发的规则，用于审计："同时触发 TimeStop + StructureStop，TimeStop 胜出（优先级更高）"。

### 3.4 Grade 联动从「收紧止损」改为「缩小仓位」

**GPT 核心关切**。当前 `StructureStopRule.grade_tighten` 和 `FixedStopRule.grade_adjust` 直接改止损距离。但结构失效位由市场结构定义——弱信号不代表结构失效位变了。把 WEAK 信号的止损从 3750 收紧到 3787.5，只是更容易被噪声洗掉。

**共识**: 四份审查中 GPT 最明确反对 grade → 止损收紧，其他三份未直接挑战但也未明确支持。

**v2 方案**: Grade 影响仓位，不影响止损：

```
初始止损 = 结构失效位（不可改）
信号级别影响:
  IDEAL    → 标准仓位
  STANDARD → 0.7× 标准仓位
  WEAK     → 0.4× 标准仓位
```

保留 `grade_tighten` 作为可选开关（默认关闭），供后续研究对比「收紧止损 vs 缩小仓位」的效果差异。

### 3.5 ExitManager 从单仓位到多仓位

**GPT 建议**。当前 `ExitManager` 假设一次只管理一个仓位。未来多品种/加仓/分批止盈场景需要一个 manager 管理多个 `position_id`。

**v2 方案**: 

```python
class ExitManager:
    def __init__(self, rules, *, strict: bool = False):
        self._states: dict[str, _PositionState] = {}  # position_id → state

    def on_entry(self, position_id: str = "default", **anchors): ...
    def check(self, position_id: str = "default", **kwargs) -> ExitSignal | None: ...
    def on_close(self, position_id: str = "default"): ...
```

向后兼容: `position_id` 默认为 `"default"`，不传时行为与 v1 相同。GPT 的原始建议要求每个 position_id 独立保存 entry_price / quantity_remaining / entry_signal_id / MFE / MAE / bars_held / partial_exit_history。

---

## 四、具体规则改进（P1–P2）

### 4.1 StructureStopRule — 增加 buffer 和锚点来源追踪

问题:
- **GLM**: 三个锚点（invalidation / bi_begin / zs）语义不同，`max(candidates)` 合并过紧
- **Gemini**: 跳空和针头需要 buffer

v2 方案:

```python
class StructureStopRule(ExitRule):
    def __init__(self, *,
                 anchor: str = "tightest",    # "invalidation" | "bi_begin" | "zs" | "tightest"
                 buffer_points: float = 0.0,  # 缓冲点数（ATR × 系数 或 固定 tick）
                 use_close_for_break: bool = False,  # True = 收盘价跌破才算
                 grade_tighten: bool = False,  # 默认关闭
                 ):
```

`anchor_source` 记录在 `ExitSignal` 的 `metadata` 中用于审计。

### 4.2 FixedStopRule — 从固定点数到 ATR 倍数

问题: **GPT** 指出 `180 points` 跨品种不通用

v2 方案: 保留 `stop_points` 作为固定模式，新增 `stop_atr_mult` 模式:

```python
class FixedStopRule(ExitRule):
    def __init__(self, *,
                 stop_points: float | None = None,   # 固定点数
                 stop_atr_mult: float | None = None,  # ATR 倍数
                 atr_period: int = 14,
                 max_risk_pct: float | None = None,   # 账户净值百分比
                 ):
```

三种模式互斥，异常时抛 `ValueError`。ATR 值由策略层通过 `ExitContext.atr_value` 传入。

### 4.3 TrailingStopRule — 增加结构追踪和 ATR 追踪变体

问题:
- **GPT**: "达到 500 点浮盈后回撤 50%" 缺乏理论支撑
- **Gemini**: 结构追踪（跌破新形成的高点/低点）更符合缠论

v2 方案: 增加 `trail_mode` 参数:

| mode | 追踪线计算 | 适用场景 |
|---|---|---|
| `mfe_ratio`（现有）| MFE × giveback_ratio | 通用 |
| `atr_channel` | entry + N × ATR，每次新高更新 | 波段 |
| `structure`（新增）| 最近 N 根 bar 的最低点（多仓）| 趋势 |

`structure` 模式下追踪线只能上移（多仓）或下移（空仓），与 Gemini 的锚点推移建议一致。

### 4.4 TimeStopRule — 增加真实时间维度

问题: **GLM + GPT**: `bars_since_entry` 不考虑夜盘/日盘 bar 密度差异

v2 方案:

```python
class TimeStopRule(ExitRule):
    def __init__(self, *,
                 max_bars: int | None = None,
                 max_hours: float | None = None,
                 max_sessions: int | None = None,  # 交易 session 数
                 ):
```

`ExitContext` 增加 `entry_time: datetime` 字段，规则内计算 elapsed。

### 4.5 OppositeSignalRule — 增加级别过滤

问题: **Gemini**: 15 分钟入场不应被 5 分钟反向信号洗出

v2 方案:

```python
class OppositeSignalRule(ExitRule):
    def __init__(self, *,
                 mode: str = "flat",
                 min_timeframe: str | None = None,  # "15m" — 同级别及以上
                 require_confirmed: bool = True,     # 仅 confirmed
                 require_available: bool = True,     # 仅 available_at ≤ now
                 only_high_grade: bool = False,
                 ):
```

`available_at` 检查是 GPT 强调的防未来函数要求。

### 4.6 MACDCrossRule — 降级为辅助指标

**GPT + GLM 一致**: MACD 不应独立平仓，应作为结构转弱的确认条件。

v2 方案: 修复 Bug 2.2 后，将 MACDCrossRule 的 `category` 标记为 `"auxiliary"`（最低优先级），默认不出现在标准规则集中。

---

## 五、新增规则（P2）

### 5.1 MAEProtectRule（DeepSeek 建议）

最大不利偏移保护: MAE 超过阈值时无条件止损。与 FixedStopRule 互补——FixedStopRule 是静态止损线，MAEProtectRule 检测"还没触及止损线但已经亏得太多"的路径恶化。

### 5.2 ATRTrailingRule（DeepSeek 建议）

ATR 倍数跟踪止损: `止损线 = entry ± N × ATR(14)`，每次新高/新低更新。

### 5.3 PartialExitRule（Gemini 建议）

非全平出场: `ExitSignal.exit_volume_ratio = 0.5`。应用场景: 触及第一个 trailing stop 目标时减仓 50%，剩余底仓等反向信号清场。这需要对 `SimulatedExecutionEngine` 增加部分平仓支持。

---

## 六、集成计划（P0 — 最紧急）

### 6.1 ExitManager 接入 run_chan_trigger_backtest

**所有四份审查均标记为 P0**。当前 `run_chan_trigger_backtest` 仍用「新 BSP 出现 → 反手」模式，完全没有出场逻辑。

**接入方案**:

1. `GradedSignal` 增加出场锚点快照（GLM 指出的前置阻塞）:
   ```python
   @dataclass(frozen=True)
   class GradedSignal:
       # ... 现有字段 ...
       # 新增:
       bi_begin_price: float | None = None
       zs_high: float | None = None
       zs_low: float | None = None
       invalidation_price: float | None = None
       entry_grade: str = "standard"
   ```

2. `run_chan_trigger_backtest` 的逐 bar 循环中，开仓时调 `manager.on_entry(...)`，每根 bar 调 `manager.check(...)`。

3. 先跑 baseline 对比（GLM 建议）: "有无 ExitManager" 的权益曲线差异——如果差异很小，说明现有反手模式已覆盖大部分止损；如果差异大，说明旧回测止损有漏洞。

**测试要求**（GPT 列出的关键测试）:

1. 多头跳空低开穿透止损 → fill = open
2. 空头跳空高开穿透止损 → fill = open
3. 同一根 bar 同时触发止损与追踪止损 → 高优先级胜出
4. 追踪止损不能用本 bar 高点制造未来 MFE
5. 反向信号在 available_at 之前不得生效
6. 多头与空头逻辑镜像测试
7. 多 position_id 状态不串扰
8. 平仓后重新开仓，旧状态不残留
9. 回测顺序式运行与逐 bar 回放结果一致

### 6.2 出场归因报表

**GPT + Gemini + DeepSeek 均建议**: 不仅需要权益曲线，还需要出场归因:

| 指标 | 说明 |
|---|---|
| 每种 exit_reason 的次数 | 结构止损 vs 固定止损 vs 追踪止盈 vs 时间止损 vs 反向信号 |
| 平均 realized R | 按 exit_reason 分组 |
| 平均 MFE / MAE | 出场前最大浮盈/浮亏 |
| 出场后 N bar 走势 | 提前出场 vs 延迟出场检测 |
| 同一 bar 多规则同时触发次数 | DeepSeek check_all() 审计 |
| 跳空造成的额外滑点 | GPT 建议 |

**Gemini 特别建议**: 做 MFE/MAE 散点图——如果大量盈利单 MFE 走到 400 点后最终亏损出场，说明 TrailingStopRule 或减仓机制必须立即介入。

---

## 七、实施顺序

```
Phase 0: 修 Bug（本次迭代 — 最优先）
  ├── 0.1 ExitManager strict 模式 + 异常计数 (GLM P0)
  ├── 0.2 MACDCrossRule 从状态检查改为事件检查 (GLM P0)
  └── 0.3 ExitSignal 拆分为 trigger_price + execution_mode (GPT P0)

Phase 1: 集成 + 验证（下一次迭代）
  ├── 1.1 GradedSignal 增加出场锚点 (GLM P1)
  ├── 1.2 ExitManager 接入 run_chan_trigger_backtest (所有审查 P0)
  ├── 1.3 出场归因报表 v0 (GPT P0)
  ├── 1.4 10 个集成测试（跳空、MFE 反路径、多仓位、方向对称）(GPT)
  └── 1.5 RB 全量回测对比: 旧反手 vs 新 ExitManager (GLM)

Phase 2: 架构重构
  ├── 2.1 ExitPlan / ExitState / ExitCandidate / ExitFill 四对象拆分 (GPT)
  ├── 2.2 规则 category + priority 替代列表顺序 (GPT)
  ├── 2.3 TrailingState 增加 bar_extreme 追踪 (GLM + DeepSeek)
  ├── 2.4 ExitManager 多仓位支持 (GPT)
  └── 2.5 Grade 联动从收紧止损改为缩小仓位 (GPT)

Phase 3: 规则增强
  ├── 3.1 StructureStopRule: buffer + anchor source tracking (Gemini + GPT)
  ├── 3.2 FixedStopRule: ATR 倍数 + 最大风险百分比 (GPT)
  ├── 3.3 TrailingStopRule: structure / ATR channel mode (GPT + Gemini)
  ├── 3.4 TimeStopRule: max_hours + max_sessions (GLM + GPT)
  ├── 3.5 OppositeSignalRule: min_timeframe + available_at check (Gemini + GPT)
  └── 3.6 MACDCrossRule → auxiliary category, 默认不启用 (GPT + GLM)

Phase 4: 新规则 + 高级功能
  ├── 4.1 MAEProtectRule (DeepSeek)
  ├── 4.2 ATRTrailingRule (DeepSeek)
  ├── 4.3 PartialExitRule + exit_volume_ratio (Gemini)
  ├── 4.4 ExitManager.update_anchors() 动态锚点推移 (Gemini)
  └── 4.5 出场效率分析工具 check_all() + analyze_exit_efficiency() (DeepSeek)
```

---

## 八、风险登记表

| 风险 | 严重性 | 缓解措施 |
|---|---|---|
| ExitManager 静默吞异常 → 止损失效 | 致命 | Phase 0.1 strict 模式 |
| MACDCrossRule 开仓即出场 | 致命 | Phase 0.2 改为事件驱动 |
| exit_price 当成交价 → 回测虚高 | 高 | Phase 0.3 ExitFill 拆分 |
| 追踪止损 bar 内路径歧义 | 高 | Phase 2.3 dual-tracking |
| Grade → 止损收紧 vs 缩小仓位 | 中 | Phase 2.5 改为仓位联动 |
| 单仓位假设无法扩展 | 中 | Phase 2.4 多位置支持 |
| 出场规则未在真实数据上验证 | 高 | Phase 1.5 RB 全量回测对比 |

---

## 九、不再修改的 v1 设计决策（记录留档）

以下 v1 决策经过四份审查后确认**保持不变**:

1. **ExitRule 纯函数接口** — 四份审查一致认可
2. **ExitContext frozen dataclass** — 不可变快照每 bar 重建
3. **TrailingState 由 ExitManager 管理** — 不从规则内部维护状态
4. **不依赖 CBi / CBS_Point / CChan** — 零耦合设计
5. **锚点在 on_entry 时快照** — 不受后续缠论计算重构影响
6. **60 个单元测试的测试策略** — 充分覆盖规则逻辑

---

*本计划由 Claude Fable 5 综合 GPT-5.2、Gemini 3.0、GLM-5.0、DeepSeek v4 四份独立审查后撰写。Phase 0 的三项 Bug 修复建议在所有审查中均获得最优先共识。*
