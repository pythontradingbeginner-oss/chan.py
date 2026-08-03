# 缠论 BSP 策略接入 vnpy 的方法

## 第一步：确保策略文件在正确的位置

vnpy 的 CTA 策略管理器会扫描以下目录下的 `*.py` 文件，提取所有 `CtaTemplate` 子类：

1. **用户策略目录**：`C:\Users\Administrator\strategies\`
2. **vnpy_ctastrategy 内置目录**：`vnpy_ctastrategy/strategies/`

P7 生产基线策略文件在本仓库内：
```
H:\Github\chan.py\vnpy_chan\chan_bsp_strategy.py
```

`C:\Users\Administrator\strategies\chan_bsp_strategy.py` 必须由上述仓库文件
原字节部署，不能单独维护另一份实现。旧版文件已备份为
`chan_bsp_strategy.py.legacy-20260713.bak`，该扩展名不会被 CTA 策略扫描器加载。

部署后运行以下只读检查，校验仓库策略、用户目录策略、严格配置和
RiskManager 发布模板的 SHA256：

```powershell
python scripts\verify_p7_release.py
```

正式冻结发布时使用更严格的检查；它还要求工作树干净、发布状态为
`frozen`，并验证 `release_commit` 所指冻结实现提交中的策略、配置和
RiskManager 模板哈希。允许当前 HEAD 是位于冻结实现提交之后的清单封印提交：

```powershell
python scripts\verify_p7_release.py --require-release-ready
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
   - config_yaml: configs/rb_15m_qingpai_strict.yaml
   - kl_window: 15  (K 线周期，分钟)
   - fixed_size: 1  (固定手数)
   - load_days: 30  (初始化加载历史 K 线天数)
   - shadow_mode: true  (默认影子运行，不真实下单)
   - production_ready: false
   - forward_confirmed: false
   - risk_manager_confirmed: false
5. 点击「初始化」
6. 点击「启动」后先观察影子日志，不会真实下单
```

## 策略参数说明

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| chan_project_path | str | H:/Github/chan.py | chan.py 项目根目录 |
| config_yaml | str | configs/rb_15m_qingpai_strict.yaml | P7 生产候选策略配置 |
| kl_window | int | 15 | K 线周期 (分钟)，实盘门禁支持 5/15/60 |
| fixed_size | int | 1 | 固定开仓手数 |
| load_days | int | 30 | 初始化时加载的历史数据天数 |
| production_ready | bool | false | 真实下单人工确认 |
| shadow_mode | bool | true | 影子运行；为 true 时只记录信号不发单 |
| forward_confirmed | bool | false | 前向仿真/影子观察通过确认 |
| risk_manager_confirmed | bool | false | VeighNa RiskManager 已启用确认 |
| risk_manager_setting_path | str | C:/Users/Administrator/.vntrader/risk_manager_setting.json | RiskManager 配置检查路径 |

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
| production_status | shadow / production / blocked |
| production_block_reason | 实盘门禁阻断原因 |
| risk_status | 策略内 RiskManager 状态 |
| order_status | CTA 委托状态摘要 |

## 策略内部管线 (每个 15 分钟 bar 触发)

```
1m BarData (vnpy CTP)
    │
    ▼
RbSessionBarAggregator (RB 交易时段感知 5m/15m/60m)
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
    ├── 如果无持仓 + grade 通过 + production gate ready → vnpy buy/short
    │
    └── 如果反向 BSP → vnpy sell/cover (策略反转)
```

## P7 真实下单门禁

默认 `shadow_mode=true`，策略只记录信号，不会真实下单。要允许真实开仓，
必须同时满足：

- `production_ready=true`
- `shadow_mode=false`
- `forward_confirmed=true`
- `risk_manager_confirmed=true`
- `risk_manager_setting_path` 指向的 VeighNa RiskManager 配置至少有一条启用规则
- 策略配置中 `production.enabled=true` 且包含硬风控参数和出场规则
- 当前没有待恢复活动委托

发布参考文件：

- `configs/p7_release_manifest.json`
- `configs/veighna_risk_manager_setting.p7.json`

当前清单若为 `candidate_uncommitted`，只表示部署候选物哈希一致，不表示
已经通过正式发布门禁。

当前用户目录下的 RiskManager 配置若全部 `active=false`，真实模式会被阻断。

## 修改策略参数

策略参数在 `config_yaml` 指定的 YAML 文件中：
```yaml
# configs/rb_15m_qingpai_strict.yaml
code: RB_MAIN
kl_type: K_15M
allow_short: true

grading:
  min_grade: standard

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
  max_loss_points: 600.0
  max_consecutive_losses: 5

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
