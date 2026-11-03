"""Shared helpers for the research tools: data loading, policy variants, in-process replay."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from backtesting.run_replay import VARIANTS
from backtesting.unified_replay import (
    COST_SCENARIOS, SYMBOLS, Candidate, CostModel, Frames, SimulationConfig, SimulationResult,
    generate_candidates, load_bridge_bar_exports, load_csv_frames, simulate,
)
from config_microcapital import apply_agent_threshold_overrides, get_policy_config
from core.policy.envelope import canonical_policy_hash

PROJECT_ROOT = Path(__file__).parents[1]
ARTIFACTS = PROJECT_ROOT / "artifacts" / "research"
VOLATILE_FIELDS = ("trade_id", "timestamp_utc")


def discover_symbols(bridge_dir: Path | None = None, data_dir: Path | None = None) -> list[str]:
    """Symbols (in SYMBOLS order) that have an M15 file in the given source."""
    if (bridge_dir is None) == (data_dir is None):
        raise ValueError("exactly one of bridge_dir or data_dir is required")
    if bridge_dir is not None:
        return [s for s in SYMBOLS if (Path(bridge_dir) / f"bars_{s}_M15.json").exists()]
    return [s for s in SYMBOLS if (Path(data_dir) / f"{s}_M15.csv").exists()]


def load_frames(*, bridge_dir: Path | None = None, data_dir: Path | None = None,
                symbols: Sequence[str] | None = None) -> Frames:
    symbols = list(symbols) if symbols else discover_symbols(bridge_dir, data_dir)
    if not symbols:
        raise FileNotFoundError("no M15 bar files found for any of the six majors")
    if bridge_dir is not None and data_dir is None:
        return load_bridge_bar_exports(Path(bridge_dir), symbols)
    if data_dir is not None and bridge_dir is None:
        return load_csv_frames(Path(data_dir), symbols=symbols)
    raise ValueError("exactly one of bridge_dir or data_dir is required")


def dataset_hashes(directory: Path) -> dict[str, str]:
    """sha256 (first 16 hex chars) per *.json / *.csv file, as in run_replay's dataset_sha256_16."""
    out = {}
    for path in sorted(Path(directory).iterdir()):
        if path.suffix not in {".json", ".csv"} or not path.is_file():
            continue
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        out[path.name] = h.hexdigest()[:16]
    return out


def policy_for_variant(name: str) -> tuple[dict, str]:
    if name not in VARIANTS:
        raise ValueError(f"unknown variant {name!r}; expected one of {sorted(VARIANTS)}")
    policy = apply_agent_threshold_overrides(get_policy_config(mode_id="core_srs", env={}), VARIANTS[name] or None)
    return policy, canonical_policy_hash(policy)


def run_replay_in_process(frames: Frames, policy: Mapping[str, object], cost: CostModel, *,
                          exit_mode: str = "policy", start: pd.Timestamp | None = None,
                          end: pd.Timestamp | None = None
                          ) -> tuple[list[Candidate], dict[str, Counter], SimulationResult]:
    """Same two stages as run_replay: candidates at base cost, then simulate at ``cost``."""
    candidates: list[Candidate] = []
    funnels: dict[str, Counter] = {}
    for symbol in [s for s in SYMBOLS if s in frames]:
        cands, funnel = generate_candidates(symbol, frames, policy, COST_SCENARIOS["base"], start=start, end=end)
        candidates.extend(cands)
        funnels[symbol] = funnel
    candidates.sort(key=lambda c: (c.decision_time, SYMBOLS.index(c.symbol)))
    result = simulate(candidates, frames, policy, cost, SimulationConfig(exit_mode=exit_mode, start=start, end=end))
    return candidates, funnels, result


def decision_fingerprint(candidate: Candidate | None, funnel: Mapping[str, int]) -> dict[str, Any]:
    """Comparable view of one decision; drops fields filled from the wall clock or uuid4."""
    keys = sorted(k for k, v in funnel.items() if v)
    fp: dict[str, Any] = {"funnel": keys[0] if len(keys) == 1 else keys, "signal": None, "regime": None}
    if candidate is not None:
        fp["signal"] = {k: v for k, v in candidate.signal.items() if k not in VOLATILE_FIELDS}
        fp["regime"] = {k: v for k, v in candidate.regime.items() if k not in VOLATILE_FIELDS}
    return fp


def parse_ts(value: str | None) -> pd.Timestamp | None:
    if value is None:
        return None
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_json(path: Path, obj: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")


def add_source_args(parser) -> None:
    """--bridge-dir | --data-dir, --symbols and --variant, shared by the CLIs."""
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--bridge-dir", type=Path, help="directory of EA bars_SYMBOL_TF.json exports")
    src.add_argument("--data-dir", type=Path, help="directory of SYMBOL_TF.csv history")
    parser.add_argument("--symbols", nargs="+", default=None, choices=SYMBOLS)
    parser.add_argument("--variant", default="baseline", choices=sorted(VARIANTS))


def source_dir(args) -> Path:
    return Path(args.bridge_dir or args.data_dir)
