"""
Telegram Bill Splitter Bot (with MongoDB persistence)
=====================================================
A group chat bot that lets members split bills item-by-item.

Features:
- Upload receipt photo (OCR) or manually add items
- THB / JPY currency with auto or manual JPY->THB conversion
- Anyone can /join a bill, pick items via inline buttons
- Creator can assign items to members
- Final summary with per-person totals
- MongoDB persistence -- bills survive restarts

Commands:
  /newbill    - Start a new bill session
  /join       - Join the current bill
  /additem    - Add item manually: /additem <n> <price>
  /items      - Show all items with pick buttons
  /pick       - Pick item by number: /pick <number>
  /assign     - Assign item: /assign <number> @user
  /unpick     - Remove yourself from item: /unpick <number>
  /done       - Finalize bill and show summary
  /cancel     - Cancel current bill
  /history    - Show past bills in this chat
  /help       - Show help message
"""

import os
import re
import io
import logging
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

import httpx
from pymongo import MongoClient
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)

# --- Config ---

BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGO_DB", "bill_splitter")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
EXCHANGE_API_URL = "https://api.exchangerate-api.com/v4/latest/JPY"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --- MongoDB Setup ---

mongo_client = MongoClient(MONGO_URI)
db = mongo_client[MONGO_DB]
bills_col = db["bills"]

bills_col.create_index("chat_id")
bills_col.create_index([("chat_id", 1), ("is_finalized", 1)])


# --- DB Helpers ---

def new_bill_doc(chat_id, creator_id, creator_name):
    return {
        "chat_id": chat_id,
        "creator_id": creator_id,
        "creator_name": creator_name,
        "currency": None,
        "jpy_to_thb_rate": None,
        "service_charge_pct": None,  # e.g. 10.0 for 10%
        "vat_pct": None,             # e.g. 7.0 for 7%
        "fees_mode": None,           # "both_inclusive", "sc_exclusive_vat_inclusive", "both_exclusive", or None
        "items": [],
        "members": {str(creator_id): creator_name},
        "next_item_id": 1,
        "created_at": datetime.now(timezone.utc),
        "is_finalized": False,
        "awaiting_photo": False,
        "awaiting_manual_rate": False,
        "awaiting_fees": False,
    }


def get_active_bill(chat_id):
    return bills_col.find_one({"chat_id": chat_id, "is_finalized": False})


def save_bill(bill):
    bills_col.replace_one({"_id": bill["_id"]}, bill)


def add_item_to_bill(bill, name, price):
    item = {
        "id": bill["next_item_id"],
        "name": name,
        "price": price,
        "claimed_by": [],
    }
    bill["items"].append(item)
    bill["next_item_id"] += 1
    save_bill(bill)
    return item


def get_item(bill, item_id):
    for item in bill["items"]:
        if item["id"] == item_id:
            return item
    return None


def item_per_person(item):
    n = len(item["claimed_by"])
    return item["price"] / n if n > 0 else item["price"]


def person_total(bill, user_id):
    total = 0.0
    uid_str = str(user_id)
    for item in bill["items"]:
        for claim in item["claimed_by"]:
            if str(claim["user_id"]) == uid_str:
                total += item_per_person(item)
                break
    return total


def bill_total(bill):
    return sum(i["price"] for i in bill["items"])


def bill_grand_total(bill):
    """Total including service charge and VAT based on fees_mode."""
    subtotal = bill_total(bill)
    sc_pct = bill.get("service_charge_pct") or 0
    vat_pct = bill.get("vat_pct") or 0
    mode = bill.get("fees_mode")

    if mode == "both_inclusive":
        return subtotal
    elif mode == "sc_exclusive_vat_inclusive":
        return subtotal * (1 + sc_pct / 100)
    elif mode == "both_exclusive":
        after_sc = subtotal * (1 + sc_pct / 100)
        return after_sc * (1 + vat_pct / 100)
    else:
        return subtotal


def person_grand_total(bill, user_id):
    """Person's total including proportional service charge and VAT."""
    subtotal = bill_total(bill)
    if subtotal == 0:
        return 0
    p_subtotal = person_total(bill, user_id)
    ratio = p_subtotal / subtotal
    return bill_grand_total(bill) * ratio


def person_fee_breakdown(bill, user_id):
    """Return (item_subtotal, sc_amount, vat_amount, total) for a person."""
    subtotal = bill_total(bill)
    if subtotal == 0:
        return 0, 0, 0, 0
    p_sub = person_total(bill, user_id)
    sc_pct = bill.get("service_charge_pct") or 0
    vat_pct = bill.get("vat_pct") or 0
    mode = bill.get("fees_mode")

    if mode == "both_inclusive":
        # Back-calculate: price = base * (1+sc%) * (1+vat%)
        divisor = (1 + sc_pct / 100) * (1 + vat_pct / 100)
        base = p_sub / divisor if divisor else p_sub
        sc_amt = base * sc_pct / 100
        vat_amt = (base + sc_amt) * vat_pct / 100
        return p_sub, sc_amt, vat_amt, p_sub

    elif mode == "sc_exclusive_vat_inclusive":
        # SC added on top, VAT is inside the total (total = subtotal + SC)
        total = p_sub * (1 + sc_pct / 100)
        sc_amt = p_sub * sc_pct / 100
        # Back-calculate VAT from total: total = before_vat * (1+vat%)
        before_vat = total / (1 + vat_pct / 100) if vat_pct else total
        vat_amt = total - before_vat
        return p_sub, sc_amt, vat_amt, total

    elif mode == "both_exclusive":
        sc_amt = p_sub * sc_pct / 100
        vat_amt = (p_sub + sc_amt) * vat_pct / 100
        total = p_sub + sc_amt + vat_amt
        return p_sub, sc_amt, vat_amt, total

    else:
        return p_sub, 0, 0, p_sub


# --- Helpers ---

def get_display_name(user):
    if user.username:
        return f"@{user.username}"
    name = user.first_name or ""
    if user.last_name:
        name += f" {user.last_name}"
    return name.strip() or f"User#{user.id}"


async def fetch_jpy_to_thb_rate():
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            resp = await c.get(EXCHANGE_API_URL)
            data = resp.json()
            return data["rates"].get("THB")
    except Exception as e:
        logger.error(f"Failed to fetch exchange rate: {e}")
        return None


async def parse_receipt_ocr(image_bytes, currency):
    """Extract items and prices from receipt image using Google Gemini API."""
    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY not set")
        return [], None, None, None

    import base64
    import json
    b64_image = base64.b64encode(image_bytes).decode("utf-8")

    if image_bytes[:8] == b'\x89PNG\r\n\x1a\n':
        mime_type = "image/png"
    elif image_bytes[:2] == b'\xff\xd8':
        mime_type = "image/jpeg"
    else:
        mime_type = "image/jpeg"

    currency_hint = "Thai Baht (THB)" if currency == "THB" else "Japanese Yen (JPY)"

    prompt = (
        f"This is a receipt photo. The currency is {currency_hint}.\n"
        "Extract ALL food/drink items and their prices from this receipt.\n"
        "Respond ONLY with a JSON object, no other text, no markdown.\n\n"
        "Format:\n"
        '{"items": [{"name": "item name", "price": 123.45}, ...], '
        '"service_charge_pct": null, "vat_pct": null, "fees_mode": null}\n\n'
        "Rules:\n"
        "- Include individual items only, not subtotals/totals/tax/service charge lines\n"
        "- Use the original item name from the receipt\n"
        "- Price should be a number without currency symbols\n"
        "- For service_charge_pct: if the receipt shows a service charge, "
        "put the percentage as a number (e.g. 10 for 10%). Otherwise null.\n"
        "- For vat_pct: if the receipt shows VAT/tax, "
        "put the percentage as a number (e.g. 7 for 7%). Otherwise null.\n"
        "- For fees_mode: determine how fees are applied. Must be one of:\n"
        '  "both_inclusive" — item prices already include SC and VAT. '
        "The TOTAL equals the sum of items.\n"
        '  "sc_exclusive_vat_inclusive" — SC is added on top of item subtotal, '
        "but VAT is already included in the final total (not added again). "
        "TOTAL = subtotal + SC. VAT line is just a breakdown.\n"
        '  "both_exclusive" — both SC and VAT are added on top. '
        "TOTAL = subtotal + SC + VAT.\n"
        "  null — if you cannot determine.\n"
        "- To detect: compare the sum of item prices to the TOTAL on the receipt. "
        "If TOTAL = sum of items → both_inclusive. "
        "If TOTAL = sum + SC (and VAT is shown as breakdown of total) → sc_exclusive_vat_inclusive. "
        "If TOTAL = sum + SC + VAT → both_exclusive.\n"
        "- If you can't read the receipt clearly, return: "
        '{"items": [], "service_charge_pct": null, "vat_pct": null, "fees_mode": null}\n'
    )

    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"

        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": mime_type,
                                "data": b64_image,
                            }
                        },
                        {"text": prompt},
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 2000,
            },
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload)

        raw = resp.text
        logger.info(f"Gemini API status={resp.status_code} body={raw[:500]}")

        if resp.status_code != 200:
            return [], None, None, None

        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()

        # Clean up — extract JSON array
        if "```" in text:
            text = re.search(r'```(?:json)?\s*(.*?)```', text, re.DOTALL).group(1).strip()

        items_data = json.loads(text)

        # Handle both formats: new dict format and legacy array format
        sc_pct = None
        vat_pct = None
        fees_mode = None
        if isinstance(items_data, dict):
            sc_pct = items_data.get("service_charge_pct")
            vat_pct = items_data.get("vat_pct")
            fees_mode = items_data.get("fees_mode")
            items_list = items_data.get("items", [])
        else:
            items_list = items_data

        items = []
        for item in items_list:
            name = str(item.get("name", "")).strip()
            price = float(item.get("price", 0))
            if name and price > 0:
                items.append((name, price))

        logger.info(f"Gemini extracted {len(items)} items, sc={sc_pct}%, vat={vat_pct}%, mode={fees_mode}")
        return items, sc_pct, vat_pct, fees_mode

    except Exception as e:
        logger.error(f"Gemini API error: {e}")
        try:
            logger.error(f"Response body: {resp.text[:500]}")
        except Exception:
            pass
        return [], None, None, None


# --- Keyboards ---

def currency_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🇹🇭 THB", callback_data="currency:THB"),
            InlineKeyboardButton("🇯🇵 JPY", callback_data="currency:JPY"),
        ]
    ])


def rate_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Auto-fetch today's rate", callback_data="rate:auto")],
        [InlineKeyboardButton("✏️ Enter rate manually", callback_data="rate:manual")],
    ])


def input_method_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📸 Upload receipt photo", callback_data="input:photo")],
        [InlineKeyboardButton("✏️ Add items manually", callback_data="input:manual")],
    ])


FEES_MODE_LABELS = {
    "both_inclusive": "All inclusive (total = items)",
    "sc_exclusive_vat_inclusive": "SC on top, VAT included",
    "both_exclusive": "SC + VAT both on top",
}


def fees_confirm_keyboard(sc, vat, mode):
    """Inline buttons to confirm or edit detected fees."""
    mode_label = FEES_MODE_LABELS.get(mode, mode)
    label = f"✅ SC {sc:.0f}% + VAT {vat:.0f}% — {mode_label}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=f"fees:confirm:{sc}:{vat}:{mode}")],
        [InlineKeyboardButton("🔄 Change fee mode", callback_data=f"fees:pickmode:{sc}:{vat}")],
        [InlineKeyboardButton("✏️ Edit fees manually", callback_data="fees:edit")],
        [InlineKeyboardButton("❌ No fees on this bill", callback_data="fees:none")],
    ])


def fees_mode_keyboard(sc, vat):
    """Inline buttons to pick fee mode."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "1️⃣ All inclusive (total = items)",
            callback_data=f"fees:confirm:{sc}:{vat}:both_inclusive",
        )],
        [InlineKeyboardButton(
            "2️⃣ SC on top, VAT included",
            callback_data=f"fees:confirm:{sc}:{vat}:sc_exclusive_vat_inclusive",
        )],
        [InlineKeyboardButton(
            "3️⃣ SC + VAT both on top",
            callback_data=f"fees:confirm:{sc}:{vat}:both_exclusive",
        )],
        [InlineKeyboardButton("❌ No fees", callback_data="fees:none")],
    ])


def items_keyboard(bill):
    buttons = []
    for item in bill["items"]:
        claimed_names = ", ".join(c["name"] for c in item["claimed_by"]) if item["claimed_by"] else "—"
        label = f"#{item['id']} {item['name']} ({item['price']:,.0f}) [{claimed_names}]"
        if len(label) > 60:
            label = label[:57] + "..."
        buttons.append([InlineKeyboardButton(label, callback_data=f"pick:{item['id']}")])
    buttons.append([InlineKeyboardButton("✅ Done — show summary", callback_data="finalize")])
    return InlineKeyboardMarkup(buttons)


# --- Formatting ---

def format_items_list(bill):
    if not bill["items"]:
        return "📋 No items yet."

    symbol = "¥" if bill["currency"] == "JPY" else "฿"
    lines = [f"📋 *Bill Items* ({bill['currency']})\n"]

    for item in bill["items"]:
        claimed = ", ".join(c["name"] for c in item["claimed_by"]) if item["claimed_by"] else "❌ unclaimed"
        lines.append(f"`#{item['id']}` {item['name']} — {symbol}{item['price']:,.0f}  →  _{claimed}_")

    lines.append(f"\n💰 *Total: {symbol}{bill_total(bill):,.0f}*")
    return "\n".join(lines)


def format_summary(bill):
    symbol = "¥" if bill["currency"] == "JPY" else "฿"
    subtotal = bill_total(bill)
    grand = bill_grand_total(bill)
    rate = bill.get("jpy_to_thb_rate")
    sc_pct = bill.get("service_charge_pct") or 0
    vat_pct = bill.get("vat_pct") or 0
    mode = bill.get("fees_mode")
    lines = []

    lines.append("💸 *BILL SPLIT SUMMARY*")
    lines.append("━━━━━━━━━━━━━━━━━━━━")

    created = bill["created_at"]
    if isinstance(created, datetime):
        lines.append(f"📅 {created.strftime('%Y-%m-%d %H:%M')}")
    else:
        lines.append(f"📅 {created}")

    lines.append(f"💴 Currency: *{bill['currency']}*")

    if mode and (sc_pct or vat_pct):
        mode_label = FEES_MODE_LABELS.get(mode, "")

        if mode == "both_inclusive":
            divisor = (1 + sc_pct / 100) * (1 + vat_pct / 100)
            base = subtotal / divisor if divisor else subtotal
            sc_amount = base * sc_pct / 100
            vat_amount = (base + sc_amount) * vat_pct / 100
            lines.append(f"💰 *Total: {symbol}{subtotal:,.0f}* ({mode_label})")
            if sc_pct:
                lines.append(f"    ↳ SC {sc_pct:.0f}%: {symbol}{sc_amount:,.0f}")
            if vat_pct:
                lines.append(f"    ↳ VAT {vat_pct:.0f}%: {symbol}{vat_amount:,.0f}")

        elif mode == "sc_exclusive_vat_inclusive":
            sc_amount = subtotal * sc_pct / 100
            total_after_sc = subtotal + sc_amount
            before_vat = total_after_sc / (1 + vat_pct / 100) if vat_pct else total_after_sc
            vat_amount = total_after_sc - before_vat
            lines.append(f"💰 Subtotal: {symbol}{subtotal:,.0f}")
            if sc_pct:
                lines.append(f"🧾 + SC {sc_pct:.0f}%: {symbol}{sc_amount:,.0f}")
            lines.append(f"💰 *Total: {symbol}{grand:,.0f}* ({mode_label})")
            if vat_pct:
                lines.append(f"    ↳ VAT {vat_pct:.0f}%: {symbol}{vat_amount:,.0f}")

        elif mode == "both_exclusive":
            sc_amount = subtotal * sc_pct / 100
            vat_amount = (subtotal + sc_amount) * vat_pct / 100
            lines.append(f"💰 Subtotal: {symbol}{subtotal:,.0f}")
            if sc_pct:
                lines.append(f"🧾 + SC {sc_pct:.0f}%: {symbol}{sc_amount:,.0f}")
            if vat_pct:
                lines.append(f"🧾 + VAT {vat_pct:.0f}%: {symbol}{vat_amount:,.0f}")
            lines.append(f"💰 *Grand Total: {symbol}{grand:,.0f}*")
    else:
        lines.append(f"💰 *Total: {symbol}{subtotal:,.0f}*")

    if bill["currency"] == "JPY" and rate:
        thb_grand = grand * rate
        lines.append(f"🔄 Rate: ¥1 = ฿{rate:.4f}")
        lines.append(f"💰 Total (THB): *฿{thb_grand:,.2f}*")

    lines.append(f"👥 Members: {len(bill['members'])}")
    lines.append("━━━━━━━━━━━━━━━━━━━━\n")

    for uid_str, name in bill["members"].items():
        uid = int(uid_str)
        person_items = []
        for item in bill["items"]:
            for claim in item["claimed_by"]:
                if str(claim["user_id"]) == uid_str:
                    share = item_per_person(item)
                    n_people = len(item["claimed_by"])
                    suffix = f" ÷{n_people}" if n_people > 1 else ""
                    person_items.append(f"    • {item['name']}: {symbol}{share:,.0f}{suffix}")
                    break

        p_sub, p_sc, p_vat, p_total = person_fee_breakdown(bill, uid)
        lines.append(f"👤 *{name}*")

        if person_items:
            lines.append("\n".join(person_items))
            if mode and (sc_pct or vat_pct):
                if mode == "both_inclusive":
                    lines.append(f"    Items: {symbol}{p_sub:,.0f}")
                    if sc_pct:
                        lines.append(f"    _(includes SC: {symbol}{p_sc:,.0f})_")
                    if vat_pct:
                        lines.append(f"    _(includes VAT: {symbol}{p_vat:,.0f})_")
                    lines.append(f"    → *Pay: {symbol}{p_total:,.0f}*")
                elif mode == "sc_exclusive_vat_inclusive":
                    lines.append(f"    Items: {symbol}{p_sub:,.0f}")
                    if sc_pct:
                        lines.append(f"    + SC {sc_pct:.0f}%: {symbol}{p_sc:,.0f}")
                    if vat_pct:
                        lines.append(f"    _(includes VAT: {symbol}{p_vat:,.0f})_")
                    lines.append(f"    → *Pay: {symbol}{p_total:,.0f}*")
                elif mode == "both_exclusive":
                    lines.append(f"    Items: {symbol}{p_sub:,.0f}")
                    if sc_pct:
                        lines.append(f"    + SC {sc_pct:.0f}%: {symbol}{p_sc:,.0f}")
                    if vat_pct:
                        lines.append(f"    + VAT {vat_pct:.0f}%: {symbol}{p_vat:,.0f}")
                    lines.append(f"    → *Pay: {symbol}{p_total:,.0f}*")
            else:
                lines.append(f"    → *Pay: {symbol}{p_sub:,.0f}*")
            if bill["currency"] == "JPY" and rate:
                thb_amount = p_total * rate
                lines.append(f"    _(≈ ฿{thb_amount:,.2f})_")
        else:
            lines.append("    _No items picked_")
        lines.append("")

    unclaimed = [i for i in bill["items"] if not i["claimed_by"]]
    if unclaimed:
        lines.append("⚠️ *Unclaimed items:*")
        for item in unclaimed:
            lines.append(f"    • #{item['id']} {item['name']}: {symbol}{item['price']:,.0f}")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("Please transfer your share 🙏")

    return "\n".join(lines)


# --- Command Handlers ---

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🧾 *Bill Splitter Bot* — Help\n\n"
        "*Start a bill:*\n"
        "/newbill — Start a new bill in this chat\n"
        "/join — Join the current bill\n\n"
        "*Add items:*\n"
        "Upload a receipt photo, or:\n"
        "/additem `name price` — Add item manually\n"
        "  _Example:_ `/additem Pad Thai 150`\n\n"
        "*Pick items:*\n"
        "/items — Show items with pick buttons\n"
        "/pick `number` — Pick an item for yourself\n"
        "/unpick `number` — Remove yourself from item\n"
        "/resetpicks — Clear all your picks\n"
        "/assign `number @user` — Assign item to someone\n\n"
        "*Finish:*\n"
        "/done — Finalize and show summary\n"
        "/cancel — Cancel current bill\n"
        "/history — View past bills\n"
        "/setfees `sc vat mode` — Set fees\n"
        "  _Modes:_ `both_inc` / `sc_exc` / `both_exc`\n"
        "  _Example:_ `/setfees 10 7 both_exc`\n\n"
        "*Currencies:* THB 🇹🇭 and JPY 🇯🇵\n"
        "AI reads fees from receipt and asks for confirmation.\n"
        "Use /setfees to adjust anytime."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_newbill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user

    active = get_active_bill(chat_id)
    if active:
        await update.message.reply_text(
            "⚠️ There's already an active bill.\n"
            "Use /done to finalize or /cancel to start fresh."
        )
        return

    bill = new_bill_doc(chat_id, user.id, get_display_name(user))
    bills_col.insert_one(bill)

    await update.message.reply_text(
        f"🧾 *New Bill* started by {get_display_name(user)}!\n\n"
        "First, choose the currency:",
        parse_mode="Markdown",
        reply_markup=currency_keyboard(),
    )


async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    bill = get_active_bill(chat_id)

    if not bill:
        await update.message.reply_text("No active bill. Start one with /newbill")
        return

    uid_str = str(user.id)
    if uid_str in bill["members"]:
        await update.message.reply_text(f"{get_display_name(user)}, you're already in! 👍")
        return

    bill["members"][uid_str] = get_display_name(user)
    save_bill(bill)

    await update.message.reply_text(
        f"✅ *{get_display_name(user)}* joined the bill!\n"
        f"👥 Members: {len(bill['members'])}",
        parse_mode="Markdown",
    )


async def cmd_additem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    bill = get_active_bill(chat_id)

    if not bill:
        await update.message.reply_text("No active bill. Start one with /newbill")
        return

    text = re.sub(r'^/additem(@\w+)?\s*', '', update.message.text.strip())
    if not text:
        await update.message.reply_text("Usage: `/additem Pad Thai 150`", parse_mode="Markdown")
        return

    parts = text.rsplit(None, 1)
    if len(parts) < 2:
        await update.message.reply_text("Provide both name and price.\nExample: `/additem Pad Thai 150`", parse_mode="Markdown")
        return

    name = parts[0].strip()
    try:
        price = float(parts[1].replace(",", ""))
        if price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Invalid price. Example: `/additem Pad Thai 150`", parse_mode="Markdown")
        return

    item = add_item_to_bill(bill, name, price)
    symbol = "¥" if bill["currency"] == "JPY" else "฿"
    await update.message.reply_text(
        f"✅ Added `#{item['id']}` *{item['name']}* — {symbol}{item['price']:,.0f}",
        parse_mode="Markdown",
    )


async def cmd_items(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    bill = get_active_bill(chat_id)

    if not bill:
        await update.message.reply_text("No active bill. Start one with /newbill")
        return
    if not bill["items"]:
        await update.message.reply_text("No items yet. Upload a receipt photo or use /additem")
        return

    text = format_items_list(bill) + "\n\n👇 Tap an item to claim it:"
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=items_keyboard(bill))


async def cmd_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    bill = get_active_bill(chat_id)

    if not bill:
        await update.message.reply_text("No active bill.")
        return

    uid_str = str(user.id)
    if uid_str not in bill["members"]:
        await update.message.reply_text("You need to /join the bill first!")
        return

    match = re.search(r'/pick(?:@\w+)?\s+(\d+)', update.message.text.strip())
    if not match:
        await update.message.reply_text("Usage: `/pick 1`", parse_mode="Markdown")
        return

    item = get_item(bill, int(match.group(1)))
    if not item:
        await update.message.reply_text(f"Item #{match.group(1)} not found.")
        return

    name = get_display_name(user)
    if any(str(c["user_id"]) == uid_str for c in item["claimed_by"]):
        await update.message.reply_text(f"You already picked #{item['id']}!")
        return

    item["claimed_by"].append({"user_id": user.id, "name": name})
    save_bill(bill)

    n = len(item["claimed_by"])
    symbol = "¥" if bill["currency"] == "JPY" else "฿"
    share_text = f" ({symbol}{item_per_person(item):,.0f} each)" if n > 1 else ""
    await update.message.reply_text(
        f"✅ *{name}* picked `#{item['id']}` {item['name']}{share_text}\n"
        f"  Shared by {n} {'people' if n > 1 else 'person'}",
        parse_mode="Markdown",
    )


async def cmd_unpick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    bill = get_active_bill(chat_id)

    if not bill:
        await update.message.reply_text("No active bill.")
        return

    match = re.search(r'/unpick(?:@\w+)?\s+(\d+)', update.message.text.strip())
    if not match:
        await update.message.reply_text("Usage: `/unpick 1`", parse_mode="Markdown")
        return

    item = get_item(bill, int(match.group(1)))
    if not item:
        await update.message.reply_text(f"Item #{match.group(1)} not found.")
        return

    uid_str = str(user.id)
    item["claimed_by"] = [c for c in item["claimed_by"] if str(c["user_id"]) != uid_str]
    save_bill(bill)
    await update.message.reply_text(f"🔄 Removed you from `#{item['id']}` {item['name']}", parse_mode="Markdown")


async def cmd_assign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    bill = get_active_bill(chat_id)

    if not bill:
        await update.message.reply_text("No active bill.")
        return
    if user.id != bill["creator_id"]:
        await update.message.reply_text("Only the bill creator can assign items.")
        return

    match = re.search(r'/assign(?:@\w+)?\s+(\d+)\s+@(\w+)', update.message.text.strip())
    if not match:
        await update.message.reply_text("Usage: `/assign 1 @username`", parse_mode="Markdown")
        return

    item = get_item(bill, int(match.group(1)))
    if not item:
        await update.message.reply_text(f"Item #{match.group(1)} not found.")
        return

    target_username = match.group(2)
    target_uid = None
    target_name = None
    for uid_str, name in bill["members"].items():
        if name.lower() == f"@{target_username}".lower():
            target_uid = uid_str
            target_name = name
            break

    if not target_uid:
        await update.message.reply_text(f"@{target_username} hasn't joined yet. They need to /join first.")
        return

    if any(str(c["user_id"]) == target_uid for c in item["claimed_by"]):
        await update.message.reply_text(f"{target_name} already has item #{item['id']}.")
        return

    item["claimed_by"].append({"user_id": int(target_uid), "name": target_name})
    save_bill(bill)
    await update.message.reply_text(
        f"✅ Assigned `#{item['id']}` {item['name']} → *{target_name}*",
        parse_mode="Markdown",
    )


async def cmd_resetpicks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    bill = get_active_bill(chat_id)

    if not bill:
        await update.message.reply_text("No active bill.")
        return

    uid_str = str(user.id)
    count = 0
    for item in bill["items"]:
        before = len(item["claimed_by"])
        item["claimed_by"] = [c for c in item["claimed_by"] if str(c["user_id"]) != uid_str]
        count += before - len(item["claimed_by"])

    save_bill(bill)

    if count > 0:
        await update.message.reply_text(
            f"🔄 Cleared all your picks ({count} items removed).\n"
            "Use /items to pick again.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text("You haven't picked any items yet.")


async def cmd_setfees(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    bill = get_active_bill(chat_id)

    if not bill:
        await update.message.reply_text("No active bill.")
        return

    text = re.sub(r'^/setfees(@\w+)?\s*', '', update.message.text.strip())
    if not text:
        sc = bill.get("service_charge_pct") or 0
        vat = bill.get("vat_pct") or 0
        mode = bill.get("fees_mode")
        mode_label = FEES_MODE_LABELS.get(mode, "not set")
        await update.message.reply_text(
            f"Current fees: SC {sc:.0f}% | VAT {vat:.0f}% ({mode_label})\n\n"
            "Usage: `/setfees 10 7 MODE`\n"
            "Modes:\n"
            "  `both_inc` — All inclusive (total = items)\n"
            "  `sc_exc` — SC on top, VAT included\n"
            "  `both_exc` — SC + VAT both on top\n"
            "To clear: `/setfees 0 0`",
            parse_mode="Markdown",
        )
        return

    parts = text.split()
    try:
        sc = float(parts[0])
        vat = float(parts[1]) if len(parts) > 1 else 0
        if sc < 0 or vat < 0:
            raise ValueError
    except (ValueError, IndexError):
        await update.message.reply_text(
            "Usage: `/setfees 10 7 both_exc`\n"
            "Modes: `both_inc` / `sc_exc` / `both_exc`",
            parse_mode="Markdown",
        )
        return

    # Parse mode flag
    fees_mode = None
    if len(parts) > 2:
        flag = parts[2].lower()
        mode_map = {
            "both_inc": "both_inclusive",
            "both_inclusive": "both_inclusive",
            "inclusive": "both_inclusive",
            "inc": "both_inclusive",
            "sc_exc": "sc_exclusive_vat_inclusive",
            "sc_exclusive_vat_inclusive": "sc_exclusive_vat_inclusive",
            "sc_exclusive": "sc_exclusive_vat_inclusive",
            "both_exc": "both_exclusive",
            "both_exclusive": "both_exclusive",
            "exclusive": "both_exclusive",
            "exc": "both_exclusive",
        }
        fees_mode = mode_map.get(flag)

    if fees_mode is None and (sc > 0 or vat > 0):
        # Show mode picker buttons
        await update.message.reply_text(
            f"SC {sc:.0f}% + VAT {vat:.0f}% — how are fees applied?",
            parse_mode="Markdown",
            reply_markup=fees_mode_keyboard(sc, vat),
        )
        return

    bill["service_charge_pct"] = sc
    bill["vat_pct"] = vat
    bill["fees_mode"] = fees_mode if (sc > 0 or vat > 0) else None
    bill["awaiting_fees"] = False
    save_bill(bill)

    symbol = "¥" if bill["currency"] == "JPY" else "฿"
    subtotal = bill_total(bill)
    mode_label = FEES_MODE_LABELS.get(fees_mode, "")
    text = f"✅ Fees set: SC {sc:.0f}% | VAT {vat:.0f}%"
    if mode_label:
        text += f" ({mode_label})"
    if subtotal > 0:
        text += f"\n💰 Total: {symbol}{bill_grand_total(bill):,.0f}"
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    bill = get_active_bill(chat_id)

    if not bill:
        await update.message.reply_text("No active bill.")
        return
    if not bill["items"]:
        await update.message.reply_text("No items in the bill yet!")
        return

    bill["is_finalized"] = True
    bill["finalized_at"] = datetime.now(timezone.utc)
    save_bill(bill)
    await update.message.reply_text(format_summary(bill), parse_mode="Markdown")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    bill = get_active_bill(chat_id)

    if not bill:
        await update.message.reply_text("No active bill to cancel.")
        return
    if user.id != bill["creator_id"]:
        await update.message.reply_text("Only the bill creator can cancel.")
        return

    bills_col.delete_one({"_id": bill["_id"]})
    await update.message.reply_text("🗑️ Bill cancelled.")


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    past_bills = list(
        bills_col.find({"chat_id": chat_id, "is_finalized": True})
        .sort("created_at", -1)
        .limit(5)
    )

    if not past_bills:
        await update.message.reply_text("No past bills found.")
        return

    lines = ["📜 *Recent Bills*\n"]
    for b in past_bills:
        symbol = "¥" if b["currency"] == "JPY" else "฿"
        total = bill_total(b)
        created = b["created_at"]
        date_str = created.strftime("%Y-%m-%d %H:%M") if isinstance(created, datetime) else str(created)
        members_count = len(b["members"])
        items_count = len(b["items"])
        lines.append(
            f"• {date_str} — {symbol}{total:,.0f} "
            f"({items_count} items, {members_count} people)"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# --- Callback Query Handler ---

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    user = update.effective_user
    bill = get_active_bill(chat_id)
    data = query.data

    if not bill:
        await query.edit_message_text("No active bill session.")
        return

    if data.startswith("currency:"):
        if user.id != bill["creator_id"]:
            await query.answer("Only the bill creator can set this.", show_alert=True)
            return

        currency = data.split(":")[1]
        bill["currency"] = currency

        if currency == "JPY":
            save_bill(bill)
            await query.edit_message_text(
                "🇯🇵 Currency set to *JPY*\n\n"
                "How do you want to set the JPY → THB conversion rate?",
                parse_mode="Markdown",
                reply_markup=rate_keyboard(),
            )
        else:
            save_bill(bill)
            await query.edit_message_text(
                "🇹🇭 Currency set to *THB*\n\n"
                "Now add items to the bill:",
                parse_mode="Markdown",
                reply_markup=input_method_keyboard(),
            )

    elif data == "rate:auto":
        rate = await fetch_jpy_to_thb_rate()
        if rate:
            bill["jpy_to_thb_rate"] = rate
            save_bill(bill)
            await query.edit_message_text(
                f"🔄 Rate fetched: *¥1 = ฿{rate:.4f}*\n\n"
                "Now add items to the bill:",
                parse_mode="Markdown",
                reply_markup=input_method_keyboard(),
            )
        else:
            bill["awaiting_manual_rate"] = True
            save_bill(bill)
            await query.edit_message_text(
                "❌ Failed to fetch rate. Please enter manually.\n"
                "Type the rate (e.g. `0.25` means ¥1 = ฿0.25):",
                parse_mode="Markdown",
            )

    elif data == "rate:manual":
        bill["awaiting_manual_rate"] = True
        save_bill(bill)
        await query.edit_message_text(
            "✏️ Enter the JPY → THB rate.\n"
            "Example: `0.25` means ¥1 = ฿0.25",
            parse_mode="Markdown",
        )

    elif data == "input:photo":
        bill["awaiting_photo"] = True
        save_bill(bill)
        await query.edit_message_text(
            "📸 Send me a photo of the receipt!\n\n"
            "I'll try to extract items and prices using OCR.\n"
            "You can also use /additem to add items manually."
        )

    elif data == "input:manual":
        await query.edit_message_text(
            "✏️ Add items using the command:\n"
            "`/additem Item Name 150`\n\n"
            "When done adding, use /items to see and pick items.\n"
            "Use /setfees to set service charge & VAT before finalizing.",
            parse_mode="Markdown",
        )

    elif data.startswith("fees:confirm:"):
        parts = data.split(":")
        sc = float(parts[2])
        vat = float(parts[3])
        mode = parts[4]  # "both_inclusive", "sc_exclusive_vat_inclusive", "both_exclusive"
        bill["service_charge_pct"] = sc
        bill["vat_pct"] = vat
        bill["fees_mode"] = mode
        bill["awaiting_fees"] = False
        save_bill(bill)
        symbol = "¥" if bill["currency"] == "JPY" else "฿"
        mode_label = FEES_MODE_LABELS.get(mode, mode)
        grand = bill_grand_total(bill)
        await query.edit_message_text(
            f"✅ Fees confirmed: SC {sc:.0f}% + VAT {vat:.0f}% ({mode_label})\n"
            f"💰 Total: {symbol}{grand:,.0f}\n\n"
            "Members: /join then /items to pick your items!",
            parse_mode="Markdown",
        )

    elif data.startswith("fees:pickmode:"):
        parts = data.split(":")
        sc = float(parts[2])
        vat = float(parts[3])
        await query.edit_message_text(
            f"SC {sc:.0f}% + VAT {vat:.0f}% — how are fees applied?",
            parse_mode="Markdown",
            reply_markup=fees_mode_keyboard(sc, vat),
        )

    elif data == "fees:edit":
        bill["awaiting_fees"] = True
        save_bill(bill)
        await query.edit_message_text(
            "✏️ Enter fees using the command:\n"
            "`/setfees 10 7 MODE`\n\n"
            "Modes: `both_inc` / `sc_exc` / `both_exc`\n"
            "Example: `/setfees 10 7 both_exc`\n"
            "Or `/setfees 0 0` for no fees.",
            parse_mode="Markdown",
        )

    elif data == "fees:none":
        bill["service_charge_pct"] = 0
        bill["vat_pct"] = 0
        bill["fees_mode"] = None
        bill["awaiting_fees"] = False
        save_bill(bill)
        symbol = "¥" if bill["currency"] == "JPY" else "฿"
        await query.edit_message_text(
            f"✅ No fees applied.\n"
            f"💰 Total: {symbol}{bill_total(bill):,.0f}\n\n"
            "Members: /join then /items to pick your items!",
            parse_mode="Markdown",
        )

    elif data.startswith("pick:"):
        item_id = int(data.split(":")[1])
        item = get_item(bill, item_id)
        if not item:
            await query.answer("Item not found.", show_alert=True)
            return

        uid_str = str(user.id)
        if uid_str not in bill["members"]:
            bill["members"][uid_str] = get_display_name(user)

        name = get_display_name(user)
        already = any(str(c["user_id"]) == uid_str for c in item["claimed_by"])
        if already:
            item["claimed_by"] = [c for c in item["claimed_by"] if str(c["user_id"]) != uid_str]
            await query.answer(f"Removed from #{item['id']}")
        else:
            item["claimed_by"].append({"user_id": user.id, "name": name})
            await query.answer(f"Picked #{item['id']} {item['name']}!")

        save_bill(bill)

        text = format_items_list(bill) + "\n\n👇 Tap an item to claim/unclaim:"
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=items_keyboard(bill))
        except Exception:
            pass

    elif data == "finalize":
        if not bill["items"]:
            await query.answer("No items to finalize!", show_alert=True)
            return

        bill["is_finalized"] = True
        bill["finalized_at"] = datetime.now(timezone.utc)
        save_bill(bill)
        await query.edit_message_text(format_summary(bill), parse_mode="Markdown")


# --- Photo Handler (OCR) ---

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    bill = get_active_bill(chat_id)
    if not bill:
        return

    bill["awaiting_photo"] = False
    save_bill(bill)

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    photo_bytes = await file.download_as_bytearray()

    msg = await update.message.reply_text("🔍 Reading receipt with Gemini AI...")
    items, sc_pct, vat_pct, fees_mode = await parse_receipt_ocr(bytes(photo_bytes), bill["currency"])

    if not items:
        await msg.edit_text(
            "😕 Couldn't extract items from this receipt.\n\n"
            "Try a clearer photo or add items manually:\n"
            "`/additem Item Name 150`",
            parse_mode="Markdown",
        )
        return

    detected_sc = sc_pct if sc_pct is not None else None
    detected_vat = vat_pct if vat_pct is not None else None

    symbol = "¥" if bill["currency"] == "JPY" else "฿"
    added_lines = []
    for name, price in items:
        item = add_item_to_bill(bill, name, price)
        added_lines.append(f"`#{item['id']}` {item['name']} — {symbol}{price:,.0f}")

    total = sum(p for _, p in items)
    text = (
        f"✅ Extracted *{len(items)}* items from receipt:\n\n"
        + "\n".join(added_lines)
        + f"\n\n💰 Subtotal: {symbol}{total:,.0f}"
    )

    # Validate fees_mode
    valid_modes = ("both_inclusive", "sc_exclusive_vat_inclusive", "both_exclusive")
    if fees_mode not in valid_modes:
        fees_mode = None

    # Ask for fee confirmation
    if detected_sc is not None or detected_vat is not None:
        sc_val = detected_sc or 0
        vat_val = detected_vat or 0
        mode = fees_mode or "both_inclusive"  # default guess
        mode_label = FEES_MODE_LABELS.get(mode, "")
        text += f"\n\n🧾 Detected: SC {sc_val:.0f}% | VAT {vat_val:.0f}% — {mode_label}"
        text += "\nPlease confirm or edit:"
        save_bill(bill)
        await msg.edit_text(
            text,
            parse_mode="Markdown",
            reply_markup=fees_confirm_keyboard(sc_val, vat_val, mode),
        )
    else:
        # Nothing detected — suggest common rates based on currency
        if bill["currency"] == "THB":
            text += "\n\n🧾 No fees detected on receipt."
            text += "\nDoes this bill have service charge & VAT?"
            save_bill(bill)
            await msg.edit_text(
                text,
                parse_mode="Markdown",
                reply_markup=fees_confirm_keyboard(10, 7, "both_inclusive"),
            )
        else:
            text += "\n\n🧾 No fees detected on receipt."
            text += "\nDoes this bill have any fees?"
            save_bill(bill)
            await msg.edit_text(
                text,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✏️ Enter fees manually", callback_data="fees:edit")],
                    [InlineKeyboardButton("❌ No fees on this bill", callback_data="fees:none")],
                ]),
            )


# --- Text Handler (manual rate) ---

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    bill = get_active_bill(chat_id)
    if not bill or not bill.get("awaiting_manual_rate"):
        return

    text = update.message.text.strip().replace(",", "")
    try:
        rate = float(text)
        if rate <= 0:
            raise ValueError
        bill["jpy_to_thb_rate"] = rate
        bill["awaiting_manual_rate"] = False
        save_bill(bill)
        await update.message.reply_text(
            f"✅ Rate set: *¥1 = ฿{rate:.4f}*\n\n"
            "Now add items to the bill:",
            parse_mode="Markdown",
            reply_markup=input_method_keyboard(),
        )
    except ValueError:
        await update.message.reply_text("Please enter a valid number (e.g. `0.25`)", parse_mode="Markdown")


# --- Main ---

def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("=" * 50)
        print("ERROR: Set your BOT_TOKEN!")
        print("  export BOT_TOKEN=your_token_here")
        print("=" * 50)
        return

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("newbill", cmd_newbill))
    app.add_handler(CommandHandler("join", cmd_join))
    app.add_handler(CommandHandler("additem", cmd_additem))
    app.add_handler(CommandHandler("items", cmd_items))
    app.add_handler(CommandHandler("pick", cmd_pick))
    app.add_handler(CommandHandler("unpick", cmd_unpick))
    app.add_handler(CommandHandler("assign", cmd_assign))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("setfees", cmd_setfees))
    app.add_handler(CommandHandler("resetpicks", cmd_resetpicks))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    logger.info("Bill Splitter Bot running (MongoDB: %s/%s)", MONGO_URI, MONGO_DB)

    # Webhook mode for Cloud Run, polling for local dev
    WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")  # e.g. https://your-service-xxx.run.app
    PORT = int(os.environ.get("PORT", "8080"))

    if WEBHOOK_URL:
        logger.info("Starting in WEBHOOK mode on port %d", PORT)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=f"/webhook/{BOT_TOKEN}",
            webhook_url=f"{WEBHOOK_URL}/webhook/{BOT_TOKEN}",
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        logger.info("Starting in POLLING mode")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()