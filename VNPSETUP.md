# 缠论 BSP 策略接入 vnpy 的方法

## 第一步：确保策略文件在正确的位置

vnpy 的 CTA 策略管理器会扫描以下目录下的 `*.py` 文件，提取所有 `CtaTemplate` 子类：

1. **用户策略目录**：`C:\Users\Administrator\strategies\`
2. **vnpy_ctastrategy 内置目录**：`vnpy_ctastrategy/strategies/`

已创建的策略文件：
```
C:\Users\Administrator\strategies\chan_bsp_strategy.py
```

## 第二步：确保 PYTHONPATH 包含 chan.py 项目

策略文件内部会自动尝试如下路径：
```python
H:/Github/chan.py
C:/Users/Administrator/Github/chan.py
```

也可以在 vnpy GUI 中加载策略后，通过参数 `chan_project_path` 手动修改。

或者直接在 Windows 环境变量中设置：
```
PYTHONPATH=H:\Github\chan.py
```

## 第三步：在 vnpy GUI 中加载

```
1. 打开 vnpy 主程序
2. 点击左侧「CTA策略」
3. 在策略管理器中选择 "ChanBspStrategy"
4. 配置参数：
   - chan_project_path: H:/Github/chan.py
   - config_yaml: configs/rb_15m_trend_ideal.yaml
   - kl_window: 15  (K 线周期，分钟)
   - fixed_size: 1  (固定手数)
   - load_days: 30  (初始化加载历史 K 线天数)
5. 点击「初始化」
6. 点击「启动」
```

## 策略参数说明

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| chan_project_path | str | H:/Github/chan.py | chan.py 项目根目录 |
| config_yaml | str | configs/rb_15m_trend_ideal.yaml | 策略配置文件 (相对或绝对路径) |
| kl_window | int | 15 | K 线周期 (分钟) |
| fixed_size | int | 1 | 固定开仓手数 |
| load_days | int | 30 | 初始化时加载的历史数据天数 |

## GUI 实时监控变量

| 变量 | 说明 |
|---|---|
| bi_count | 当前笔数 |
| seg_count | 当前线段数 |
| zs_count | 当前中枢数 |
| last_bsp_type | 最新买卖点类型 (1/1p/2/2s/3a/3b) |
| last_bsp_direction | 最新买卖点方向 (buy/sell) |
| last_signal_grade | 最新信号评分 (ideal/standard/weak) |
| bars_processed | 已处理 K 线数 |
| exit_reason | 最近一次出场原因 |
| total_trades | 累计交易笔数 |
| total_pnl | 累计盈亏 (点) |
| peak_equity | 峰值权益 |

## 策略内部管线 (每个 15 分钟 bar 触发)

```
BarData (vnpy CTP)
    │
    ▼
CKLine_Unit (chan.py)
    │
    ▼
CChan.trigger_load()
    │
    ├── BSP 检测 (形态学买卖点)
    │
    ▼
SignalExtractor → SignalEvent (客观事实快照)
    │
    ▼
T1/T2/T3 评分器 → SignalAssessment (structural_score → grade)
    │
    ▼
GradeFilter (只通过 grade >= config.min_grade)
    │
    ├── 如果有持仓 → ExitManager.check()
    │     ├── StructureStopRule (结构锚点止损)
    │     ├── FixedStopRule (固定点数止损)
    │     ├── TrailingStopRule (跟踪止损)
    │     └── TimeStopRule (超时出场)
    │
    ├── 如果无持仓 + grade 通过 → vnpy buy/short
    │
    └── 如果反向 BSP → vnpy sell/cover (策略反转)
```

## 修改策略参数

策略参数在 `config_yaml` 指定的 YAML 文件中：
```yaml
# configs/rb_15m_trend_ideal.yaml
code: RB_MAIN
kl_type: K_15M
allow_short: true

grading:
  min_grade: ideal     # 修改此处调整信号质量要求

exits:
  - type: StructureStopRule
    priority: 10
  - type: FixedStopRule
    priority: 20
    stop_points: 180.0  # 修改此处调整止损点数
  - type: TrailingStopRule
    priority: 30
    trigger_points: 500.0  # 修改此处调整跟踪止损触发点
    giveback_ratio: 0.5    # 修改此处调整允许回吐比例
  - type: TimeStopRule
    priority: 50
    max_bars: 192  # 修改此处调整持仓上限

risk:
  max_abs_position: 1
  max_consecutive_losses: 5  # 修改此处调整连续亏损上限

execution:
  fee_points: 1.0     # 修改此处调整手续费假设
  slippage_points: 1.0  # 修改此处调整滑点假设
```

## 四个模式

可以通过修改 `config_yaml` 指向不同的配置文件来切换策略模式：

| 模式 | 配置文件 | grade 门槛 | 适用场景 |
|---|---|---|---|
| 保守 | configs/rb_15m_trend_ideal.yaml | ideal | 减少信号，提高质量 |
| 平衡 | configs/rb_15m_trend_standard.yaml | standard | 增加频率，接受更多交易 |
| 激进 | (自定义) | weak | 所有 BSP 都接受 |
| 仅多 | (自定义) | ideal | allow_short: false |
