import json
import logging
import os
import urllib.parse
import urllib.request
from datetime import datetime

logger = logging.getLogger("alerts")

def send_telegram_message(text: str) -> bool:
    """Sends a message via Telegram Bot API using standard library."""
    enabled = os.getenv("TELEGRAM_ENABLED", "0") == "1"
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not enabled or not token or not chat_id:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as response:
            if response.status == 200:
                return True
            else:
                logger.error("Telegram API returned status: %d", response.status)
    except Exception as e:
        logger.error("Failed to send Telegram message: %s", str(e))
    
    return False

def alert_risk_halt(rule_name: str, reason: str, severity: str = "BLOCK"):
    """Specific alert for risk-based trading halts."""
    icon = "🛑" if severity == "BLOCK" else "⚠️"
    msg = (
        f"<b>{icon} RISK EVENT: {rule_name}</b>\n"
        f"<b>Severity:</b> {severity}\n"
        f"<b>Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"<b>Reason:</b> {reason}"
    )
    send_telegram_message(msg)

def alert_trade_execution(trade_id: str, symbol: str, direction: str, lot_size: float, price: float):
    """Specific alert for trade execution."""
    icon = "🚀" if direction.upper() == "BUY" else "🔻"
    msg = (
        f"<b>{icon} TRADE EXECUTED</b>\n"
        f"<b>Symbol:</b> {symbol}\n"
        f"<b>Action:</b> {direction}\n"
        f"<b>Lots:</b> {lot_size}\n"
        f"<b>Price:</b> {price}\n"
        f"<b>ID:</b> <code>{trade_id}</code>"
    )
    send_telegram_message(msg)

def alert_trade_exit(trade_id: str, symbol: str, pnl: float, r_multiple: float | None = None):
    """Specific alert for trade exit."""
    icon = "💰" if pnl >= 0 else "📉"
    r_text = f"\n<b>R-Multiple:</b> {r_multiple:.2f}R" if r_multiple is not None else ""
    msg = (
        f"<b>{icon} TRADE CLOSED</b>\n"
        f"<b>Symbol:</b> {symbol}\n"
        f"<b>PnL:</b> ${pnl:.2f}{r_text}\n"
        f"<b>ID:</b> <code>{trade_id}</code>"
    )
    send_telegram_message(msg)
