"""
transaction_store.py — In-memory transaction cache with async-safe deduplication.

Tracks every seen transaction and whether the bot has already sent DMs for:
  - Event 1: new transaction detected
  - Event 2: transaction reached ≥ 2 confirmations
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("crypto-notifier.store")


@dataclass
class TxRecord:
    txid: str
    chain: str
    address: str
    amount: str
    usd_value: Optional[str]
    block_height: Optional[int]
    timestamp: Optional[str]
    asset: str = ""            # token/coin symbol from Tatum payload (e.g. 'USDT', 'SOL', 'LTC')
    confirmations: int = 0

    # Notification flags — prevent duplicate DMs
    notified_new: bool = False
    notified_confirmed: bool = False


class TransactionStore:
    """Thread-safe (asyncio.Lock) in-memory store for transaction records."""

    def __init__(self) -> None:
        self._store: dict[str, TxRecord] = {}
        self._lock = asyncio.Lock()

    async def upsert(self, data: dict) -> TxRecord:
        """
        Insert a new record or update an existing one.
        Returns the updated TxRecord.
        """
        txid: str = data["txid"]
        async with self._lock:
            if txid in self._store:
                record = self._store[txid]
                # Update mutable fields
                record.confirmations = data.get("confirmations", record.confirmations)
                record.block_height = data.get("block_height", record.block_height)
                record.usd_value = data.get("usd_value", record.usd_value)
                log.debug("Updated tx %s: confirmations=%d", txid, record.confirmations)
            else:
                record = TxRecord(
                    txid=txid,
                    chain=data["chain"],
                    address=data["address"],
                    amount=data.get("amount", "0"),
                    usd_value=data.get("usd_value"),
                    block_height=data.get("block_height"),
                    timestamp=data.get("timestamp"),
                    asset=data.get("asset", ""),
                    confirmations=data.get("confirmations", 0),
                )
                self._store[txid] = record
                log.info("New tx stored: %s on %s (asset=%s)", txid, data["chain"], data.get("asset", "?"))
            return record

    async def get(self, txid: str) -> Optional[TxRecord]:
        async with self._lock:
            return self._store.get(txid)

    async def mark_notified_new(self, txid: str) -> None:
        async with self._lock:
            if txid in self._store:
                self._store[txid].notified_new = True

    async def mark_notified_confirmed(self, txid: str) -> None:
        async with self._lock:
            if txid in self._store:
                self._store[txid].notified_confirmed = True

    async def all_records(self) -> list[TxRecord]:
        async with self._lock:
            return list(self._store.values())

    async def size(self) -> int:
        async with self._lock:
            return len(self._store)


# Shared singleton used by both webhook_server and bot
store = TransactionStore()
