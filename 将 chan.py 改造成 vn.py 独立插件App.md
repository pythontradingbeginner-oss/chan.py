# 将 chan.py 改造成 vn.py 独立插件/App

## Summary

- 对比结果：两者重合在量化交易、K线、回测、数据导入、策略研究规则；不重合的是核心目录和平台职责。不能直接复制 vn.py 的 `AGENTS.md`，否则会把 `vnpy/event`、`vnpy/trader`、`vnpy/alpha` 等不存在于 `chan.py` 的结构写错。
- v1 目标：在 `H:\Github\chan.py` 内新增一个独立 `vnpy_chan` 插件/App，让 vn.py 的 `BarData`、数据库历史K线和 GUI 菜单能驱动现有 `chan.py` 缠论计算与 RB 期货回测。
- 不改 `H:\Github\vnpy-master` 源码；`vnpy-master` 只作为接口参考和本地依赖来源。

## Key Changes

- 新增 `vnpy_chan` 包：
  - `ChanAnalysisApp`：继承 vn.py `BaseApp`，可通过 `main_engine.add_app(ChanAnalysisApp)` 注册。
  - `ChanAnalysisEngine`：继承 vn.py `BaseEngine`，封装历史K线加载、BarData 转换、缠论触发回放、信号/回测调用。
  - `converter.py`：提供 `BarData -> CKLine_Unit`、`window -> KL_TYPE`、`BarData list -> DataFrame` 转换，默认时区按 Asia/Shanghai 处理。`window` 仅是 chan.py 侧聚合参数，不与 vn.py `Interval` 混用。
  - `ui.py`：提供最小可用的 Qt 窗口，用于选择 RB、SHFE、1m/15m、时间范围、手续费/滑点、是否只做多，并显示回测摘要和成交表。
- 整合现有能力：
  - 复用 `chan_futures` 的 `run_chan_trigger_backtest`、风控、模拟撮合和最小策略，不重写缠论算法。
  - 复用 `data_foundation` 的 vn.py SQLite 导入、RB 清洗、连续主力合约和聚合逻辑。RB 连续主力回测走 `load_raw_from_vnpy("RB%") -> clean_rb_1m_bars -> build_continuous_contract -> aggregate_continuous_1m_to_Nm` 多合约管线；官方 `get_database().load_bar_data()` 仅用于「指定单合约」研究场景，不作默认路径。
  - 保留原有 `Chan.py`、`ChanConfig.py`、`Common/`、`Bi/`、`Seg/`、`ZS/` 等核心计算目录，不迁入 vn.py 主包。
- 新增入口和文档：
  - 增加示例脚本，展示在 vn.py `MainEngine` 中注册 `ChanAnalysisApp`。
  - 增加或更新 `AGENTS.md`，内容改为 chan.py/vn.py 插件版协作说明：说明项目背景、关键目录、开发命令、vn.py 适配边界、运行产物禁提交规则。
  - 增加安装说明：新增 `pyproject.toml` 使本仓库可 editable 安装；依赖本地/环境中的 `vnpy`、`vnpy_sqlite`，`PySide6` 为可选依赖（仅 UI 需要）。`vnpy_chan/__init__.py` 在 import 时自动把仓库根注入 `sys.path`，保证 `from Chan import CChan` 等顶层导入在干净环境可用。

## Public Interfaces

- `from vnpy_chan import ChanAnalysisApp`
- `ChanAnalysisEngine.load_bar(symbol, exchange, interval, start, end) -> list[BarData]`：薄封装官方 `get_database().load_bar_data()`，仅用于单合约研究。
- `ChanAnalysisEngine.run_backtest(bars, config) -> ChanBacktestResult`：接受 `list[BarData]` 或 DataFrame，转 DataFrame 后调 `run_chan_trigger_backtest`。
- `ChanAnalysisEngine.run_from_database(config) -> ChanBacktestResult`：RB 连续主力默认走多合约管线（`load_raw_from_vnpy("RB%")` + 清洗 + 连续主力 + 聚合），`window>1` 时聚合到目标周期。
- `bar_to_klu(bar, kl_type)` 和 `bars_to_ohlc_frame(bars)` 作为稳定转换函数，供测试和外部脚本复用。

### ChanRunConfig 完整定义

```python
from dataclasses import dataclass
from datetime import datetime
from vnpy.trader.constant import Exchange, Interval

@dataclass(frozen=True)
class ChanRunConfig:
    symbol_pattern: str = "RB%"           # LIKE 模式（连续主力路径）；与 symbol 互斥
    symbol: str | None = None             # 精确合约（单合约路径）；与 symbol_pattern 互斥
    exchange: Exchange = Exchange.SHFE
    interval: Interval = Interval.MINUTE  # 仅 1m 入库可取
    window: int = 1                       # 1|15|30|60，映射 KL_TYPE
    start: datetime | None = None
    end: datetime | None = None
    fee_points: float = 1.0
    slippage_points: float = 1.0
    long_only: bool = False
    limit: int | None = None
```

映射规则：
- `symbol_pattern` 非空 → `run_from_database()` 多合约管线
- `symbol` 非空 → `load_bar()` 单合约官方 API 路径
- `long_only=True` → `ChanBacktestConfig(allow_short=False)`
- `window` → `KL_TYPE` 映射：1→K_1M, 15→K_15M, 30→K_30M, 60→K_60M；不支持的值报 `ValueError`

### converter.py 双重命名约定

vnpy 官方 `BarData` 属性名与 `data_foundation/loader.py` 的 SQL 别名列名不同：

| 来源 | open | high | low | close |
|---|---|---|---|---|
| `BarData` 对象属性 | `open_price` | `high_price` | `low_price` | `close_price` |
| `load_raw_from_vnpy` DataFrame | `open` | `high` | `low` | `close` |

- `bar_to_klu(bar, kl_type)`：处理 `BarData` 对象，通过属性访问取 `bar.open_price` 等
- `run_from_database()` 返回的 DataFrame 已通过 SQL 别名统一为 chan.py 命名，直接复用 `chan_futures/feed.py` 的 `row_to_klu`

## Test Plan

- 新增单元测试：
  - vn.py `BarData` 能正确转换为 `CKLine_Unit`，包含 open/high/low/close/volume/turnover/open_interest。
  - `window=1/15/30/60 -> KL_TYPE.K_1M/K_15M/K_30M/K_60M` 映射正确，`window=7` 等不支持窗口明确报错；该映射与 vn.py `Interval` 解耦。
  - 使用伪造数据库对象验证 `ChanAnalysisEngine.load_bar()`（单合约官方 API 路径）。
  - 使用伪造 `load_raw_from_vnpy` 输出验证 `run_from_database()` 多合约管线：清洗、连续主力、聚合、回测全链路数据流。
  - `ChanAnalysisApp` 可被 `MainEngine.add_app()` 注册，engine 名称和 app 名称稳定。
- 回归测试：
  - 运行现有 `tests/test_chan_futures.py`、`tests/test_loader.py`、`tests/test_bar_aggregation.py`。
  - 运行完整 `pytest tests`；如 PySide6/vn.py 插件依赖缺失，GUI 测试标记 skip，并在结果中说明。
- 手动验收：
  - 用 RB 连续主力数据跑 `limit=5000` 的 1m smoke backtest。
  - 用 15m 聚合数据跑一次固定候选策略，确认摘要、成交数、收益点数输出正常。
  - 在 vn.py 示例入口中注册 App，确认菜单出现并能打开窗口。

## ChanLiveEngine（实盘引擎 v2）

v1 仅做回测和研究。v2 通过 `ChanLiveEngine` 接入 CTP 实盘，架构如下：

```
vnpy CTP Gateway ──Tick/Bar──→ MainEngine 事件总线
                                    │
                          EVENT_BAR 订阅
                                    │
                          ChanLiveEngine.on_bar(bar)
                                    │
                          bar_to_klu(bar, kl_type)
                                    │
                          chan.trigger_load({kl_type: [klu]})
                                    │
                          策略信号（BSP 买卖点）
                                    │
                          main_engine.send_order()
```

核心设计要点：
- `ChanLiveEngine` 继承 `BaseEngine`，构造函数签名为 `(main_engine, event_engine)`，内部 `engine_name="ChanLive"`
- `register_event()` 调用 `self.event_engine.register(EVENT_BAR, self._on_bar)`
- `_on_bar(event)` 内取 `event.data: BarData`，经 `bar_to_klu` 转换后触发缠论计算
- 信号生成后用 `self.main_engine.send_order(req)` 下单，走 vnpy OmsEngine
- 多级别联（如 1m 驱动 15m）通过内部维护多个 `trigger_load` 管线实现
- 持仓查询通过 `self.main_engine.get_position(vt_symbol)`，风控复用 `chan_futures/risk.py`
- v1 不包含实盘下单；`ChanLiveEngine` 为 v2 预留接口，v1 先实现事件订阅和信号日志输出

Core File: `vnpy_chan/live_engine.py`

## pyproject.toml 模板

现仓库无 `pyproject.toml`，需新增以支持 editable 安装：

```toml
[project]
name = "chan.py"
version = "1.0.0"
description = "缠论量化分析框架 + vnpy 插件"
requires-python = ">=3.10"
dependencies = [
    "pandas",
    "numpy",
]

[project.optional-dependencies]
vnpy = ["vnpy", "vnpy_sqlite"]
gui = ["PySide6>=6.5"]

[tool.setuptools.packages.find]
include = ["Chan*", "Common*", "KLine*", "Bi*", "Seg*", "ZS*", "Math*", "Plot*",
            "BuySellPoint*", "Combiner*", "DataAPI*", "ChanModel*", "chan_futures*",
            "data_foundation*", "vnpy_chan*"]
```

安装命令：
```bash
pip install -e .              # 仅回测/研究（无 GUI）
pip install -e ".[vnpy]"     # 含 vnpy 回测集成
pip install -e ".[vnpy,gui]" # 含 GUI
```

## 实现约束与导入边界

- **BaseApp 类属性**：必须在**类级别**赋值（非 `__init__` 实例变量），`MainEngine.add_app()` 通过 `app.app_name` 等直接读取类属性：
  ```python
  class ChanAnalysisApp(BaseApp):
      app_name = "ChanAnalysis"
      app_module = "vnpy_chan"
      app_path = Path(__file__).parent
      display_name = "缠论分析"
      engine_class = ChanAnalysisEngine
      widget_name = "ChanAnalysisWidget"
      icon_name = "chan.ico"
  ```
- **BaseEngine 构造签名**：`MainEngine.add_engine()` 调用 `engine_class(self, self.event_engine)` 仅传 2 个参数。`ChanAnalysisEngine.__init__` 必须签名为 `(self, main_engine, event_engine)`，在内部设置 `engine_name="ChanAnalysis"` 后调 `super().__init__(main_engine, event_engine, engine_name)`。同样 `ChanLiveEngine` 签名为 `(main_engine, event_engine)`，`engine_name="ChanLive"`。
- **命名映射**：`ChanRunConfig.long_only` 经 `ChanBacktestConfig(allow_short=not long_only)` 传入，两端不得各自维护同义开关。
- **PySide6 延迟导入**：`ui.py` 在函数体内 `from PySide6 ...`，`converter.py`/`engine.py`/`live_engine.py` 顶层不得 import Qt；保证 `from vnpy_chan import ChanAnalysisApp` 在无 PySide6 环境可用，GUI 测试因此可安全 skip。
- **导入边界**：`vnpy_chan` 作为独立包，但其内部仍用 chan.py 顶层导入（`from Chan import CChan` 等）。v1 通过 `vnpy_chan/__init__.py` 自动 `sys.path.insert` 仓库根解决；v2 再评估是否改包内相对导入。

## Assumptions

- 工作范围限定在 `H:\Github\chan.py`，不直接修改 `H:\Github\vnpy-master`。
- v1 只做 RB 期货 1m/15m 的 vn.py 平台接入和回测/研究，不做真实下单、不接 CTP 实盘网关。`ChanLiveEngine` 实盘引擎为 v2 预留接口设计，v1 仅实现 `EVENT_BAR` 订阅和信号日志输出。
- 现有未提交改动视为用户已有工作；实现时只新增/修改 vn.py 适配所需文件，不回退已有 `README.md`、`data_foundation/`、`chan_futures/`、`scripts/`、`tests/` 改动。
- `AGENTS.md` 将写成 chan.py 插件化后的真实协作说明，而不是复制 vn.py 仓库的原文。
- vn.py `Interval` 仅 `MINUTE/HOUR/DAILY/WEEKLY/TICK`，无 15m 等枚举；故「15m/30m/60m」一律由 chan.py 聚合产生，不从 vn.py 库直接取。
- 现仓库无 `pyproject.toml` 且根 `__init__.py` 为空，editable 安装需先补 `pyproject.toml`；导入边界在 v1 用 `sys.path` 注入兜底。
