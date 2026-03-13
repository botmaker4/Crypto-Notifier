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
    Normalise a Tatum ADDRESS_EVENT payload into a consistent internal dict.

    Tatum ADDRESS_EVENT payload structure:
    {
      "address": "the monitored address",
      "txId": "transaction hash",
      "blockNumber": 12345,
      "chain": "BSC",
      "type": "incoming",        # "incoming" or "outgoing"
      "amount": "0.5",
      "counterAddress": "sender address",
      "asset": "BNB",            # or token contract address
      "mempool": false,
      "confirmations": 2,
      "timestamp": 1710000000    # unix epoch (may be absent)
    }

    Returns None if the event should be skipped.
    """
    # ── Chain ──
    chain = (data.get("chain") or "").upper()
    if not chain:
        log.debug("No chain in payload, ignoring: %s", data)
        return None

    # ── Only process incoming transactions ──
    tx_type = (data.get("type") or "incoming").lower()
    if tx_type == "outgoing":
        log.debug("Ignoring outgoing tx %s", data.get("txId"))
        return None

    # ── Address ──
    address = (data.get("address") or "").strip()
    if not address:
        log.debug("No address in payload, ignoring")
        return None

    # ── TXID ──
    txid = (
        data.get("txId")
        or data.get("txHash")
        or data.get("hash")
        or data.get("transactionHash")
        or ""
    ).strip()
    if not txid:
        log.debug("No txid in payload, ignoring")
        return None

    # ── Amount ──
    amount = str(data.get("amount") or "0")

    # ── USD value (not always present on free tier) ──
    usd_value: Optional[str] = None
    raw_asset = data.get("asset")
    if isinstance(raw_asset, dict):
        usd_value = str(raw_asset.get("usdValue") or "")
    elif data.get("usdValue"):
        usd_value = str(data["usdValue"])

    # ── Confirmations / block info ──
    confirmations: int = int(data.get("confirmations") or 0)
    block_height: Optional[int] = data.get("blockNumber") or data.get("blockHeight")

    # Tatum sends timestamp as unix int — convert to string for display
    raw_ts = data.get("timestamp") or data.get("blockTimestamp")
    timestamp: Optional[str] = str(int(raw_ts)) if raw_ts else None

    # ── Match address to a monitored wallet ──
    chain_key = chain  # fallback
    matched = False
    for ck, addr in config.ADDRESSES.items():
        if addr.lower() == address.lower():
            chain_key = ck
            matched = True
            break

    if not matched:
        log.debug("Address %s is not in monitored list, ignoring", address)
        return None

    log.info(
        "Incoming %s tx %s → %s  amount=%s  confirmations=%d",
        chain_key, txid[:16], address[:12], amount, confirmations,
    )

    return {
        "txid": txid,
        "chain": chain_key,
        "address": address,
        "amount": amount,
        "usd_value": usd_value or None,
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
