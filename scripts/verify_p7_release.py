"""Verify P7 release artifacts without changing the repository."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = PROJECT_ROOT / "configs" / "p7_release_manifest.json"


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
