"""R10 freeze manifest: create/check round trip and one reason code per kind of drift."""
from __future__ import annotations

import json

import pytest

from config_microcapital import get_policy_config
from core.policy.envelope import canonical_policy_hash
from research.forward_test import freeze

POLICY_HASH = "a" * 64
COMMIT = "1" * 40


def _tree(root):
    files = {
        "SRS_v1.md": "# SRS v1\n",
        "fx_ai_engine/config_microcapital.py": "BASE_RISK_PCT = 0.032\n",
        "fx_ai_engine/mt5_ea/FX_Execution.mq5": f'input string ExpectedPolicyHash = "{POLICY_HASH}";\n',
        "fx_ai_engine/core/risk/hard_risk_engine.py": "LIMIT = 1\n",
        "fx_ai_engine/bridge/signal_router.py": "ROUTE = 1\n",
        "fx_ai_engine/bridge/pending_signals/sig.json": "{}",  # runtime file, not frozen
        "fx_ai_engine/core/agents/technical.py": "X = 1\n",  # outside the protected paths
        "build/FX_Execution.ex5": "binary-v1",
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return root


def _fns(policy=POLICY_HASH, commit=COMMIT):
    return {"policy_hash_fn": lambda root: policy, "git_fn": lambda root: commit}


@pytest.fixture()
def repo(tmp_path):
    return _tree(tmp_path / "repo")


@pytest.fixture()
def manifest(repo):
    return freeze.create_manifest(repo, ea_build=repo / "build/FX_Execution.ex5", label="t", **_fns())


def _codes(report):
    return [(m["code"], m["item"]) for m in report["mismatches"]]


def test_round_trip_matches_and_records_every_item(repo, manifest):
    assert manifest["schema"] == freeze.SCHEMA
    assert manifest["policy"]["policy_hash"] == POLICY_HASH
    assert manifest["config"]["sha256"] == freeze.sha256_file(repo / freeze.CONFIG_PATH)
    assert manifest["ea_source"]["pinned_policy_hash"] == POLICY_HASH
    assert manifest["ea_build"]["sha256"] == freeze.sha256_file(repo / "build/FX_Execution.ex5")
    assert manifest["git"]["commit"] == COMMIT
    assert sorted(manifest["protected_files"]) == [
        "SRS_v1.md", "fx_ai_engine/bridge/signal_router.py", "fx_ai_engine/config_microcapital.py",
        "fx_ai_engine/core/risk/hard_risk_engine.py", "fx_ai_engine/mt5_ea/FX_Execution.mq5"]
    assert freeze.manifest_warnings(manifest) == []
    report = freeze.check_manifest(json.loads(json.dumps(manifest)), repo, **_fns())
    assert report["verdict"] == "MATCH" and report["mismatches"] == []


def test_changed_config_hash_is_flagged(repo, manifest):
    (repo / freeze.CONFIG_PATH).write_text("BASE_RISK_PCT = 0.015\n", encoding="utf-8")
    report = freeze.check_manifest(manifest, repo, **_fns())
    assert report["verdict"] == "MISMATCH"
    assert _codes(report) == [("CONFIG_HASH_CHANGED", freeze.CONFIG_PATH),
                              ("PROTECTED_FILE_CHANGED", freeze.CONFIG_PATH)]


def test_policy_hash_change_and_stale_ea_pin(repo, manifest):
    report = freeze.check_manifest(manifest, repo, **_fns(policy="b" * 64))
    assert [c for c, _ in _codes(report)] == ["POLICY_HASH_CHANGED", "EA_PIN_POLICY_MISMATCH"]


def test_policy_hash_unavailable_fails_closed(repo, manifest):
    def boom(root):
        raise RuntimeError("no python")
    report = freeze.check_manifest(manifest, repo, policy_hash_fn=boom, git_fn=lambda r: COMMIT)
    assert [c for c, _ in _codes(report)] == ["POLICY_HASH_UNAVAILABLE"]


def test_ea_source_and_build_drift(repo, manifest):
    (repo / "build/FX_Execution.ex5").write_text("binary-v2", encoding="utf-8")
    assert [c for c, _ in _codes(freeze.check_manifest(manifest, repo, **_fns()))] == ["EA_BUILD_HASH_CHANGED"]
    (repo / "build/FX_Execution.ex5").unlink()
    assert [c for c, _ in _codes(freeze.check_manifest(manifest, repo, **_fns()))] == ["EA_BUILD_MISSING"]
    ea = repo / freeze.EA_SOURCE_PATH
    ea.write_text(ea.read_text(encoding="utf-8") + "// edit\n", encoding="utf-8")
    codes = [c for c, _ in _codes(freeze.check_manifest(manifest, repo, **_fns()))]
    assert codes == ["EA_SOURCE_HASH_CHANGED", "EA_BUILD_MISSING", "PROTECTED_FILE_CHANGED"]


def test_protected_file_added_removed_and_commit_change(repo, manifest):
    (repo / "fx_ai_engine/core/risk/new_rule.py").write_text("X = 2\n", encoding="utf-8")
    (repo / "fx_ai_engine/bridge/signal_router.py").unlink()
    (repo / "fx_ai_engine/bridge/pending_signals/other.json").write_text("{}", encoding="utf-8")  # ignored
    (repo / "fx_ai_engine/core/agents/technical.py").write_text("X = 3\n", encoding="utf-8")  # not protected
    report = freeze.check_manifest(manifest, repo, **_fns(commit="2" * 40))
    assert _codes(report) == [("PROTECTED_FILE_REMOVED", "fx_ai_engine/bridge/signal_router.py"),
                              ("PROTECTED_FILE_ADDED", "fx_ai_engine/core/risk/new_rule.py"),
                              ("GIT_COMMIT_CHANGED", "git.commit")]


def test_tampered_or_foreign_manifest_is_invalid(repo, manifest):
    tampered = {**manifest, "policy": {**manifest["policy"], "policy_hash": "c" * 64}}
    report = freeze.check_manifest(tampered, repo, **_fns(policy="c" * 64))
    assert report["verdict"] == "INVALID_MANIFEST"
    assert _codes(report) == [("MANIFEST_DIGEST_MISMATCH", "manifest_sha256")]
    assert freeze.check_manifest({"schema": "other"}, repo, **_fns())["verdict"] == "INVALID_MANIFEST"


def test_create_refuses_without_policy_hash(repo):
    def boom(root):
        raise RuntimeError("print_policy_hash failed")
    with pytest.raises(RuntimeError, match="policy hash"):
        freeze.create_manifest(repo, ea_build=None, policy_hash_fn=boom, git_fn=lambda r: None)


def test_missing_build_and_commit_are_warned_not_hidden(repo):
    m = freeze.create_manifest(repo, ea_build=None, **_fns(commit=None))
    assert [w.split(":")[0] for w in freeze.manifest_warnings(m)] == ["EA_BUILD_NOT_RECORDED", "GIT_COMMIT_NOT_RECORDED"]
    assert freeze.check_manifest(m, repo, **_fns(commit=None))["verdict"] == "MATCH"


def test_policy_hash_from_cli_matches_print_policy_hash_on_this_checkout():
    expected = canonical_policy_hash(get_policy_config(mode_id="core_srs"))
    assert freeze.policy_hash_from_cli(freeze.DEFAULT_REPO_ROOT) == expected
