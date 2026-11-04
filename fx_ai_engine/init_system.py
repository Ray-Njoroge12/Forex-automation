import logging
import os
from pathlib import Path

from database import connection as db_connection
from database import db as db_mod

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
    """Initialise the current schema and every idempotent migration."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    previous_path = db_connection.DB_PATH
    db_connection.DB_PATH = db_path
    try:
        db_mod.run_all_migrations()
    finally:
        db_connection.DB_PATH = previous_path
    logging.info("Database initialized successfully at: %s", db_path)


if __name__ == "__main__":
    base_dir = Path(os.getcwd())
    logging.info("Starting Phase 1: Infrastructure Initialization...")

    create_directory_structure(base_dir)
    initialize_database(base_dir / "database" / "trading_state.db")

    logging.info("Phase 1 Complete. The environment is ready.")
