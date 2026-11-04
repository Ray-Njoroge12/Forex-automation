"""R10 freeze manifest: record the configuration a forward test runs on, then check it still matches.

Plan Phase 3 item 6 and Phase 6 no-change rule. The manifest records:
  * policy hash, as printed by ``validation.print_policy_hash --mode core_srs``
    (run as a subprocess in the target checkout, with this process's environment);
  * sha256 of fx_ai_engine/config_microcapital.py;
  * sha256 of the EA source, the ExpectedPolicyHash pinned in it, and sha256 of
    the compiled EA (.ex5) in the terminal's MQL5/Experts folder when found;
  * sha256 of every code file (.py, .mq5, .mqh, .md) under the AGENTS.md
    protected paths; runtime files in bridge/ (json, lock) are not code and are skipped;
  * the git commit (``git rev-parse HEAD``) when the target is a git checkout.

Both commands are read-only against the target checkout: they read files, run
print_policy_hash and ``git rev-parse``, and write only the manifest / report.

Usage (from fx_ai_engine/):
    ../.venv/bin/python -m research.forward_test.freeze create --out <manifest.json> [--repo-root ..] [--label ...]
    ../.venv/bin/python -m research.forward_test.freeze check --manifest <manifest.json> [--repo-root ..]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

SCHEMA = "fx-forward-freeze/1"
POLICY_MODE = "core_srs"
CONFIG_PATH = "fx_ai_engine/config_microcapital.py"
EA_SOURCE_PATH = "fx_ai_engine/mt5_ea/FX_Execution.mq5"
PROTECTED_PATHS = (  # AGENTS.md "Protected paths"
    "fx_ai_engine/core/risk",
    "fx_ai_engine/config_microcapital.py",
    "fx_ai_engine/mt5_ea",
    "fx_ai_engine/bridge",
    "SRS_v1.md",
)
CODE_SUFFIXES = (".py", ".mq5", ".mqh", ".md")
DEFAULT_EA_BUILD = (Path.home() / "Library" / "Application Support" / "net.metaquotes.wine.metatrader5" / "drive_c"
                    / "Program Files" / "MetaTrader 5" / "MQL5" / "Experts" / "FX_Execution.ex5")
EA_PIN_RE = re.compile(r'input string ExpectedPolicyHash = "([0-9a-f]*)";')
DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[3]

PolicyHashFn = Callable[[Path], str]
GitFn = Callable[[Path], "str | None"]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def protected_file_hashes(repo_root: Path, paths: tuple[str, ...] = PROTECTED_PATHS) -> dict[str, str]:
    """Repo-relative path -> sha256 for every code file under the protected paths."""
    root = Path(repo_root)
    out: dict[str, str] = {}
    for rel in paths:
        target = root / rel
        files = [target] if target.is_file() else sorted(target.rglob("*")) if target.is_dir() else []
        for f in files:
            if f.is_file() and f.suffix in CODE_SUFFIXES and "__pycache__" not in f.parts:
                out[f.relative_to(root).as_posix()] = sha256_file(f)
    return dict(sorted(out.items()))


def policy_hash_from_cli(repo_root: Path) -> str:
    """Run ``validation.print_policy_hash --mode core_srs --json`` inside the target checkout."""
    proc = subprocess.run(
        [sys.executable, "-m", "validation.print_policy_hash", "--mode", POLICY_MODE, "--json"],
        cwd=Path(repo_root) / "fx_ai_engine", capture_output=True, text=True, timeout=120, env=dict(os.environ),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"print_policy_hash failed ({proc.returncode}): {proc.stderr.strip()[-500:]}")
    return str(json.loads(proc.stdout.strip().splitlines()[-1])["policy_hash"])


def git_commit(repo_root: Path) -> str | None:
    try:
        proc = subprocess.run(["git", "-C", str(repo_root), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None


def _file_entry(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"path": None, "sha256": None}
    return {"path": str(path), "sha256": sha256_file(path) if Path(path).is_file() else None}


def snapshot(repo_root: Path, *, ea_build: Path | None, policy_hash_fn: PolicyHashFn = policy_hash_from_cli,
             git_fn: GitFn = git_commit) -> dict[str, Any]:
    """Current state of every frozen item. Missing items are None, never guessed."""
    root = Path(repo_root)
    try:
        policy_hash, policy_error = policy_hash_fn(root), None
    except Exception as exc:  # recorded as a reason code by the caller
        policy_hash, policy_error = None, str(exc)
    config = root / CONFIG_PATH
    ea_src = root / EA_SOURCE_PATH
    pin = None
    if ea_src.is_file():
        match = EA_PIN_RE.search(ea_src.read_text(encoding="utf-8", errors="replace"))
        pin = match.group(1) if match else None
    return {
        "policy": {"mode": POLICY_MODE, "policy_hash": policy_hash, "error": policy_error,
                   "source": f"validation.print_policy_hash --mode {POLICY_MODE}"},
        "config": {"path": CONFIG_PATH, "sha256": sha256_file(config) if config.is_file() else None},
        "ea_source": {"path": EA_SOURCE_PATH, "sha256": sha256_file(ea_src) if ea_src.is_file() else None,
                      "pinned_policy_hash": pin},
        "ea_build": _file_entry(ea_build),
        "protected_files": protected_file_hashes(root),
        "git": {"commit": git_fn(root)},
    }


def create_manifest(repo_root: Path, *, ea_build: Path | None, label: str = "", now: datetime | None = None,
                    policy_hash_fn: PolicyHashFn = policy_hash_from_cli, git_fn: GitFn = git_commit) -> dict[str, Any]:
    snap = snapshot(repo_root, ea_build=ea_build, policy_hash_fn=policy_hash_fn, git_fn=git_fn)
    if snap["policy"]["policy_hash"] is None:
        raise RuntimeError(f"cannot freeze without a policy hash: {snap['policy']['error']}")
    if snap["config"]["sha256"] is None or snap["ea_source"]["sha256"] is None:
        raise RuntimeError("cannot freeze: config_microcapital.py or the EA source is missing")
    snap["policy"].pop("error")
    body = {
        "schema": SCHEMA,
        "created_at_utc": (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
        "label": label,
        "repo_root_at_freeze": str(Path(repo_root).resolve()),
        **snap,
    }
    return {**body, "manifest_sha256": canonical_digest(body)}


def manifest_warnings(manifest: Mapping[str, Any]) -> list[str]:
    out = []
    if manifest["ea_build"]["sha256"] is None:
        out.append("EA_BUILD_NOT_RECORDED: no compiled EA hash; the build cannot be checked later")
    if manifest["git"]["commit"] is None:
        out.append("GIT_COMMIT_NOT_RECORDED: target is not a git checkout")
    pin = manifest["ea_source"]["pinned_policy_hash"]
    if pin != manifest["policy"]["policy_hash"]:
        out.append(f"EA_PIN_POLICY_MISMATCH: EA pins {pin}, policy is {manifest['policy']['policy_hash']}")
    return out


def _mismatch(code: str, item: str, expected: Any, actual: Any) -> dict[str, Any]:
    return {"code": code, "item": item, "expected": expected, "actual": actual}


def compare(manifest: Mapping[str, Any], current: Mapping[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    exp, act = manifest["policy"]["policy_hash"], current["policy"]["policy_hash"]
    if act is None:
        out.append(_mismatch("POLICY_HASH_UNAVAILABLE", "policy_hash", exp, current["policy"].get("error")))
    elif act != exp:
        out.append(_mismatch("POLICY_HASH_CHANGED", "policy_hash", exp, act))
    for key, changed, missing in (("config", "CONFIG_HASH_CHANGED", "CONFIG_MISSING"),
                                  ("ea_source", "EA_SOURCE_HASH_CHANGED", "EA_SOURCE_MISSING")):
        exp, act = manifest[key]["sha256"], current[key]["sha256"]
        if act is None:
            out.append(_mismatch(missing, manifest[key]["path"], exp, None))
        elif act != exp:
            out.append(_mismatch(changed, manifest[key]["path"], exp, act))
    pin, policy = current["ea_source"]["pinned_policy_hash"], current["policy"]["policy_hash"]
    if current["ea_source"]["sha256"] is not None and policy is not None and pin != policy:
        out.append(_mismatch("EA_PIN_POLICY_MISMATCH", "ea_source.pinned_policy_hash", policy, pin))
    exp = manifest["ea_build"]["sha256"]
    if exp is not None:
        act = current["ea_build"]["sha256"]
        if act is None:
            out.append(_mismatch("EA_BUILD_MISSING", str(current["ea_build"]["path"]), exp, None))
        elif act != exp:
            out.append(_mismatch("EA_BUILD_HASH_CHANGED", str(current["ea_build"]["path"]), exp, act))
    frozen, now = manifest["protected_files"], current["protected_files"]
    for path in sorted(set(frozen) | set(now)):
        if path not in now:
            out.append(_mismatch("PROTECTED_FILE_REMOVED", path, frozen[path], None))
        elif path not in frozen:
            out.append(_mismatch("PROTECTED_FILE_ADDED", path, None, now[path]))
        elif frozen[path] != now[path]:
            out.append(_mismatch("PROTECTED_FILE_CHANGED", path, frozen[path], now[path]))
    exp = manifest["git"]["commit"]
    if exp is not None:
        act = current["git"]["commit"]
        if act is None:
            out.append(_mismatch("GIT_COMMIT_UNAVAILABLE", "git.commit", exp, None))
        elif act != exp:
            out.append(_mismatch("GIT_COMMIT_CHANGED", "git.commit", exp, act))
    return out


def check_manifest(manifest: Mapping[str, Any], repo_root: Path, *, ea_build: Path | None = None,
                   now: datetime | None = None, policy_hash_fn: PolicyHashFn = policy_hash_from_cli,
                   git_fn: GitFn = git_commit) -> dict[str, Any]:
    """MATCH, MISMATCH (one entry per reason code and item) or INVALID_MANIFEST."""
    checked_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    base = {"tool": "r10_freeze_check", "checked_at_utc": checked_at, "repo_root": str(Path(repo_root).resolve()),
            "manifest_sha256": manifest.get("manifest_sha256")}
    if manifest.get("schema") != SCHEMA:
        return {**base, "verdict": "INVALID_MANIFEST",
                "mismatches": [_mismatch("MANIFEST_SCHEMA_UNSUPPORTED", "schema", SCHEMA, manifest.get("schema"))]}
    body = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    if canonical_digest(body) != manifest.get("manifest_sha256"):
        return {**base, "verdict": "INVALID_MANIFEST",
                "mismatches": [_mismatch("MANIFEST_DIGEST_MISMATCH", "manifest_sha256",
                                         manifest.get("manifest_sha256"), canonical_digest(body))]}
    build = ea_build if ea_build is not None else (Path(manifest["ea_build"]["path"]) if manifest["ea_build"]["path"] else None)
    current = snapshot(repo_root, ea_build=build, policy_hash_fn=policy_hash_fn, git_fn=git_fn)
    mismatches = compare(manifest, current)
    return {**base, "verdict": "MISMATCH" if mismatches else "MATCH", "frozen_at_utc": manifest["created_at_utc"],
            "label": manifest.get("label", ""), "policy_hash": current["policy"]["policy_hash"],
            "git_commit": current["git"]["commit"], "protected_files_checked": len(current["protected_files"]),
            "mismatches": mismatches,
            "notes": [] if manifest["ea_build"]["sha256"] is not None else ["EA_BUILD_NOT_RECORDED: build not checked"]}


def render_check(report: Mapping[str, Any]) -> str:
    lines = [f"Freeze check: {report['verdict']}  (manifest {str(report.get('manifest_sha256'))[:16]}, "
             f"checked {report['checked_at_utc']})", f"Repo: {report['repo_root']}"]
    if "policy_hash" in report:
        lines.append(f"Policy hash now: {report['policy_hash']}; git commit now: {report['git_commit']}; "
                     f"protected files: {report['protected_files_checked']}")
    for m in report["mismatches"]:
        lines.append(f"  {m['code']:<26} {m['item']}  expected={m['expected']} actual={m['actual']}")
    for note in report.get("notes", []):
        lines.append(f"  note: {note}")
    return "\n".join(lines)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="R10 freeze manifest: create at freeze time, check during the forward test")
    sub = parser.add_subparsers(dest="cmd", required=True)
    create = sub.add_parser("create", help="record the frozen configuration")
    check = sub.add_parser("check", help="compare the current configuration with a manifest")
    for p in (create, check):
        p.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT, help="checkout to freeze/check (default: this one)")
    build = create.add_mutually_exclusive_group()
    build.add_argument("--ea-build", type=Path, default=None,
                       help=f"compiled FX_Execution.ex5 to hash (default: {DEFAULT_EA_BUILD} if present)")
    build.add_argument("--no-ea-build", action="store_true", help="do not hash a compiled EA")
    create.add_argument("--out", type=Path, required=True, help="manifest JSON to write")
    create.add_argument("--label", default="")
    check.add_argument("--manifest", type=Path, required=True)
    check.add_argument("--ea-build", type=Path, default=None, help="compiled EA to hash (default: the manifest's path)")
    check.add_argument("--report", type=Path, default=None, help="optional JSON report to write")
    args = parser.parse_args(argv)
    repo = args.repo_root.resolve()
    if args.cmd == "create":
        build = None if args.no_ea_build else (args.ea_build or (DEFAULT_EA_BUILD if DEFAULT_EA_BUILD.is_file() else None))
        manifest = create_manifest(repo, ea_build=build, label=args.label)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        print(f"Freeze manifest written: {args.out}")
        print(f"  manifest_sha256 {manifest['manifest_sha256']}")
        print(f"  policy_hash     {manifest['policy']['policy_hash']}")
        print(f"  config sha256   {manifest['config']['sha256']}")
        print(f"  EA source       {manifest['ea_source']['sha256']} (pin {manifest['ea_source']['pinned_policy_hash']})")
        print(f"  EA build        {manifest['ea_build']['sha256']} ({manifest['ea_build']['path']})")
        print(f"  protected files {len(manifest['protected_files'])}; git commit {manifest['git']['commit']}")
        for w in manifest_warnings(manifest):
            print(f"  warning: {w}")
        return 0
    report = check_manifest(_load_json(args.manifest), repo, ea_build=args.ea_build)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(render_check(report))
    return 0 if report["verdict"] == "MATCH" else 1


if __name__ == "__main__":
    raise SystemExit(main())
