# 🧾 Telegram Bill Splitter Bot

A Telegram group chat bot that splits restaurant bills item-by-item. Upload a receipt photo and let AI extract items, or add them manually. Members pick what they ordered and get an accurate split including service charge and VAT.

Built for splitting bills in **Thailand (THB)** and **Japan (JPY)** with automatic currency conversion.

## Features

- **AI Receipt Scanning** — Send a receipt photo and Gemini AI extracts items, prices, service charge, and VAT automatically
- **Manual Entry** — Add items with `/additem Pad Thai 150`
- **Item Picking** — Members tap inline buttons to claim their items. Shared items (e.g. appetizers) auto-split between claimers
- **Tax-Inclusive / Exclusive** — AI detects whether the bill total already includes tax or if fees are added on top, and calculates accordingly
- **Service Charge & VAT** — Detected from receipt with confirmation buttons. Fees distributed proportionally based on each person's share
- **JPY → THB Conversion** — Auto-fetch exchange rate or enter manually
- **MongoDB Persistence** — Bills survive bot restarts
- **Group Chat Ready** — Multiple members join, pick items, and see a final per-person summary

## Commands

| Command | Description |
|---|---|
| `/newbill` | Start a new bill — choose currency and input method |
| `/join` | Join the current active bill |
| `/additem Name 150` | Add an item manually with name and price |
| `/items` | Show all items with inline pick/unclaim buttons |
| `/pick 3` | Pick item #3 for yourself |
| `/unpick 3` | Remove yourself from item #3 |
| `/resetpicks` | Clear all your picked items at once |
| `/assign 3 @user` | Assign item #3 to someone (creator only) |
| `/setfees 10 7 inclusive` | Set service charge & VAT (inclusive/exclusive) |
| `/done` | Finalize bill and show per-person summary |
| `/cancel` | Cancel current bill (creator only) |
| `/history` | Show last 5 finalized bills |
| `/help` | Show help message |

## How It Works

```
1. /newbill → Pick currency (THB 🇹🇭 or JPY 🇯🇵)
2. Upload receipt photo → AI extracts items + detects fees
3. Confirm service charge & VAT (inclusive or exclusive)
4. Members /join the bill
5. /items → Everyone taps buttons to pick their items
6. /done → Bot shows per-person breakdown with fees
```

### Tax-Inclusive vs Tax-Exclusive

The bot handles both billing styles:

**Tax-inclusive** (common in Thailand) — Item prices already include VAT/SC. The total equals the sum of items. Fees are shown as a breakdown for info only.

```
👤 Alice
    • Thai Milk Tea: ฿95
    • Vanilla Financier: ฿45
    Items: ฿140
    (includes SC: ฿0)
    (includes VAT: ฿9.16)
    → Pay: ฿140
```

**Tax-exclusive** — Fees are added on top of item prices.

```
👤 Alice
    • Steak: ฿500
    Items: ฿500
    + SC 10%: ฿50
    + VAT 7%: ฿38.50
    → Pay: ฿589
```

## Setup

### Prerequisites

- Python 3.10+
- MongoDB (local or [MongoDB Atlas](https://www.mongodb.com/atlas) free tier)
- Telegram Bot Token from [@BotFather](https://t.me/BotFather)
- Google Gemini API Key from [AI Studio](https://aistudio.google.com/apikey) (free tier)

### 1. Clone and install

```bash
git clone <your-repo-url>
cd bill-splitter-bot
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment

Create a `.env` file:

```env
BOT_TOKEN=your_telegram_bot_token
MONGO_URI=mongodb+srv://user:pass@cluster.mongodb.net/?retryWrites=true&w=majority
MONGO_DB=bill_splitter
GEMINI_API_KEY=your_gemini_api_key
```

### 3. Configure BotFather

In Telegram, message [@BotFather](https://t.me/BotFather):

1. `/setprivacy` → Select your bot → **Disable** (required for group messages)
2. `/setcommands` → Set bot commands:
```
newbill - Start a new bill
join - Join the current bill
additem - Add item: /additem Name Price
items - Show items with pick buttons
pick - Pick item by number
unpick - Remove yourself from item
resetpicks - Clear all your picks
assign - Assign item to user
setfees - Set service charge and VAT
done - Finalize bill
cancel - Cancel current bill
history - View past bills
help - Show help
```

### 4. Run

```bash
python bill_splitter_bot.py
```

## Deployment

### Docker

```bash
docker build -t bill-splitter-bot .
docker run -d --name bill-bot --env-file .env --restart unless-stopped bill-splitter-bot
```

### Docker Compose (with local MongoDB)

```bash
docker-compose up -d
```

### VPS Hosting Options

| Provider | Cost | Notes |
|---|---|---|
| Oracle Cloud | Free | 4 ARM CPU, 24GB RAM. Best free tier |
| Hetzner | €3.29/mo | Reliable, EU/US datacenters |
| Vultr | $3.50/mo | Tokyo datacenter available |
| DigitalOcean | $4/mo | Singapore datacenter available |
| Railway | ~$5/mo | Deploy from Git, no server management |

For deployment instructions, see [DEPLOY.md](DEPLOY.md).

## Project Structure

```
├── bill_splitter_bot.py   # Main bot application
├── requirements.txt       # Python dependencies
├── Dockerfile             # Container build file
├── docker-compose.yml     # Docker Compose with MongoDB
├── DEPLOY.md              # Deployment guide for Oracle Cloud
├── README.md              # This file
└── .env                   # Environment variables (not committed)
```

## Tech Stack

- **Python** — python-telegram-bot (async)
- **Google Gemini 2.5 Flash** — Receipt OCR and fee detection
- **MongoDB** — Bill persistence
- **httpx** — Async HTTP for API calls

## License

MIT