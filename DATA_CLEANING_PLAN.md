# RB 数据清洗口径

本项目已采用统一的 SHFE/RB 静态交易日历与会话模型：一分钟行情使用结束时间标签，日盘从 `09:01` 开始，周五夜盘归入下周一交易日；节假日、2020 年夜盘暂停和交易所特殊休市均由仓库内静态日历表达。超出日历范围直接报错。

清洗按 `symbol + trading_day + session` 独立审计预期分钟，不插值。`EXPECTED_BAR_ABSENT` 只表示预期 bar 未出现，不能单凭分钟数据断言行情源丢包。5/15/30/60 分钟聚合不跨 session、交易日或主力合约，不完整窗口及 session 尾段进入审计。

连续合约一次主力选择同时生成：

- `RB_1m_continuous_raw.parquet`：策略、成交模拟和审计默认输入；
- `RB_1m_continuous_adjusted.parquet`：后复权研究数据；
- 共用换月日志和主力选择审计。

涨跌停标志仅为基于上一交易日收盘代理的“估算触及”，不代表官方限价或无法成交。

执行顺序：

```bash
python scripts/run_data_cleaning.py
python scripts/run_build_continuous.py
python scripts/run_build_bars.py
python scripts/run_quality_report.py
```

`load_continuous_1m()` 默认读取原价；研究后复权数据时显式传入 `price_mode="adjusted"`。
