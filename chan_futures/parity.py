"""Cross-runtime comparison for canonical decision traces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .trade_intent import DecisionTraceRecord


@dataclass(frozen=True, slots=True)
class TraceMismatch:
    runtime: str
    index: int
    expected: DecisionTraceRecord | None
    actual: DecisionTraceRecord | None


@dataclass(frozen=True, slots=True)
class RuntimeParityReport:
    baseline_runtime: str
    trace_lengths: dict[str, int]
    mismatches: tuple[TraceMismatch, ...]

    @property
    def consistent(self) -> bool:
        return not self.mismatches

    def assert_consistent(self) -> None:
        if self.consistent:
            return
        first = self.mismatches[0]
        raise AssertionError(
            "runtime decision traces differ: "
            f"baseline={self.baseline_runtime}, runtime={first.runtime}, "
            f"index={first.index}, expected={first.expected}, actual={first.actual}"
        )


def compare_runtime_traces(
    traces: Mapping[str, Sequence[DecisionTraceRecord]],
) -> RuntimeParityReport:
    """Compare all normalized traces against the first runtime."""
    if len(traces) < 2:
        raise ValueError("at least two runtime traces are required")

    names = list(traces)
    baseline_name = names[0]
    baseline = traces[baseline_name]
    mismatches: list[TraceMismatch] = []

    for runtime_name in names[1:]:
        actual = traces[runtime_name]
        common = min(len(baseline), len(actual))
        for index in range(common):
            if baseline[index] != actual[index]:
                mismatches.append(
                    TraceMismatch(
                        runtime=runtime_name,
                        index=index,
                        expected=baseline[index],
                        actual=actual[index],
                    )
                )
        for index in range(common, max(len(baseline), len(actual))):
            mismatches.append(
                TraceMismatch(
                    runtime=runtime_name,
                    index=index,
                    expected=baseline[index] if index < len(baseline) else None,
                    actual=actual[index] if index < len(actual) else None,
                )
            )

    return RuntimeParityReport(
        baseline_runtime=baseline_name,
        trace_lengths={name: len(trace) for name, trace in traces.items()},
        mismatches=tuple(mismatches),
    )

