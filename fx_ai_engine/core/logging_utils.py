import logging


def configure_logging() -> None:
    """Sets a consistent logging format across the project."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    )
