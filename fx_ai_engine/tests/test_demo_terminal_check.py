from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from validation.demo_terminal_check import inspect_snapshot


@pytest.mark.parametrize('overrides', [
    {'account_is_demo': False}, {'terminal_connected': False}, {'account_is_demo': 'true'}, {'account_authenticated': False},
    {'snapshot_source': 'mock_feedback_simulator'}, {'timestamp': 'invalid'},
    {'timestamp': '2026-11-03T10:00:31+00:00'}, {'timestamp': '2026-11-03T10:00:00'},
])
def test_demo_check_fails_closed(tmp_path, overrides):
    folder = tmp_path / 'feedback'
    folder.mkdir()
    snapshot = {'timestamp': '2026-11-03T10:00:00+00:00', 'account_is_demo': True, 'account_authenticated': True, 'terminal_connected': True}
    (folder / 'account_snapshot.json').write_text(json.dumps(snapshot | overrides))
    now = datetime(2026, 11, 3, 10, 0, 0, tzinfo=timezone.utc)
    assert not inspect_snapshot(tmp_path, now=now)['approved']


def test_fresh_snapshot_passes_and_stale_or_missing_fails(tmp_path):
    now = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)
    assert not inspect_snapshot(tmp_path, now=now)['approved']
    folder = tmp_path / 'feedback'
    folder.mkdir()
    path = folder / 'account_snapshot.json'
    path.write_text(json.dumps({'timestamp': now.isoformat(), 'account_is_demo': True, 'account_authenticated': True, 'terminal_connected': True}))
    assert inspect_snapshot(tmp_path, now=now)['approved']
    assert not inspect_snapshot(tmp_path, now=now + timedelta(seconds=31))['approved']
