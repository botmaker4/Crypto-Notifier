# 🪙 Crypto Notifier — Discord Bot

A professional Discord bot that monitors cryptocurrency wallet addresses across **5 blockchains** using **Tatum webhooks** and sends rich **DM alerts** via discord.py.

---

## Features

- 🔔 **Real-time DM alerts** — instant notification the moment a new transaction hits your wallet
- ✅ **Confirmation alert** — second DM when the transaction reaches 2+ confirmations
- 🚫 **No duplicate alerts** — dedup flags prevent the same event firing twice
- 🔐 **Webhook signature verification** — HMAC-SHA512 validates every Tatum request
- 📊 **Rich Discord embeds** — chain, amount, USD value, TXID, block height, explorer link
- 🌐 **Multi-chain** — LTC, USDT-BSC, USDT-Polygon, SOL, BNB in one bot

---

## Supported Networks

| Key    | Network                   | Explorer                    |
|--------|---------------------------|-----------------------------|
| `LTC`  | Litecoin                  | blockchair.com/litecoin     |
| `BSC`  | BNB Smart Chain (USDT)    | bscscan.com                 |
| `MATIC`| Polygon (USDT)            | polygonscan.com             |
| `SOL`  | Solana                    | solscan.io                  |
| `BNB`  | BNB Smart Chain (native)  | bscscan.com                 |

> **Tatum free tier** supports up to **5 subscribed addresses** total.

---

## Project Structure

```
Crypto-Notifier/
├── bot.py               # Main entrypoint — discord.py + uvicorn
├── webhook_server.py    # FastAPI webhook receiver
├── tatum_client.py      # Tatum REST API wrapper
├── transaction_store.py # In-memory tx cache & dedup logic
├── config.py            # Env-var loader & validation
├── requirements.txt
├── .env.example
└── README.md
```

---

## Setup

### 1. Prerequisites

- Python 3.11+
- A [Tatum account](https://dashboard.tatum.io) (free tier)
- A Discord bot with **Message Content** disabled and **DM** permissions
- A public HTTPS URL for your webhook (use [ngrok](https://ngrok.com) for local dev)

### 2. Install Dependencies

```bash
cd c:\coding\Crypto-Notifier
pip install -r requirements.txt
```

### 3. Configure Environment

```bash
copy .env.example .env
```

Edit `.env` and fill in every value:

| Variable               | Description                                              |
|------------------------|----------------------------------------------------------|
| `DISCORD_TOKEN`        | Your Discord bot token                                   |
| `DISCORD_USER_ID`      | Your Discord User ID (enable Developer Mode to copy it)  |
| `TATUM_API_KEY`        | From [Tatum Dashboard](https://dashboard.tatum.io)       |
| `TATUM_WEBHOOK_SECRET` | Random secret — generate with `python -c "import secrets; print(secrets.token_hex(32))"` |
| `WEBHOOK_URL`          | Public URL Tatum will POST to, e.g. `https://abc.ngrok.io/webhook` |
| `WEBHOOK_PORT`         | Local port (default `8000`)                              |
| `ADDRESSES`            | JSON object: `{"LTC":"Lxxx","BSC":"0x...","MATIC":"0x...","SOL":"...","BNB":"0x..."}` |

### 4. Expose Your Webhook (Local Dev)

```bash
# In a separate terminal
ngrok http 8000
```

Copy the `https://` forwarding URL and set it as `WEBHOOK_URL` in your `.env`:

```
WEBHOOK_URL=https://abc123.ngrok-free.app/webhook
```

### 5. Run the Bot

```bash
python bot.py
```

On startup, the bot will:
1. Connect to Discord
2. Register Tatum webhook subscriptions for all configured addresses
3. Start the webhook server on port `8000`
4. Begin listening for transaction events

---

## Testing

### Simulate a Webhook (PowerShell)

```powershell
$body = '{"chain":"LTC","address":"LYourAddress","txId":"test123abc","amount":"0.5","blockNumber":12345,"confirmations":0,"timestamp":"1710000000"}'
$secret = "your-webhook-secret-here"

$hmac = [System.Security.Cryptography.HMAC]::Create("HMACSHA512")
$hmac.Key = [System.Text.Encoding]::UTF8.GetBytes($secret)
$sig = [System.BitConverter]::ToString($hmac.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($body))).Replace("-","").ToLower()

Invoke-WebRequest -Uri "http://localhost:8000/webhook" `
  -Method POST `
  -ContentType "application/json" `
  -Headers @{"x-payload-hash"=$sig} `
  -Body $body
```

Expected: You receive a **"New Transaction Detected"** DM within 2 seconds.

### Test Confirmation Alert

Resend the same payload with `"confirmations":2` — you receive a **"Transaction Confirmed"** DM.  
Resending again does nothing (dedup protection).

### Health Check

```bash
curl http://localhost:8000/health
# → {"status":"ok","monitored_addresses":5}
```

---

## How It Works

```
Tatum API ──POST──▶ /webhook (FastAPI)
                        │
                   Verify HMAC-SHA512
                        │
                   Normalise payload
                        │
                   Upsert to TransactionStore
                        │
                   Put into asyncio.Queue
                        │
              Discord Bot background task
                        │
              ┌─────────▼──────────┐
              │  notified_new?      │──No──▶ Send "New TX" DM
              │  confirmations ≥ 2? │──Yes─▶ Send "Confirmed" DM
              └────────────────────┘
```

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| "DISCORD_USER_ID is not set" | Enable Developer Mode → right-click username → Copy User ID |
| No DM received | Check the bot shares a server with you OR has already messaged you (Discord requirement) |
| Webhook 401 | Ensure `TATUM_WEBHOOK_SECRET` matches what you set in Tatum Dashboard |
| Tatum subscription fails | Check `TATUM_API_KEY` is valid and you have < 5 subscriptions |
| ngrok URL changed | Update `WEBHOOK_URL` in `.env` and restart — subscriptions will be re-registered |

> **Important:** Discord bots can only DM users who share a server with the bot OR have previously messaged the bot. Make sure to invite your bot to at least one shared server.
