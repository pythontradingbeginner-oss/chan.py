# AGENTS.md for chan.py vn.py Plugin

## 项目背景

本仓库以 `chan.py` 缠论计算框架为核心，提供分形、笔、线段、中枢、买卖点、指标计算、绘图、数据接入和策略研究能力。当前扩展目标是把这些能力包装成独立 `vnpy_chan` 插件/App，让 vn.py 的历史 K 线、事件总线和 GUI 菜单可以驱动现有缠论计算与 RB 期货回测。

不要把本仓库当成 `vnpy` 主仓库本身。本仓库不会迁入或修改 `vnpy/event`、`vnpy/trader`、`vnpy/alpha` 等 vn.py 核心目录；这些目录只作为外部依赖和接口参考。

## 关键目录

- `Chan.py`、`ChanConfig.py`：缠论计算主入口和配置。
- `Bi/`、`Seg/`、`ZS/`、`KLine/`、`BuySellPoint/`：缠论结构计算核心。
- `Common/`：枚举、时间、异常和通用工具。
- `DataAPI/`：原项目数据源适配。
- `data_foundation/`：RB 期货 vn.py SQLite 数据读取、清洗、连续主力、聚合、特征和质量报告。
- `chan_futures/`：轻量期货回测桥接，复用 `CChan.trigger_load()`、BSP 信号、风控和模拟撮合。
- `vnpy_chan/`：vn.py 独立插件/App，包含转换层、回测引擎、GUI 入口和 v2 实盘预留引擎。
- `scripts/`：研究和批处理脚本，默认输出到 `reports/` 或 `data/processed/`。
- `tests/`：单元测试和研究流程回归测试。

## 开发命令

推荐 Python 3.10+。如果只做回测/研究：

```bash
pip install -e .
```

如果要使用 vn.py 数据库和插件接口：

```bash
pip install -e ".[vnpy]"
```

如果要打开 GUI：

```bash
pip install -e ".[vnpy,gui]"
```

常用检查：

```bash
pytest tests
pytest tests/test_chan_futures.py tests/test_loader.py tests/test_bar_aggregation.py tests/test_vnpy_chan.py
```

## vn.py 适配边界

- `vnpy_chan.converter.bar_to_klu()` 处理 vn.py `BarData` 对象字段：`open_price`、`high_price`、`low_price`、`close_price`。
- `data_foundation.load_raw_from_vnpy()` 处理 vn.py SQLite `DbBarData` 表，并把价格列别名成 `open`、`high`、`low`、`close`。
- RB 连续主力默认走多合约管线：`load_raw_from_vnpy("RB%") -> clean_rb_1m_bars -> build_continuous_contract -> aggregate_continuous_1m_to_Nm`。
- 官方 `get_database().load_bar_data()` 只用于指定单合约研究路径。
- vn.py `Interval` 没有 15m、30m、60m 枚举，插件中的 `window` 是 chan.py 侧聚合和 `KL_TYPE` 映射参数。
- v1 不做真实下单；`vnpy_chan.live_engine.ChanLiveEngine` 只预留 `EVENT_BAR` 订阅和信号日志输出。

## 编码约定

- 面向用户的中文文档保持 UTF-8 中文，不要转义成 ASCII。
- 不要重写缠论核心算法来适配 vn.py；优先在 `vnpy_chan/` 和 `chan_futures/` 外围转换。
- 公共接口、枚举、配置键、回测输出列名应视为外部脚本可能依赖的接口。
- Qt/PySide6 只能在 `ui.py` 内延迟导入；转换层和引擎层不得顶层导入 GUI 依赖。
- 示例和测试不要写入真实账号、密钥、服务器地址或用户本地绝对路径，除非是文档中的可替换默认示例。

## 运行产物

不要提交 `.vntrader/`、数据库文件、日志、真实账号配置、行情数据、`reports/` 下的大型结果、`data/processed/` 生成物、notebook 大体积输出或环境缓存。
