import logging
import os
import sqlite3
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


DIRECTORIES = [
    "core/agents",
    "core/risk",
    "core/indicators",
    "bridge/pending_signals",
    "bridge/active_locks",
    "bridge/feedback",
    "mt5_ea",
    "database",
    "tests/fixtures",
]


def create_directory_structure(base_path: Path) -> None:
    """Creates the folder hierarchy for the FX AI Engine."""
    for directory in DIRECTORIES:
        dir_path = base_path / directory
        dir_path.mkdir(parents=True, exist_ok=True)
        logging.info("Verified directory: %s", dir_path)


def initialize_database(db_path: Path) -> None:
    """Initializes SQLite database with baseline schema for phase 1."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_ticket INTEGER UNIQUE,
            symbol TEXT NOT NULL,
            order_type TEXT NOT NULL,
            lot_size REAL NOT NULL,
            entry_price REAL,
            stop_loss REAL NOT NULL,
            take_profit REAL NOT NULL,
            market_regime TEXT,
            status TEXT DEFAULT 'PENDING',
            profit_loss REAL DEFAULT 0.0,
            r_multiple REAL DEFAULT 0.0,
            open_time DATETIME DEFAULT CURRENT_TIMESTAMP,
            close_time DATETIME
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS account_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            balance REAL NOT NULL,
            equity REAL NOT NULL,
            daily_loss_percent REAL DEFAULT 0.0,
            weekly_loss_percent REAL DEFAULT 0.0,
            consecutive_losses INTEGER DEFAULT 0,
            is_trading_halted BOOLEAN DEFAULT 0
        )
        """
    )

    conn.commit()
    conn.close()
    logging.info("Database initialized successfully at: %s", db_path)


if __name__ == "__main__":
    base_dir = Path(os.getcwd())
    logging.info("Starting Phase 1: Infrastructure Initialization...")

    create_directory_structure(base_dir)
    initialize_database(base_dir / "database" / "trading_state.db")

    logging.info("Phase 1 Complete. The environment is ready.")
