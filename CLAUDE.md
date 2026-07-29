# CLAUDE.md

## 项目概述

chan.py 是一个基于缠中说禅理论（缠论）的量化分析框架。核心功能是对 K 线数据进行分型、笔、线段、中枢和买卖点的计算，并提供策略研究、回测和实盘交易的基础设施。当前版本是 V3 公开版，约 5300 行代码（完整版约 22000 行）。

项目同时包含一套面向上海期货交易所 RB（螺纹钢）期货的数据预处理管线（data_foundation）、一个轻量级期货回测桥接模块（chan_futures），以及一个 vn.py 独立插件 App（vnpy_chan）。

## 技术架构与模块划分

```
chan.py/
├── Chan.py              # 缠论计算主入口 CChan，管理多级别数据加载和计算流程
├── ChanConfig.py        # 全局配置类 CChanConfig，控制笔/段/中枢/买卖点/指标参数
│
├── KLine/               # K 线数据结构层
│   ├── KLine_Unit.py    # 单根 K 线，持有 OHLCV、指标值、父/子级别引用
│   ├── KLine.py         # 合并 K 线 (CKLine)，继承 CKLine_Combiner，含分型判断
│   ├── KLine_List.py    # K 线列表，组装 BiList/SegList/ZSList/BSPointList 和指标模型
│   └── TradeInfo.py     # 成交量/成交额/换手率等交易指标
│
├── Combiner/            # K 线合并（包含处理）层
│   ├── KLine_Combiner.py # 通用合并器基类 CKLine_Combiner[T]，处理包含、方向、分型
│   └── Combine_Item.py  # 合并元素抽象
│
├── Bi/                  # 笔 (Bi) 计算
│   ├── Bi.py            # 笔类 CBi，含方向、起止 KLC、MACD 度量计算
│   ├── BiList.py        # 笔列表 CBiList，实现笔的识别、更新、虚笔管理
│   └── BiConfig.py      # 笔配置：严格笔/宽笔、分型检查方法、缺口处理
│
├── Seg/                 # 线段 (Segment) 计算 — 整个框架最复杂的部分
│   ├── Seg.py           # 线段类 CSeg[LINE_TYPE]，含中枢列表、趋势线
│   ├── SegListComm.py   # 线段列表通用父类，处理首尾虚段收集
│   ├── SegListChan.py   # 缠论原文线段算法：基于特征序列 (EigenFX) 的线段划分
│   ├── SegListDef.py    # 线段破坏定义算法
│   ├── SegListDYH.py    # 都业华 1+1 终结算法
│   ├── SegConfig.py     # 线段配置
│   ├── Eigen.py         # 特征序列元素 CEigen，处理笔的包含和分型
│   └── EigenFX.py       # 特征序列分型 CEigenFX，三段式特征序列判断线段结束
│
├── ZS/                  # 中枢 (ZhongShu) 计算
│   ├── ZS.py            # 中枢类 CZS，含范围、进出笔、背驰判断
│   ├── ZSList.py        # 中枢列表 CZSList，支持 normal/over_seg/auto 三种算法
│   └── ZSConfig.py      # 中枢配置：合并模式、一笔中枢开关
│
├── BuySellPoint/        # 买卖点 (BSP) 计算
│   ├── BS_Point.py      # 买卖点类 CBS_Point，含类型标记和特征字典
│   ├── BSPointList.py   # 买卖点列表，实现 1/2/3 类买卖点及盘整背驰的计算逻辑
│   └── BSPointConfig.py # 买卖点配置：背驰率、中枢数、MACD 算法等
│
├── Common/              # 通用基础设施
│   ├── CEnum.py         # 所有枚举：K 线级别、方向、分型、买卖点类型、数据字段等
│   ├── CTime.py         # 缠论时间类，处理多级别时间对齐（auto 模式处理日线 23:59）
│   ├── ChanException.py # 异常类和错误码枚举 (ErrCode)
│   ├── cache.py         # @make_cache 方法级 memoize 装饰器
│   └── func_util.py     # 工具函数：区间重叠判断、级别排序校验等
│
├── Math/                # 技术指标计算
│   ├── MACD.py          # MACD 指标（EMA 递推增量计算）
│   ├── BOLL.py          # 布林线
│   ├── RSI.py           # RSI 指标
│   ├── KDJ.py           # KDJ 指标
│   ├── Demark.py        # 德马克指标
│   ├── TrendModel.py    # 趋势模型（均线/最大值/最小值）
│   └── TrendLine.py     # 趋势线计算（支撑/阻力线）
│
├── DataAPI/             # 数据源适配层
│   ├── CommonStockAPI.py # 抽象基类 CCommonStockApi
│   ├── BaoStockAPI.py   # 百度股票数据源
│   ├── AkshareAPI.py    # AKShare 数据源
│   ├── ccxt.py          # 加密货币交易所数据（CCXT）
│   └── csvAPI.py        # 本地 CSV 文件数据源
│
├── ChanModel/           # 特征和模型（公开版仅含基础框架）
│   └── Features.py      # 特征字典类 CFeatures
│
├── Plot/                # 画图模块（matplotlib）
│   ├── PlotDriver.py    # 静态绘图引擎
│   ├── AnimatePlotDriver.py # 逐帧动画回放
│   └── PlotMeta.py      # 图元数据
│
├── data_foundation/     # RB 期货数据预处理管线
│   ├── data/
│   │   ├── loader.py           # 从 vn.py SQLite 加载 RB 数据
│   │   ├── cleaning.py         # 多合约清洗、会话完整性审计
│   │   ├── continuous_contract.py # 主力合约连续化与后复权
│   │   ├── bars.py             # 1m→N 分钟聚合（不跨 session）
│   │   ├── calendar.py         # SHFE 交易日历与会话模型
│   │   ├── quality_report.py   # 数据质量报告
│   │   └── static/             # 静态日历/交易规则 CSV
│   ├── features/
│   │   ├── dc_structure.py     # DC 结构特征（牛/熊/震荡分类）
│   │   └── trend_macd.py       # 趋势 K 线 MACD 特征
│   ├── pivots.py               # 方向性变点 (DC) 检测器
│   └── charts/                 # 图表绘制
│
├── chan_futures/         # 轻量期货回测桥接
│   ├── feed.py           # DataFrame → CKLine_Unit 转换
│   ├── strategy.py       # MinimalChanTrendStrategy：基于最新 BSP 的趋势策略
│   ├── execution.py      # 模拟撮合引擎（含手续费/滑点）
│   ├── risk.py           # 风控管理（仓位限制/最大亏损）
│   └── backtest.py       # 回测主函数 run_chan_trigger_backtest()
│
├── vnpy_chan/            # vn.py 独立插件 App
│   ├── __init__.py       # 懒加载入口
│   ├── app.py            # BaseApp 子类，注册到 vn.py 菜单
│   ├── engine.py         # ChanAnalysisEngine：回测引擎 + vn.py 数据库集成
│   ├── converter.py      # BarData ↔ CKLine_Unit ↔ DataFrame 转换
│   ├── live_engine.py    # 实盘引擎预留（EVENT_BAR 订阅）
│   └── ui.py             # PySide6 GUI 界面
│
├── scripts/              # 研究脚本和批处理
├── tests/                # 单元测试和研究流程回归测试
├── Debug/                # 策略 demo（strategy_demo[1-6].py）
├── reports/              # 研究报告输出目录
└── main.py               # 最小可运行 demo
```

## 缠论核心算法实现位置

### 1. 分型 (Fractal)

- **K 线合并与分型识别**：`Combiner/KLine_Combiner.py:127-145` — `update_fx()` 方法，根据前后合并 K 线的高低点关系判定顶/底分型。
- **分型有效性校验**：`KLine/KLine.py:45-97` — `check_fx_valid()`，支持 strict/loss/half/totally 四种校验标准。

### 2. 笔 (Bi)

- **笔的识别与更新**：`Bi/BiList.py` — `CBiList` 类：
  - `update_bi()` (L48-56)：每根新 K 线到来时更新笔列表
  - `can_make_bi()` (L178-186)：判断是否满足成笔条件（跨度、分型有效性、尾部极值）
  - `try_add_virtual_bi()` (L120-141)：计算未确认的「虚笔」
  - `update_peak()` (L73-84)：处理次高点/次低点成笔
- **笔的 MACD 度量**：`Bi/Bi.py:189-215` — 支持 area/peak/full_area/diff/slope/amp 等 12 种 MACD 算法。
- **笔配置**：`Bi/BiConfig.py` — 笔算法(normal/fx)、严格笔、分型检查方法等。

### 3. 线段 (Segment)

线段计算是整个框架最复杂的模块，提供三种算法：

- **特征序列算法（chan，默认）**：`Seg/SegListChan.py` + `Seg/EigenFX.py`
  - `CEigenFX`（EigenFX.py）：将反方向笔作为特征序列，三元素法判断线段结束
  - 第一二元素有缺口 (`gap=True`) 时需反向分型确认
  - 包含处理 (`exclude_included`) 和实际突破检查 (`actual_break()`)
  - `can_be_end()`（L82-93）：判断线段是否可以结束
- **线段破坏定义算法**：`Seg/SegListDef.py`
- **1+1 终结算法**：`Seg/SegListDYH.py`
- **虚段处理**：`Seg/SegListComm.py` — `collect_left_seg()` (L112-119)，处理最后一个确定线段之后的未确认线段。
- **趋势线**：`Math/TrendLine.py` — 线段内部的支撑/阻力趋势线计算。

### 4. 中枢 (ZhongShu)

- **中枢构建**：`ZS/ZSList.py` — `CZSList` 类：
  - `cal_bi_zs()` (L91-130)：三种中枢算法：
    - `normal`（默认）：段内中枢，不跨段
    - `over_seg`：跨段中枢
    - `auto`：确定段用 normal，未确定段用 over_seg
  - `try_combine()` (L157-161)：中枢合并（zs 模式/peak 模式）
- **中枢背驰判断**：`ZS/ZS.py:162-174` — `is_divergence()`，比较进出中枢笔的 MACD 度量。
- **中枢类**：`ZS/ZS.py` — `CZS`，含进中枢笔 `bi_in`、出中枢笔 `bi_out`、高低范围、子中枢列表等。

### 5. 买卖点 (Buy/Sell Points)

- **买卖点计算**：`BuySellPoint/BSPointList.py` — `CBSPointList` 类：
  - `cal_single_bs1point()` (L165-213)：一类买卖点（趋势背驰）和盘整背驰
  - `treat_bsp2()` (L224-255)：二类买卖点（回撤确认）
  - `treat_bsp2s()` (L257-295)：类二买卖点
  - `treat_bsp3_after()` (L323-366)：三类买卖点（中枢在后）
  - `treat_bsp3_before()` (L368-400)：三类买卖点（中枢在前）
- **买卖点类型**：`Common/CEnum.py` — `BSP_TYPE` 枚举：T1/ T1P/ T2/ T2S/ T3A/ T3B

### 6. 多级别联立计算

- **CChan.load_iterator()**：`Chan.py:238-271` — 递归加载多级别 K 线，建立父子 K 线关系，核心是「大级别驱动小级别」的嵌套迭代模式。
- **trigger_step 模式**：逐根 K 线回放，每帧返回当前缠论元素快照。
- **trigger_load 模式**：从外部按需喂入 K 线，适用于实盘增量更新。

## 依赖环境与运行入口

### Python 版本

- 最低要求 **Python 3.11**（框架是计算密集型，Python 3.11 相比 3.8 有约 16% 的性能提升）

### 核心依赖

```toml
# pyproject.toml
dependencies = ["numpy", "pandas"]
optional-dependencies = {
    "vnpy": ["vnpy", "vnpy_sqlite"],
    "gui": ["PySide6>=6.5"],
}
```

### 安装方式

```bash
# 仅回测/研究
pip install -e .

# 含 vn.py 数据库和插件接口
pip install -e ".[vnpy]"

# 含 GUI
pip install -e ".[vnpy,gui]"
```

### 运行入口

| 入口 | 说明 |
|---|---|
| `python main.py` | 最小 demo：加载 sz.000001 日线数据，计算缠论元素并画图 |
| `python scripts/run_data_cleaning.py --report` | RB 数据清洗 + 质量报告 |
| `python scripts/run_build_continuous.py` | 构建 RB 连续主力合约 |
| `python scripts/run_build_bars.py` | 聚合生成多周期 K 线 |
| `python scripts/run_quality_report.py` | 生成数据质量 HTML 报告 |
| `python scripts/run_chan_rb_minute_trend_strategy.py --limit 5000` | 缠论 BSP 趋势策略回测 |
| `python scripts/run_rb_main_candidate_strategy.py` | RB 主力候选策略回测 |
| `python scripts/run_rb_main_candidate_exit_research.py` | 出场规则对比研究 |
| `python scripts/run_rb_chan_bsp_filter_param_search.py` | BSP 过滤器参数搜索 |
| `python scripts/run_final_15m_strategy_vnpy_backtest.py` | 15 分钟最终策略回测 |
| `python scripts/run_trend_macd_research.py` | Trend MACD 研究 |
| `python examples/run_vnpy_chan.py` | vn.py 插件使用示例 |
| `pytest tests` | 运行全部测试 |

### 数据源

- **BaoStock**（默认）：A 股日线/分钟线
- **AKShare**：A 股/港股/美股
- **CCXT**：加密货币交易所
- **CSV**：本地 CSV 文件
- **自定义**：继承 `CCommonStockApi` 并通过 `custom:文件名.类名` 接入

### 数据管线（RB 期货）

```
vn.py SQLite (DbBarData) → load_raw_from_vnpy("RB%")
  → clean_rb_1m_bars() → RB_1m_raw_clean.parquet
  → build_continuous_contract() → RB_1m_continuous.parquet
  → aggregate_continuous_1m_to_Nm() → N 分钟聚合 K 线
```

数据按 `symbol + trading_day + session` 审计，使用上海时区 (`Asia/Shanghai`) 的静态交易日历，聚合不跨 session 或主力合约。

## 策略退役标准

当以下任一条件触发时，策略应暂停实盘交易并进行人工复盘：

### 硬退役条件 (触发立即暂停)

1. **连续亏损月份**: 连续 3 个自然月策略净值月度亏损 (Sharpe < 0 且 total_return < 0)
2. **回撤超限**: 实盘累计回撤超过历史 walk-forward 最大 OOS 回撤的 1.5 倍
3. **单月极端回撤**: 单月回撤超过 800 点 (RB 螺纹钢，15 分钟周期)
4. **信号断崖**: 连续 5 个交易日无任何 BSP 信号产生 (可能数据管线故障)
5. **滑点失控**: 实盘平均滑点超过回测假设 (1.0 pt) 的 3 倍且持续一周

### 软预警条件 (触发提醒但不暂停)

1. **夏普衰减**: 滚动 3 个月 Sharpe < 0.3 (回测 baseline: IDEAL 全量 Sharpe=0.15)
2. **胜率下降**: 滚动 20 笔交易胜率 < 45% (回测 baseline: 57.4%)
3. **信号漂移**: DriftMonitor 报告 ≥2 个特征严重漂移 (drift_pct > 150%)
4. **盈利因子下降**: 滚动 20 笔 PF < 0.8

### 退役后处理

1. 暂停实盘，保留信号日志 (SignalJournal) 和成交记录
2. 在最新数据上重新跑 walk-forward 回测，检查策略逻辑是否仍然有效
3. 如果 walk-forward 也失效 → 标记策略为 RETIRED，归档配置和研究报告
4. 如果 walk-forward 仍有效 → 排查执行层问题 (滑点、延迟、数据质量)

### 定期审计节奏

- **每日**: 查看成交记录和风控状态，确认无异常
- **每周**: 运行 DriftMonitor，检查信号分布漂移
- **每月**: 输出月度绩效报告 (StandardMetrics)，更新逐年对比表
- **每季**: 重新跑全量 walk-forward，检查是否有更优参数组合

当前项目已完成缠论基础元素计算、数据管线、回测框架和 vn.py 插件化的基础架构。下一步的核心目标是将主观交易策略系统性地进行程序化。以下是分阶段的开发计划：

### 第一阶段：策略信号规范化（当前已部分完成）

1. **BSP 信号提取标准化**
   - 将 `BuySellPoint/BSPointList.py` 产出的买卖点信号以结构化格式导出（CSV/Parquet），保留 `bi_idx`、`klu_idx`、`type`、`is_buy`、特征值等字段。
   - 信号应与 `data_foundation` 的清洗管线对齐，确保每一条信号都可追溯到具体的 K 线位置和主力合约。

2. **入场规则配置化**
   - 将当前散落在各研究脚本中的入场过滤逻辑（DC 结构、OBV、成交量、BSP 类型组合）统一为 JSON/YAML 配置文件格式。
   - 实现 `StrategyEntryConfig` 数据类，支持组合多个过滤条件（AND/OR 逻辑）。

### 第二阶段：出场规则体系

3. **出场规则可插拔框架**
   - 抽取现有研究中的各类出场规则（跟踪止损 trailing stop、固定盈亏比、结构破坏出场、MACD 死叉出场等）为独立的 `ExitRule` 子类。
   - 每个出场规则实现 `check_exit(bar, position, entry)` 接口，返回 `ExitSignal | None`。
   - 支持出场规则叠加（例如：先触发跟踪止损 OR 结构破坏即出场）。

4. **出场规则参数优化**
   - 基于 `ExitRule` 框架，对每种出场规则的核心参数（如 trailing stop 的回撤比例、均线周期）进行网格搜索/贝叶斯优化。
   - 输出各类出场规则在统一入场条件下的绩效对比。

### 第三阶段：完整策略回测与评估

5. **策略配置标准化**
   - 定义 `StrategyConfig`：包含数据源、品种、周期、入场规则集、出场规则集、仓位管理、费用/滑点。
   - 一次配置即可驱动完整的回测流程，避免当前每个研究脚本重复组装管线。

6. **回测报告标准化**
   - 统一输出格式：逐笔交易表、净值曲线、逐年收益、最大回撤、夏普比率、胜率、盈亏比。
   - 生成研究目录结构：`reports/{strategy_name}/{变体}/` 下固定放置 bars/fills/summary/trades/config。

### 第四阶段：实盘对接（通过 vn.py）

7. **实盘信号生成**
   - `vnpy_chan/live_engine.py` 中订阅 vn.py `EVENT_BAR`，实时触发 `CChan.trigger_load()` 增量计算。
   - 将最新 BSP 信号通过上述入场规则过滤后生成 actionable signal。

8. **实盘订单管理**
   - 对接 vn.py 的 `MainEngine.send_order()`，实现自动开仓/平仓。
   - 实现仓位状态机：idle → pending_long → long → pending_close → idle。
   - 记录每笔实盘交易的信号来源和执行情况，用于事后分析。

9. **风控集成**
   - 每日最大亏损限制、单品种最大仓位、最大连续亏损次数。
   - 风控触发后自动暂停交易并推送通知。

### 第五阶段：策略迭代与优化

10. **策略参数自动化搜索**
    - 基于 `chan_futures/backtest.py` 的快速回测能力，实现参数空间的并行搜索。
    - 对入场/出场参数进行系统性优化，输出 Pareto 前沿（收益 vs 回撤）。

11. **多周期多品种扩展**
    - 将当前聚焦 RB 的策略框架推广到更多品种（HC、I、J 等黑色系）。
    - 探索多周期信号共振（如 15 分钟信号 + 60 分钟趋势过滤）。

12. **策略绩效监控**
    - 实盘运行后持续跟踪策略表现，与历史回测对比（检测过拟合/市场体制变化）。
    - 建立策略退役标准（如连续 N 个月 Sharpe < 0 或回撤超过历史最大回撤的 1.5 倍）。
