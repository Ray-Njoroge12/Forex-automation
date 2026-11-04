from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from validation import update_market_context

NOW = datetime(2026, 11, 3, tzinfo=timezone.utc)


def fixture(tmp_path, monkeypatch):
    source = tmp_path / 'bridge' / 'market' / 'calendar.json'
    source.parent.mkdir(parents=True)
    events = [{'datetime_utc': '2026-11-13T12:00:00Z', 'currency': 'USD', 'impact': 'high', 'event': 'release'}]
    source.write_text(json.dumps(events))
    status = {'calendar_complete': True, 'calendar_generated_at': NOW.isoformat(),
              'calendar_high_impact_events': 1, 'terminal_connected': True, 'account_is_demo': True}
    (source.parent / 'status.json').write_text(json.dumps(status))
    monkeypatch.setattr(update_market_context, 'DATA_DIR', tmp_path / 'destination')
    return source, status


@pytest.mark.parametrize('change', [
    {'calendar_complete': False}, {'calendar_complete': None}, {'terminal_connected': False},
    {'account_is_demo': False}, {'calendar_high_impact_events': 2},
    {'calendar_generated_at': '2026-11-02T20:00:00Z'}, {'calendar_generated_at': '2026-11-03T01:00:00Z'},
])
def test_incomplete_or_stale_mt5_calendar_is_not_installed(tmp_path, monkeypatch, change):
    source, status = fixture(tmp_path, monkeypatch)
    (source.parent / 'status.json').write_text(json.dumps(status | change))
    with pytest.raises(ValueError):
        update_market_context.install_calendar(source, now_utc=NOW)
    assert not update_market_context.DATA_DIR.exists()


def test_mt5_calendar_with_complete_fresh_status_installs(tmp_path, monkeypatch):
    source, _ = fixture(tmp_path, monkeypatch)
    destination = update_market_context.install_calendar(source, now_utc=NOW)
    assert json.loads(destination.read_text()) == json.loads(source.read_text())
    assert (destination.parent / 'economic_calendar.manifest.json').exists()


def test_old_ea_status_does_not_certify_export(tmp_path, monkeypatch):
    source, status = fixture(tmp_path, monkeypatch)
    status.pop('calendar_complete')
    (source.parent / 'status.json').write_text(json.dumps(status))
    with pytest.raises(ValueError, match='completeness is unverified'):
        update_market_context.install_calendar(source, now_utc=NOW)
