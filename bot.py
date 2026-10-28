import os
import logging
import sqlite3
from datetime import datetime, timezone

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

# Required environment variables:
# BOT_TOKEN, ADMIN_ID
# Optional: BKASH_NUMBER, NAGAD_NUMBER, DB_PATH
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
BKASH_NUMBER = os.getenv("BKASH_NUMBER", "আপনার bKash নম্বর সেট করা হয়নি").strip()
NAGAD_NUMBER = os.getenv("NAGAD_NUMBER", "আপনার Nagad নম্বর সেট করা হয়নি").strip()
DB_PATH = os.getenv("DB_PATH", "bot.db").strip() or "bot.db"

if not BOT_TOKEN or not ADMIN_ID_RAW.isdigit():
    raise RuntimeError("BOT_TOKEN এবং ADMIN_ID Environment Variables-এ সঠিকভাবে সেট করুন।")
ADMIN_ID = int(ADMIN_ID_RAW)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("panel-bot")

# Demo catalogue. Admin can add products with /addproduct NAME | PRICE | STOCK
# STOCK is a comma-separated list of panel keys/serials.
DEFAULT_PRODUCTS = [
    ("Demo Panel", 50, ["DEMO-KEY-001", "DEMO-KEY-002"])
]

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            balance INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            price INTEGER NOT NULL CHECK(price >= 0),
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS stock (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL REFERENCES products(id),
            serial TEXT NOT NULL UNIQUE,
            sold INTEGER NOT NULL DEFAULT 0,
            sold_to INTEGER,
            sold_at TEXT
        );
        CREATE TABLE IF NOT EXISTS deposits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            method TEXT NOT NULL,
            amount INTEGER NOT NULL CHECK(amount > 0),
            transaction_id TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            reviewed_by INTEGER
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            price INTEGER NOT NULL,
            serial TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """)
        for name, price, serials in DEFAULT_PRODUCTS:
            con.execute(
                "INSERT OR IGNORE INTO products(name, price, active) VALUES(?,?,1)",
                (name, price)
            )
            product = con.execute("SELECT id FROM products WHERE name=?", (name,)).fetchone()
            for serial in serials:
                con.execute(
                    "INSERT OR IGNORE INTO stock(product_id, serial) VALUES(?,?)",
                    (product["id"], serial)
                )

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def touch_user(tg_user):
    with db() as con:
        con.execute(
            """INSERT INTO users(user_id, username, balance, created_at)
               VALUES(?,?,0,?)
               ON CONFLICT(user_id) DO UPDATE SET username=excluded.username""",
            (tg_user.id, tg_user.username or "", now())
        )

def is_admin(user_id):
    return user_id == ADMIN_ID

def main_keyboard(user_id):
    rows = [
        ["🛒 প্যানেল কিনুন", "💰 আমার ব্যালেন্স"],
        ["💳 ডিপোজিট", "📦 আমার অর্ডার"],
        ["📞 যোগাযোগ"]
    ]
    if is_admin(user_id):
        rows.append(["👑 Admin Panel"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    touch_user(user)
    await update.effective_message.reply_text(
        f"স্বাগতম {user.first_name or 'বন্ধু'}!\n\n"
        "এটি প্যানেল বিক্রির বটের প্রাথমিক সংস্করণ। মেনু থেকে একটি অপশন বেছে নিন।",
        reply_markup=main_keyboard(user.id)
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "মেনু ব্যবহার করুন। অ্যাডমিন কমান্ড:\n"
        "/admin — Admin Panel\n"
        "/addproduct নাম | দাম | সিরিয়াল১,সিরিয়াল২ — পণ্য/স্টক যোগ\n"
        "/products — পণ্যের তালিকা"
    )

async def show_balance(update, context):
    uid = update.effective_user.id
    touch_user(update.effective_user)
    with db() as con:
        row = con.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()
    await update.effective_message.reply_text(f"আপনার ব্যালেন্স: ৳{row['balance']}")

async def products_text(update, context):
    with db() as con:
        rows = con.execute("""
            SELECT p.id,p.name,p.price,COUNT(s.id) AS available
            FROM products p LEFT JOIN stock s ON s.product_id=p.id AND s.sold=0
            WHERE p.active=1 GROUP BY p.id ORDER BY p.id
        """).fetchall()
    if not rows:
        await update.effective_message.reply_text("এখন কোনো পণ্য নেই।")
        return
    buttons = [
        [InlineKeyboardButton(
            f"{r['name']} — ৳{r['price']} (স্টক {r['available']})",
            callback_data=f"buy:{r['id']}"
        )] for r in rows
    ]
    await update.effective_message.reply_text(
        "কেনার জন্য পণ্য নির্বাচন করুন:", reply_markup=InlineKeyboardMarkup(buttons)
    )

async def deposit_start(update, context):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("bKash", callback_data="deposit_method:bkash")],
        [InlineKeyboardButton("Nagad", callback_data="deposit_method:nagad")],
    ])
    await update.effective_message.reply_text("ডিপোজিটের পেমেন্ট পদ্ধতি বেছে নিন:", reply_markup=keyboard)

async def my_orders(update, context):
    uid = update.effective_user.id
    with db() as con:
        rows = con.execute(
            "SELECT id,price,serial,created_at FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10",
            (uid,)
        ).fetchall()
    if not rows:
        await update.effective_message.reply_text("আপনার এখনো কোনো অর্ডার নেই।")
        return
    text = "আপনার সাম্প্রতিক অর্ডার:\n\n" + "\n".join(
        f"অর্ডার #{r['id']} — ৳{r['price']}\nKey: {r['serial']}\nতারিখ: {r['created_at']}"
        for r in rows
    )
    await update.effective_message.reply_text(text)

async def admin_panel(update, context):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.effective_message.reply_text("⛔ এই প্যানেল শুধু অ্যাডমিনের জন্য।")
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏳ Pending Deposit", callback_data="admin:pending")],
        [InlineKeyboardButton("👥 ইউজার সংখ্যা", callback_data="admin:users")],
        [InlineKeyboardButton("📦 পণ্য/স্টক", callback_data="admin:stockhelp")],
    ])
    await update.effective_message.reply_text("👑 Admin Panel", reply_markup=keyboard)

async def addproduct_cmd(update, context):
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("⛔ শুধু অ্যাডমিন এই কমান্ড ব্যবহার করতে পারবেন।")
        return
    raw = " ".join(context.args)
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) != 3 or not parts[0] or not parts[1].isdigit():
        await update.effective_message.reply_text(
            "ব্যবহার:\n/addproduct পণ্যের নাম | দাম | KEY-1,KEY-2\n"
            "উদাহরণ:\n/addproduct Premium Panel | 100 | KEY-A1,KEY-A2"
        )
        return
    name, price_text, serials_text = parts
    price = int(price_text)
    serials = [s.strip() for s in serials_text.split(",") if s.strip()]
    try:
        with db() as con:
            con.execute("INSERT INTO products(name,price,active) VALUES(?,?,1)", (name, price))
            pid = con.execute("SELECT id FROM products WHERE name=?", (name,)).fetchone()["id"]
            for serial in serials:
                con.execute("INSERT INTO stock(product_id,serial) VALUES(?,?)", (pid, serial))
        await update.effective_message.reply_text(
            f"পণ্য যোগ হয়েছে: {name}\nদাম: ৳{price}\nস্টক যোগ: {len(serials)}"
        )
    except sqlite3.IntegrityError:
        await update.effective_message.reply_text(
            "পণ্য যোগ করা যায়নি। একই নামের পণ্য বা duplicate serial আগে থেকেই থাকতে পারে।"
        )

async def products_cmd(update, context):
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("⛔ শুধু অ্যাডমিন।")
        return
    await products_text(update, context)

async def handle_text(update, context):
    msg = (update.effective_message.text or "").strip()
    uid = update.effective_user.id
    touch_user(update.effective_user)

    # User is entering deposit details after selecting a payment method.
    pending_flow = context.user_data.get("deposit_flow")
    if pending_flow == "amount":
        if not msg.isdigit() or int(msg) <= 0:
            await update.effective_message.reply_text("সঠিক টাকার পরিমাণ লিখুন, যেমন 200।")
            return
        context.user_data["deposit_amount"] = int(msg)
        context.user_data["deposit_flow"] = "trx"
        await update.effective_message.reply_text("এখন Transaction ID লিখুন।")
        return
    if pending_flow == "trx":
        method = context.user_data.get("deposit_method", "")
        amount = context.user_data.get("deposit_amount")
        trx = msg.strip()
        if len(trx) < 4 or len(trx) > 100 or any(ch.isspace() for ch in trx):
            await update.effective_message.reply_text("Transaction ID সঠিকভাবে লিখুন (স্পেস ছাড়া)।")
            return
        try:
            with db() as con:
                cur = con.execute(
                    """INSERT INTO deposits(user_id,method,amount,transaction_id,status,created_at)
                       VALUES(?,?,?,?, 'pending', ?)""",
                    (uid, method, amount, trx, now())
                )
                dep_id = cur.lastrowid
            context.user_data.pop("deposit_flow", None)
            context.user_data.pop("deposit_amount", None)
            context.user_data.pop("deposit_method", None)
            await update.effective_message.reply_text(
                f"✅ ডিপোজিট অনুরোধ জমা হয়েছে।\nRequest: #{dep_id}\n"
                f"পদ্ধতি: {method}\nপরিমাণ: ৳{amount}\nঅ্যাডমিন যাচাই করার পর ব্যালেন্স যোগ হবে।",
                reply_markup=main_keyboard(uid)
            )
            try:
                await context.bot.send_message(
                    ADMIN_ID,
                    f"নতুন ডিপোজিট অনুরোধ #{dep_id}\nUser: {uid}\n"
                    f"পদ্ধতি: {method}\nপরিমাণ: ৳{amount}\nTrxID: {trx}",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ Approve", callback_data=f"dep:approve:{dep_id}"),
                        InlineKeyboardButton("❌ Cancel", callback_data=f"dep:cancel:{dep_id}")
                    ]])
                )
            except Exception:
                log.exception("Could not notify admin about deposit")
        except sqlite3.IntegrityError:
            await update.effective_message.reply_text(
                "এই Transaction ID আগে জমা হয়েছে। অনুগ্রহ করে সঠিক নতুন ID দিন।"
            )
        return

    if msg == "🛒 প্যানেল কিনুন":
        await products_text(update, context)
    elif msg == "💰 আমার ব্যালেন্স":
        await show_balance(update, context)
    elif msg == "💳 ডিপোজিট":
        await deposit_start(update, context)
    elif msg == "📦 আমার অর্ডার":
        await my_orders(update, context)
    elif msg == "📞 যোগাযোগ":
        await update.effective_message.reply_text(
            "অ্যাডমিনের সঙ্গে যোগাযোগ করতে আপনার বট অ্যাডমিনের নির্ধারিত Telegram account ব্যবহার করুন।"
        )
    elif msg == "👑 Admin Panel":
        await admin_panel(update, context)
    else:
        await update.effective_message.reply_text("মেনু থেকে একটি অপশন বেছে নিন।", reply_markup=main_keyboard(uid))

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data or ""

    if data.startswith("deposit_method:"):
        method_code = data.split(":", 1)[1]
        if method_code == "bkash":
            method, number = "bKash", BKASH_NUMBER
        else:
            method, number = "Nagad", NAGAD_NUMBER
        context.user_data["deposit_method"] = method
        context.user_data["deposit_flow"] = "amount"
        await q.message.reply_text(
            f"পেমেন্ট পদ্ধতি: {method}\nSend Money নম্বর: {number}\n\n"
            "পেমেন্ট করার পর কত টাকা পাঠিয়েছেন তা লিখুন।\n"
            "সতর্কতা: পেমেন্টের আগে নম্বরটি অ্যাডমিনের কাছ থেকে যাচাই করুন।"
        )
        return

    if data.startswith("buy:"):
        try:
            pid = int(data.split(":")[1])
        except ValueError:
            return
        with db() as con:
            product = con.execute(
                "SELECT id,name,price FROM products WHERE id=? AND active=1", (pid,)
            ).fetchone()
            user = con.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()
            stock = con.execute(
                "SELECT id,serial FROM stock WHERE product_id=? AND sold=0 ORDER BY id LIMIT 1",
                (pid,)
            ).fetchone()
            if not product:
                await q.message.reply_text("পণ্যটি পাওয়া যায়নি।")
                return
            if not user or user["balance"] < product["price"]:
                balance = user["balance"] if user else 0
                await q.message.reply_text(
                    f"পর্যাপ্ত ব্যালেন্স নেই। দাম ৳{product['price']}, আপনার ব্যালেন্স ৳{balance}।"
                )
                return
            if not stock:
                await q.message.reply_text("দুঃখিত, এই পণ্যের স্টক শেষ।")
                return
            # Transaction keeps balance deduction and stock sale together.
            con.execute("UPDATE users SET balance=balance-? WHERE user_id=?",
                        (product["price"], uid))
            con.execute("UPDATE stock SET sold=1,sold_to=?,sold_at=? WHERE id=? AND sold=0",
                        (uid, now(), stock["id"]))
            con.execute(
                "INSERT INTO orders(user_id,product_id,price,serial,created_at) VALUES(?,?,?,?,?)",
                (uid, pid, product["price"], stock["serial"], now())
            )
            order_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        await q.message.reply_text(
            f"✅ ক্রয় সফল!\nঅর্ডার: #{order_id}\nপণ্য: {product['name']}\n"
            f"মূল্য: ৳{product['price']}\nআপনার প্যানেল Key:\n{stock['serial']}\n\n"
            "Key নিরাপদে রাখুন।"
        )
        if is_admin(uid):
            pass
        return

    if data.startswith("admin:"):
        if not is_admin(uid):
            await q.message.reply_text("⛔ অনুমতি নেই।")
            return
        action = data.split(":")[1]
        if action == "pending":
            with db() as con:
                rows = con.execute(
                    "SELECT id,user_id,method,amount,transaction_id FROM deposits "
                    "WHERE status='pending' ORDER BY id LIMIT 20"
                ).fetchall()
            if not rows:
                await q.message.reply_text("কোনো Pending Deposit নেই।")
                return
            for r in rows:
                await q.message.reply_text(
                    f"ডিপোজিট #{r['id']}\nUser: {r['user_id']}\n{r['method']} ৳{r['amount']}\n"
                    f"TrxID: {r['transaction_id']}",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ Approve", callback_data=f"dep:approve:{r['id']}"),
                        InlineKeyboardButton("❌ Cancel", callback_data=f"dep:cancel:{r['id']}")
                    ]])
                )
        elif action == "users":
            with db() as con:
                count = con.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
            await q.message.reply_text(f"মোট নিবন্ধিত ইউজার: {count}")
        elif action == "stockhelp":
            await q.message.reply_text(
                "পণ্য যোগ করতে:\n/addproduct নাম | দাম | KEY-1,KEY-2\n"
                "উদাহরণ:\n/addproduct Premium Panel | 100 | KEY-A1,KEY-A2\n"
                "শুধু বৈধ ও বিক্রির অনুমতি থাকা প্যানেল Key যোগ করুন।"
            )
        return

    if data.startswith("dep:"):
        if not is_admin(uid):
            await q.message.reply_text("⛔ অনুমতি নেই।")
            return
        _, action, dep_text = data.split(":")
        dep_id = int(dep_text)
        with db() as con:
            dep = con.execute("SELECT * FROM deposits WHERE id=?", (dep_id,)).fetchone()
            if not dep:
                await q.message.reply_text("ডিপোজিট পাওয়া যায়নি।")
                return
            if dep["status"] != "pending":
                await q.message.reply_text(f"এই অনুরোধ ইতিমধ্যে {dep['status']} হয়েছে।")
                return
            if action == "approve":
                con.execute("UPDATE deposits SET status='approved',reviewed_at=?,reviewed_by=? WHERE id=? AND status='pending'",
                            (now(), uid, dep_id))
                con.execute("UPDATE users SET balance=balance+? WHERE user_id=?",
                            (dep["amount"], dep["user_id"]))
                status_text = "approved"
            elif action == "cancel":
                con.execute("UPDATE deposits SET status='cancelled',reviewed_at=?,reviewed_by=? WHERE id=? AND status='pending'",
                            (now(), uid, dep_id))
                status_text = "cancelled"
            else:
                return
        await q.message.reply_text(f"ডিপোজিট #{dep_id}: {status_text}")
        try:
            if action == "approve":
                await context.bot.send_message(
                    dep["user_id"],
                    f"✅ আপনার ডিপোজিট #{dep_id} অনুমোদিত হয়েছে। ৳{dep['amount']} ব্যালেন্সে যোগ হয়েছে।"
                )
            else:
                await context.bot.send_message(
                    dep["user_id"],
                    f"❌ আপনার ডিপোজিট #{dep_id} বাতিল করা হয়েছে। প্রয়োজনে অ্যাডমিনের সঙ্গে যোগাযোগ করুন।"
                )
        except Exception:
            log.exception("Could not notify deposit user")
        return

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error: %s", context.error, exc_info=context.error)

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("addproduct", addproduct_cmd))
    app.add_handler(CommandHandler("products", products_cmd))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)
    log.info("Telegram panel bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
