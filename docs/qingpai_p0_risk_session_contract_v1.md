# P0 风险 Session 接口契约 v1

状态：`engineering_default`

## 目的

把 `daily_loss_limit` 的生命周期从成交副作用中分离，并统一回测、CTA 和轻量 live engine 的 RB 交易日口径。

## 规范接口

```python
RiskManager.advance_session(
    session_key,
    *,
    observed_at=None,
    source="",
) -> bool
```

- `session_key`：规范化交易日键。RB 使用 `YYYY-MM-DD` 形式的 `trading_day`。
- `observed_at`：首次观察到该 session 的市场/事件时间，只用于顺序检查和审计。
- `source`：`backtest_bar`、`cta_bar`、`cta_fill`、`live_bar`、`live_fill` 等运行端来源。
- 返回值：发生 session 前进并重置日内状态时为 `True`，首次初始化或幂等调用为 `False`。

## 状态转换

1. 首次 session：设置当前 session，不清除其他全局风险状态。
2. 同一 session：幂等，不重置日内损益。
3. 后续 session：仅重置 `_daily_realized`，更新 session 元数据。
4. 更早 session：拒绝并抛出异常，不允许迟到事件回滚风险日历。
5. 状态恢复：若持久化状态已有当前 session，首次同 session 事件不得重复清零。

## 调用顺序

- 每根可执行 bar 在风险审批前推进 session。
- 平仓 fill 在更新日内已实现盈亏前推进 session。
- fill 不得假设一定晚于新 session 的首根策略 bar；bar 与成交回报共用同一 resolver。
- 合法平仓不经过开仓审批，但其风险状态更新仍必须带正确 session。

## RB Session 解析

统一使用 `RBTradingCalendar.map_datetime()`。

- 夜盘映射到所属后续交易日。
- 周五夜盘可映射到下周一交易日。
- 输入已有 `trading_day` 时，必须与日历映射一致。
- 日历越界、非交易分钟或显式交易日冲突时失败关闭，不回退到 `timestamp.date()`。

## 不在 v1 决定的事项

- `max_consecutive_losses` 的恢复事件。
- `max_loss_points` 的人工复位或永久停机语义。
- `max_drawdown_pct` 的人工复位、恢复带或永久停机语义。

这些事项由 `OPEN-005` 继续保持未决。
