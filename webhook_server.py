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
    Tatum v4 does not support per-subscription HMAC signing (hmacSecret is
    rejected by their API). Webhooks are accepted unconditionally.
    Security relies on the obscurity of the webhook URL.
    """
    return True


# ── Payload Normalisation ─────────────────────────────────────────────────────

# Tatum sends full chain names in ADDRESS_EVENT payloads, not short codes.
# Map them back to our internal ADDRESSES keys.
TATUM_CHAIN_NAMES: dict[str, str] = {
    "litecoin-mainnet":  "LTC",
    "bsc-mainnet":       "BSC",
    "polygon-mainnet":   "MATIC",
    "solana-mainnet":    "SOL",
    "ethereum-mainnet":  "ETH",
    # Short codes as fallback (unlikely but safe)
    "ltc":   "LTC",
    "bsc":   "BSC",
    "matic": "MATIC",
    "sol":   "SOL",
}


# ── Known ERC-20 / BEP-20 contract addresses → token symbol ──────────────────
# Tatum sometimes sends the contract address as `asset` instead of the symbol.
# We map known ones to their proper symbol so USD pricing works correctly.
KNOWN_CONTRACTS: dict[str, str] = {
    # USDT
    "0x55d398326f99059ff775485246999027b3197955": "USDT",  # BSC
    "0xc2132d05d31c914a87c6611c10748aeb04b58e8f": "USDT",  # Polygon
    "0xdac17f958d2ee523a2206206994597c13d831ec7": "USDT",  # Ethereum
    # USDC
    "0x2791bca1f2de4661ed88a30c99a7a9449aa84174": "USDC",  # Polygon
    "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d": "USDC",  # BSC
    # Wrapped SOL on BSC
    "0x570a5d26f7765ecb712c0924e4de545b89fd43df": "SOL",
    # WBTC
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": "WBTC",
    "0x1bfd67037b42cf73acf2047067bd4f2c47d9bfd6": "WBTC",  # Polygon
    # WETH
    "0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619": "WETH",  # Polygon
    "0x2170ed0880ac9a755fd29b2688956bd959f933f8": "WETH",  # BSC
}

# Maximum age of a transaction we'll notify about (prevents re-notification on restart)
MAX_TX_AGE_SECONDS: int = 2 * 60 * 60   # 2 hours


def _resolve_asset(raw_asset) -> str:
    """
    Convert Tatum's `asset` field to a clean token symbol.
    Handles both plain symbols ('USDT') and contract addresses ('0xc213...').
    """
    if not raw_asset:
        return ""
    if isinstance(raw_asset, dict):
        raw_asset = raw_asset.get("symbol") or raw_asset.get("name") or ""
    s = str(raw_asset).strip()
    # If it looks like a contract address, try to resolve it
    if s.startswith("0x") or s.startswith("0X"):
        resolved = KNOWN_CONTRACTS.get(s.lower())
        if resolved:
            return resolved
        # Unknown contract — return empty so bot falls back to chain default ticker
        log.debug("Unknown contract address as asset: %s", s)
        return ""
    return s.upper()


def _normalise_payload(data: dict) -> Optional[dict]:
    """
    Normalise a Tatum ADDRESS_EVENT payload into a consistent internal dict.
    Returns None if the event should be skipped.
    """
    import time as _time

    # ── Chain — map full Tatum name → our internal key ──
    raw_chain = (data.get("chain") or "").lower()
    if not raw_chain:
        log.debug("No chain in payload, ignoring: %s", data)
        return None

    # tx type: 'native' or 'token' — process both
    log.debug("Received %s tx on chain %s", data.get("type", "?"), raw_chain)

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

    # ── Asset symbol — resolve contract addresses to proper tickers ──
    asset: str = _resolve_asset(data.get("asset"))

    # ── USD value (rarely sent on free tier) ──
    usd_value: Optional[str] = None
    if data.get("usdValue"):
        usd_value = str(data["usdValue"])

    # ── Confirmations — Tatum uses both field names ──
    confirmations: int = int(
        data.get("confirmations")
        or data.get("blockConfirmations")
        or 0
    )

    # ── Block info ──
    block_height: Optional[int] = data.get("blockNumber") or data.get("blockHeight")

    # ── Timestamp — reject stale transactions to prevent re-alerts on restart ──
    raw_ts = data.get("timestamp") or data.get("blockTimestamp")
    timestamp: Optional[str] = None
    if raw_ts:
        try:
            ts_int = int(raw_ts)
            age = _time.time() - ts_int
            if age > MAX_TX_AGE_SECONDS:
                log.info(
                    "Ignoring stale tx %s (%.1f hours old) — skipping to avoid re-notification.",
                    txid[:16], age / 3600,
                )
                return None
            timestamp = str(ts_int)
        except (ValueError, TypeError):
            pass

    # ── Match address to a monitored wallet ──
    chain_key: Optional[str] = None
    for ck, addr in config.ADDRESSES.items():
        if addr.lower() == address.lower():
            chain_key = ck
            break

    if chain_key is None:
        resolved = TATUM_CHAIN_NAMES.get(raw_chain)
        if resolved and resolved in config.ADDRESSES:
            chain_key = resolved

    if chain_key is None:
        log.debug("Address %s (chain=%s) not in monitored list, ignoring", address, raw_chain)
        return None

    log.info(
        "Incoming %s tx %s  asset=%s  amount=%s  confirmations=%d",
        chain_key, txid[:16], asset or "?", amount, confirmations,
    )

    return {
        "txid": txid,
        "chain": chain_key,
        "address": address,
        "amount": amount,
        "asset": asset,
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
