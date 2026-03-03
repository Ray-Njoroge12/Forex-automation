import os
from dataclasses import dataclass


@dataclass(frozen=True)
class MT5Credentials:
    login: int
    password: str
    server: str


class CredentialsError(ValueError):
    """Raised when required MT5 credentials are missing or invalid."""


def load_mt5_credentials_from_env() -> MT5Credentials:
    """Loads MT5 credentials from environment variables."""
    raw_login = os.getenv("MT5_LOGIN", "").strip()
    password = os.getenv("MT5_PASSWORD", "").strip()
    server = os.getenv("MT5_SERVER", "").strip()

    missing = []
    if not raw_login:
        missing.append("MT5_LOGIN")
    if not password:
        missing.append("MT5_PASSWORD")
    if not server:
        missing.append("MT5_SERVER")

    if missing:
        raise CredentialsError(
            f"Missing required MT5 environment variables: {', '.join(missing)}"
        )

    try:
        login = int(raw_login)
    except ValueError as exc:
        raise CredentialsError("MT5_LOGIN must be an integer account number") from exc

    return MT5Credentials(login=login, password=password, server=server)
