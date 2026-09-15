"""Print the exact policy hash that must be pinned in the MT5 EA inputs."""
from __future__ import annotations

import argparse
import json

from config_microcapital import MODE_CONFIGS, get_policy_config
from core.policy.envelope import canonical_policy_hash


def build_policy_pin(mode_id: str) -> dict[str, str]:
    policy = get_policy_config(mode_id=mode_id)
    return {
        "mode_id": str(policy["MODE_ID"]),
        "policy_hash": canonical_policy_hash(policy),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the FX policy hash for MT5 EA deployment pinning")
    parser.add_argument("--mode", choices=sorted(MODE_CONFIGS), default="core_srs")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = build_policy_pin(args.mode)
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(result["policy_hash"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
