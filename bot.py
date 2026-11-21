"""ESMO Telegram receipt bot:  order (text or photo)  ->  AI  ->  receipt (PNG + PDF)."""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import tempfile

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters)

import ai_parser
import config
import core
import receipt

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("esmo-bot")
# httpx logs full request URLs at INFO level, and Telegram URLs contain the bot token - keep them out of the logs
for _noisy in ("httpx", "httpcore", "telegram", "hpack"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

HELP = (
    "🧾 *ESMO Receipt Bot*\n\n"
    "Send me an order as *text* or a *photo*. I'll read it, ask you anything that is missing "
    "(retail/wholesale, payment method, customer…) and send back the receipt.\n\n"
    "Example:\n`Alhaji Musa, Zaria. Amprococ 100g x20, Tyloesmo 250 x10, wholesale, transfer, discount 5000`\n\n"
    "Commands:\n"
    "/products – price list\n"
    "Send your app's *backup.json* file to load ALL your products at once\n"
    "/setprice NAME | retail | whole | supply – add/update products (one per line for many)\n"
    "/delproduct NAME – remove a product\n"
    "/setinv 800 – set the next invoice number\n"
    "/today – today's sales summary\n"
    "/export – download all sales as CSV\n\n"
    "Admin only:\n"
    "/users – who can use the bot\n"
    "/adduser ID – give someone access\n"
    "/removeuser ID – remove access"
)


def authorised(update: Update) -> bool:
    return bool(update.effective_user) and core.is_allowed(update.effective_user.id)


async def guard(update: Update) -> bool:
    """Allowed people pass. Everyone else is ignored completely: no reply, no error, nothing."""
    return authorised(update)


async def admin_guard(update: Update) -> bool:
    if not await guard(update):
        return False
    if not core.is_admin(update.effective_user.id):
        await update.effective_message.reply_text("Only the admin can do that.")
        return False
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await update.message.reply_text(HELP, parse_mode="Markdown")


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    # Setup mode: while nobody is configured yet, show the ID so the owner can set it. Otherwise only allowed people get a reply.
    if core.allowed_ids() and not core.is_allowed(user.id):
        return
    await update.message.reply_text(f"Your Telegram ID: {user.id}")


def _parse_id(args):
    return int(args[0]) if args and args[0].lstrip("-").isdigit() else None


async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_guard(update):
        return
    admins = sorted(core.admin_ids())
    others = sorted(core.allowed_ids() - core.admin_ids())
    lines = ["👥 People who can use this bot", ""] + [f"{i}  (admin)" for i in admins] + [str(i) for i in others]
    lines += ["", "/adduser ID  •  /removeuser ID",
              "New people can get their ID by messaging @userinfobot on Telegram."]
    await update.message.reply_text("\n".join(lines))


async def adduser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_guard(update):
        return
    uid = _parse_id(context.args)
    if uid is None:
        await update.message.reply_text("Use: /adduser 123456789\n(they can find their ID by messaging @userinfobot)")
        return
    if core.is_allowed(uid):
        await update.message.reply_text(f"{uid} already has access.")
        return
    try:
        core.add_user(uid)
    except Exception:
        await update.message.reply_text("❌ Could not save that (database problem). Try again in a minute.")
        return
    await update.message.reply_text(f"✅ {uid} can now use the bot.")


async def removeuser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_guard(update):
        return
    uid = _parse_id(context.args)
    if uid is None:
        await update.message.reply_text("Use: /removeuser 123456789")
        return
    try:
        removed = core.remove_user(uid)
    except ValueError:
        await update.message.reply_text("That is an admin. Admins are set in ALLOWED_USER_IDS on Render.")
        return
    except Exception:
        await update.message.reply_text("❌ Could not save that (database problem). Try again in a minute.")
        return
    await update.message.reply_text(f"🗑 {uid} removed." if removed else f"{uid} was not on the list.")


async def products_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    items = core.products()
    if not items:
        await update.message.reply_text("No products yet. Send your app's backup.json, or use /setprice.")
        return
    rows = [f"{p['name']} | {p['retail']:,.0f} | {p['whole']:,.0f} | {p['supply']:,.0f}" for p in items]
    chunk, size = ["PRODUCT | RETAIL | WHOLE | SUPPLY   (" + str(len(items)) + " products)"], 0
    for r in rows:
        if size + len(r) > 3500:
            await update.message.reply_text("\n".join(chunk))
            chunk, size = [], 0
        chunk.append(r)
        size += len(r) + 1
    await update.message.reply_text("\n".join(chunk))


async def setprice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    parts = update.message.text.split(None, 1)
    lines = [l for l in (parts[1].splitlines() if len(parts) > 1 else []) if l.strip()]
    if not lines:
        await update.message.reply_text(
            "Use: /setprice NAME | retail | whole | supply\nOne price for all types: /setprice NAME | 1300\n"
            "Many products: put one per line after /setprice.")
        return
    saved, bad = [], []
    for line in lines:
        row = core.parse_price_line(line)
        if row:
            core.upsert_product(*row)
            saved.append(row[0])
        else:
            bad.append(line.strip())
    msg = f"✅ Saved {len(saved)} product(s)." if saved else ""
    if bad:
        msg += "\n⚠️ Could not read:\n" + "\n".join(f"• {x}" for x in bad[:10])
    await update.message.reply_text(msg.strip())


async def delproduct_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    name = " ".join(context.args)
    ok = core.delete_product(name) if name else False
    await update.message.reply_text("🗑 Removed." if ok else "Product not found. Use /products to see exact names.")


async def setinv_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if context.args and context.args[0].isdigit():
        core.set_invoice(int(context.args[0]))
        await update.message.reply_text(f"✅ Next invoice number is #{context.args[0]}")
    else:
        await update.message.reply_text(f"Next invoice number is #{core.peek_invoice()}.\nChange it with /setinv 800")


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    day = core.now().strftime("%d/%m/%Y")
    s = core.day_summary(day)
    lines = [f"📊 {day}", f"Invoices: {s['count']}" + (f" (+{s['voided']} void)" if s["voided"] else ""),
             f"Total sales: NGN {s['total']:,.0f}"]
    for mode, amt in s["by_mode"].items():
        lines.append(f"  {mode}: NGN {amt:,.0f}" + ("  ← owed (credit)" if mode == "LOAN" else ""))
    await update.message.reply_text("\n".join(lines))


async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    path = core.export_csv(os.path.join(tempfile.gettempdir(), "esmo_sales.csv"))
    with open(path, "rb") as f:
        await update.message.reply_document(f, filename="ESMO_sales.csv")


def _caption(order, warnings):
    lines = [f"🧾 INV #{order['inv']} — {order['customer']}",
             f"Total: NGN {order['total']:,.0f}  •  {order['mode']}  •  {order['sale_type'].title()}"]
    if warnings:
        lines.append("")
        lines.append("⚠️ Please check:")
        lines += [f"• {w}" for w in warnings[:6]]
        if len(warnings) > 6:
            lines.append(f"• …and {len(warnings) - 6} more")
    cap = "\n".join(lines)
    return cap if len(cap) <= 1000 else cap[:997] + "…"


PENDING = "pending"


def _cancel_btn(pid):
    return InlineKeyboardButton("✖ Cancel order", callback_data=f"q:{pid}:cancel:x")


async def ask_next(context: ContextTypes.DEFAULT_TYPE, pending):
    """Ask the next missing detail, or finish the order when nothing is missing."""
    q = core.next_question(pending["parsed"])
    if q is None:
        await finish_order(context, pending)
        return
    kind, idx = q
    pid, p = pending["pid"], pending["parsed"]
    cancel = _cancel_btn(pid)
    kb = None
    if kind == "sale":
        text = "Is this a retail, wholesale or supply sale?"
        kb = [[InlineKeyboardButton("Retail", callback_data=f"q:{pid}:sale:retail"),
               InlineKeyboardButton("Wholesale", callback_data=f"q:{pid}:sale:whole"),
               InlineKeyboardButton("Supply", callback_data=f"q:{pid}:sale:supply")]]
    elif kind == "pay":
        text = "How is the customer paying?"
        kb = [[InlineKeyboardButton("💵 Cash", callback_data=f"q:{pid}:pay:CASH"),
               InlineKeyboardButton("🏦 Transfer", callback_data=f"q:{pid}:pay:TRANSFER")],
              [InlineKeyboardButton("💳 POS", callback_data=f"q:{pid}:pay:POS"),
               InlineKeyboardButton("📒 Loan / Debt", callback_data=f"q:{pid}:pay:LOAN")]]
    elif kind == "cust":
        text = "What is the customer's name? Type it, or tap Walk-in."
        kb = [[InlineKeyboardButton("🚶 Walk-in customer", callback_data=f"q:{pid}:cust:walkin")]]
    elif kind == "addr":
        text = f"What is {p['customer']}'s address / town? Type it, or tap Skip."
        kb = [[InlineKeyboardButton("⏭ Skip", callback_data=f"q:{pid}:addr:skip")]]
    elif kind == "qty":
        text = f"How many {p['items'][idx]['name']}? Type the quantity."
        kb = [[InlineKeyboardButton("Remove this item", callback_data=f"q:{pid}:rm:{idx}")]]
    elif kind == "disc":
        sub = core.subtotal_of(p)
        text = (f"Subtotal is NGN {sub:,.0f}.\nAny discount? Type the amount (e.g. 5000) or a percent (e.g. 5%), "
                f"or tap No discount.")
        kb = [[InlineKeyboardButton("No discount", callback_data=f"q:{pid}:disc:none")]]
    else:  # price
        it = p["items"][idx]
        text = (f"{it['name']} is not in the price list.\nWhat is the price per unit? Type the amount, e.g. 1200.")
        kb = [[InlineKeyboardButton("Remove this item", callback_data=f"q:{pid}:rm:{idx}")]]
    kb.append([cancel])
    msg = await context.bot.send_message(pending["chat_id"], "❓ " + text, reply_markup=InlineKeyboardMarkup(kb))
    pending["q_msg"] = msg.message_id


async def _close_question(context, pending, note=None):
    """Remove the buttons from the last question (and show what was chosen)."""
    mid = pending.get("q_msg")
    if not mid:
        return
    try:
        if note:
            await context.bot.edit_message_text(note, pending["chat_id"], mid)
        else:
            await context.bot.edit_message_reply_markup(pending["chat_id"], mid, reply_markup=None)
    except Exception:
        pass


async def finish_order(context: ContextTypes.DEFAULT_TYPE, pending):
    chat_id = pending["chat_id"]
    context.user_data.pop(PENDING, None)
    try:
        order, warnings = core.build_order(pending["parsed"])
    except core.OrderProblem as e:
        await context.bot.send_message(chat_id, f"❌ {e}")
        return
    t = core.now()
    try:
        inv = core.take_invoice()
    except Exception as ex:
        log.error("invoice counter failed: %s", ex)
        await context.bot.send_message(chat_id, "❌ I could not reach the database, so no invoice was created. "
                                                "Please send the order again in a minute.")
        return
    order.update(inv=inv, date=t.strftime("%d/%m/%Y"), time=t.strftime("%H:%M"), status="ISSUED")
    saved = True
    try:
        await asyncio.to_thread(core.record_sale, order)
    except Exception as ex:
        saved = False
        log.error("saving sale %s failed: %s", inv, ex)
        warnings.insert(0, "NOT saved to the sales ledger (database problem). Note this invoice number.")
    with tempfile.TemporaryDirectory() as tmp:
        png, pdf = os.path.join(tmp, f"INV_{inv}.png"), os.path.join(tmp, f"INV_{inv}.pdf")
        await asyncio.to_thread(receipt.render_receipt, order, png, pdf)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Void this invoice", callback_data=f"void:{inv}")]]) if saved else None
        with open(png, "rb") as f:
            await context.bot.send_photo(chat_id, f, caption=_caption(order, warnings), reply_markup=kb)
        with open(pdf, "rb") as f:
            await context.bot.send_document(chat_id, f, filename=f"ESMO_Invoice_{inv}.pdf")


async def start_order(update: Update, context: ContextTypes.DEFAULT_TYPE, text, image_bytes):
    msg = update.message
    old = context.user_data.pop(PENDING, None)
    if old:
        await _close_question(context, old, "↩️ Previous order dropped.")
    status = await msg.reply_text("🔍 Reading the order…")
    await context.bot.send_chat_action(msg.chat_id, ChatAction.TYPING)
    try:
        raw = await asyncio.to_thread(ai_parser.parse_order, text, image_bytes, [p["name"] for p in core.products()])
        parsed = core.normalize_parsed(raw)
    except ai_parser.AIBusy:
        await status.edit_text("⏳ The free AI is busy right now. Please send the order again in a minute.")
        return
    except Exception as e:
        log.error("AI failure: %s", type(e).__name__)
        await status.edit_text("❌ I could not read that order. Please try again, or send clearer text/photo.")
        return
    if not parsed["items"]:
        await status.edit_text("❌ I could not find any products in that. Send the order as text, or a clearer photo.")
        return
    await status.delete()
    pending = {"pid": random.randint(1000, 9999), "parsed": parsed, "chat_id": msg.chat_id}
    context.user_data[PENDING] = pending
    seen = "\n".join(f"• {i['name']} × {i['qty'] or '?'}" for i in parsed["items"])
    await context.bot.send_message(msg.chat_id, f"👀 I read:\n{seen}\n\nA few quick questions…" if core.next_question(parsed)
                                   else f"👀 I read:\n{seen}")
    await ask_next(context, pending)


def _typed_number(text):
    try:
        v = float(text.replace(",", "").replace("₦", "").strip())
        return v if v > 0 else None
    except ValueError:
        return None


async def try_answer_with_text(context, pending, text):
    """Use a typed message as the answer to the current question. Returns False if it looks like a new order."""
    p = pending["parsed"]
    q = core.next_question(p)
    if q is None:
        return False
    kind, idx = q
    if kind == "sale":
        v = core.parse_sale_type(text)
        if not v or len(text) > 30:
            return False
        p["sale_type"] = v
    elif kind == "pay":
        v = core.parse_pay_mode(text)
        if not v or len(text) > 30:
            return False
        p["payment_mode"] = v
    elif kind == "cust":
        if len(text) > 80:
            return False
        p["customer"] = "WALK-IN" if text.lower().replace(" ", "").replace("-", "") == "walkin" else text
    elif kind == "addr":
        if len(text) > 120:
            return False
        p["address"] = "N/A" if text.lower() in ("skip", "-", "none") else text
    elif kind == "disc":
        sub = core.subtotal_of(p)
        d = core.parse_discount(text, sub)
        if d is None:
            if len(text) > 25:
                return False
            await context.bot.send_message(pending["chat_id"],
                                           "Type an amount like 5000, a percent like 5%, or 'no'.")
            return True
        if d > sub:
            await context.bot.send_message(pending["chat_id"], f"The discount can't be more than the subtotal (NGN {sub:,.0f}).")
            return True
        p["discount"], p["discount_done"] = d, True
    else:
        n = _typed_number(text)
        if n is None:
            if len(text) > 25:
                return False
            await context.bot.send_message(pending["chat_id"], "Please type just a number, e.g. 1200.")
            return True
        p["items"][idx]["qty" if kind == "qty" else "unit_price"] = n
    await _close_question(context, pending)
    await ask_next(context, pending)
    return True


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    text = update.message.text.strip()
    pending = context.user_data.get(PENDING)
    if pending and await try_answer_with_text(context, pending, text):
        return
    await start_order(update, context, text, None)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    tg_file = await update.message.photo[-1].get_file()
    data = bytes(await tg_file.download_as_bytearray())
    await start_order(update, context, update.message.caption, data)


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    doc = update.message.document
    name = (doc.file_name or "").lower()
    if (doc.mime_type or "").startswith("image/"):
        tg_file = await doc.get_file()
        data = bytes(await tg_file.download_as_bytearray())
        await start_order(update, context, update.message.caption, data)
    elif name.endswith(".json"):
        if (doc.file_size or 0) > 5_000_000:
            await update.message.reply_text("That file is too large.")
            return
        tg_file = await doc.get_file()
        try:
            count, nxt = core.import_backup(json.loads(bytes(await tg_file.download_as_bytearray())))
        except (ValueError, UnicodeDecodeError):
            await update.message.reply_text("❌ That doesn't look like the app's backup.json (no products found).")
            return
        deleted = True
        try:   # the backup also contains the app login, so remove it from the chat
            await update.message.delete()
        except Exception:
            deleted = False
        await update.message.chat.send_message(
            f"✅ Loaded {count} products from your app. Next invoice number: #{nxt}.\n"
            + ("🔒 I deleted the backup file from this chat because it contains your app login."
               if deleted else "⚠️ Please delete that backup file from this chat - it contains your app login."))
    else:
        await update.message.reply_text("Send the order as text or an image. To load products, send the app's backup.json.")


async def on_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not authorised(update):
        await q.answer()
        return
    _, pid, field, value = q.data.split(":", 3)
    pending = context.user_data.get(PENDING)
    if not pending or str(pending["pid"]) != pid:
        await q.answer("This question has expired.", show_alert=True)
        return
    p = pending["parsed"]
    await q.answer()
    if field == "cancel":
        context.user_data.pop(PENDING, None)
        await q.edit_message_text("✖ Order cancelled.")
        return
    note = None
    if field == "sale":
        p["sale_type"], note = value, f"✅ Sale type: {value.replace('whole', 'wholesale').title()}"
    elif field == "pay":
        p["payment_mode"], note = value, f"✅ Payment: {value}"
    elif field == "cust":
        p["customer"], note = "WALK-IN", "✅ Customer: walk-in"
    elif field == "addr":
        p["address"], note = "N/A", "✅ Address skipped"
    elif field == "disc":
        p["discount"], p["discount_done"], note = 0, True, "✅ No discount"
    elif field == "rm":
        i = int(value)
        if 0 <= i < len(p["items"]):
            note = f"✅ Removed {p['items'][i]['name']}"
            p["items"].pop(i)
        if not p["items"]:
            context.user_data.pop(PENDING, None)
            await q.edit_message_text("✖ No items left, order cancelled.")
            return
    await q.edit_message_text(note or "✅")
    await ask_next(context, pending)


async def on_void(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not authorised(update):
        await q.answer()
        return
    inv = int(q.data.split(":")[1])
    if core.void_sale(inv):
        await q.answer("Invoice voided")
        await q.edit_message_caption((q.message.caption or "") + f"\n\n❌ INV #{inv} VOIDED", reply_markup=None)
    else:
        await q.answer("Invoice not found", show_alert=True)


async def serve_webhook(app):
    """Hosted mode: Telegram pushes updates to us (works on hosts that sleep when idle)."""
    import uvicorn
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import PlainTextResponse, Response
    from starlette.routing import Route

    # Telegram only accepts A-Z a-z 0-9 _ - in the secret token, so derive a safe hex token from WEBHOOK_SECRET
    secret = hashlib.sha256(config.WEBHOOK_SECRET.encode()).hexdigest()

    async def telegram(request: Request):
        if not hmac.compare_digest(request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), secret):
            return Response(status_code=403)
        await app.update_queue.put(Update.de_json(await request.json(), app.bot))
        return Response()

    async def health(_: Request):
        return PlainTextResponse("ESMO receipt bot is running")

    web = Starlette(routes=[Route("/telegram", telegram, methods=["POST"]),
                            Route("/", health, methods=["GET", "HEAD"]),
                            Route("/healthz", health, methods=["GET", "HEAD"])])
    server = uvicorn.Server(uvicorn.Config(web, host="0.0.0.0", port=config.PORT, use_colors=False, log_level="warning"))
    async with app:
        await app.bot.set_webhook(f"{config.BASE_URL}/telegram", secret_token=secret,
                                  allowed_updates=Update.ALL_TYPES)
        await app.start()
        log.info("Webhook mode: listening on port %s, webhook %s/telegram", config.PORT, config.BASE_URL)
        await server.serve()
        await app.stop()


def add_handlers(app):
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("products", products_cmd))
    app.add_handler(CommandHandler("setprice", setprice_cmd))
    app.add_handler(CommandHandler("delproduct", delproduct_cmd))
    app.add_handler(CommandHandler("setinv", setinv_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("export", export_cmd))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("adduser", adduser_cmd))
    app.add_handler(CommandHandler("removeuser", removeuser_cmd))
    app.add_handler(CallbackQueryHandler(on_void, pattern=r"^void:\d+$"))
    app.add_handler(CallbackQueryHandler(on_question, pattern=r"^q:\d+:"))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))


def main():
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in the .env file.")
    log.info("AI providers, in order: %s", ", ".join(config.ready_providers()))
    if not config.ALLOWED_USER_IDS:
        log.warning("ALLOWED_USER_IDS is empty - there is no admin. Send /myid to the bot to get your ID and set it.")
    from storage import STORE
    log.info("Data is kept in: %s", STORE.name)
    if config.BASE_URL:
        if not config.WEBHOOK_SECRET:
            raise SystemExit("Hosted mode needs WEBHOOK_SECRET (any long random text) so strangers cannot fake Telegram messages.")
        if STORE.name == "local files":
            log.warning("Hosted mode but no SUPABASE_URL/SUPABASE_KEY: sales and invoice numbers will be LOST on restart!")
        app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).updater(None).build()
        add_handlers(app)
        asyncio.run(serve_webhook(app))
    else:
        app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
        add_handlers(app)
        log.info("ESMO receipt bot running (polling)…")
        app.run_polling()


if __name__ == "__main__":
    main()
