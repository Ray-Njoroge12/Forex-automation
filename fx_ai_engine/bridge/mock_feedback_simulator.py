from __future__ import annotations

import json
import os
import zlib
from datetime import datetime, timezone
from pathlib import Path

from core.account_status import AccountStatus
from core.bridge_utils import get_mock_runtime_state_path

DEFAULT_START_BALANCE = 10000.0
DEFAULT_OUTCOME_PATTERN = ("WIN", "LOSS", "WIN", "BREAKEVEN", "SMALL_WIN", "LOSS")
_NAMED_R_MULTIPLES = {
    "WIN": None,
    "LOSS": -1.0,
    "BREAKEVEN": 0.0,
    "SMALL_WIN": 1.2,
    "SMALL_LOSS": -0.5,
    "BIG_WIN": 3.0,
}


class MockFeedbackSimulator:
    def __init__(
        self,
        pending_dir: str | Path,
        feedback_dir: str | Path,
        exits_dir: str | Path,
    ) -> None:
        self.pending_dir = Path(pending_dir)
        self.feedback_dir = Path(feedback_dir)
        self.exits_dir = Path(exits_dir)
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.feedback_dir.mkdir(parents=True, exist_ok=True)
        self.exits_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_path = self.feedback_dir / "account_snapshot.json"
        self.state_path = get_mock_runtime_state_path(self.pending_dir.parent)

    def _ticket_pair(self, trade_id: str) -> tuple[int, int]:
        seed = zlib.crc32(trade_id.encode("utf-8")) & 0x7FFFFFFF
        ticket = 700000 + (seed % 1000000)
        return ticket, ticket + 1000000

    def _write_json(self, path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=True), encoding="utf-8")

    def clear_account_snapshot(self) -> None:
        if not self.snapshot_path.exists():
            return
        try:
            payload = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if payload.get("snapshot_source") == "mock_feedback_simulator":
            self.snapshot_path.unlink(missing_ok=True)

    def clear_runtime_state(self) -> None:
        self.state_path.unlink(missing_ok=True)

    def _default_state(self) -> dict[str, float | int]:
        try:
            start_balance = float(os.getenv("MT5_MOCK_START_BALANCE", str(DEFAULT_START_BALANCE)))
        except ValueError:
            start_balance = DEFAULT_START_BALANCE
        if start_balance <= 0:
            start_balance = DEFAULT_START_BALANCE
        rounded = round(start_balance, 2)
        return {
            "balance": rounded,
            "equity": rounded,
            "outcome_index": 0,
            "consecutive_losses": 0,
        }

    def _load_state(self) -> dict[str, float | int]:
        state = self._default_state()
        if not self.state_path.exists():
            return state
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return state
        try:
            balance = round(max(float(payload.get("balance", state["balance"])), 0.01), 2)
            equity = round(max(float(payload.get("equity", balance)), 0.01), 2)
            outcome_index = max(int(payload.get("outcome_index", 0)), 0)
            consecutive_losses = max(int(payload.get("consecutive_losses", 0)), 0)
        except (TypeError, ValueError):
            return state
        return {
            "balance": balance,
            "equity": equity,
            "outcome_index": outcome_index,
            "consecutive_losses": consecutive_losses,
        }

    def _save_state(self, state: dict[str, float | int]) -> None:
        self._write_json(self.state_path, state)

    def _outcome_pattern(self) -> list[str]:
        raw = os.getenv("MT5_MOCK_OUTCOME_PATTERN", "").strip()
        if not raw:
            return list(DEFAULT_OUTCOME_PATTERN)
        tokens = [token.strip().upper() for token in raw.split(",") if token.strip()]
        return tokens or list(DEFAULT_OUTCOME_PATTERN)

    def _resolve_outcome(self, token: str, target_rr: float) -> tuple[str, float]:
        mapped = _NAMED_R_MULTIPLES.get(token)
        if mapped is None and token not in _NAMED_R_MULTIPLES:
            try:
                r_multiple = float(token)
            except ValueError:
                r_multiple = target_rr
        elif mapped is None:
            r_multiple = target_rr
        else:
            r_multiple = mapped
        if r_multiple > 0:
            return "CLOSED_WIN", round(r_multiple, 4)
        if r_multiple < 0:
            return "CLOSED_LOSS", round(r_multiple, 4)
        return "CLOSED_BREAKEVEN", 0.0

    def _execution_slippage(self, outcome_index: int) -> float:
        return (0.0, 0.00001, -0.00001, 0.00002)[outcome_index % 4]

    def process_pending(self, *, account_status: AccountStatus) -> int:
        state = self._load_state()
        pattern = self._outcome_pattern()
        processed = 0
        for path in sorted(self.pending_dir.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            trade_id = str(payload["trade_id"])
            ticket, position_ticket = self._ticket_pair(trade_id)
            now = datetime.now(timezone.utc).isoformat()
            target_rr = round(
                float(payload.get("take_profit_pips", 0.0)) / max(float(payload.get("stop_pips", 1.0)), 1e-9),
                4,
            )
            outcome_token = pattern[int(state["outcome_index"]) % len(pattern)]
            exit_status, r_multiple = self._resolve_outcome(outcome_token, target_rr)
            base_balance = round(max(float(state["balance"]), 0.01), 2)
            risk_usd = round(max(base_balance * float(payload.get("risk_percent", 0.0)), 0.0), 4)
            profit_loss = round(risk_usd * r_multiple, 2)
            slippage = self._execution_slippage(int(state["outcome_index"]))
            entry_price = round(float(payload.get("limit_price", 1.1001)) + slippage, 6)

            self._write_json(
                self.feedback_dir / f"execution_{trade_id}.json",
                {
                    "feedback_source": "mock_feedback_simulator",
                    "trade_id": trade_id,
                    "ticket": ticket,
                    "position_ticket": position_ticket,
                    "status": "EXECUTED",
                    "entry_price": entry_price,
                    "slippage": slippage,
                    "spread_at_entry": 0.0001 + (int(state["outcome_index"]) % 3) * 0.00002,
                    "profit_loss": 0.0,
                    "r_multiple": 0.0,
                    "close_time": now,
                },
            )
            self._write_json(
                self.exits_dir / f"exit_{trade_id}.json",
                {
                    "feedback_source": "mock_feedback_simulator",
                    "ticket": position_ticket,
                    "position_ticket": position_ticket,
                    "trade_id": trade_id,
                    "profit_loss": profit_loss,
                    "status": exit_status,
                    "r_multiple": r_multiple,
                    "is_final_exit": True,
                    "close_time": now,
                },
            )

            if profit_loss < 0:
                state["consecutive_losses"] = int(state["consecutive_losses"]) + 1
            elif profit_loss > 0:
                state["consecutive_losses"] = 0
            final_balance = round(max(base_balance + profit_loss, 0.01), 2)
            state["balance"] = final_balance
            state["equity"] = final_balance
            state["outcome_index"] = int(state["outcome_index"]) + 1
            self._write_json(
                self.snapshot_path,
                {
                    "timestamp": now,
                    "snapshot_source": "mock_feedback_simulator",
                    "balance": final_balance,
                    "equity": final_balance,
                    "margin_free": final_balance,
                    "open_positions_count": 0,
                    "open_symbols": [],
                    "floating_pnl": 0.0,
                    "consecutive_losses": int(state["consecutive_losses"]),
                },
            )

            path.unlink(missing_ok=True)
            processed += 1
        if processed > 0:
            self._save_state(state)
        return processed