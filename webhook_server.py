"""
webhook_server.py — FastAPI server that receives Tatum webhook events.

Tatum POSTs an event to WEBHOOK_URL whenever a monitored address has:
  - A new incoming transaction
  - A confirmation count update

This server:
  1. Verifies the HMAC-SHA512 signature (x-payload-hash header)
  2. Normalises the payload across all supported chains
  3. Puts a parsed TxEvent dict into the shared asyncio.Queue
  4. Returns 200 immediately so Tatum won't retry

The asyncio.Queue is consumed by the Discord bot background task.
"""

import hashlib
import hmac
import json
import logging
from typing import Optional

from fastapi import FastAPI, Request, Response, HTTPException

import config
from transaction_store import store

log = logging.getLogger("crypto-notifier.webhook")

# Shared queue injected at startup by bot.py
_event_queue: Optional["asyncio.Queue"] = None  # type: ignore[reportMissingModuleSource]

app = FastAPI(title="Crypto Notifier Webhook", docs_url=None, redoc_url=None)


def set_event_queue(q) -> None:
    """Called by bot.py to inject the shared asyncio.Queue."""
    global _event_queue
    _event_queue = q


# ── Signature Verification ────────────────────────────────────────────────────

def _verify_signature(raw_body: bytes, header_sig: str) -> bool:
    """
    Tatum signs webhook payloads with HMAC-SHA512 using your webhook secret.
    Header: x-payload-hash  (lowercase hex)
    """
    if not config.TATUM_WEBHOOK_SECRET:
        # Secret not configured — skip verification (not recommended for production)
        log.warning("TATUM_WEBHOOK_SECRET not set; skipping signature verification.")
        return True

    expected = hmac.new(
        config.TATUM_WEBHOOK_SECRET.encode(),
        raw_body,
        hashlib.sha512,
    ).hexdigest()

    return hmac.compare_digest(expected, header_sig.lower())


# ── Payload Normalisation ─────────────────────────────────────────────────────

def _normalise_payload(data: dict) -> Optional[dict]:
    """
    Tatum payload fields vary per chain. This function extracts a consistent
    set of fields regardless of chain type.

    Returns None if the event should be ignored (e.g. outgoing tx).
    """
    chain = (data.get("chain") or "").upper()
    if not chain:
        log.debug("Ignoring webhook without chain field: %s", data)
        return None

    # Determine our internal chain key
    # Tatum sends: LTC, BSC, MATIC, SOL
    chain_key = chain  # consistent with our ADDRESSES keys

    address = (
        data.get("address")
        or data.get("to")
        or data.get("toAddress")
        or ""
    ).strip()

    txid = (
        data.get("txId")
        or data.get("txHash")
        or data.get("hash")
        or data.get("transactionHash")
        or ""
    ).strip()

    if not txid:
        log.debug("Ignoring webhook without txid: %s", data)
        return None

    # Amount — Tatum usually sends amount as string
    amount = str(
        data.get("amount")
        or data.get("value")
        or data.get("asset", {}).get("amount", "0")
        if isinstance(data.get("asset"), dict) else data.get("amount", "0")
    )

    usd_value: Optional[str] = None
    if data.get("counterAddress"):
        pass  # Not provided in basic tier
    asset = data.get("asset")
    if isinstance(asset, dict):
        usd_value = asset.get("usdValue")

    confirmations: int = int(data.get("confirmations") or data.get("blockConfirmations") or 0)
    block_height: Optional[int] = data.get("blockNumber") or data.get("blockHeight")
    timestamp: Optional[str] = str(data.get("timestamp") or data.get("blockTimestamp") or "")

    # Filter to only monitored addresses
    monitored = {v.lower(): k for k, v in config.ADDRESSES.items()}
    if address.lower() not in monitored and not any(
        address.lower() == v.lower() for v in config.ADDRESSES.values()
    ):
        log.debug("Ignoring tx for unmonitored address %s", address)
        return None

    # Resolve chain key from ADDRESSES mapping
    for ck, addr in config.ADDRESSES.items():
        if addr.lower() == address.lower():
            chain_key = ck
            break

    return {
        "txid": txid,
        "chain": chain_key,
        "address": address,
        "amount": amount,
        "usd_value": usd_value,
        "block_height": block_height,
        "timestamp": timestamp,
        "confirmations": confirmations,
        "raw": data,
    }


# ── Webhook Endpoint ───────────────────────────────────────────────────────────

@app.post("/webhook")
async def receive_webhook(request: Request) -> Response:
    raw_body = await request.body()

    # ── Signature check ──
    sig_header = request.headers.get("x-payload-hash", "")
    if not _verify_signature(raw_body, sig_header):
        log.warning("Webhook signature mismatch — possible spoofed request.")
        raise HTTPException(status_code=401, detail="Invalid signature")

    # ── Parse body ──
    try:
        data = json.loads(raw_body)
    except json.JSONDecodeError:
        log.error("Received non-JSON webhook body: %s", raw_body[:200])
        raise HTTPException(status_code=400, detail="Invalid JSON")

    log.debug("Webhook received: %s", data)

    # ── Normalise ──
    event = _normalise_payload(data)
    if event is None:
        # Quietly ignored (unmonitored address, missing fields, etc.)
        return Response(status_code=200)

    # ── Upsert into store ──
    record = await store.upsert(event)

    # ── Push to Discord bot queue ──
    if _event_queue is not None:
        await _event_queue.put(record)
        log.info(
            "Queued event: txid=%s chain=%s confirmations=%d",
            record.txid, record.chain, record.confirmations,
        )
    else:
        log.error("Event queue not initialised — Discord bot may not have started yet.")

    return Response(status_code=200)


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "monitored_addresses": len(config.ADDRESSES)}
