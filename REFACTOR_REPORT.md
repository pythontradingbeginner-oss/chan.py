# chan.py 策略框架重构完成报告

> 日期: 2026-07-13
> 测试: 203 passed, 0 failed, 1 warning
> 总代码量: 6,136 行 (30 个文件)

---

## 总体目标

将散落在 5 份重复研究脚本中的回测逻辑重构为统一的生产级量化策略框架。

---

## Phase 1: 统一配置 + 回测核心

| 文件 | 状态 | 说明 |
|---|---|---|
| `chan_futures/config.py` | 新建 | 统一 `StrategyConfig` + 8 个嵌套 dataclass (ChanParams / GradingParams / EntryPolicyConfig / ExitRuleSpec / RiskParams / SizingParams / ExecutionParams / FilterParams) |
| `chan_futures/config_loader.py` | 新建 | YAML/JSON 加载 + 9 条出场规则注册表 + ExitManager 构建 |
| `chan_futures/backtest.py` | 重写 | 统一 `run_backtest()` + ExitManager 接入 + 过滤器管线 + 向后兼容 wrapper |
| `configs/rb_15m_trend_ideal.yaml` | 新建 | 已知最佳配置: IDEAL grade + Structure + Fixed180 + Trail500/50 + Time192 |
| `configs/rb_15m_trend_standard.yaml` | 新建 | 平衡版: STANDARD grade, 含风控 |

### Phase 2: ExitManager + 风控 + 仓位

| 文件 | 状态 | 说明 |
|---|---|---|
| `chan_futures/risk.py` | 重写 | 33→221 行: 5 个风控维度 (max_abs_position / max_loss_points / daily_loss_limit / max_consecutive_losses / max_drawdown_pct) + 有状态追踪 + on_fill() |
| `chan_futures/sizing.py` | 新建 | FixedSizer / ATRSizer / FixedFractionalSizer + 工厂函数 |

### Phase 3: CLI 工具 + 参数扫描 + Walkforward

| 文件 | 状态 | 说明 |
|---|---|---|
| `chan_futures/cli.py` | 新建 | Click 驱动 CLI: `chan backtest/sweep/walkforward/data-check` |
| `chan_futures/sweep.py` | 新建 | 参数网格扫描引擎: `--param exits.TrailingStopRule.trigger_points=300,500,800` 格式 |
| `chan_futures/walkforward.py` | 新建 | Anchored walkforward: 训练/测试窗口按日期切分, OOS metrics 汇总 |
| `pyproject.toml` | 修改 | `chan = "chan_futures.cli:main"` console_scripts 入口点 |

### Phase 4: 核心算法测试 + 数据管线加固

| 文件 | 状态 | 测试数 |
|---|---|---|
| `tests/test_chan_bi.py` | 新建 | 7 (平直K线无笔/最小跨度/振幅正数/方向一致性/KLC跨度/严格vs宽松) |
| `tests/test_chan_seg.py` | 新建 | 5 (三笔成段/方向一致性/bi_list归属/方向交替) |
| `tests/test_chan_zs.py` | 新建 | 5 (高低合法/极值关系/重叠形成中枢/min_zs_cnt过滤/normal模式) |
| `tests/test_chan_bsp.py` | 新建 | 4 (is_buy/BSP归属/divergence_rate/bs_type过滤) |
| `tests/test_signal_extractor.py` | 新建 | 7 (事件生成/去重/必填字段 + 日历加载/唯一性/夜盘归属/节假日) |

### Phase 5: DC/OBV/Volume 过滤管线

| 文件 | 状态 | 说明 |
|---|---|---|
| `chan_futures/filters.py` | 新建 | Sticky DC 趋势分类器 + OBV 均线 + 成交量放大 + FilterContext |
| `configs/rb_15m_trend_filtered.yaml` | 新建 | DC30 sticky + OBV40 + Vol1.0 配置 |

### 修改的脚本

| 文件 | 变更 |
|---|---|
| `scripts/run_full_backtest_pipeline.py` | 改用 `run_backtest()` 统一核心, sweep/walkforward 内联 |
| `scripts/run_joint_sweep.py` | 删除 `run_one()` 内部回测循环, 改用 `run_backtest()` |
| `scripts/run_walkforward.py` | 删除 `run_one_pass()` 内部回测循环, 改用 `run_backtest()` |
| `strategy_policy/reporting.py` | V2 增强: Sharpe(bar-level年化)/Sortino/Calmar/盈亏比/成本分解/收益分布/逐年完整指标 |

---

## 绩效对比

| 指标 | 无过滤 (基线) | DC30+OBV+Vol (sticky) | 变化 |
|---|---|---|---|
| 交易笔数 | 331 | **16** | ↓95% |
| 净利润(点) | +904 | +350 | ↓61% |
| 最大回撤(点) | 1,404 | **487** | ↓65% |
| Calmar | 0.10 | 0.11 | +10% |
| 胜率 | 57.4% | 56.2% | -1% |
| **盈亏比** | 0.81 | **1.11** | +37% |
| **盈利因子** | 1.10 | **1.43** | +30% |
| 每笔期望(点) | +4.4 | **+19.8** | 4.5x |
| Sharpe | 0.15 | 0.24 | +60% |
| 偏态 | +0.38 | -0.11 | 正偏→轻度负偏 |
| **成本吞噬** | 49.3% | **15.5%** | ↓69% |
| 最大回撤持续 | 12,290 bar | 16,550 bar | ↑35% |
| 盈利年比例 | 6/8 (75%) | 4/8 (50%) | ↓33% |

**过滤带来的正向改进**: PF 1.10→1.43 (+30%), 盈亏比 0.81→1.11 (+37%), 成本吞噬 49%→16% (-69%), DD 1,404→487 (-65%)。

**过滤带来的问题**: 净利润从 +904 降到 +350 (-61%), 盈利年从 6/8 降到 4/8, 偏态从正偏变为负偏。

核心矛盾: **过滤太紧→信号太稀缺→1 笔亏损年就拖累全年; 过滤太松→噪声多→成本吞噬毛利**。原始研究报告中的 27 笔/+3,133 点/胜率 63%/PF 2.68 使用了**不同的回测框架**（更简单的 bar 级别模拟 + `dc_is_bull/dc_is_bear` 预计算列 + fast backtest arrays），而新的统一回测核心使用逐笔缠论计算 + ExitManager, 因此产出不完全可比。下一步应是调参 (dc_entry_buffer/exit_buffer) 以在信号质量和数量之间找到更好的平衡点。

---

## 待办

1. **调参 DC 过滤**: 尝试 dc_entry_buffer=10.0 或 20.0, 寻找 16~30 笔之间的最佳点
2. **接入 BSP 类型过滤** (`run_rb_chan_bsp_filter_param_search.py` 的参数网格搜索逻辑)
3. **试验不带过滤的 STANDARD grade** —— 当前 filtered 默认用 IDEAL, 加上过滤后信号进一步稀疏
4. **K 线级别歧义**: 原始研究的回测使用 `aggregate_continuous_1m_to_Nm` 产生的 15m bars, 而新的 `run_backtest()` 使用 `data/processed/RB_15m_continuous_raw.parquet` —— 两者来自同一管线但可能有微小的 bar 对齐差异
