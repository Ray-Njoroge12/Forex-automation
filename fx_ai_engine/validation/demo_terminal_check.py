"""Read an EA snapshot to check demo-terminal connectivity without sending orders."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def inspect_snapshot(bridge_dir: Path, *, now: datetime | None = None, max_age_seconds: float = 30.0) -> dict[str, Any]:
    result: dict[str, Any] = {"approved": False, "snapshot_path": str(bridge_dir / 'feedback' / 'account_snapshot.json')}
    try:
        data = json.loads((bridge_dir / 'feedback' / 'account_snapshot.json').read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            raise ValueError('snapshot is not an object')
        stamp = datetime.fromisoformat(str(data['timestamp']).replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            raise ValueError('snapshot timestamp has no timezone')
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError('current time has no timezone')
        age = (current - stamp).total_seconds()
        if age < -5 or age > max_age_seconds:
            raise ValueError('snapshot timestamp is stale or in the future')
        if data.get('snapshot_source') == 'mock_feedback_simulator':
            raise ValueError('snapshot is from the mock simulator')
        if data.get('account_is_demo') is not True:
            raise ValueError('snapshot does not confirm a demo account')
        if data.get('account_authenticated') is not True:
            raise ValueError('snapshot does not confirm an authenticated account')
        if data.get('terminal_connected') is not True:
            raise ValueError('snapshot does not confirm terminal connectivity')
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        result['reason'] = str(exc)
        return result
    result.update(approved=True, reason='fresh EA snapshot reports connected demo account',
                  snapshot_age_seconds=round(age, 3), broker_identity_verified=False)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bridge-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    report = inspect_snapshot(args.bridge_dir)
    print(json.dumps(report, indent=2))
    return 0 if report['approved'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
