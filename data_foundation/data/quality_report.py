from __future__ import annotations

import base64
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Literal

import matplotlib.pyplot as plt
import pandas as pd

from .cleaning import EXTREME_MOVE, LIMIT_DOWN, LIMIT_UP, CleaningLog


def generate_quality_report(
    cleaning_log: CleaningLog,
    cleaned_df: pd.DataFrame,
    continuous_df: pd.DataFrame,
    switch_log_df: pd.DataFrame,
    output_path: Path,
    format: Literal["html", "markdown"] = "html",
    aggregation_audit_df: pd.DataFrame | None = None,
) -> Path:
    """Generate a markdown or HTML data quality report."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sections = _build_sections(
        cleaning_log, cleaned_df, continuous_df, switch_log_df, aggregation_audit_df
    )

    if format == "markdown":
        output_path.write_text("\n\n".join(sections.values()), encoding="utf-8")
        return output_path
    if format != "html":
        raise ValueError(f"unsupported report format: {format}")

    charts = _build_charts(cleaned_df, continuous_df, switch_log_df)
    body = "\n".join(_markdown_to_html(section) for section in sections.values())
    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>RB Data Quality Report</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 32px; line-height: 1.5; }}
    table {{ border-collapse: collapse; margin: 16px 0; }}
    th, td {{ border: 1px solid #ddd; padding: 6px 10px; }}
    th {{ background: #f4f4f4; }}
    img {{ max-width: 100%; border: 1px solid #ddd; margin: 12px 0; }}
  </style>
</head>
<body>
{body}
<h2>Charts</h2>
{charts}
</body>
</html>
"""
    output_path.write_text(html, encoding="utf-8")
    return output_path


def _build_sections(
    cleaning_log: CleaningLog,
    cleaned_df: pd.DataFrame,
    continuous_df: pd.DataFrame,
    switch_log_df: pd.DataFrame,
    aggregation_audit_df: pd.DataFrame | None = None,
) -> dict[str, str]:
    overview = _overview(cleaning_log, cleaned_df, continuous_df, switch_log_df)
    completeness = _completeness(cleaning_log)
    anomalies = _anomalies(cleaned_df)
    limits = _limit_days(cleaned_df)
    switches = _switches(switch_log_df)
    sections = {
        "overview": overview,
        "completeness": completeness,
        "anomalies": anomalies,
        "limits": limits,
        "switches": switches,
    }
    if aggregation_audit_df is not None:
        sections["aggregation"] = _aggregation(aggregation_audit_df)
    return sections


def _aggregation(audit: pd.DataFrame) -> str:
    if audit.empty:
        return "# Aggregation Audit\n\nNo aggregation windows."
    summary = (
        audit.groupby(["frequency_minutes", "status", "reason"], dropna=False, as_index=False)
        .size()
        .rename(columns={"size": "windows"})
    )
    daily = (
        audit[audit["status"] == "GENERATED"]
        .groupby(["frequency_minutes", "trading_day"], as_index=False)
        .size()
        .rename(columns={"size": "generated_bars"})
    )
    return (
        "# Aggregation Audit\n\n## Window outcomes\n\n"
        + _dataframe_table(summary)
        + "\n\n## Daily generated-bar range\n\n"
        + _dataframe_table(
            daily.groupby("frequency_minutes", as_index=False)["generated_bars"]
            .agg(["min", "max", "mean"])
            .reset_index()
        )
    )


def _overview(
    cleaning_log: CleaningLog,
    cleaned_df: pd.DataFrame,
    continuous_df: pd.DataFrame,
    switch_log_df: pd.DataFrame,
) -> str:
    dates = pd.to_datetime(cleaned_df["datetime"]) if not cleaned_df.empty else pd.Series(dtype="datetime64[ns]")
    rows = [
        ("Start", _date_text(dates.min() if not dates.empty else None)),
        ("End", _date_text(dates.max() if not dates.empty else None)),
        ("Raw rows", cleaning_log.raw_rows),
        ("Cleaned rows", cleaning_log.final_rows),
        ("Continuous rows", len(continuous_df)),
        ("Unique contracts", cleaned_df["symbol"].nunique() if "symbol" in cleaned_df else 0),
        ("Main switches", len(switch_log_df)),
    ]
    return "# Overview\n\n" + _table(["Metric", "Value"], rows)


def _completeness(cleaning_log: CleaningLog) -> str:
    if cleaning_log.missing_details.empty:
        return "# Completeness\n\nNo missing expected RB minutes were detected."

    frame = cleaning_log.missing_details.copy()
    frame["month"] = pd.to_datetime(frame["trading_day"]).dt.to_period("M").astype(str)
    monthly = frame.groupby("month", as_index=False).size().rename(columns={"size": "expected_bars_absent"})
    top = (
        frame.groupby(["symbol", "trading_day", "session"], as_index=False)
        .size()
        .rename(columns={"size": "expected_bars_absent"})
        .sort_values("expected_bars_absent", ascending=False)
        .head(20)
    )
    return (
        "# Completeness\n\n"
        "An absent expected bar can mean no trade or a source-data gap; minute bars alone cannot distinguish them.\n\n"
        "## Expected bars absent by month\n\n"
        + _dataframe_table(monthly)
        + "\n\n## Top affected contract sessions\n\n"
        + _dataframe_table(top)
    )


def _anomalies(cleaned_df: pd.DataFrame) -> str:
    if cleaned_df.empty:
        return "# Anomaly Events\n\nNo data."

    frame = cleaned_df.sort_values(["symbol", "datetime"]).copy()
    frame["return"] = frame.groupby("symbol")["close"].pct_change()
    extreme = frame[(frame["flags"].astype(int) & EXTREME_MOVE) != 0].copy()
    extreme["abs_return"] = extreme["return"].abs()
    columns = ["datetime", "symbol", "return", "open", "high", "low", "close"]
    top = extreme.sort_values("abs_return", ascending=False).head(50)[columns]
    if top.empty:
        top_text = "No extreme moves were flagged."
    else:
        top_text = _dataframe_table(top)
    return (
        "# Anomaly Events\n\n"
        "## Extreme move Top 50\n\n"
        + top_text
        + "\n\n## OHLC logic errors\n\n"
        "Invalid OHLC rows are removed during cleaning; see the cleaning log counts."
    )


def _limit_days(cleaned_df: pd.DataFrame) -> str:
    if cleaned_df.empty:
        return "# Limit Days\n\nNo data."
    mask = (cleaned_df["flags"].astype(int) & (LIMIT_UP | LIMIT_DOWN)) != 0
    columns = [
        "datetime", "trading_day", "symbol", "flags", "proxy_previous_close",
        "limit_ratio", "estimated_limit_up", "estimated_limit_down", "limit_rule_source",
    ]
    columns = [column for column in columns if column in cleaned_df.columns]
    events = cleaned_df.loc[mask, columns].drop_duplicates()
    if events.empty:
        return "# Limit Days\n\nNo limit-up or limit-down days were flagged."
    return (
        "# Estimated Limit Touches\n\n"
        "These are minute-level proxy touches, not official limits or evidence that orders were unfillable.\n\n"
        + _dataframe_table(events)
    )


def _switches(switch_log_df: pd.DataFrame) -> str:
    if switch_log_df.empty:
        return "# Main Contract Switches\n\nNo switches."
    return "# Main Contract Switches\n\n" + _dataframe_table(switch_log_df)


def _build_charts(
    cleaned_df: pd.DataFrame,
    continuous_df: pd.DataFrame,
    switch_log_df: pd.DataFrame,
) -> str:
    images = []
    if not continuous_df.empty:
        images.append(("Adjusted vs raw close", _price_chart(continuous_df, switch_log_df)))
    if "trading_day" in cleaned_df and not cleaned_df.empty:
        images.append(("Monthly completeness", _missing_heatmap(cleaned_df)))
    return "\n".join(
        f"<h3>{title}</h3><img src=\"data:image/png;base64,{image}\" />"
        for title, image in images
    )


def _price_chart(continuous_df: pd.DataFrame, switch_log_df: pd.DataFrame) -> str:
    frame = continuous_df.sort_values("datetime")
    fig, axis = plt.subplots(figsize=(11, 4))
    axis.plot(frame["datetime"], frame["raw_close"], label="raw close", linewidth=0.8)
    axis.plot(frame["datetime"], frame["close"], label="adjusted close", linewidth=0.8)
    for row in switch_log_df.itertuples():
        axis.axvline(pd.Timestamp(row.switch_date), color="red", alpha=0.25)
    axis.legend(loc="upper left")
    axis.set_title("RB continuous close")
    return _figure_to_base64(fig)


def _missing_heatmap(cleaned_df: pd.DataFrame) -> str:
    frame = cleaned_df.copy()
    frame["month"] = pd.to_datetime(frame["trading_day"]).dt.to_period("M").astype(str)
    counts = frame.groupby("month").size().tail(36)
    fig, axis = plt.subplots(figsize=(11, 3))
    axis.bar(counts.index, counts.values)
    axis.set_title("Monthly retained one-minute rows")
    axis.tick_params(axis="x", rotation=90)
    return _figure_to_base64(fig)


def _figure_to_base64(fig: plt.Figure) -> str:
    buffer = BytesIO()
    fig.tight_layout()
    fig.savefig(buffer, format="png", dpi=130)
    plt.close(fig)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _table(headers: list[str], rows: list[tuple[object, object]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    lines.extend(f"| {left} | {right} |" for left, right in rows)
    return "\n".join(lines)


def _dataframe_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "No rows."
    headers = [str(column) for column in frame.columns]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in frame.itertuples(index=False, name=None):
        lines.append("| " + " | ".join(_format_cell(value) for value in row) + " |")
    return "\n".join(lines)


def _format_cell(value: object) -> str:
    if pd.isna(value):
        return ""
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _markdown_to_html(markdown: str) -> str:
    lines = []
    in_table = False
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line:
            if in_table:
                lines.append("</table>")
                in_table = False
            continue
        if line.startswith("# "):
            lines.append(f"<h1>{line[2:]}</h1>")
        elif line.startswith("## "):
            lines.append(f"<h2>{line[3:]}</h2>")
        elif line.startswith("|"):
            if "---" in line:
                continue
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            tag = "th" if not in_table else "td"
            if not in_table:
                lines.append("<table>")
                in_table = True
            lines.append("<tr>" + "".join(f"<{tag}>{cell}</{tag}>" for cell in cells) + "</tr>")
        else:
            if in_table:
                lines.append("</table>")
                in_table = False
            lines.append(f"<p>{line}</p>")
    if in_table:
        lines.append("</table>")
    return "\n".join(lines)


def _date_text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, date):
        return value.isoformat()
    return str(value)
