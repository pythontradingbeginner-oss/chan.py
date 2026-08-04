"""Verify P7 release artifacts without changing the repository."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = PROJECT_ROOT / "configs" / "p7_release_manifest.json"
REQUIRED_ARTIFACTS = {
    "strategy_source",
    "strategy_deployed",
    "strategy_config",
    "risk_manager_profile",
    "rb_trading_days",
    "session_rules",
    "calendar_metadata",
}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_artifact_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def git_output(*args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def git_bytes(*args: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), *args],
        check=True,
        capture_output=True,
    )
    return completed.stdout


def git_succeeds(*args: str) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), *args],
        check=False,
        capture_output=True,
    )
    return completed.returncode == 0


def verify_release(
    manifest_path: Path = DEFAULT_MANIFEST,
    *,
    include_deployed: bool = True,
    require_release_ready: bool = False,
) -> list[str]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors: list[str] = []

    artifacts = manifest.get("artifacts", {})
    missing_artifacts = REQUIRED_ARTIFACTS - set(artifacts)
    if missing_artifacts:
        errors.append(f"manifest missing artifacts: {sorted(missing_artifacts)}")
    for name, artifact in artifacts.items():
        if name == "strategy_deployed" and not include_deployed:
            continue
        path = resolve_artifact_path(str(artifact["path"]))
        if not path.is_file():
            errors.append(f"{name}: missing file: {path}")
            continue
        actual = file_sha256(path)
        expected = str(artifact["sha256"]).lower()
        if actual != expected:
            errors.append(
                f"{name}: sha256 mismatch: expected={expected} actual={actual}"
            )
        else:
            print(f"OK {name}: {actual}  {path}")

    source = artifacts.get("strategy_source")
    deployed = artifacts.get("strategy_deployed")
    if source and deployed and include_deployed:
        source_path = resolve_artifact_path(str(source["path"]))
        deployed_path = resolve_artifact_path(str(deployed["path"]))
        if source_path.is_file() and deployed_path.is_file():
            if source_path.read_bytes() != deployed_path.read_bytes():
                errors.append("strategy_deployed: bytes differ from strategy_source")

    _verify_calendar_metadata(manifest, artifacts, errors)
    _verify_risk_profile(artifacts, errors)
    _verify_cta_setting(manifest, errors)

    release_status = str(manifest.get("release_status", ""))
    head = git_output("rev-parse", "HEAD")
    expected_head = str(manifest.get("repository_head", ""))
    if release_status != "frozen" and head != expected_head:
        errors.append(f"repository_head: expected={expected_head} actual={head}")
    else:
        print(f"OK repository_head: current={head} build={expected_head}")

    if require_release_ready:
        if release_status != "frozen":
            errors.append(
                "release_status must be 'frozen' for a production-ready release"
            )
        release_commit = str(manifest.get("release_commit") or "")
        resolved_commit = ""
        if not release_commit:
            errors.append("release_commit must identify the frozen implementation commit")
        elif not git_succeeds("rev-parse", "--verify", f"{release_commit}^{{commit}}"):
            errors.append(f"release_commit is not a valid commit: {release_commit}")
        else:
            resolved_commit = git_output(
                "rev-parse", "--verify", f"{release_commit}^{{commit}}"
            )
            if not git_succeeds("merge-base", "--is-ancestor", resolved_commit, head):
                errors.append("release_commit must be an ancestor of the current HEAD")

        if resolved_commit:
            for name, artifact in artifacts.items():
                raw_path = str(artifact["path"])
                if name == "strategy_deployed" or Path(raw_path).expanduser().is_absolute():
                    continue
                try:
                    payload = git_bytes("show", f"{resolved_commit}:{raw_path}")
                except subprocess.CalledProcessError:
                    errors.append(
                        f"{name}: missing from release_commit {resolved_commit}: {raw_path}"
                    )
                    continue
                actual = hashlib.sha256(payload).hexdigest()
                expected = str(artifact["sha256"]).lower()
                if actual != expected:
                    errors.append(
                        f"{name}: release_commit sha256 mismatch: "
                        f"expected={expected} actual={actual}"
                    )
                else:
                    print(f"OK {name} in release_commit: {actual}")

        if not bool(manifest.get("source_tree_clean_at_build")):
            errors.append("source_tree_clean_at_build must be true")
        dirty = git_output("status", "--porcelain", "--untracked-files=all", "--", ".")
        if dirty:
            errors.append("repository worktree is not clean")

    return errors


def _verify_calendar_metadata(
    manifest: dict,
    artifacts: dict,
    errors: list[str],
) -> None:
    artifact = artifacts.get("calendar_metadata")
    if not artifact:
        return
    path = resolve_artifact_path(str(artifact["path"]))
    if not path.is_file():
        return
    metadata = json.loads(path.read_text(encoding="utf-8"))
    expected = str(manifest.get("calendar_valid_through", ""))
    if metadata.get("valid_through") != expected:
        errors.append(
            "calendar_metadata: valid_through mismatch: "
            f"expected={expected} actual={metadata.get('valid_through')}"
        )
    if metadata.get("minute_label_convention") != "bar_end":
        errors.append("calendar_metadata: minute_label_convention must be bar_end")
    if not metadata.get("source_urls"):
        errors.append("calendar_metadata: source_urls must not be empty")


def _verify_risk_profile(artifacts: dict, errors: list[str]) -> None:
    artifact = artifacts.get("risk_manager_profile")
    if not artifact:
        return
    path = resolve_artifact_path(str(artifact["path"]))
    if not path.is_file():
        return
    profile = json.loads(path.read_text(encoding="utf-8"))
    if not any(
        isinstance(value, dict) and bool(value.get("active"))
        for value in profile.values()
    ):
        errors.append("risk_manager_profile: no active hard-risk rule")


def _verify_cta_setting(manifest: dict, errors: list[str]) -> None:
    spec = manifest.get("cta_strategy_setting")
    if not isinstance(spec, dict):
        errors.append("cta_strategy_setting specification is missing")
        return
    path = resolve_artifact_path(str(spec.get("path", "")))
    if not path.is_file():
        errors.append(f"cta_strategy_setting: missing file: {path}")
        return
    payload = json.loads(path.read_text(encoding="utf-8"))
    strategy_name = str(spec.get("strategy_name", ""))
    strategy = payload.get(strategy_name)
    if not isinstance(strategy, dict):
        errors.append(f"cta_strategy_setting: missing strategy {strategy_name}")
        return
    setting = strategy.get("setting", {})
    for name, expected in spec.get("required_setting", {}).items():
        actual = setting.get(name)
        if actual != expected:
            errors.append(
                f"cta_strategy_setting.{name}: expected={expected!r} actual={actual!r}"
            )
    for name in spec.get("forbidden_setting", []):
        if name in setting:
            errors.append(f"cta_strategy_setting: forbidden legacy key {name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--skip-deployed", action="store_true")
    parser.add_argument("--require-release-ready", action="store_true")
    args = parser.parse_args()

    errors = verify_release(
        args.manifest.resolve(),
        include_deployed=not args.skip_deployed,
        require_release_ready=args.require_release_ready,
    )
    if errors:
        for error in errors:
            print(f"ERROR {error}")
        return 1

    print("P7 release artifact verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
