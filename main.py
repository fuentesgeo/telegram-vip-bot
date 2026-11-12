import asyncio
import logging
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from telegram import (
    Update, LabeledPrice, ChatPermissions, InlineKeyboardButton,
    InlineKeyboardMarkup, InputPaidMediaPhoto
)
from telegram.constants import ChatType
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    PreCheckoutQueryHandler, ContextTypes, filters
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
GROUP_ID = int(os.getenv("GROUP_ID", "0"))
MONTHLY_PRICE = int(os.getenv("MONTHLY_PRICE_STARS", "500"))
DB_PATH = os.getenv("DB_PATH", "vip_store.db")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "").strip().lstrip("@")
CHECK_INTERVAL = 60
SUBSCRIPTION_SECONDS = 30 * 24 * 60 * 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("telegram-vip")


def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("""
        CREATE TABLE IF NOT EXISTS invoice_payloads (
            payload TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS members (
            user_id INTEGER PRIMARY KEY,
            expires_at INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            last_charge_id TEXT,
            updated_at INTEGER NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            payload TEXT NOT NULL,
            charge_id TEXT NOT NULL,
            amount INTEGER NOT NULL,
            currency TEXT NOT NULL,
            paid_at INTEGER NOT NULL,
            recurring INTEGER NOT NULL DEFAULT 0
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id TEXT NOT NULL,
            price INTEGER NOT NULL,
            caption TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL
        )
    """)
    con.commit()
    return con


def is_owner(user_id: int) -> bool:
    return OWNER_ID != 0 and user_id == OWNER_ID


def private_chat(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type == ChatType.PRIVATE)


async def owner_only(update: Update) -> bool:
    if not update.effective_user or not is_owner(update.effective_user.id):
        if update.effective_message:
            await update.effective_message.reply_text("Este comando está reservado al propietario del bot.")
        return False
    return True


def format_date(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not private_chat(update):
        return
    await update.effective_message.reply_text(
        "✨ Bienvenido/a a VIP.\n\n"
        "Aquí puedes comprar una membresía mensual para acceder al canal y al grupo privado, "
        "o desbloquear fotografías individuales.\n\n"
        f"⭐ Membresía mensual: {MONTHLY_PRICE} Telegram Stars.\n\n"
        "Comandos:\n"
        "/subscribe - comprar o renovar membresía\n"
        "/photos - ver fotografías de pago\n"
        "/status - consultar tu acceso\n"
        "/cancelrenewal - cancelar la renovación automática\n"
        "/paysupport - ayuda con pagos\n\n"
        "Solo contenido legal de personas adultas que hayan dado su consentimiento."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not private_chat(update):
        await update.effective_message.reply_text("Abre el bot por privado para contratar la membresía.")
        return
    user = update.effective_user
    payload = f"vip_{user.id}_{uuid.uuid4().hex[:16]}"
    con = connect()
    con.execute(
        "INSERT INTO invoice_payloads(payload,user_id,created_at) VALUES(?,?,?)",
        (payload, user.id, int(time.time()))
    )
    con.commit()
    con.close()

    await context.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title="Membresía VIP mensual",
        description="Acceso durante 30 días al canal VIP y al grupo privado. Renovación automática mensual.",
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice("Membresía VIP por 30 días", MONTHLY_PRICE)],
        subscription_period=SUBSCRIPTION_SECONDS,
        protect_content=True,
    )


async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    con = connect()
    row = con.execute(
        "SELECT user_id FROM invoice_payloads WHERE payload=?", (query.invoice_payload,)
    ).fetchone()
    con.close()

    valid = (
        row is not None
        and int(row["user_id"]) == query.from_user.id
        and query.currency == "XTR"
        and query.total_amount == MONTHLY_PRICE
    )
    await query.answer(
        ok=valid,
        error_message=None if valid else "No pude validar esta factura. Vuelve a abrir /subscribe."
    )


async def send_access_links(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    expires = int(time.time()) + 10 * 60
    links = []
    for chat_id, label in ((CHANNEL_ID, "Canal VIP"), (GROUP_ID, "Grupo privado")):
        invite = await context.bot.create_chat_invite_link(
            chat_id=chat_id,
            name=f"VIP-{user_id}",
            member_limit=1,
            expire_date=expires,
        )
        links.append(f"{label}: {invite.invite_link}")
    await context.bot.send_message(
        chat_id=user_id,
        text="✅ Pago confirmado. Tu acceso está listo.\n\n"
             + "\n".join(links)
             + "\n\nLos enlaces son personales, de un solo uso y caducan en 10 minutos.",
        protect_content=True,
    )


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    payment = msg.successful_payment
    user_id = update.effective_user.id

    con = connect()
    payload_row = con.execute(
        "SELECT user_id FROM invoice_payloads WHERE payload=?", (payment.invoice_payload,)
    ).fetchone()
    if (
        payload_row is None
        or int(payload_row["user_id"]) != user_id
        or payment.currency != "XTR"
        or payment.total_amount <= 0
    ):
        con.close()
        log.error("Pago recibido que no coincide con una factura pendiente; user=%s", user_id)
        await msg.reply_text("No pude asociar este pago a una factura válida. Contacta con /paysupport.")
        return

    now = int(time.time())
    expires_at = getattr(payment, "subscription_expiration_date", None) or (now + SUBSCRIPTION_SECONDS)
    charge_id = payment.telegram_payment_charge_id
    existing = con.execute(
        "SELECT expires_at, active FROM members WHERE user_id=?", (user_id,)
    ).fetchone()
    was_active = bool(existing and existing["active"] and int(existing["expires_at"]) > now)

    con.execute("""
        INSERT INTO members(user_id,expires_at,active,last_charge_id,updated_at)
        VALUES(?,?,1,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            expires_at=excluded.expires_at,
            active=1,
            last_charge_id=excluded.last_charge_id,
            updated_at=excluded.updated_at
    """, (user_id, int(expires_at), charge_id, now))
    con.execute("""
        INSERT INTO payments(user_id,payload,charge_id,amount,currency,paid_at,recurring)
        VALUES(?,?,?,?,?,?,?)
    """, (
        user_id, payment.invoice_payload, charge_id, payment.total_amount,
        payment.currency, now, int(bool(getattr(payment, "is_recurring", False)))
    ))
    con.commit()
    con.close()

    await msg.reply_text(
        f"✅ Pago confirmado. Tu membresía está activa hasta {format_date(int(expires_at))}."
    )
    if not was_active:
        try:
            await send_access_links(user_id, context)
        except Exception:
            log.exception("No se pudieron crear/enviar enlaces de acceso para %s", user_id)
            await context.bot.send_message(
                user_id,
                "Tu pago fue registrado, pero no pude generar los enlaces. Contacta con /paysupport."
            )
    else:
        await context.bot.send_message(
            user_id,
            "Tu membresía se renovó correctamente. Conservas tu acceso actual.",
            protect_content=True,
        )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not private_chat(update):
        return
    con = connect()
    row = con.execute("SELECT expires_at,active FROM members WHERE user_id=?", (update.effective_user.id,)).fetchone()
    con.close()
    now = int(time.time())
    if row and row["active"] and int(row["expires_at"]) > now:
        await update.effective_message.reply_text(
            f"🟢 Tu membresía está activa hasta {format_date(int(row['expires_at']))}."
        )
    else:
        await update.effective_message.reply_text(
            "🔴 No tienes una membresía activa. Usa /subscribe para obtener acceso."
        )


async def cancel_renewal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not private_chat(update):
        return
    con = connect()
    row = con.execute(
        "SELECT last_charge_id,expires_at,active FROM members WHERE user_id=?",
        (update.effective_user.id,)
    ).fetchone()
    con.close()
    if not row or not row["last_charge_id"] or not row["active"]:
        await update.effective_message.reply_text("No encuentro una suscripción activa que pueda cancelar.")
        return
    try:
        await context.bot.edit_user_star_subscription(
            user_id=update.effective_user.id,
            telegram_payment_charge_id=row["last_charge_id"],
            is_canceled=True,
        )
        await update.effective_message.reply_text(
            "La renovación automática quedó cancelada. Mantendrás el acceso hasta "
            f"{format_date(int(row['expires_at']))}."
        )
    except Exception:
        log.exception("Error al cancelar renovación")
        await update.effective_message.reply_text(
            "No pude cancelar la renovación desde aquí. Contacta con /paysupport."
        )


async def paysupport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if SUPPORT_USERNAME:
        text = f"Para ayuda con pagos, escribe a @{SUPPORT_USERNAME}."
    else:
        text = "Para ayuda con pagos, contacta con el administrador del canal o grupo."
    await update.effective_message.reply_text(text)


async def photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not private_chat(update):
        await update.effective_message.reply_text("Abre el bot por privado para ver el catálogo.")
        return
    con = connect()
    rows = con.execute(
        "SELECT id,price,caption FROM photos WHERE active=1 ORDER BY id DESC"
    ).fetchall()
    con.close()
    if not rows:
        await update.effective_message.reply_text("Todavía no hay fotografías disponibles.")
        return
    for row in rows:
        caption = row["caption"] or "Contenido exclusivo"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"Desbloquear por ⭐ {row['price']}",
                callback_data=f"photo:{row['id']}"
            )
        ]])
        await update.effective_message.reply_text(
            f"📸 {caption}\nPrecio: ⭐ {row['price']}",
            reply_markup=keyboard,
            protect_content=True,
        )


async def buy_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        photo_id = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.edit_message_text("Producto no válido.")
        return
    con = connect()
    row = con.execute(
        "SELECT file_id,price,caption FROM photos WHERE id=? AND active=1", (photo_id,)
    ).fetchone()
    con.close()
    if not row:
        await query.edit_message_text("Esta fotografía ya no está disponible.")
        return
    try:
        await context.bot.send_paid_media(
            chat_id=query.from_user.id,
            star_count=int(row["price"]),
            media=[InputPaidMediaPhoto(media=row["file_id"])],
            payload=f"photo_{photo_id}_{query.from_user.id}_{uuid.uuid4().hex[:8]}",
            caption=row["caption"][:1024],
            protect_content=True,
        )
        await query.message.reply_text(
            "Te he enviado la publicación de pago por privado. Completa la compra en Telegram para verla."
        )
    except Exception:
        log.exception("Error enviando paid media")
        await query.message.reply_text("No pude enviar esta fotografía. Inténtalo más tarde.")


async def add_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return
    msg = update.effective_message
    if not private_chat(update) or not msg.photo:
        await msg.reply_text(
            "Envía una fotografía al bot con este pie de foto:\n"
            "/addphoto 100 Descripción de la fotografía\n\n"
            "Cambia 100 por el precio en Telegram Stars."
        )
        return
    parts = (msg.caption or "").split(maxsplit=2)
    if len(parts) < 2 or parts[0].split("@")[0] != "/addphoto":
        await msg.reply_text("Formato: /addphoto PRECIO descripción")
        return
    try:
        price = int(parts[1])
        if not 1 <= price <= 25000:
            raise ValueError
    except ValueError:
        await msg.reply_text("El precio debe ser un número entre 1 y 25000 Stars.")
        return
    caption = parts[2] if len(parts) > 2 else "Contenido exclusivo"
    con = connect()
    cur = con.execute(
        "INSERT INTO photos(file_id,price,caption,active,created_at) VALUES(?,?,?,1,?)",
        (msg.photo[-1].file_id, price, caption, int(time.time()))
    )
    photo_id = cur.lastrowid
    con.commit()
    con.close()
    await msg.reply_text(f"✅ Fotografía #{photo_id} añadida al catálogo por ⭐ {price}.")


async def delete_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Uso: /delphoto ID")
        return
    try:
        photo_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("El ID debe ser numérico.")
        return
    con = connect()
    cur = con.execute("UPDATE photos SET active=0 WHERE id=?", (photo_id,))
    con.commit()
    con.close()
    await update.effective_message.reply_text(
        "Fotografía retirada del catálogo." if cur.rowcount else "No encontré esa fotografía."
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await owner_only(update):
        return
    con = connect()
    now = int(time.time())
    active = con.execute(
        "SELECT COUNT(*) AS n FROM members WHERE active=1 AND expires_at>?", (now,)
    ).fetchone()["n"]
    total_stars = con.execute(
        "SELECT COALESCE(SUM(amount),0) AS n FROM payments"
    ).fetchone()["n"]
    photo_count = con.execute(
        "SELECT COUNT(*) AS n FROM photos WHERE active=1"
    ).fetchone()["n"]
    con.close()
    await update.effective_message.reply_text(
        f"📊 Panel básico\nMiembros activos: {active}\n"
        f"Stars registrados: {total_stars}\nFotografías activas: {photo_count}"
    )


async def remove_from_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    try:
        await context.bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
        await context.bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True)
    except Exception as exc:
        log.warning("No se pudo retirar user %s de chat %s: %s", user_id, chat_id, exc)


async def expiry_worker(app: Application):
    while True:
        try:
            now = int(time.time())
            con = connect()
            rows = con.execute(
                "SELECT user_id FROM members WHERE active=1 AND expires_at<=?", (now,)
            ).fetchall()
            for row in rows:
                user_id = int(row["user_id"])
                await remove_from_chat(app, CHANNEL_ID, user_id)
                await remove_from_chat(app, GROUP_ID, user_id)
                con.execute(
                    "UPDATE members SET active=0,updated_at=? WHERE user_id=?",
                    (now, user_id)
                )
                try:
                    await app.bot.send_message(
                        user_id,
                        "Tu membresía VIP ha vencido y se ha retirado tu acceso. "
                        "Puedes volver a suscribirte con /subscribe."
                    )
                except Exception:
                    pass
            con.commit()
            con.close()
        except Exception:
            log.exception("Error en la comprobación de vencimientos")
        await asyncio.sleep(CHECK_INTERVAL)


async def post_init(app: Application):
    app.create_task(expiry_worker(app))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Error no controlado al procesar actualización", exc_info=context.error)


def main():
    if not BOT_TOKEN:
        raise SystemExit("Configura BOT_TOKEN en el archivo .env.")
    if not OWNER_ID or not CHANNEL_ID or not GROUP_ID:
        raise SystemExit("Configura OWNER_ID, CHANNEL_ID y GROUP_ID en el archivo .env.")
    if CHANNEL_ID == GROUP_ID:
        raise SystemExit("CHANNEL_ID y GROUP_ID deben ser diferentes.")
    connect().close()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("cancelrenewal", cancel_renewal))
    app.add_handler(CommandHandler("paysupport", paysupport))
    app.add_handler(CommandHandler("photos", photos))
    app.add_handler(CommandHandler("addphoto", add_photo))
    app.add_handler(CommandHandler("delphoto", delete_photo))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CallbackQueryHandler(buy_photo, pattern=r"^photo:\d+$"))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler(filters.PHOTO & filters.CaptionRegex(r"^/addphoto(?:@\w+)?\s"), add_photo))
    app.add_error_handler(error_handler)
    log.info("Bot VIP iniciado.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
