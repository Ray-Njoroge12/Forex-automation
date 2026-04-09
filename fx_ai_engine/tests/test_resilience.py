from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

import watchdog
from core.alerts import alert_risk_halt, alert_trade_execution, alert_trade_exit, send_telegram_message
from core.health import _health_status, update_health_status


@pytest.fixture(autouse=True)
def reset_health_status() -> None:
    _health_status.update(
        {
            "status": "starting",
            "last_heartbeat": None,
            "start_time": datetime.now().isoformat(),
            "version": "1.2.0-hardened",
            "account_balance": 0.0,
            "is_trading_halted": False,
        }
    )


def _mock_http_response(*, status: int = 200, body: dict | None = None) -> MagicMock:
    response = MagicMock()
    response.status = status
    response.read.return_value = json.dumps(body or {}).encode("utf-8")
    response.__enter__.return_value = response
    return response


def test_send_telegram_message() -> None:
    env = {
        "TELEGRAM_ENABLED": "1",
        "TELEGRAM_TOKEN": "test_token",
        "TELEGRAM_CHAT_ID": "test_chat_id",
    }
    with patch.dict(os.environ, env, clear=False):
        with patch("urllib.request.urlopen", return_value=_mock_http_response(status=200)) as mock_urlopen:
            assert send_telegram_message("Test message") is True
            mock_urlopen.assert_called_once()

            req = mock_urlopen.call_args[0][0]
            assert req.full_url == "https://api.telegram.org/bottest_token/sendMessage"
            assert req.get_header("Content-type") == "application/json"

            payload = json.loads(req.data.decode("utf-8"))
            assert payload["chat_id"] == "test_chat_id"
            assert payload["text"] == "Test message"


def test_alert_wrappers_call_sender() -> None:
    with patch("core.alerts.send_telegram_message") as mock_send:
        alert_risk_halt("Rule1", "Reason1", "BLOCK")
        assert mock_send.called
        assert "BLOCK" in mock_send.call_args[0][0]
        assert "Rule1" in mock_send.call_args[0][0]

        mock_send.reset_mock()
        alert_trade_execution("T1", "EURUSD", "BUY", 0.1, 1.1000)
        assert mock_send.called
        assert "EURUSD" in mock_send.call_args[0][0]
        assert "BUY" in mock_send.call_args[0][0]

        mock_send.reset_mock()
        alert_trade_exit("T1", "EURUSD", 100.0, 2.5)
        assert mock_send.called
        assert "CLOSED" in mock_send.call_args[0][0]
        assert "100.00" in mock_send.call_args[0][0]
        assert "2.50R" in mock_send.call_args[0][0]


def test_health_status_updates_heartbeat() -> None:
    update_health_status(status="running", account_balance=1500.0)

    assert _health_status["status"] == "running"
    assert _health_status["account_balance"] == 1500.0
    assert _health_status["last_heartbeat"] is not None


def test_watchdog_health_staleness_logic() -> None:
    recent_hb = (datetime.now() - timedelta(minutes=2)).isoformat()
    stale_hb = (datetime.now() - timedelta(minutes=11)).isoformat()

    assert watchdog._is_health_stale({"last_heartbeat": recent_hb}) is False
    assert watchdog._is_health_stale({"last_heartbeat": stale_hb}) is True
    assert watchdog._is_health_stale({}) is True


def test_watchdog_get_health() -> None:
    with patch(
        "urllib.request.urlopen",
        return_value=_mock_http_response(status=200, body={"status": "ok"}),
    ):
        assert watchdog._get_health() == {"status": "ok"}

    with patch("urllib.request.urlopen", side_effect=Exception("failed")):
        assert watchdog._get_health() is None
