# 🧾 Telegram Bill Splitter Bot

A Telegram group chat bot that splits restaurant bills item-by-item. Upload a receipt photo and let AI extract items, prices, discounts, service charge, and VAT automatically — or add items manually. Members pick what they ordered, and the creator can assign items to non-members (guests) too.

Built for splitting bills in **Thailand (THB)** and **Japan (JPY)** with automatic currency conversion.

## Features

- **AI Receipt Scanning** — Send a receipt photo and Gemini AI extracts items, prices, discounts, service charge, and VAT
- **Discount Handling** — Per-item discounts (e.g. UOB 10% off) are extracted as net prices automatically. Mixed discounts (some items discounted, some not) are supported
- **Manual Entry** — Add items with `/additem Pad Thai 150`
- **Item Picking** — Members tap inline buttons to claim their items. Shared items auto-split between claimers
- **Guest Assignment** — Creator can assign items to non-members (guests) who aren't in the Telegram group. Guests appear in the final summary with their own totals
- **3 Fee Modes** — AI detects how service charge and VAT are applied (see below)
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
| `/assign 3` | Show list of members + guests to assign item #3 |
| `/assign 3 @user` | Assign item #3 to a member |
| `/assign 3 John` | Assign item #3 to a non-member (guest) |
| `/unassign 3 John` | Remove someone's assignment from item #3 |
| `/setfees 10 7 MODE` | Set service charge & VAT with fee mode |
| `/done` | Finalize bill and show per-person summary |
| `/cancel` | Cancel current bill (creator only) |
| `/history` | Show last 5 finalized bills |
| `/help` | Show help message |

## How It Works

```
1. /newbill → Pick currency (THB 🇹🇭 or JPY 🇯🇵)
2. Upload receipt photo → AI extracts items + detects fees + handles discounts
3. Confirm fee mode (see below)
4. Members /join the bill
5. /items → Everyone taps buttons to pick their items
6. Creator uses /assign for guests who aren't in the group
7. /done → Bot shows per-person breakdown with fees
```

### Fee Modes

The bot supports 3 fee calculation modes, plus no fees:

| Mode | Command shortcut | Calculation | Example (฿1,000 subtotal) |
|---|---|---|---|
| **Both inclusive** | `both_inc` | Item prices already include SC + VAT | Pay ฿1,000 |
| **SC exclusive, VAT inclusive** | `sc_exc` | SC added on top, VAT is just a breakdown | Pay ฿1,100 |
| **Both exclusive** | `both_exc` | SC + VAT both added on top | Pay ฿1,177 |
| **No fees** | `/setfees 0 0` | Just item prices | Pay ฿1,000 |

AI detects the mode from the receipt by comparing the item subtotal to the total. You can confirm or change it via inline buttons or `/setfees`.

**Both inclusive** (total = items):
```
👤 Alice
    • Thai Milk Tea: ฿95
    • Vanilla Financier: ฿45
    Items: ฿140
    (includes SC: ฿0)
    (includes VAT: ฿9.16)
    → Pay: ฿140
```

**SC exclusive, VAT inclusive** (total = items + SC):
```
👤 Alice
    • Ramen: ฿500
    Items: ฿500
    + SC 10%: ฿50
    (includes VAT: ฿35.98)
    → Pay: ฿550
```

**Both exclusive** (total = items + SC + VAT):
```
👤 Alice
    • Steak: ฿500
    Items: ฿500
    + SC 10%: ฿50
    + VAT 7%: ฿38.50
    → Pay: ฿589
```

### Discounts

When a receipt has per-item discounts (e.g. credit card promos, member discounts), the AI extracts **net prices after discount**. For example:

```
Receipt shows:
  Meat Lovers     480.00
  #Pro UOB 10%    -48.00

Bot extracts:
  Meat Lovers → ฿432 (net price)
```

Mixed discounts (some items discounted, others not) are handled correctly — each item gets its own net price.

### Assigning Items to Guests

The bill creator can assign items to people who aren't in the Telegram group:

- `/assign 3` — Shows inline buttons with all members + existing guests + "Add non-member"
- `/assign 3 John` — Directly assigns to a guest named "John"
- `/unassign 3 John` — Removes the assignment

Guests appear in the final summary with a `(guest)` tag:

```
👤 John (guest)
    • Latte: ฿117
    Items: ฿117
    + SC 10%: ฿12
    + VAT 7%: ฿9
    → Pay: ฿138
```

The member count shows guests separately: `👥 Members: 3 + 2 guests`

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
assign - Assign item to member or guest
unassign - Remove assignment from item
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
- **Google Gemini 2.5 Flash** — Receipt OCR, fee detection, discount extraction
- **MongoDB** — Bill persistence
- **httpx** — Async HTTP for API calls

## License

MIT