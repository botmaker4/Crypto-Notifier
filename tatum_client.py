"""
tatum_client.py — Async Tatum v4 REST API client.

Handles:
  - Creating ADDRESS_EVENT webhook subscriptions (valid Tatum v4 type)
  - Listing existing subscriptions (to avoid duplicates on restart)
  - Deleting subscriptions
  - Auto-setup for all configured addresses at bot startup
"""

import asyncio
import logging
from typing import Optional

import httpx

import config

log = logging.getLogger("crypto-notifier.tatum")

# Tatum chain identifiers used in subscription API calls
# Maps our internal keys (from ADDRESSES env) → Tatum chain value
CHAIN_MAP: dict[str, str] = {
    "LTC":   "LTC",
    "BSC":   "BSC",
    "MATIC": "MATIC",
    "SOL":   "SOL",
    "BNB":   "BSC",   # BNB/BEP-20 also uses the BSC chain in Tatum
}

# Human-readable labels for Discord embeds
CHAIN_LABELS: dict[str, str] = {
    "LTC":   "Litecoin (LTC)",
    "BSC":   "BNB Smart Chain (BSC)",
    "MATIC": "Polygon (MATIC / POL)",
    "SOL":   "Solana (SOL)",
    "BNB":   "BNB Smart Chain (BNB)",
}

# Blockchain explorer base URLs for transaction links
EXPLORER_URLS: dict[str, str] = {
    "LTC":   "https://blockchair.com/litecoin/transaction/",
    "BSC":   "https://bscscan.com/tx/",
    "MATIC": "https://polygonscan.com/tx/",
    "SOL":   "https://solscan.io/tx/",
    "BNB":   "https://bscscan.com/tx/",
}


def _headers() -> dict[str, str]:
    return {
        "x-api-key": config.TATUM_API_KEY,
        "Content-Type": "application/json",
    }


async def create_subscription(chain_key: str, address: str, webhook_url: str) -> Optional[dict]:
    """
    Register a Tatum ADDRESS_EVENT subscription.
    ADDRESS_EVENT is the correct v4 type for monitoring all transactions
    (incoming/outgoing native + fungible) on a given address.
    Returns the created subscription dict, or None on failure.
    """
    tatum_chain = CHAIN_MAP.get(chain_key.upper())
    if not tatum_chain:
        log.error("Unknown chain key: %s", chain_key)
        return None

    payload = {
        "type": "ADDRESS_EVENT",
        "attr": {
            "address": address,
            "chain": tatum_chain,
            "url": webhook_url,
        },
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{config.TATUM_API_BASE}/subscription",
                json=payload,
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            log.info("Subscription created for %s [%s] → id=%s", address, chain_key, data.get("id"))
            return data
    except httpx.HTTPStatusError as e:
        log.error(
            "Tatum subscription failed for %s [%s]: %s — %s",
            address, chain_key, e.response.status_code, e.response.text,
        )
    except Exception as e:
        log.error("Unexpected error creating subscription for %s: %s", address, e)
    return None


async def list_subscriptions() -> list[dict]:
    """Return all current subscriptions for this API key."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{config.TATUM_API_BASE}/subscription",
                params={"pageSize": 50},
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            # Tatum returns either a list or {"data": [...]}
            if isinstance(data, list):
                return data
            return data.get("data", [])
    except Exception as e:
        log.error("Failed to list subscriptions: %s", e)
        return []


async def delete_subscription(subscription_id: str) -> bool:
    """Delete a Tatum subscription by its ID."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.delete(
                f"{config.TATUM_API_BASE}/subscription/{subscription_id}",
                headers=_headers(),
            )
            resp.raise_for_status()
            log.info("Deleted subscription id=%s", subscription_id)
            return True
    except Exception as e:
        log.error("Failed to delete subscription %s: %s", subscription_id, e)
        return False


async def setup_all_subscriptions() -> None:
    """
    Called at bot startup. Registers subscriptions for all addresses in config.
    Skips addresses that are already subscribed to avoid hitting the 5-slot limit.
    """
    if not config.ADDRESSES:
        log.warning("No addresses configured — nothing to subscribe.")
        return

    existing = await list_subscriptions()
    # Build a set of (address.lower(), tatum_chain) already registered
    already_registered: set[tuple[str, str]] = set()
    for sub in existing:
        attr = sub.get("attr", {})
        addr = (attr.get("address") or "").lower()
        chain = (attr.get("chain") or "").upper()
        if addr and chain:
            already_registered.add((addr, chain))

    log.info("Found %d existing subscription(s) on Tatum.", len(existing))

    tasks = []
    for chain_key, address in config.ADDRESSES.items():
        tatum_chain = CHAIN_MAP.get(chain_key.upper(), chain_key.upper())
        key = (address.lower(), tatum_chain)
        if key in already_registered:
            log.info("Skipping %s [%s] — already subscribed.", address, chain_key)
        else:
            tasks.append(create_subscription(chain_key, address, config.WEBHOOK_URL))

    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        success = sum(1 for r in results if isinstance(r, dict))
        log.info("Subscription setup complete: %d new subscription(s) registered.", success)
    else:
        log.info("All addresses already subscribed — no new subscriptions needed.")
