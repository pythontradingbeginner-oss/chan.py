from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "configs" / "qingpai_rule_contract_v1.json"
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "qingpai_p0"


def test_qingpai_rule_contract_has_unique_traceable_rules() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    rules = contract["rules"]
    rule_ids = [rule["id"] for rule in rules]

    assert contract["status"] == "provisional"
    assert len(rule_ids) == len(set(rule_ids))
    assert all(rule["status"] in contract["statuses"] for rule in rules)
    assert all(rule["decision"].strip() for rule in rules)
    assert all(rule["evidence"] for rule in rules)


def test_qingpai_p0_samples_are_point_in_time_fixtures() -> None:
    manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))
    windows = pd.read_csv(FIXTURE_DIR / "rb_15m_windows.csv", parse_dates=["datetime"])
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    rule_ids = {rule["id"] for rule in contract["rules"]}

    sample_ids = [sample["sample_id"] for sample in manifest["samples"]]
    assert len(sample_ids) == len(set(sample_ids))
    assert set(sample_ids) == set(windows["sample_id"])

    for sample in manifest["samples"]:
        assert set(sample["rule_ids"]) <= rule_ids
        sample_bars = windows[windows["sample_id"] == sample["sample_id"]].copy()
        decision_time = pd.Timestamp(sample["decision_time"])
        assert sample_bars["datetime"].is_monotonic_increasing
        assert (sample_bars["datetime"] == decision_time).any()
        visible = sample_bars[sample_bars["is_visible_at_decision"]]
        hidden = sample_bars[~sample_bars["is_visible_at_decision"]]
        assert not visible.empty
        assert visible["datetime"].max() == decision_time
        assert hidden.empty or hidden["datetime"].min() > decision_time


def test_qingpai_p0_signal_samples_freeze_target_structure() -> None:
    manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))
    signal_samples = [sample for sample in manifest["samples"] if sample["target_bi_idx"] is not None]

    assert signal_samples
    for sample in signal_samples:
        target = sample["target_bsp_snapshot"]
        assert target["bi_idx"] == sample["target_bi_idx"]
        assert sample["target_bsp"] in target["types"]
        assert target["direction"] == sample["direction"]
        assert sample["target_event"] is not None
        assert sample["target_assessment"] is not None
