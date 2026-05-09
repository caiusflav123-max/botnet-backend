import asyncio
import time
import random
import string
import requests
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import html
import re
import sqlite3
import threading

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters

# ==============================
# HELPERS
# ==============================

def safe_bold(text):
    if text is None:
        return "<b>—</b>"
    return f"<b>{html.escape(str(text))}</b>"

def fix_html(text):
    text = re.sub(r'<b><b>', '<b>', text)
    text = re.sub(r'</b></b>', '</b>', text)
    return text

# ==============================
# DATABASE SETUP 
# ==============================

db_lock = threading.Lock()
conn = sqlite3.connect("bot_data.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS premium_users (
    user_id INTEGER PRIMARY KEY,
    expiry TEXT,
    notified INTEGER DEFAULT 0
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS command_status (
    command TEXT PRIMARY KEY,
    status TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    name TEXT,
    username TEXT,
    total_checks INTEGER DEFAULT 0,
    premium INTEGER DEFAULT 0
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS group_credits_db (
    chat_id INTEGER PRIMARY KEY,
    credits INTEGER DEFAULT 0,
    is_free_group INTEGER DEFAULT 0
)
""")

conn.commit()

# STORAGE & LIMITS 

user_chats = set()
group_chats = set()
group_credits = defaultdict(int)   
free_groups = set()               
cooldowns = {}

daily_lookup_usage = defaultdict(int)
daily_bind_usage = defaultdict(int)

free_user_bind_count = defaultdict(int)
free_user_lookup_count = defaultdict(int)

group_daily_lookup_usage = defaultdict(int)
group_daily_bind_usage = defaultdict(int)
GROUP_DAILY_LOOKUP_LIMIT = 10
GROUP_DAILY_BIND_LIMIT = 10

last_reset = datetime.now(timezone.utc)

LOOKUP_FREE_LIMIT = 2
BIND_FREE_LIMIT = 3

spam_tracker = defaultdict(list)
BLOCK_DURATION = 5 * 60
blocked_users = set()

# ==============================
# LOAD PERSISTED GROUP CREDITS
# ==============================

_rows = cursor.execute("SELECT chat_id, credits, is_free_group FROM group_credits_db").fetchall()
for _chat_id, _credits, _is_free in _rows:
    group_credits[_chat_id] = _credits
    if _is_free:
        free_groups.add(_chat_id)

# ==============================
# CONFIGURATION
# ==============================

BOT_TOKEN = "8348428937:AAHuZCDqorDqk6-B26RmOybt9UPcsPj6kcE"
OWNER_ID = 8651510445
API_BASE_URL = "https://checkton.online/backend/info"
API_KEY = "U6RzpkZGy0Y5Ue-QXkARcbyPXyriYgJFjw2ywMDWDOs"
ADMIN_USERNAME = "@Official_Caius1"
START_PHOTO_URL = "https://i.postimg.cc/5yqWVYBj/6327940205746261899.jpg"
CN31_GENERATOR_URL = "https://veritoncn31captchagen.netlify.app"

# ==============================
# DB HELPER 
# ==============================

def db_execute(query, params=()):
    with db_lock:
        cursor.execute(query, params)
        conn.commit()

def db_fetchone(query, params=()):
    with db_lock:
        cursor.execute(query, params)
        return cursor.fetchone()

def db_fetchall(query, params=()):
    with db_lock:
        cursor.execute(query, params)
        return cursor.fetchall()

def save_group_credits(chat_id):
    """Persist a group's credits to the database."""
    credits = group_credits.get(chat_id, 0)
    is_free = 1 if chat_id in free_groups else 0
    db_execute(
        "INSERT OR REPLACE INTO group_credits_db (chat_id, credits, is_free_group) VALUES (?, ?, ?)",
        (chat_id, credits, is_free)
    )

# ==============================
# FUNCTIONS
# ==============================

def check_cooldown(user_id):
    if user_id == OWNER_ID:
        return 0

    now = time.time()

    if user_id in cooldowns:
        remaining = cooldowns[user_id] - now
        if remaining > 0:
            return int(remaining) + 1
        else:
            del cooldowns[user_id]

    return 0


def set_cooldown(user_id):
    if user_id == OWNER_ID:
        return

    cd = random.choice([
        random.randint(30, 45),
        random.randint(45, 75),
        random.randint(60, 90),
        random.randint(90, 120),
        120
    ])
    cooldowns[user_id] = time.time() + cd


def has_active_subscription(user_id=None, chat_id=None):
    if user_id is None:
        return False

    row = db_fetchone(
        "SELECT expiry FROM premium_users WHERE user_id=?",
        (user_id,)
    )

    if row:
        expiry = datetime.fromisoformat(row[0])
        if expiry > datetime.now(timezone.utc):
            return True

    return False


def reset_daily_usage():
    global last_reset, daily_lookup_usage, daily_bind_usage

    now = datetime.now(timezone.utc)

    if now.date() != last_reset.date():
        daily_lookup_usage.clear()
        daily_bind_usage.clear()
        free_user_bind_count.clear()
        free_user_lookup_count.clear()
        group_daily_lookup_usage.clear()
        group_daily_bind_usage.clear()
        last_reset = now


def buy_button():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Buy Subscription", url="https://t.me/Official_Caius")]
    ])


def admin_button():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "👤 Contact Admin",
            url=f"https://t.me/{ADMIN_USERNAME.replace('@', '')}"
        )]
    ])


def check_lookup_free_usage(user_id):
    daily_lookup_usage.setdefault(user_id, 0)

    if daily_lookup_usage[user_id] < LOOKUP_FREE_LIMIT:
        daily_lookup_usage[user_id] += 1
        free_user_lookup_count[user_id] = free_user_lookup_count.get(user_id, 0) + 1
        remaining = LOOKUP_FREE_LIMIT - daily_lookup_usage[user_id]
        return True, remaining

    return False, 0


def check_bind_free_usage(user_id):
    daily_bind_usage.setdefault(user_id, 0)

    if daily_bind_usage[user_id] < BIND_FREE_LIMIT:
        daily_bind_usage[user_id] += 1
        free_user_bind_count[user_id] = free_user_bind_count.get(user_id, 0) + 1
        remaining = BIND_FREE_LIMIT - daily_bind_usage[user_id]
        return True, remaining

    return False, 0


def should_trigger_cooldown_bind(user_id):
    return free_user_bind_count.get(user_id, 0) >= 3


def should_trigger_cooldown_lookup(user_id):
    return free_user_lookup_count.get(user_id, 0) >= 2


def check_group_daily_lookup(chat_id):
    used = group_daily_lookup_usage.get(chat_id, 0)
    if used < GROUP_DAILY_LOOKUP_LIMIT:
        group_daily_lookup_usage[chat_id] = used + 1
        return True, GROUP_DAILY_LOOKUP_LIMIT - used - 1
    return False, 0


def check_group_daily_bind(chat_id):
    used = group_daily_bind_usage.get(chat_id, 0)
    if used < GROUP_DAILY_BIND_LIMIT:
        group_daily_bind_usage[chat_id] = used + 1
        return True, GROUP_DAILY_BIND_LIMIT - used - 1
    return False, 0


def check_spam(user_id):
    if user_id == OWNER_ID:
        return False

    if has_active_subscription(user_id=user_id):
        return False

    now = time.time()
    spam_tracker.setdefault(user_id, [])

    spam_tracker[user_id] = [
        t for t in spam_tracker[user_id]
        if now - t < BLOCK_DURATION
    ]

    spam_tracker[user_id].append(now)

    if len(spam_tracker[user_id]) >= 5:
        blocked_users.add(user_id)
        return True

    return False


def unblock_user(user_id):
    blocked_users.discard(user_id)
    spam_tracker.pop(user_id, None)


def check_private_chat(update: Update):
    user_id = update.effective_user.id
    chat_type = update.effective_chat.type

    if user_id == OWNER_ID:
        return True, None

    if has_active_subscription(user_id=user_id):
        return True, None

    if chat_type == "private":
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🤖 Join Group", url="https://t.me/veritonofficialgroup")]
        ])
        return False, markup

    return True, None


def fetch_api_data(roleid, zoneid, type_):
    try:
        payload = {
            "role_id": roleid,
            "zone_id": zoneid,
            "type": type_
        }

        r = requests.post(
            API_BASE_URL,
            json=payload,
            headers={"x-api-key": API_KEY, "Content-Type": "application/json"},
            timeout=15
        )

        if r.status_code != 200:
            return None, f"❌ API Error: {r.status_code}\n{r.text}"

        resp_json = r.json()

        if resp_json.get("status") == -1 or resp_json.get("data") is None:
            credits_exhausted = resp_json.get("message", "").lower()
            if "credit" in credits_exhausted or "exhaust" in credits_exhausted or resp_json.get("status") == -2:
                return None, "CREDITS_EXHAUSTED"
            return None, "❌ Failed to fetch data information!"

        return resp_json.get("data"), None

    except requests.exceptions.RequestException as e:
        return None, f"❌ Request failed: {str(e)}"
    except Exception as e:
        return None, str(e)


def get_command_status(command):
    row = db_fetchone(
        "SELECT status FROM command_status WHERE command=?",
        (command,)
    )
    return row[0] if row else None

# ==============================
# CENTRAL ACCESS CHECK
# ==============================

async def can_use_bot(user_id, chat_id, msg):

    if user_id in blocked_users:
        if msg:
            await msg.reply_text("🚫 You are blocked from using this bot.")
        return False

    if chat_id < 0:
        if group_credits.get(chat_id, 0) <= 0:
            if msg:
                await msg.reply_text(
                    "❌ <b>GROUP CREDITS EXHAUSTED</b>\n\n"
                    "This group has no remaining credits to use the bot.\n"
                    "<b>What can I do?</b>\n"
                    "- Contact the owner for your credits\n"
                    "<i>Note: Per request, bind or lookup automatically deducts 1 credit!</i>",
                    parse_mode="HTML",
                    reply_markup=admin_button()
                )
            return False
        return True

    return True


# ==========================
# GROUP CREDIT HELPERS
# ==========================

def check_group_credits(chat_id):
    return group_credits.get(chat_id, 0) > 0


def deduct_credit(chat_id):
    if group_credits.get(chat_id, 0) > 0:
        group_credits[chat_id] -= 1
        save_group_credits(chat_id)

# ==============================
# START COMMAND
# ==============================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type

    if chat_type == "private":
        user_chats.add(chat_id)
    else:
        group_chats.add(chat_id)

    username = update.effective_user.first_name

    keyboard = [
        [
            InlineKeyboardButton("🔧 CN31 Captcha Generator", url=CN31_GENERATOR_URL)
        ],
        [
            InlineKeyboardButton("💰 View Pricing", callback_data="pricing"),
            InlineKeyboardButton("🤖 Ask AI", callback_data="ai")
        ],
        [
            InlineKeyboardButton("❓ Help & Commands", callback_data="help"),
            InlineKeyboardButton("💎 Check my Premium", callback_data="premium"),
            InlineKeyboardButton("👤 Contact Admin", url=f"https://t.me/{ADMIN_USERNAME.replace('@', '')}")
        ]
    ]

    text = f"""
👋 <b>Welcome to MLBB Veriton Checker Bot {username}!</b>

🔥 <b>The Super Ultimation Checker Veriton's</b>

⚡<b>What I can do?</b>
🎮 Match Performance - Recent results, KDA, MVP 
💎 Skins &amp; Cosmetics - Owned skins &amp; collection 
📊 Popularity &amp; Social - Followers, squad, affinity 
📍 Location &amp; Insights - Logins &amp; device tracking 
🤖 AI Assistance - Ask the ai whatever you want, based on MLBB (Maintenance)

💬 <b>Contact admin for your subscription and support</b>! 📩
"""

    if update.callback_query:
        query = update.callback_query
        await query.answer()

        try:
            await query.message.edit_caption(
                caption=text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
        except Exception:
            await query.message.reply_photo(
                photo=START_PHOTO_URL,
                caption=text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )

    else:
        await update.message.reply_photo(
            photo=START_PHOTO_URL,
            caption=text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )

async def setto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    if len(context.args) < 2:
        await update.message.reply_text("❌ Usage: /setto <command|all> <status>")
        return

    command = context.args[0].lower()
    status = " ".join(context.args[1:]).lower()

    commands_list = ["lookup", "bind"]

    if status == "off":
        if command == "all":
            db_execute("DELETE FROM command_status")
            await update.message.reply_text(
                "✅ All commands are now <b>ACTIVE</b>",
                parse_mode="HTML"
            )
            return

        if command not in commands_list:
            await update.message.reply_text("❌ Invalid command.")
            return

        db_execute("DELETE FROM command_status WHERE command=?", (command,))
        await update.message.reply_text(
            f"✅ Command <b>/{command}</b> is now <b>ACTIVE</b>",
            parse_mode="HTML"
        )
        return

    if command == "all":
        for cmd in commands_list:
            db_execute(
                "INSERT OR REPLACE INTO command_status (command, status) VALUES (?, ?)",
                (cmd, status)
            )
        await update.message.reply_text(
            f"🚧 All commands set to <b>{status}</b>",
            parse_mode="HTML"
        )
        return

    if command not in commands_list:
        await update.message.reply_text("❌ Invalid command.")
        return

    db_execute(
        "INSERT OR REPLACE INTO command_status (command, status) VALUES (?, ?)",
        (command, status)
    )
    await update.message.reply_text(
        f"🚧 Command <b>/{command}</b> set to <b>{status}</b>",
        parse_mode="HTML"
    )

# BLOCK COMMAND
async def block(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    if len(context.args) < 2:
        await update.message.reply_text("❌ Usage: /block <user_id> <reason>")
        return

    try:
        uid = int(context.args[0])
        reason = " ".join(context.args[1:])

        blocked_users.add(uid)

        try:
            await context.bot.send_message(
                chat_id=uid,
                text=(
                    "🚫 <b>YOUR ACCOUNT BLOCKED</b>\n\n"
                    f"⛔ Reason: {reason}\n\n"
                    "⚠️ You cannot use lookup and bind commands.\n"
                    "📩 Contact admin if you think this is a mistake."
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass

        await update.message.reply_text(
            f"🚫 User blocked successfully!\n\n"
            f"🆔 ID: <code>{uid}</code>\n"
            f"⛔ Reason: <b>{reason}</b>",
            parse_mode="HTML"
        )

    except Exception:
        await update.message.reply_text("❌ Invalid format. Usage: /block <user_id> <reason>")

# UNBLOCK COMMAND
async def unblock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    if len(context.args) < 1:
        await update.message.reply_text("❌ Usage: /unblock <user_id>")
        return

    try:
        uid = int(context.args[0])
        unblock_user(uid)

        await update.message.reply_text(
            f"✅ User unblocked!\n\n🆔 ID: <code>{uid}</code>",
            parse_mode="HTML"
        )
    except Exception:
        await update.message.reply_text("❌ Invalid format.")

# ==============================
# CHECK ACTIVE SUBSCRIPTIONS
# ==============================

async def checksubs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    rows = db_fetchall("SELECT user_id, expiry FROM premium_users")

    text = "💎 <b>ACTIVE SUBSCRIPTION</b>\n\n"

    if rows:
        for uid, exp in rows:
            try:
                user = await context.bot.get_chat(uid)
                username = f"@{user.username}" if user.username else "No Username"
                fullname = f"{user.first_name or ''} {user.last_name or ''}".strip()
            except Exception:
                username = "Unknown"
                fullname = "Unknown User"

            text += (
                f"👤 <b>PREMIUM USER</b>\n"
                f"• ID: <code>{uid}</code>\n"
                f"• Name: <b>{fullname}</b>\n"
                f"• Username: {username}\n"
                f"• Expiry: <b>{exp}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
            )
    else:
        text += "❌ No active premium users\n\n"

    text += f"📊 <b>Total Active:</b> {len(rows)}"

    active_groups = {cid: credits for cid, credits in group_credits.items() if credits > 0}

    text += "\n\n━━━━━━━━━━━━━━━━━━━━\n"
    text += "👥 <b>GROUPS CREDITS</b>\n\n"

    if active_groups:
        for cid, credits in active_groups.items():
            mode = "🆓 Free Group" if cid in free_groups else "🔵 Group Mode"
            try:
                chat = await context.bot.get_chat(cid)
                group_name = chat.title or "Unknown Group"
            except Exception:
                group_name = "Unknown Group"

            text += (
                f"👥 <b>{html.escape(group_name)}</b>\n"
                f"• ID: <code>{cid}</code>\n"
                f"• Credits: <b>{credits}</b>\n"
                f"• Mode: {mode}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
            )
    else:
        text += "❌ No groups with credits\n\n"

    text += f"📊 <b>Total Groups:</b> {len(active_groups)}"

    await update.message.reply_text(text, parse_mode="HTML")

# CHECKUSERS
async def checkusers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    rows = db_fetchall("SELECT name, username, total_checks, premium FROM users")

    if not rows:
        await update.message.reply_text("❌ No users found.")
        return

    text = "👥 <b>BOT USERS LIST</b>\n\n"

    for name, username, total, premium in rows:
        text += (
            f"👤 Name: <b>{html.escape(str(name))}</b>\n"
            f"📛 Username: @{username}\n"
            f"📊 Total: <b>{total}</b>\n"
            f"💎 Premium: {'Yes' if premium else 'No'}\n"
            f"━━━━━━━━━━━━━━\n"
        )

    await update.message.reply_text(text, parse_mode="HTML")

# ==============================
# TRANSFER CREDITS COMMAND
# ==============================

async def transfer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ You are not authorized.")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Invalid Format. Use:\n"
            "/transfer &lt;chat_id&gt; &lt;amount&gt;\n"
            "/transfer &lt;chat_id&gt; &lt;amount&gt; Free Group\n"
            "/transfer &lt;chat_id&gt; &lt;amount&gt; Group Mode",
            parse_mode="HTML"
        )
        return

    try:
        target = int(context.args[0])
        amount = int(context.args[1])

        role = " ".join(context.args[2:]).strip() if len(context.args) > 2 else "Group Mode"
        is_free_group = role.lower() == "free group"

        if is_free_group:
            free_groups.add(target)
        else:
            free_groups.discard(target)

        if target not in group_credits:
            group_credits[target] = 0

        group_credits[target] += amount
        save_group_credits(target)

        role_label = "🆓 Free Group" if is_free_group else "🔵 Group Mode"

        await update.message.reply_text(
            f"✅ Credits added successfully!\n\n"
            f"👥 Group ID: <code>{target}</code>\n"
            f"➕ Amount: <b>{amount}</b>\n"
            f"💰 Total Credits: <b>{group_credits[target]}</b>\n"
            f"🏷️ Mode: {role_label}",
            parse_mode="HTML"
        )

        try:
            await context.bot.send_message(
                chat_id=target,
                text=(
                    f"💰 <b>CREDITS TRANSFER CONFIRMED</b>\n\n"
                    f"➕ Amount: <code>{amount}</code>\n"
                    f"🏷️ Mode: {role_label}\n"
                    f"📊 Group credits have been added to your Group!\n\n"
                    f"🚀 You can now use /lookup and /bind again!"
                ),
                parse_mode="HTML"
            )
        except Exception as e:
            print("Failed to send message:", e)

    except Exception:
        await update.message.reply_text("❌ Invalid format.\nUse: /transfer &lt;chat_id&gt; &lt;amount&gt;", parse_mode="HTML")

# REMOVE GROUP CREDITS
async def removecredits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Only the owner can use this command.")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Usage:\n<code>/removecredits &lt;group_id&gt; &lt;amount&gt;</code>",
            parse_mode="HTML"
        )
        return

    try:
        group_id = int(context.args[0])
        amount = int(context.args[1])

        if amount <= 0:
            await update.message.reply_text("❌ Amount must be greater than 0.")
            return

        current = group_credits.get(group_id, 0)

        if current <= 0:
            await update.message.reply_text("❌ This group has no credits.")
            return

        new_balance = max(0, current - amount)
        group_credits[group_id] = new_balance
        save_group_credits(group_id)

        await update.message.reply_text(
            f"✅ Credits removed successfully!\n\n"
            f"👥 Group ID: <code>{group_id}</code>\n"
            f"➖ Removed: <b>{amount}</b>\n"
            f"💰 Remaining: <b>{new_balance}</b>",
            parse_mode="HTML"
        )

    except ValueError:
        await update.message.reply_text("❌ Invalid input. Use numbers only.")

# ==============================
# PRICING COMMAND
# ==============================

async def pricing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("‼️ Help & Commands", callback_data="help"),
            InlineKeyboardButton("💎 Check my Premium", callback_data="premium"),
            InlineKeyboardButton("🤖 Ask AI", callback_data="ai")
        ],
        [
            InlineKeyboardButton("👤 Contact Admin", url=f"https://t.me/{ADMIN_USERNAME.replace('@', '')}")
        ]
    ]

    text = """
💰 <b>Veriton Checker Bot Subscription Pricing</b>   

📆 <b>Available Plans Type:</b>

🔹 <b>1st Plan</b>
• Duration: <b>No Days Duration</b>
• Lookups: <b>Unlimited Checks</b>
• Binds: <b>Unlimited Checks</b>
• Price: <b>₱55</b>
• Amount Transfer: <b>400 Credits</b>

🔹 <b>2nd Plan</b>
• Duration: <b>No Days Duration</b>
• Lookups: <b>Unlimited Checks</b>
• Binds: <b>Unlimited Checks</b>
• Price: <b>₱189</b>
• Amount Transfer: <b>1099 Credits</b>

🔹 <b>3rd Plan</b> ✨
• Duration: <b>No Days Duration</b>
• Lookups: <b>Unlimited Checks</b>
• Binds: <b>Unlimited Checks</b>
• Price: <b>₱279</b>
• Amount Transfer: <b>1,506 Credits</b>

🔹 <b>4th Plan</b> 💰
• Duration: <b>No Days Duration</b>
• Lookups: <b>Unlimited Checks</b>
• Binds: <b>Unlimited Checks</b>
• Price: <b>₱469</b>
• Amount Transfer: <b>2,099 Credits</b>

🔹 <b>5th Plan</b> 💎 
• Duration: <b>No Days Duration</b>
• Lookups: <b>Unlimited Checks</b>
• Binds: <b>Unlimited Checks</b>
• Price: <b>₱655</b>
• Amount Transfer: <b>5,099 Credits</b>

🛒 <b>How To Order</b>?
 1. Add Bot First
 2. Make Admin with Permission
 3. Get your Group ID 
 4. Contact Owner 
 5. Pay your chosen Plan
 6. Tell the owner to process your order!

🌟 <b>Features:</b>
• Full MLBB player data access
• Hero / Skin count &amp; battle stats
• Device info detection
• Bind Information Checking
• Risk indicator

📌 <b>Notes:</b>
• <i>Per 1 requests bind or lookup are automatically deducted 1 Credits</i> 

💬 <b>Need Help?</b>
Contact admin for your Assistance!
"""

    if update.callback_query:
        q = update.callback_query
        try:
            if q.message.photo:
                await q.message.edit_caption(
                    caption=text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="HTML"
                )
            else:
                await q.message.edit_text(
                    text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="HTML"
                )
        except Exception:
            await q.message.reply_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
    else:
        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )

# ==============================
# HELP COMMAND
# ==============================

async def help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("💰 View Pricing", callback_data="pricing"),
            InlineKeyboardButton("💎 Check my Premium", callback_data="premium"),
            InlineKeyboardButton("🤖 Ask AI", callback_data="ai")
        ],
        [
            InlineKeyboardButton("👤 Contact Admin", url=f"https://t.me/{ADMIN_USERNAME.replace('@', '')}")
        ]
    ]

    text = """
❓ <b>Veriton Checker Bot - Help and Commands</b>

✨ <b>Available Commands:</b>
🔹 /start
- Show welcome message

🔹 /lookup &lt;role_id&gt; &lt;zone_id&gt;
- Lookup player information

🔹 /bind &lt;role_id&gt; &lt;zone_id&gt;
- Check connected platforms linked to the account

🔹 /pricing 
- View subscription prices

🔹 /myinfo
- View your account info, premium status, and daily usage

🔹 /help
- View available commands

🔹 /ask
- Ask AI whatever you want about MLBB

📌 <b>How to use the /lookup command:</b>
1. Open your MLBB account
2. Get your Role ID and Zone ID
3. Go to the group and try the command
4. Use: /lookup 123456789 1234 (example)

<b>Purpose:</b>
- Displays full account information.

📌 <b>How to use the /bind command:</b>
1. Open your MLBB account
2. Get your Role ID and Zone ID
3. Go to the group and try the command
4. Use: /bind 123456789 1234 (example)

<b>Purpose:</b>
- Shows linked platforms and devices connected to the account.

<b>Requirements:</b>
• Premium subscription required for unlimited usage
• Free users: 2 lookups/day and 3 binds/day
• Credits are automatically deducted when using lookup and bind commands

‼️ <b>Anti-Spam Protection</b>
• Automatically blocked for 5 minutes if spam is detected
• Prevents spam usage from other users
• Premium users are exempt from auto-block

💬 <b>Need more help?</b>
Contact the owner for Assistance.
"""

    if update.callback_query:
        q = update.callback_query
        try:
            if q.message.photo:
                await q.message.edit_caption(
                    caption=fix_html(text),
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="HTML"
                )
            else:
                await q.message.edit_text(
                    fix_html(text),
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="HTML"
                )
        except Exception:
            await q.message.reply_text(
                fix_html(text),
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
    else:
        await update.message.reply_text(
            fix_html(text),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )

# ==============================
# LOOKUP COMMAND
# ==============================

async def lookup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import json

    status = get_command_status("lookup")
    if status:
        username = update.effective_user.first_name
        await update.message.reply_text(
            f"<i>Hello {username}, Lookup is currently under {status}.</i>",
            parse_mode="HTML"
        )
        return

    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type
    user_id = update.effective_user.id

    if chat_type == "private":
        user_chats.add(chat_id)
    else:
        group_chats.add(chat_id)

    if not await can_use_bot(user_id, chat_id, update.message):
        return

    reset_daily_usage()

    if len(context.args) == 0:
        await update.message.reply_text(
            "❌ <b>Invalid Format Use:</b>\n"
            "/lookup &lt;role_id&gt; &lt;zone_id&gt; or\n"
            "/lookup &lt;role_id&gt; (zone_id)",
            parse_mode="HTML"
        )
        return

    raw_text = " ".join(context.args)
    numbers = re.findall(r"\d+", raw_text)

    roleid = numbers[0] if len(numbers) >= 1 else None
    zoneid = numbers[1] if len(numbers) >= 2 else None

    if not roleid:
        await update.message.reply_text("❌ Invalid Role ID.")
        return

    if not zoneid:
        await update.message.reply_text(
            "❌ <b>Zone ID is required Use:</b>\n"
            "/lookup &lt;role_id&gt; &lt;zone_id&gt; or\n"
            "/lookup &lt;role_id&gt; (zone_id)",
            parse_mode="HTML"
        )
        return

    is_premium_user = has_active_subscription(user_id=user_id)

    allowed, markup = check_private_chat(update)
    if not allowed:
        await update.message.reply_text(
            "‼️ You are not allowed to use this command in private chat. Join the group‼️",
            reply_markup=markup
        )
        return

    if chat_id < 0 and user_id != OWNER_ID:
        grp_ok, grp_remaining = check_group_daily_lookup(chat_id)
        if not grp_ok:
            await update.message.reply_text(
                f"❌ <b>Group Daily Lookup Limit Reached ({GROUP_DAILY_LOOKUP_LIMIT}/{GROUP_DAILY_LOOKUP_LIMIT})</b>\n\n"
                "This group has used all its free daily lookups. Resets tomorrow!\n"
                "💎 Contact admin for support.",
                parse_mode="HTML",
                reply_markup=admin_button()
            )
            return

    if not is_premium_user:
        free_ok, remaining = check_lookup_free_usage(user_id)
        if not free_ok:
            await update.message.reply_text(
                "❌ Daily free lookup limit reached (2/2). Try again tomorrow.\n"
                "💎 <b>Want unlimited lookups?</b>\n"
                "Get your Premium subscription now!",
                reply_markup=buy_button(),
                parse_mode="HTML"
            )
            return
        if should_trigger_cooldown_lookup(user_id):
            set_cooldown(user_id)
            free_user_lookup_count[user_id] = 0

    cd = 0
    if not is_premium_user:
        cd = check_cooldown(user_id)

    if cd > 0:
        if cd >= 60:
            cd_text = f"{cd // 60} minute(s) {cd % 60} second(s)" if cd % 60 else f"{cd // 60} minute(s)"
        else:
            cd_text = f"{cd} second(s)"

        await update.message.reply_text(
            f"<b>Lookup time out, please wait</b> <b>{cd_text}</b> <b>before using again!</b>\n\n"
            f"💎 <b>Tips:</b> Premium users have no cooldown!",
            reply_markup=admin_button(),
            parse_mode="HTML"
        )
        return

    if user_id == OWNER_ID:
        mode_label = "👑 Admin "
    elif chat_id in free_groups:
        mode_label = "🆓 Free Group Check"
    elif chat_id < 0:
        mode_label = "🔵 Group Mode"
    else:
        mode_label = "🔄 Processing Now..."

    processing_msg = await update.message.reply_text(
        f"⏳ Looking up info for {roleid}...\n<b>{mode_label}</b>",
        parse_mode="HTML"
    )

    MAX_RETRY = 5
    data = None
    error = "Unknown error"

    for attempt in range(1, MAX_RETRY + 1):
        try:
            data, error = fetch_api_data(roleid, zoneid, "lookup")
            if data and not error:
                break
        except Exception as e:
            data, error = None, str(e)

        try:
            await processing_msg.edit_text(
                f"⏳ Looking up info for player {roleid}...\n"
                f"<b>(Retry {attempt}/{MAX_RETRY}...)</b>",
                parse_mode="HTML"
            )
        except Exception:
            pass

        if attempt < MAX_RETRY:
            await asyncio.sleep(1)

    try:
        await processing_msg.delete()
    except Exception:
        pass

    if not data or error:
        if error == "CREDITS_EXHAUSTED":
            await update.message.reply_text(
                "❌ <b>API CREDITS EXHAUSTED</b>\n\n"
                "The bot's API credits have run out. Please contact the owner to refill.",
                parse_mode="HTML",
                reply_markup=admin_button()
            )
        else:
            await update.message.reply_text(
                f"❌ Failed to fetch data information for player <code>{roleid}</code>. ERROR: Get player info failed!",
                parse_mode="HTML"
            )
        return

    def clean_num(v):
        if v is None:
            return None
        v = str(v).strip()
        if v.replace(".", "", 1).isdigit():
            return v
        if v.lower() in ["none", "null", "", "n/a", "unknown", "-", "--"]:
            return None
        return v

    def clean_txt(v):
        if v is None:
            return None
        v = str(v).strip()
        if v.lower() in ["none", "null", "", "n/a", "unknown", "-", "--"]:
            return None
        return v

    def SB(x):
        if x is None:
            return "<b>—</b>"
        return f"<b>{html.escape(str(x))}</b>"

    real_creation_date = clean_txt(data.get("ttl"))

    if is_premium_user:
        creation_date = SB(real_creation_date)
        followers = SB(clean_num(data.get('followers')))
        last_login = SB(clean_txt(data.get('last_login')))
        last_country = SB(clean_txt(data.get('last_country_logged')))
    else:
        creation_date = "<b>(Only Premium Users)</b>"
        followers = "<b>(Only Premium Users)</b>"
        last_login = "<b>(Only Premium Users)</b>"
        last_country = "<b>(Only Premium Users)</b>"

    top3 = data.get('top_3_hero_details') or []
    top3_text = ""
    for i, hero in enumerate(top3):
        top3_text += f" - #{i+1}: {hero.get('hero','—')} : {hero.get('matches','—')}|{hero.get('win_rate','—')}|{hero.get('power','—')}\n"

    affinity_list = data.get('affinity_list') or []
    affinity_text = f"<b>{', '.join(map(str, affinity_list))}</b>" if affinity_list else "<b>—</b>"

    fav_hero_raw = data.get('favorite_hero') or data.get('most_used_hero') or data.get('fav_hero')
    fav_hero_bold = SB(clean_txt(fav_hero_raw))
    total_tournament_bold = SB(clean_num(data.get('total_tournament')))
    emblem_level_bold = SB(clean_num(data.get('emblem_level')))
    server_bold = SB(clean_txt(data.get('server') or data.get('region')))
    account_type_bold = SB(clean_txt(data.get('account_type') or data.get('user_type')))
    highest_win_streak_bold = SB(clean_num(data.get('highest_win_streak') or data.get('highest_win_streak_rank')))
    total_loss_bold = SB(clean_num(data.get('total_loss') or data.get('total_losses')))
    win_loss_ratio_bold = SB(clean_txt(data.get('win_loss_ratio')))
    magic_wheel_bold = SB(clean_num(data.get('magic_wheel') or data.get('magic_wheel_count')))
    bp_bold = SB(clean_num(data.get('battle_points') or data.get('bp')))

    lmd = data.get('last_match_data') or {}
    lmd_hero     = SB(clean_txt(lmd.get('hero_name')))
    lmd_kills    = SB(clean_num(lmd.get('kills')))
    lmd_deaths   = SB(clean_num(lmd.get('deaths')))
    lmd_assists  = SB(clean_num(lmd.get('assists')))
    lmd_gold     = SB(clean_num(lmd.get('gold')))
    lmd_dmg      = SB(clean_num(lmd.get('hero_damage')))
    lmd_turret   = SB(clean_num(lmd.get('turret_damage')))
    lmd_taken    = SB(clean_num(lmd.get('damage_taken')))

    role_id_bold         = SB(clean_txt(data.get('role_id') or roleid))
    zone_id_bold         = SB(clean_txt(data.get('zone_id') or zoneid))
    level_bold           = SB(clean_num(data.get('level')))
    name_bold            = SB(clean_txt(data.get('name')))
    current_tier_bold    = SB(clean_txt(data.get('current_tier')))
    max_tier_bold        = SB(clean_txt(data.get('max_tier')))
    hero_count_bold      = SB(clean_num(data.get('hero_count')))
    skin_count_bold      = SB(clean_num(data.get('skin_count')))
    win_rate_bold        = SB(clean_txt(data.get('overall_win_rate')))
    total_match_bold     = SB(clean_num(data.get('total_match_played')))
    total_wins_bold      = SB(clean_num(data.get('total_wins')))
    total_mvp_bold       = SB(clean_num(data.get('total_mvp')))
    kda_bold             = SB(clean_txt(str(data.get('kda')) if data.get('kda') is not None else None))
    collector_lv_bold    = SB(clean_num(data.get('collector_level')))
    collector_title_bold = SB(clean_txt(data.get('collector_title')))
    squad_name_bold      = SB(clean_txt(data.get('squad_name')))
    squad_prefix_bold    = SB(clean_txt(data.get('squad_prefix')))
    squad_id_bold        = SB(clean_txt(str(data.get('squad_id')) if data.get('squad_id') else None))
    achievement_bold     = SB(clean_num(data.get('achievement_points')))
    likes_bold           = SB(clean_num(data.get('total_likes')))
    credits_bold         = SB(clean_num(data.get('credits_score')))
    popularity_bold      = SB(clean_num(data.get('popularity')))
    flags_bold           = SB(clean_txt(data.get('flags_percentage')))
    latest_skin_bold     = SB(clean_txt(data.get('latest_skin_purchase_date')))
    last_hero_purchase_bold = SB(clean_txt(data.get('last_hero_purchase')))
    top3_used_bold       = SB(clean_txt(data.get('top3_most_used_heroes')))

    locations = data.get('locations_logged') or []
    locations_logged_bold = f"<b>{', '.join(map(str, locations))}</b>" if locations else "<b>—</b>"
    starlight_raw = str(data.get('starlite_user')).lower()
    starlight_status = "Yes" if starlight_raw in ["1", "true", "yes"] else "No"

    bind_info_raw = clean_txt(data.get('bind_info') or data.get('bindInfo'))
    bind_info_bold = SB(bind_info_raw)

    double_kill_bold     = SB(clean_num(data.get('double_kill')))
    triple_kill_bold     = SB(clean_num(data.get('triple_kill')))
    maniac_bold          = SB(clean_num(data.get('maniac_kill')))
    savage_bold          = SB(clean_num(data.get('savage_kill')))
    legendary_bold       = SB(clean_num(data.get('legendary_kill')))
    team_participation_bold = SB(clean_txt(data.get('team_participation')))
    mvp_loss_bold        = SB(clean_num(data.get('mvp_loss')))

    text = f"""
🆔 Role ID: {role_id_bold}
🌐 Zone ID: {zone_id_bold}
📅 Creation Date: {creation_date}
💿 Level: {level_bold}
🏷️ Name: {name_bold}
🌍 Server Region: {server_bold}
🎭 Account Type: {account_type_bold}
🏅 Current Tier: {current_tier_bold}
🥇 Max Tier: {max_tier_bold}
🎖️ Hero Count: {hero_count_bold}
🎨 Skin Count: {skin_count_bold}
 ● Supreme Skins: {SB(clean_num(data.get('supreme_skins')))}
 ● Grand Skins: {SB(clean_num(data.get('grand_skins')))}
 ● Exquisite Skins: {SB(clean_num(data.get('exquisite_skins')))}
 ● Deluxe Skins: {SB(clean_num(data.get('deluxe_skins')))}
 ● Exceptional Skins: {SB(clean_num(data.get('exceptional_skins')))}
 ● Common Skins: {SB(clean_num(data.get('common_skins')))}
🎮 Overall Win Rate: {win_rate_bold}
📉 Total Losses: {total_loss_bold}
⚖️ Win/Loss Ratio: {win_loss_ratio_bold}
🔥 Highest Win Streak: {highest_win_streak_bold}
🦸 Favorite Hero: {fav_hero_bold}
📊 Collector Level: {collector_lv_bold}
🎗️ Collector Title: {collector_title_bold}
🎡 Magic Wheel Count: {magic_wheel_bold}
💠 Emblem Level: {emblem_level_bold}
💰 Battle Points: {bp_bold}
⭐ Starlite User: <b>{starlight_status}</b>
👑 Squad Name: {squad_name_bold} {squad_prefix_bold}
🆔 Squad ID: {squad_id_bold}
🏆 Achievement Points: {achievement_bold}
👍 Total Likes: {likes_bold}
💳 Credits Score: {credits_bold}
👥 Followers: {followers}
📊 Popularity: {popularity_bold}
🚩 Flags: {flags_bold}
🔗 Bind Info: {bind_info_bold}
💸 Latest Skin Purchase Date: {latest_skin_bold}
🦸 Last Hero Purchase: {last_hero_purchase_bold}
🏠 Last Country Logged: {last_country}
📍 Locations Logged: {locations_logged_bold}
⏰ Last Login: {last_login}
💞 Affinity List: {affinity_text}

⭐ <b>Top 3 Heroes (Matches | Win Rate | Power)</b>
<b>{top3_text}</b>
📊 <b>Battle Records:</b>
 ● Total Matches: {total_match_bold}
 ● Total Wins: {total_wins_bold}
 ● Total MVP: {total_mvp_bold}
 ● MVP Loss: {mvp_loss_bold}
 ● KDA: {kda_bold}
 ● Team Participation: {team_participation_bold}
 ● Longest Win Streak: {SB(clean_num(data.get('longest_win_streak')))}
 ● Double Kill: {double_kill_bold}
 ● Triple Kill: {triple_kill_bold}
 ● Maniac: {maniac_bold}
 ● Savage: {savage_bold}
 ● Legendary: {legendary_bold}
 ● Most Kills: {SB(clean_num(data.get('most_kills')))}
 ● Most Assists: {SB(clean_num(data.get('most_assists')))}
 ● Highest Damage: {SB(clean_num(data.get('highest_dmg')))}
 ● Highest Damage Taken: {SB(clean_num(data.get('highest_dmg_taken')))}
 ● Highest Gold: {SB(clean_num(data.get('highest_gold')))}
 ● Min Gold/Min: {SB(clean_num(data.get('min_gold')))}
 ● Min Hero Damage: {SB(clean_num(data.get('min_hero_damage')))}
 ● Turret Dmg/Match: {SB(clean_num(data.get('turret_dmg_match')))}
 ● Most Hero Used: {top3_used_bold}

📊 <b>Last Match Game:</b>
 ● Date: {SB(clean_txt(data.get('last_match_date')))}
 ● Duration: {SB(clean_txt(data.get('last_match_duration')))}
 ● Heroes: {SB(clean_txt(data.get('last_match_heroes')))}
 ● Hero Used: {lmd_hero}
 ● K/D/A: {lmd_kills}/{lmd_deaths}/{lmd_assists}
 ● Gold: {lmd_gold}
 ● Hero Damage: {lmd_dmg}
 ● Turret Damage: {lmd_turret}
 ● Damage Taken: {lmd_taken}

📣 <b>Official Channel:</b> https://t.me/officialveritonchannels
🏠 <b>Official Group:</b> https://t.me/officialveritongroups
"""

    if not is_premium_user:
        text += "\n\n💎 Unlock full data with Premium Subscription!"

    # SAVE USER DATA
    user_obj = update.effective_user
    is_premium_int = 1 if has_active_subscription(user_id=user_obj.id) else 0

    db_execute("""
    INSERT INTO users (user_id, name, username, total_checks, premium)
    VALUES (?, ?, ?, 1, ?)
    ON CONFLICT(user_id) DO UPDATE SET
        total_checks = total_checks + 1,
        name = excluded.name,
        username = excluded.username,
        premium = excluded.premium
    """, (
        user_obj.id,
        user_obj.first_name,
        user_obj.username or "No Username",
        is_premium_int
    ))

    if chat_id < 0:
        deduct_credit(chat_id)

    if not is_premium_user:
        set_cooldown(user_id)

    await update.message.reply_text(
        fix_html(text),
        reply_markup=buy_button() if not is_premium_user else None,
        parse_mode="HTML"
    )

# ==============================
# BIND COMMAND
# ==============================

async def bind(update: Update, context: ContextTypes.DEFAULT_TYPE):

    status = get_command_status("bind")
    if status:
        username = update.effective_user.first_name
        await update.message.reply_text(
            f"<i>Hello {username}, Bind is currently under {status}.</i>",
            parse_mode="HTML"
        )
        return

    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type
    user_id = update.effective_user.id

    if chat_type == "private":
        user_chats.add(chat_id)
    else:
        group_chats.add(chat_id)

    if not await can_use_bot(user_id, chat_id, update.message):
        return

    reset_daily_usage()

    allowed, markup = check_private_chat(update)
    if not allowed:
        await update.message.reply_text(
            "‼️ You are not allowed to use this command in private chat. Join the group‼️",
            reply_markup=markup
        )
        return

    if len(context.args) == 0:
        await update.message.reply_text(
            "❌ <b>Invalid Format Use:</b>\n"
            "/bind &lt;role_id&gt; &lt;zone_id&gt; or\n"
            "/bind &lt;role_id&gt; (zone_id)",
            parse_mode="HTML"
        )
        return

    raw_text = " ".join(context.args)
    numbers = re.findall(r"\d+", raw_text)

    roleid = numbers[0] if len(numbers) >= 1 else None
    zoneid = numbers[1] if len(numbers) >= 2 else None

    if not roleid:
        await update.message.reply_text("❌ Invalid Role ID.")
        return

    if not zoneid:
        await update.message.reply_text(
            "❌ <b>Zone ID is required Use:</b>\n"
            "/bind &lt;role_id&gt; &lt;zone_id&gt; or\n"
            "/bind &lt;role_id&gt; (zone_id)",
            parse_mode="HTML"
        )
        return

    is_premium_user = has_active_subscription(user_id=user_id)

    if chat_id < 0 and user_id != OWNER_ID:
        grp_ok, grp_remaining = check_group_daily_bind(chat_id)
        if not grp_ok:
            await update.message.reply_text(
                f"❌ <b>Group Daily Bind Limit Reached ({GROUP_DAILY_BIND_LIMIT}/{GROUP_DAILY_BIND_LIMIT})</b>\n\n"
                "This group has used all its free daily binds. Resets tomorrow!\n"
                "💎 Contact admin for support.",
                parse_mode="HTML",
                reply_markup=admin_button()
            )
            return

    if not is_premium_user:
        free_ok, remaining = check_bind_free_usage(user_id)
        if not free_ok:
            await update.message.reply_text(
                "❌ Daily free bind limit reached (3/3). Try again tomorrow.\n"
                "💎 <b>Want unlimited binds?</b>\n"
                "Get your Premium subscription now!",
                reply_markup=buy_button(),
                parse_mode="HTML"
            )
            return
        if should_trigger_cooldown_bind(user_id):
            set_cooldown(user_id)
            free_user_bind_count[user_id] = 0

    if not is_premium_user:
        cd = check_cooldown(user_id)
        if cd > 0:
            if cd >= 60:
                cd_text = f"{cd // 60} minute(s) {cd % 60} second(s)" if cd % 60 else f"{cd // 60} minute(s)"
            else:
                cd_text = f"{cd} second(s)"
            await update.message.reply_text(
                f"<b>Bind time out, please wait</b> <b>{cd_text}</b> <b>before using again!</b>\n\n"
                f"💎 <b>Tips:</b> Premium users have no cooldown!",
                reply_markup=admin_button(),
                parse_mode="HTML"
            )
            return

    if user_id == OWNER_ID:
        mode_label = "👑 Admin"
    elif chat_id in free_groups:
        mode_label = "🆓 Free Group"
    elif chat_id < 0:
        mode_label = "🔵 Group Mode"
    else:
        mode_label = "🔄 Processing Now..."

    processing_msg = await update.message.reply_text(
        f"⏳ Looking up bind for {roleid}...\n<b>{mode_label}</b>",
        parse_mode="HTML"
    )

    MAX_RETRY = 5
    data = None
    error = "Unknown error"

    for attempt in range(1, MAX_RETRY + 1):
        try:
            data, error = fetch_api_data(roleid, zoneid, "bind")
            if data and not error:
                break
        except Exception as e:
            data, error = None, str(e)

        try:
            await processing_msg.edit_text(
                f"⏳ Looking up bind for player {roleid}...\n"
                f"<b>(Retry {attempt}/{MAX_RETRY}...)</b>",
                parse_mode="HTML"
            )
        except Exception:
            pass

        if attempt < MAX_RETRY:
            await asyncio.sleep(1)

    try:
        await processing_msg.delete()
    except Exception:
        pass

    if not data or error:
        if error == "CREDITS_EXHAUSTED":
            await update.message.reply_text(
                "❌ <b>API CREDITS EXHAUSTED</b>\n\n"
                "The bot's API credits have run out. Please contact the owner to refill.",
                parse_mode="HTML",
                reply_markup=admin_button()
            )
        else:
            await update.message.reply_text(
                f"❌ Failed to fetch data information for player <code>{roleid}</code>. ERROR: Get player info failed!",
                parse_mode="HTML"
            )
        return

    avatar_url = data.get('avatar_url') or START_PHOTO_URL

    name = data.get('nickname')
    year_created = data.get('creation_year')
    role_id = data.get('role_id')
    zone_id = data.get('zone_id')

    accounts = data.get('bind_accounts', [])
    devices = data.get('devices', {})

    account_emojis = {
        "Moonton": "",
        "VK": "",
        "Google": "",
        "TikTok": "",
        "Facebook": "",
        "Apple": "",
        "Game Center": "",
        "Telegram": "",
        "WhatsApp": ""
    }

    accounts_text = ""
    for acc in accounts:
        platform = str(acc.get("platform"))
        connected = acc.get("connected")
        details = acc.get("details")
        emoji = account_emojis.get(platform, "🔗")
        if connected and details:
            status_txt = f"<code>{html.escape(str(details))}</code>"
        else:
            status_txt = "<i>(Not Connected)</i>"
        accounts_text += f"  • {emoji} {html.escape(platform)}: {status_txt}\n"

    android = devices.get('android', {})
    ios = devices.get('ios', {})

    total_active = android.get('active', 0) + ios.get('active', 0)
    total_inactive = android.get('inactive', 0) + ios.get('inactive', 0)
    total_devices = devices.get('total_devices', 0)

    if total_devices == 0:
        devices_text = " • <i>No Devices Connected</i>"
    else:
        devices_text = (
            f" • Android: <b>{android.get('total', 0)}</b> "
            f"(Active: <b>{android.get('active', 0)}</b>, Inactive: <b>{android.get('inactive', 0)}</b>)\n"
            f" • iOS: <b>{ios.get('total', 0)}</b> "
            f"(Active: <b>{ios.get('active', 0)}</b>, Inactive: <b>{ios.get('inactive', 0)}</b>)\n"
            f" • Total: <b>{total_devices}</b> "
            f"(Active: <b>{total_active}</b>, Inactive: <b>{total_inactive}</b>)"
        )

    bind_text = f"""
🆔 Role ID: {safe_bold(role_id)}
🌐 Zone ID: {safe_bold(zone_id)}
🏷️ Name: {safe_bold(name)}
📅 Year Created: {safe_bold(year_created)}

🔗 Bind Accounts:
{accounts_text}
📱 Devices Connected:
{devices_text}

📣 <b>Official Channel:</b> https://t.me/officialveritonchannels
🏠 <b>Official Group:</b> https://t.me/officialveritongroups
"""

    # SAVE USER DATA
    user_obj = update.effective_user
    is_premium_int = 1 if has_active_subscription(user_id=user_obj.id) else 0

    db_execute("""
    INSERT INTO users (user_id, name, username, total_checks, premium)
    VALUES (?, ?, ?, 1, ?)
    ON CONFLICT(user_id) DO UPDATE SET
        total_checks = total_checks + 1,
        name = excluded.name,
        username = excluded.username,
        premium = excluded.premium
    """, (
        user_obj.id,
        user_obj.first_name,
        user_obj.username or "No Username",
        is_premium_int
    ))

    if chat_id < 0:
        deduct_credit(chat_id)

    if not is_premium_user:
        set_cooldown(user_id)

    await update.message.reply_photo(
        photo=avatar_url,
        caption=fix_html(bind_text),
        parse_mode="HTML"
    )

# ==============================
# REMOVE PREMIUM COMMAND
# ==============================

async def removepremium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ You are not authorized.")
        return

    if len(context.args) == 0:
        await update.message.reply_text("❌ Usage:\n/removepremium &lt;user_id&gt;", parse_mode="HTML")
        return

    try:
        target_id = int(context.args[0])
    except Exception:
        await update.message.reply_text("❌ Invalid user ID.")
        return

    row = db_fetchone("SELECT expiry FROM premium_users WHERE user_id=?", (target_id,))

    if not row:
        await update.message.reply_text("❌ No active premium found.")
        return

    expire_data = row[0]
    db_execute("DELETE FROM premium_users WHERE user_id=?", (target_id,))

    await update.message.reply_text(
        f"✅ Premium removed successfully!\n\n"
        f"👤 ID: <code>{target_id}</code>\n"
        f"📅 Previous expiry: <code>{expire_data}</code>",
        parse_mode="HTML"
    )

    try:
        await context.bot.send_message(
            chat_id=target_id,
            text="❌ Your Premium has been removed by admin."
        )
    except Exception:
        pass

# ======================
# BUY COMMAND
# ======================

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = f"""
💎 <b>Veriton Premium Pricing</b> 

💰 <b>Available Type:</b>

 🔹 Type 1
• Amount: <b>50</b> 
• Duration: <b>3 Days</b>
• Lookups: <b>Unlimited</b>
• Binds: <b>Unlimited</b>

 🔹 Type 2
• Amount: <b>130</b>
• Duration: <b>7 Days</b>
• Lookups: <b>Unlimited</b>
• Binds: <b>Unlimited</b>

 🔹 Type 3
• Amount: <b>165</b>
• Duration: <b>12 Days</b>
• Lookups: <b>Unlimited</b>
• Binds: <b>Unlimited</b>

 🔹 Type 4 ✨
• Amount: <b>230</b>
• Duration: <b>25 Days</b>
• Lookups: <b>Unlimited</b>
• Binds: <b>Unlimited</b>

 🔹 Type 5 💰
• Amount: <b>300</b>
• Duration: <b>30 Days</b>
• Lookups: <b>Unlimited</b>
• Binds: <b>Unlimited</b>

 🔹 Type 6 💎
• Amount: <b>450</b>
• Duration: <b>Lifetime</b>
• Lookups: <b>Unlimited</b>
• Binds: <b>Unlimited</b>

<i>Note: This plans is not totally unlimited, if you want lifetime you need to buy Type 6</i>

📌 <b>How to Order?</b>
Contact @Official_Caius for GCash Number
Once paid, upload the receipt from this bot 
"""

    keyboard = [
        [
            InlineKeyboardButton("👤 Contact Admin", url=f"https://t.me/{ADMIN_USERNAME.replace('@', '')}"),
            InlineKeyboardButton("❓ Help & Commands", callback_data="help")
        ]
    ]

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )

# ======================
# HANDLE RECEIPT
# ======================

async def handle_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return

    user = update.effective_user
    username = f"@{user.username}" if user.username else "No Username"
    caption = update.message.caption or "No amount provided"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    text = f"""
💰 <b>NEW PAYMENT RECEIPT</b>

👤 User: {username}
🆔 ID: <code>{user.id}</code>
💵 Amount: <code>{caption}</code>
📅 Date: <code>{now}</code>
"""

    try:
        await context.bot.send_photo(
            chat_id=OWNER_ID,
            photo=update.message.photo[-1].file_id,
            caption=text,
            parse_mode="HTML"
        )
        await update.message.reply_text("✅ Receipt received! Waiting for admin approval.")
    except Exception as e:
        print(e)
        await update.message.reply_text("❌ Failed to send receipt.")

# ==============================
# MYINFO COMMAND
# ==============================

async def myinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id

    username = f"@{user.username}" if user.username else "No Username"
    fullname = f"{user.first_name or ''} {user.last_name or ''}".strip() or "Unknown"

    row = db_fetchone("SELECT expiry FROM premium_users WHERE user_id=?", (user_id,))
    if row:
        expiry = datetime.fromisoformat(row[0])
        if expiry > datetime.now(timezone.utc):
            premium_status = f"💎 Active (Expires: <code>{expiry.strftime('%Y-%m-%d %H:%M:%S')}</code>)"
        else:
            premium_status = "❌ Expired"
    else:
        premium_status = "❌ None"

    is_premium = has_active_subscription(user_id=user_id)

    lookups_used = daily_lookup_usage.get(user_id, 0)
    binds_used = daily_bind_usage.get(user_id, 0)

    if is_premium or user_id == OWNER_ID:
        lookup_info = "♾️ Unlimited (Premium)"
        bind_info = "♾️ Unlimited (Premium)"
    else:
        lookup_info = f"{lookups_used}/{LOOKUP_FREE_LIMIT} used today"
        bind_info = f"{binds_used}/{BIND_FREE_LIMIT} used today"

    cd = check_cooldown(user_id)
    if cd > 0:
        if cd >= 60:
            cd_text = f"{cd // 60} minute(s) {cd % 60} second(s)" if cd % 60 else f"{cd // 60} minute(s)"
        else:
            cd_text = f"{cd} second(s)"
        cooldown_status = f"⏳ {cd_text} remaining"
    else:
        cooldown_status = "✅ Ready"

    blocked_status = "🚫 Blocked" if user_id in blocked_users else "✅ Not Blocked"

    if user_id == OWNER_ID:
        role = "👑 Owner"
    elif is_premium:
        role = "💎 Premium User"
    else:
        role = "🆓 Free User"

    text = (
        "👤 <b>YOUR ACCOUNT INFO</b>\n\n"
        f"🆔 User ID: <code>{user_id}</code>\n"
        f"👤 Name: <b>{html.escape(fullname)}</b>\n"
        f"📛 Username: {html.escape(username)}\n"
        f"🏷️ Role: {role}\n\n"
        f"💎 Premium Status: {premium_status}\n\n"
        f"📊 <b>Daily Usage:</b>\n"
        f" • Lookups: {lookup_info}\n"
        f" • Binds: {bind_info}\n\n"
        f"⏱️ Cooldown: {cooldown_status}\n"
        f"🔒 Block Status: {blocked_status}\n\n"
        f"🏠 <b>Official Channel:</b> https://t.me/Official_VeritonChannel"
    )

    keyboard = [[InlineKeyboardButton("💰 Buy Subscription", url="https://t.me/Official_Caius")]]
    markup = InlineKeyboardMarkup(keyboard) if not is_premium and user_id != OWNER_ID else None

    await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)

# ==============================
# CALLBACK HANDLER
# ==============================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    user_id = query.from_user.id

    if data == "pricing":
        await pricing(update, context)

    elif data == "start":
        await start(update, context)

    elif data == "help":
        await help(update, context)

    elif data == "ai":
        text = (
            "🤖 <b>Veriton AI Assistant</b>\n\n"
            "Hello everyone, As of now AI command is unavailable, thank you!"
        )
        try:
            if query.message.photo:
                await query.message.edit_caption(caption=text, parse_mode="HTML")
            else:
                await query.edit_message_text(text, parse_mode="HTML")
        except Exception:
            await query.message.reply_text(text, parse_mode="HTML")

    elif data == "premium":
        row = db_fetchone("SELECT expiry FROM premium_users WHERE user_id=?", (user_id,))

        is_active = False
        expire_text = ""
        if row:
            expiry = datetime.fromisoformat(row[0])
            if expiry > datetime.now(timezone.utc):
                is_active = True
                expire_text = expiry.strftime("%Y-%m-%d %H:%M:%S")

        if is_active:
            text = (
                "💎 <b>Your Premium Status</b>\n\n"
                "✅ Activated\n"
                f"⏳ Expires: <code>{expire_text}</code>"
            )
        else:
            text = (
                "❌ You don't have premium yet.\n"
                "Use /buy or contact admin."
            )

        try:
            if query.message.photo:
                await query.message.edit_caption(
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=buy_button() if not is_active else None
                )
            else:
                await query.edit_message_text(
                    text=text,
                    parse_mode="HTML",
                    reply_markup=buy_button() if not is_active else None
                )
        except Exception:
            await query.message.reply_text(
                text=text,
                parse_mode="HTML",
                reply_markup=buy_button() if not is_active else None
            )

# ==============================
# TEST API COMMAND
# ==============================

async def testapi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Only the owner can test the API.")
        return

    try:
        payload = {
            "role_id": 1,
            "zone_id": 1,
            "type": "lookup"
        }

        headers = {
            "x-api-key": API_KEY,
            "Content-Type": "application/json"
        }

        r = requests.post(API_BASE_URL, json=payload, headers=headers, timeout=15)

        if r.status_code == 200:
            try:
                resp_data = r.json()
                if resp_data.get("data"):
                    await update.message.reply_text("✅ API is working and returned valid data!")
                else:
                    await update.message.reply_text(f"⚠️ API responded but no data:\n{resp_data}")
            except Exception:
                await update.message.reply_text("⚠️ API returned non-JSON response")
        else:
            await update.message.reply_text(
                f"❌ API Error:\nStatus: {r.status_code}\nResponse: {r.text}"
            )

    except Exception as e:
        await update.message.reply_text(f"❌ API request failed:\n{str(e)}")

# ==============================
# ADD PREMIUM COMMAND
# ==============================

async def addpremium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ You are not authorized.")
        return

    try:
        target = int(context.args[0])
        days = int(context.args[1])

        expire = datetime.now(timezone.utc) + timedelta(days=days)

        db_execute(
            "INSERT OR REPLACE INTO premium_users (user_id, expiry, notified) VALUES (?, ?, 0)",
            (target, expire.isoformat())
        )

        await update.message.reply_text(
            f"✅ Premium User Added\n\n"
            f"👤 User ID: <code>{target}</code>\n"
            f"📅 Duration: <b>{days} days</b>\n"
            f"⏳ Expires: <b>{expire.strftime('%Y-%m-%d %H:%M:%S')}</b>",
            parse_mode="HTML"
        )

        try:
            await context.bot.send_message(
                chat_id=target,
                text=(
                    "🎉 <b>PREMIUM ACTIVATED</b>\n\n"
                    "💎 Your account is now <b>PREMIUM</b>\n"
                    f"⏳ Valid until:\n<code>{expire.strftime('%Y-%m-%d %H:%M:%S')}</code>\n\n"
                    "🚀 Enjoy premium features!"
                ),
                parse_mode="HTML"
            )
        except Exception as e:
            await update.message.reply_text(
                f"⚠️ Premium added but failed to notify user.\nReason: {e}"
            )

    except Exception:
        await update.message.reply_text(
            "❌ Usage: /addpremium <user_id> <days>"
        )

# ====================================
# NOTIF USER EXPIRED
# ====================================

async def premium_watcher(app):
    while True:
        try:
            now = datetime.now(timezone.utc)

            rows = db_fetchall("SELECT user_id, expiry, notified FROM premium_users")

            for user_id, expiry_str, notified in rows:
                expiry = datetime.fromisoformat(expiry_str)
                remaining = (expiry - now).total_seconds()

                if 0 < remaining <= 60 and not notified:
                    try:
                        await app.bot.send_message(
                            chat_id=user_id,
                            text=(
                                "⚠️ <b>PREMIUM EXPIRY WARNING</b>\n\n"
                                "⏳ Your premium will expire in <b>1 minute</b>!\n"
                                "💎 Renew now to avoid interruption."
                            ),
                            parse_mode="HTML"
                        )

                        db_execute(
                            "UPDATE premium_users SET notified=1 WHERE user_id=?",
                            (user_id,)
                        )

                    except Exception:
                        pass

                if remaining <= 0:
                    db_execute("DELETE FROM premium_users WHERE user_id=?", (user_id,))

        except Exception as e:
            print("Watcher error:", e)

        await asyncio.sleep(30)

# ==============================
# ANNOUNCE COMMAND
# ==============================

async def announce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Invalid Format Use:\n"
            "/announce users &lt;message&gt;\n"
            "/announce groups &lt;message&gt;\n"
            "/announce all &lt;message&gt;",
            parse_mode="HTML"
        )
        return

    target = context.args[0].lower()
    message = " ".join(context.args[1:])

    if target == "users":
        targets = user_chats
    elif target == "groups":
        targets = group_chats
    elif target == "all":
        targets = user_chats.union(group_chats)
    else:
        await update.message.reply_text("❌ Invalid target: users/groups/all")
        return

    text = f"""
<i>{html.escape(message)}</i>
"""

    success = 0
    failed = 0

    for chat_id in targets:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML"
            )
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    await update.message.reply_text(
        f"📢 Announcement Sent!\n\n✔ Success: {success}\n❌ Failed: {failed}"
    )

# ==============================
# POST INIT — start background watcher
# ==============================

async def post_init(application):
    asyncio.create_task(premium_watcher(application))

# ==============================
# MAIN LOOP
# ==============================

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myinfo", myinfo))
    app.add_handler(CommandHandler("pricing", pricing))
    app.add_handler(CommandHandler("help", help))
    app.add_handler(CommandHandler("lookup", lookup))
    app.add_handler(CommandHandler("bind", bind))
    app.add_handler(CommandHandler("checksubs", checksubs))
    app.add_handler(CommandHandler("testapi", testapi))
    app.add_handler(CommandHandler("announce", announce))
    app.add_handler(CommandHandler("transfer", transfer))
    app.add_handler(CommandHandler("addpremium", addpremium))
    app.add_handler(CommandHandler("buy", buy))
    app.add_handler(CommandHandler("removecredits", removecredits))
    app.add_handler(CommandHandler("removepremium", removepremium))
    app.add_handler(CommandHandler("block", block))
    app.add_handler(CommandHandler("unblock", unblock))
    app.add_handler(CommandHandler("setto", setto))
    app.add_handler(CommandHandler("checkusers", checkusers))

    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.PHOTO, handle_receipt))

    print("Bot is running...")
    app.run_polling()