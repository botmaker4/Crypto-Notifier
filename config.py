"""
config.py — Centralised configuration loaded from environment variables.
"""

import os
import json
import logging
from dotenv import load_dotenv

load_dotenv()

# ── Discord ───────────────────────────────────────────────────────────────────
DISCORD_TOKEN: str = os.getenv("DISCORD_TOKEN", "")
DISCORD_USER_ID: int = int(os.getenv("DISCORD_USER_ID", "0"))

# ── Tatum ─────────────────────────────────────────────────────────────────────
TATUM_API_KEY: str = os.getenv("TATUM_API_KEY", "")
TATUM_API_BASE: str = "https://api.tatum.io/v4"
TATUM_WEBHOOK_SECRET: str = os.getenv("TATUM_WEBHOOK_SECRET", "")

# Public webhook URL that Tatum will POST events to
# e.g. https://yourserver.example.com/webhook  (or ngrok URL for local dev)
WEBHOOK_URL: str = os.getenv("WEBHOOK_URL", "http://localhost:8000/webhook")
WEBHOOK_PORT: int = int(os.getenv("WEBHOOK_PORT", "8000"))

# ── Monitored Addresses ───────────────────────────────────────────────────────
# Loaded from JSON string:  ADDRESSES={"LTC":"L...","BSC":"0x...","MATIC":"0x...","SOL":"...","BNB":"0x..."}
# Supported chain keys: LTC, BSC, MATIC, SOL, BNB
_raw_addresses: str = os.getenv("ADDRESSES", "{}")
try:
    ADDRESSES: dict[str, str] = json.loads(_raw_addresses)
except json.JSONDecodeError:
    ADDRESSES = {}

# Maximum addresses allowed by Tatum free tier
MAX_ADDRESSES: int = 5

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

log = logging.getLogger("crypto-notifier")

# ── Validation ─────────────────────────────────────────────────────────────────
def validate() -> None:
    errors = []
    if not DISCORD_TOKEN:
        errors.append("DISCORD_TOKEN is not set.")
    if DISCORD_USER_ID == 0:
        errors.append("DISCORD_USER_ID is not set.")
    if not TATUM_API_KEY:
        errors.append("TATUM_API_KEY is not set.")
    if not WEBHOOK_URL or WEBHOOK_URL == "http://localhost:8000/webhook":
        log.warning("WEBHOOK_URL is set to localhost — Tatum cannot reach this unless you use ngrok.")
    if len(ADDRESSES) == 0:
        errors.append("ADDRESSES is empty — no wallets to monitor.")
    if len(ADDRESSES) > MAX_ADDRESSES:
        errors.append(f"ADDRESSES has {len(ADDRESSES)} entries; Tatum free tier allows only {MAX_ADDRESSES}.")
    if errors:
        for e in errors:
            log.error("Config error: %s", e)
        raise RuntimeError("One or more configuration errors found. Check your .env file.")
