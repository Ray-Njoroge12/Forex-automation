from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from core.market_quote_snapshot import QuoteSnapshotError, load_demo_quotes

NOW = datetime(2026, 11, 3, 10, tzinfo=timezone.utc)


def snapshot(**overrides):
    base = {'schema_version': 1, 'timestamp': NOW.isoformat(), 'account_is_demo': True,
            'account_authenticated': True, 'terminal_connected': True,
            'quotes': [{'symbol': 'EURUSD', 'bid': 1.1, 'ask': 1.1002, 'tick_age_seconds': 2}]}
    return base | overrides


def load(tmp_path, data, **kwargs):
    path = tmp_path / 'quotes.json'
    path.write_text(json.dumps(data))
    return load_demo_quotes(path, now=NOW, **kwargs)


def test_fresh_demo_quote_and_spread(tmp_path):
    result = load(tmp_path, snapshot(timestamp=(NOW - timedelta(seconds=3)).isoformat()))
    assert result['EURUSD'].spread == pytest.approx(0.0002)
    assert result['EURUSD'].age_seconds == 5


@pytest.mark.parametrize('data', [snapshot(account_is_demo=False), snapshot(account_authenticated=False),
    snapshot(terminal_connected=False), snapshot(schema_version=True), snapshot(timestamp='2026-11-03T10:01:00Z'),
    snapshot(timestamp='2026-11-03T09:59:00Z'), snapshot(timestamp='2026-11-03T10:00:00'),
    snapshot(quotes=[{'symbol': 'EURUSD', 'bid': 1.1, 'ask': 1.0, 'tick_age_seconds': 0}]),
    snapshot(quotes=[{'symbol': 'EURUSD', 'bid': float('nan'), 'ask': 1.2, 'tick_age_seconds': 0}]),
    snapshot(quotes=[{'symbol': 'EURUSD', 'bid': 1.1, 'ask': 1.2, 'tick_age_seconds': -1}]),
    snapshot(quotes=[{'symbol': 'EURUSD', 'bid': 1.1, 'ask': 1.2, 'tick_age_seconds': 0}] * 2)])
def test_invalid_snapshots_fail_closed(tmp_path, data):
    with pytest.raises(QuoteSnapshotError):
        load(tmp_path, data)


def test_stale_symbol_is_not_returned(tmp_path):
    data = snapshot(quotes=[{'symbol': 'EURUSD', 'bid': 1.1, 'ask': 1.2, 'tick_age_seconds': 40}])
    assert load(tmp_path, data) == {}


def test_missing_or_partial_file_fails_closed(tmp_path):
    path = tmp_path / 'quotes.json'
    with pytest.raises(QuoteSnapshotError):
        load_demo_quotes(path, now=NOW)
    path.write_text('{')
    with pytest.raises(QuoteSnapshotError):
        load_demo_quotes(path, now=NOW)
