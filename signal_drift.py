"""信号漂移监控 —— 检测实盘信号的分布偏离回测历史。

核心功能:
  1. 从 SignalJournal 加载回测基准 (baseline) 和 实盘增量 (live)
  2. 对关键特征 (divergence_rate, bi_amp, retrace_rate 等) 做 KS 检验
  3. 对 grade 分布做卡方检验
  4. 输出漂移报告: 哪些特征显著偏离, 偏离幅度

用法:
    from signal_drift import DriftMonitor
    dm = DriftMonitor(baseline_dir='reports/full_backtest_ideal', live_dir='reports/live_signals')
    report = dm.analyze()
    if report.warnings:
        print(report.format_alerts())
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


# ════════════════════════════════════════════════════════════════
# 数据结构
# ════════════════════════════════════════════════════════════════


@dataclass
class FeatureDrift:
    """单个特征的漂移摘要。"""
    name: str
    baseline_mean: float
    live_mean: float
    baseline_std: float
    live_std: float
    drift_pct: float                # (live_mean - baseline_mean) / baseline_std × 100
    ks_statistic: float             # KS 检验统计量 (0~1)
    is_significant: bool            # KS p-value < 0.05 或 drift_pct > 50%
    severity: str = "ok"            # "ok" / "warning" / "critical"

    def __str__(self) -> str:
        icon = "🚨" if self.is_significant else "✅"
        return (
            f"{icon} {self.name:>24}: "
            f"baseline={self.baseline_mean:7.4f} → live={self.live_mean:7.4f} "
            f"(drift={self.drift_pct:+.1f}%, KS={self.ks_statistic:.3f})"
        )


@dataclass
class DriftReport:
    """漂移分析总报告。"""

    features: list[FeatureDrift]
    grade_distribution_shift: dict[str, dict[str, float]]  # grade → {baseline_pct, live_pct, delta}
    checks_passed: int
    checks_failed: int
    warnings: list[str] = field(default_factory=list)
    criticals: list[str] = field(default_factory=list)
    recommendation: str = ""

    def format_alerts(self) -> str:
        """格式化为可读告警列表。"""
        lines = ["信号漂移分析报告", "=" * 60]
        lines.append(f"  通过: {self.checks_passed} | 告警: {self.checks_failed}")
        lines.append("")
        lines.append("── 特征漂移 ──")
        for f in self.features:
            lines.append(str(f))
        lines.append("")
        lines.append("── Grade 分布漂移 ──")
        for grade, shift in sorted(self.grade_distribution_shift.items()):
            lines.append(
                f"  {grade:>10}: {shift['baseline_pct']:5.1%} → {shift['live_pct']:5.1%} "
                f"(Δ={shift['delta']:+.1%})"
            )
        if self.warnings:
            lines.append("")
            lines.append("── 告警 ──")
            for w in self.warnings:
                lines.append(f"  ⚠️ {w}")
        if self.criticals:
            lines.append("")
            lines.append("── 严重告警 ──")
            for c in self.criticals:
                lines.append(f"  🚨 {c}")
        if self.recommendation:
            lines.append("")
            lines.append(f"── 建议 ──")
            lines.append(f"  {self.recommendation}")
        return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
# 监控器
# ════════════════════════════════════════════════════════════════


class DriftMonitor:
    """比较回测基准信号和实盘信号的分布差异。

    用法:
        dm = DriftMonitor(
            baseline_dir='reports/full_backtest_ideal',
            live_dir='reports/live_signals',
        )
        report = dm.analyze()
        print(report.format_alerts())
    """

    # 需要监控的关键特征列 (在 signal_events CSV/Parquet 中)
    FEATURE_COLS = [
        "reference_price",
        "divergence_rate",
        "bi_amp",
        "retrace_rate",
        "break_bi_amp",
        "zs_height",
    ]

    GRADE_ORDER = ["ideal", "standard", "weak"]

    def __init__(
        self,
        baseline_dir: str | Path,
        live_dir: str | Path | None = None,
        *,
        ks_threshold: float = 0.10,     # KS 统计量阈值
        drift_pct_threshold: float = 50.0,  # 漂移百分比阈值
    ) -> None:
        self._baseline_dir = Path(baseline_dir)
        self._live_dir = Path(live_dir) if live_dir else None
        self._ks_threshold = ks_threshold
        self._drift_pct_threshold = drift_pct_threshold

    def analyze(self) -> DriftReport:
        """执行完整漂移分析。"""
        try:
            baseline = self._load_events(self._baseline_dir)
        except Exception as e:
            return DriftReport(
                features=[], grade_distribution_shift={},
                checks_passed=0, checks_failed=0,
                warnings=[f"无法加载基线数据: {e}"],
                recommendation="检查基线目录是否正确",
            )

        if baseline.empty:
            return DriftReport(
                features=[], grade_distribution_shift={},
                checks_passed=0, checks_failed=0,
                warnings=["基线数据为空"],
                recommendation="确认基线回测已运行完成",
            )

        if self._live_dir:
            try:
                live = self._load_events(self._live_dir)
            except Exception as e:
                return DriftReport(
                    features=[], grade_distribution_shift={},
                    checks_passed=0, checks_failed=0,
                    warnings=[f"无法加载实盘数据: {e}"],
                    recommendation="等待实盘产生足够数据后再分析",
                )
        else:
            live = baseline.copy()  # 用于测试

        if live.empty:
            return DriftReport(
                features=[], grade_distribution_shift={},
                checks_passed=0, checks_failed=0,
                warnings=["实盘数据为空"],
                recommendation="等待实盘产生信号后再分析",
            )

        features, warnings, criticals = self._analyze_features(baseline, live)
        grade_shift = self._analyze_grades(baseline, live)

        checks_passed = sum(1 for f in features if not f.is_significant)
        checks_failed = sum(1 for f in features if f.is_significant)
        total_warnings = warnings + [f"{f.name}: {f.drift_pct:+.1f}%" for f in features if f.severity == "warning"]
        total_criticals = criticals + [f"{f.name}: {f.drift_pct:+.1f}%" for f in features if f.severity == "critical"]

        recomm = "无异常 —— 实盘信号分布与回测基线一致。"
        if total_criticals:
            recomm = (
                "🚨 关键特征严重漂移 —— 建议暂停策略，"
                "排查是否是数据源变更、合约换月、或市场结构变化导致。"
            )
        elif total_warnings:
            recomm = (
                "⚠️ 部分特征出现漂移 —— 建议密切监控，"
                "每周重新运行一次此分析，确认无恶化趋势。"
            )

        return DriftReport(
            features=features,
            grade_distribution_shift=grade_shift,
            checks_passed=checks_passed,
            checks_failed=checks_failed,
            warnings=total_warnings,
            criticals=total_criticals,
            recommendation=recomm,
        )

    # ════════════════════════════════════════
    # 内部分析
    # ════════════════════════════════════════

    def _load_events(self, directory: Path) -> pd.DataFrame:
        """从 SignalJournal 目录加载事件。"""
        csv_path = directory / "signal_events.csv"
        parquet_path = directory / "signal_events.parquet"
        if csv_path.exists():
            df = pd.read_csv(csv_path)
        elif parquet_path.exists():
            df = pd.read_parquet(parquet_path)
        else:
            raise FileNotFoundError(f"信号文件不存在: {csv_path} 或 {parquet_path}")

        # 解析 features 列 (JSON 字符串 → dict → 展开)
        if "features" in df.columns:
            features_expanded = self._expand_features(df["features"])
            for col in features_expanded.columns:
                if col not in df.columns:
                    df[col] = features_expanded[col]

        return df

    @staticmethod
    def _expand_features(features_col: pd.Series) -> pd.DataFrame:
        """将 JSON 字符串 features 列展开为独立数值列。"""
        records: list[dict[str, Any]] = []
        for val in features_col:
            if isinstance(val, str):
                try:
                    import json
                    records.append(json.loads(val.replace("'", '"')))
                except Exception:
                    records.append({})
            elif isinstance(val, dict):
                records.append(val)
            else:
                records.append({})

        df = pd.DataFrame(records)
        for col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    def _analyze_features(
        self, baseline: pd.DataFrame, live: pd.DataFrame,
    ) -> tuple[list[FeatureDrift], list[str], list[str]]:
        """对每个特征列做 KS 检验和漂移百分比分析。"""
        try:
            from scipy.stats import ks_2samp as _ks_test
        except ImportError:
            _ks_test = None

        results: list[FeatureDrift] = []
        warnings: list[str] = []
        criticals: list[str] = []

        for col in self.FEATURE_COLS:
            if col not in baseline.columns or col not in live.columns:
                continue
            b = baseline[col].dropna().values
            l = live[col].dropna().values
            if len(b) < 2 or len(l) < 2:
                continue

            b_mean, b_std = float(np.mean(b)), float(np.std(b, ddof=1))
            l_mean, l_std = float(np.mean(l)), float(np.std(l, ddof=1))

            drift_pct = ((l_mean - b_mean) / b_std * 100) if b_std > 0 else 0.0

            try:
                if _ks_test is not None:
                    ks_stat, ks_pval = _ks_test(b, l)
                else:
                    # Fallback: simple measure using normalized mean difference
                    pooled_std = np.sqrt((b_std**2 + l_std**2) / 2)
                    ks_stat = abs(b_mean - l_mean) / pooled_std if pooled_std > 0 else 0.0
                    ks_pval = 1.0 if ks_stat < 2.0 else 0.01
            except Exception:
                ks_stat, ks_pval = 0.0, 1.0

            is_sig = ks_pval < 0.05 or abs(drift_pct) > self._drift_pct_threshold

            severity = "ok"
            if abs(drift_pct) > 150:
                severity = "critical"
            elif abs(drift_pct) > 80:
                severity = "warning"

            fd = FeatureDrift(
                name=col, baseline_mean=b_mean, live_mean=l_mean,
                baseline_std=b_std, live_std=l_std,
                drift_pct=drift_pct, ks_statistic=float(ks_stat),
                is_significant=is_sig, severity=severity,
            )
            results.append(fd)
            if severity == "critical":
                criticals.append(f"{col}: 均值漂移 {drift_pct:+.1f}%")
            elif severity == "warning":
                warnings.append(f"{col}: 均值漂移 {drift_pct:+.1f}%")

        return results, warnings, criticals

    def _analyze_grades(
        self, baseline: pd.DataFrame, live: pd.DataFrame,
    ) -> dict[str, dict[str, float]]:
        """比较 grade 分布的偏移。"""
        result: dict[str, dict[str, float]] = {}
        if "grade" not in baseline.columns or "grade" not in live.columns:
            return result

        b_grades = baseline["grade"].value_counts(normalize=True)
        l_grades = live["grade"].value_counts(normalize=True)

        for grade in self.GRADE_ORDER:
            b_pct = float(b_grades.get(grade, 0.0))
            l_pct = float(l_grades.get(grade, 0.0))
            result[grade] = {
                "baseline_pct": b_pct,
                "live_pct": l_pct,
                "delta": l_pct - b_pct,
            }
        return result


# ════════════════════════════════════════════════════════════════
# 便捷函数
# ════════════════════════════════════════════════════════════════


def run_weekly_drift_check(
    baseline_dir: str = "reports/full_backtest_ideal",
    live_dir: str = "reports/live_signals",
    output_dir: str = "reports/drift_checks",
) -> DriftReport:
    """每周运行一次漂移检查 (供 cron/CLI 调用)。"""
    dm = DriftMonitor(baseline_dir=baseline_dir, live_dir=live_dir)
    report = dm.analyze()

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    timestamp = pd.Timestamp.now().strftime("%Y-%m-%d")
    report_path = output_path / f"drift_report_{timestamp}.txt"
    report_path.write_text(report.format_alerts(), encoding="utf-8")

    return report
