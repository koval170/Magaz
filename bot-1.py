import asyncio
import json
import html
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InputMediaPhoto
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW.isdigit() else 0
DEFAULT_DB_PATH = "/data/shop.sqlite3" if os.path.isdir("/data") and os.access("/data", os.W_OK) else "shop.sqlite3"
DB_PATH = os.getenv("DB_PATH", DEFAULT_DB_PATH).strip() or DEFAULT_DB_PATH

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()
PENDING_REJECTIONS = {}
KYIV_TZ = ZoneInfo("Europe/Kyiv")
# Track bot-generated screens so main-menu navigation replaces old screens instead of stacking them.
USER_SCREENS = {}

async def clear_user_screens(user_id):
    message_ids = USER_SCREENS.pop(user_id, [])
    for message_id in message_ids:
        try:
            await bot.delete_message(chat_id=user_id, message_id=message_id)
        except Exception:
            pass

def remember_screen(user_id, message):
    if message and getattr(message, "message_id", None):
        USER_SCREENS.setdefault(user_id, []).append(message.message_id)
        # Keep a bounded list in case some old message cannot be deleted.
        USER_SCREENS[user_id] = USER_SCREENS[user_id][-40:]

async def send_user_screen(user_id, text, **kwargs):
    msg = await bot.send_message(user_id, text, **kwargs)
    remember_screen(user_id, msg)
    return msg

CATEGORIES = {
    "hoodie": ("Худі", ""),
    "sweatshirt": ("Світшот", ""),
    "longsleeve": ("Лонгслів", ""),
    "tshirt": ("Футболка", ""),
}

class ProductForm(StatesGroup):
    category = State()
    name = State()
    price = State()
    description = State()
    photos = State()
    sizes = State()
    stock = State()

class EditProductForm(StatesGroup):
    field = State()
    value = State()
    photos = State()

class ContactForm(StatesGroup):
    name = State()
    phone = State()
    address = State()

class AdminRejectForm(StatesGroup):
    reason = State()

def db():
    # Ensure the parent folder exists (FadeHost persistent storage is /data).
    parent = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(parent, exist_ok=True)
    return sqlite3.connect(DB_PATH)

def init_db():
    with closing(db()) as con:
        con.execute("""CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, price REAL NOT NULL, description TEXT NOT NULL DEFAULT '',
            photo_file_id TEXT NOT NULL DEFAULT '', sizes TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'hoodie', photos TEXT NOT NULL DEFAULT '[]', stock INTEGER NOT NULL DEFAULT 10
        )""")
        cols = {r[1] for r in con.execute("PRAGMA table_info(products)").fetchall()}
        if "category" not in cols:
            con.execute("ALTER TABLE products ADD COLUMN category TEXT NOT NULL DEFAULT 'hoodie'")
        if "photos" not in cols:
            con.execute("ALTER TABLE products ADD COLUMN photos TEXT NOT NULL DEFAULT '[]'")
        if "stock" not in cols:
            con.execute("ALTER TABLE products ADD COLUMN stock INTEGER NOT NULL DEFAULT 10")
        con.execute("""CREATE TABLE IF NOT EXISTS cart (
            user_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
            size TEXT NOT NULL DEFAULT '', quantity INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(user_id, product_id, size)
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            customer_name TEXT NOT NULL, phone TEXT NOT NULL, address TEXT NOT NULL,
            items TEXT NOT NULL, total REAL NOT NULL, status TEXT NOT NULL DEFAULT 'Очікує уточнення',
            created_at TEXT NOT NULL, stock_restored INTEGER NOT NULL DEFAULT 0
        )""")
        order_cols = {r[1] for r in con.execute("PRAGMA table_info(orders)").fetchall()}
        if "stock_restored" not in order_cols:
            con.execute("ALTER TABLE orders ADD COLUMN stock_restored INTEGER NOT NULL DEFAULT 0")
        if "cancel_reason" not in order_cols:
            con.execute("ALTER TABLE orders ADD COLUMN cancel_reason TEXT NOT NULL DEFAULT ''")
        # Separate structured order lines let us restore exact stock on cancellation.
        con.execute("""CREATE TABLE IF NOT EXISTS order_items (
            order_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
            size TEXT NOT NULL DEFAULT '', quantity INTEGER NOT NULL,
            PRIMARY KEY(order_id, product_id, size)
        )""")
        con.commit()

def money(value):
    return f"{value:g} грн"

def is_admin(user_id):
    return bool(ADMIN_ID and user_id == ADMIN_ID)

def order_status_keyboard(order_id, status):
    """Action buttons reflect the saved status; terminal orders have no actions."""
    rows = []
    if status in ("Очікує уточнення", "Очікує підтвердження"):
        rows.append([InlineKeyboardButton(text="✅ Прийняти замовлення", callback_data=f"orderstatus:{order_id}:Підтверджено")])
        rows.append([InlineKeyboardButton(text="❌ Відмовити в замовленні", callback_data=f"orderstatus:{order_id}:Скасовано")])
    elif status == "Підтверджено":
        rows.append([InlineKeyboardButton(text="📦 Відправлено", callback_data=f"orderstatus:{order_id}:Відправлено")])
        rows.append([InlineKeyboardButton(text="❌ Відмовити в замовленні", callback_data=f"orderstatus:{order_id}:Скасовано")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None

def status_icon_fn(status):
    if status == "Скасовано":
        return "❌"
    if status in ("Підтверджено", "Відправлено"):
        return "✅"
    return "🕓"

def admin_orders_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🕓 Очікують підтвердження", callback_data="orderslist:pending")],
        [InlineKeyboardButton(text="✅ Прийняті замовлення", callback_data="orderslist:accepted")],
        [InlineKeyboardButton(text="❌ Скасовані замовлення", callback_data="orderslist:cancelled")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:home")],
    ])

def admin_back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:home")]])


def main_menu(user_id):
    rows = [
        [KeyboardButton(text="🛍️ Каталог"), KeyboardButton(text="🛒 Кошик")],
        [KeyboardButton(text="📦 Мої замовлення"), KeyboardButton(text="ℹ️ Про магазин")],
        [KeyboardButton(text="🚚 Доставка й оплата"), KeyboardButton(text="📞 Контакти")],
    ]
    if is_admin(user_id):
        rows.append([KeyboardButton(text="⚙️ Адмін-панель")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

def category_keyboard():
    kb = InlineKeyboardBuilder()
    for key, (label, _) in CATEGORIES.items():
        kb.button(text=label, callback_data=f"category:{key}")
    kb.button(text="⬅️ Назад у меню", callback_data="back:menu")
    kb.adjust(2, 2, 1)
    return kb.as_markup()

def get_products(category):
    with closing(db()) as con:
        return con.execute("""SELECT id,name,price,description,photo_file_id,sizes,category,photos
            FROM products WHERE active=1 AND stock>0 AND category=? ORDER BY id DESC""", (category,)).fetchall()

def get_product(pid):
    with closing(db()) as con:
        row = con.execute("""SELECT id,name,price,description,photo_file_id,sizes,category,photos
            FROM products WHERE id=? AND active=1 AND stock>0""", (pid,)).fetchone()
    if not row:
        return None
    data = list(row)
    try:
        photos = json.loads(data[7] or "[]")
    except Exception:
        photos = []
    if not photos and data[4]:
        photos = [data[4]]
    data[7] = photos
    return data

def products_keyboard(category):
    rows = get_products(category)
    if not rows:
        return None
    kb = InlineKeyboardBuilder()
    for row in rows:
        pid, name, price = row[0], row[1], row[2]
        kb.button(text=f"🔎 Детально · {name[:35]} — {money(price)}", callback_data=f"detail:{pid}:0")
    kb.button(text="⬅️ Назад до категорій", callback_data="back:categories")
    kb.adjust(1)
    return kb.as_markup()

async def show_categories(message: Message):
    # Simple text-only category screen, as requested.
    msg = await message.answer("🛍️ <b>Каталог одягу</b>\n\nОберіть категорію:",
                               reply_markup=category_keyboard(), parse_mode="HTML")
    remember_screen(message.chat.id, msg)

async def delete_callback_message(callback: CallbackQuery):
    """Delete the previous bot screen where possible; stale/old messages are harmless."""
    try:
        await callback.message.delete()
    except Exception:
        pass

async def show_product_detail(target, pid, index=0, edit=False):
    product = get_product(pid)
    if not product:
        if isinstance(target, CallbackQuery):
            await target.answer("Товар більше недоступний.", show_alert=True)
        return
    _, name, price, description, _old_photo, sizes, category, photos = product
    photo_count = len(photos)
    if photo_count:
        index = max(0, min(index, photo_count - 1))
        photo = photos[index]
    else:
        index, photo = 0, None
    # Plain text avoids HTML injection and Telegram caption parsing errors.
    caption = f"👕 {name}\n💰 {money(price)}"
    if description:
        caption += f"\n\n{description[:600]}"
    if sizes:
        caption += f"\n\n📏 Розміри: {sizes[:120]}"
    if photo_count:
        caption += f"\n\nФото {index+1}/{photo_count}"
    caption = caption[:1000]
    kb = InlineKeyboardBuilder()
    if photo_count > 1:
        kb.row(
            InlineKeyboardButton(text="⬅️", callback_data=f"detail:{pid}:{(index-1) % photo_count}"),
            InlineKeyboardButton(text="➡️", callback_data=f"detail:{pid}:{(index+1) % photo_count}")
        )
    kb.button(text="🛒 Замовити", callback_data=f"buy:{pid}")
    kb.button(text="⬅️ Назад до товарів", callback_data=f"category:{category}")
    kb.button(text="🏠 До категорій", callback_data="back:categories")
    kb.adjust(2, 1)
    markup = kb.as_markup()
    if isinstance(target, CallbackQuery):
        # Do not call edit_media on a text message: Telegram rejects changing text to media.
        await delete_callback_message(target)
        if photo:
            await target.message.answer_photo(photo, caption=caption, reply_markup=markup)
        else:
            await target.message.answer(caption, reply_markup=markup)
    else:
        if photo:
            await target.answer_photo(photo, caption=caption, reply_markup=markup)
        else:
            await target.answer(caption, reply_markup=markup)

@dp.message(CommandStart())
async def start(message: Message):
    await clear_user_screens(message.from_user.id)
    msg = await message.answer(
        "Вітаємо в магазині одягу! 👕\n\nОберіть категорію, перегляньте фото товарів і оформіть замовлення. "
        "Продавець зв’яжеться з вами, щоб уточнити деталі.",
        reply_markup=main_menu(message.from_user.id)
    )
    remember_screen(message.from_user.id, msg)

@dp.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer("Скористайтеся кнопками меню. Для замовлення відкрийте каталог або кошик.")

@dp.message(Command("cancel"))
async def cancel_flow(message: Message, state: FSMContext):
    current = await state.get_state()
    if current is None:
        await message.answer("Немає активної дії для скасування.", reply_markup=main_menu(message.from_user.id))
        return
    await state.clear()
    await message.answer("Дію скасовано.", reply_markup=main_menu(message.from_user.id))

@dp.message(F.text == "🛍️ Каталог")
async def catalog(message: Message):
    await clear_user_screens(message.from_user.id)
    await show_categories(message)
    # show_categories sends the new screen; register the most recent message via helper implementation below.

@dp.callback_query(F.data == "back:categories")
async def back_categories(callback: CallbackQuery):
    await delete_callback_message(callback)
    await callback.message.answer("🛍️ <b>Каталог одягу</b>\n\nОберіть категорію:", reply_markup=category_keyboard(), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "back:menu")
async def back_menu(callback: CallbackQuery):
    await delete_callback_message(callback)
    await callback.message.answer("Головне меню:", reply_markup=main_menu(callback.from_user.id))
    await callback.answer()

@dp.callback_query(F.data.startswith("category:"))
async def category_open(callback: CallbackQuery):
    category = callback.data.split(":", 1)[1]
    if category not in CATEGORIES:
        await callback.answer("Категорію не знайдено.", show_alert=True)
        return
    await delete_callback_message(callback)
    rows = get_products(category)
    label = CATEGORIES[category][0]
    markup = products_keyboard(category)
    if not rows:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад до категорій", callback_data="back:categories")]
        ])
        await callback.message.answer(f"У категорії «{label}» поки немає товарів.", reply_markup=kb)
    else:
        await callback.message.answer(f"👕 Категорія: <b>{label}</b>\nОберіть товар, щоб переглянути його детально:", reply_markup=markup, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data.startswith("detail:"))
async def product_detail(callback: CallbackQuery):
    _, pid, index = callback.data.split(":")
    await show_product_detail(callback, int(pid), int(index), edit=True)
    await callback.answer()

@dp.callback_query(F.data.startswith("buy:"))
async def buy_product(callback: CallbackQuery):
    pid = int(callback.data.split(":")[1])
    product = get_product(pid)
    if not product:
        await callback.answer("Товар більше недоступний.", show_alert=True)
        return
    await delete_callback_message(callback)
    sizes = [s.strip() for s in product[5].split(",") if s.strip()] if product[5] else []
    kb = InlineKeyboardBuilder()
    if sizes:
        for size in sizes:
            kb.button(text=f"Розмір {size}", callback_data=f"orderadd:{pid}:{size}")
        kb.adjust(2)
        kb.button(text="⬅️ Назад до товару", callback_data=f"detail:{pid}:0")
        await callback.message.answer(f"Оберіть розмір для «{product[1]}»:", reply_markup=kb.as_markup())
    else:
        kb.button(text="Замовити без вибору розміру", callback_data=f"orderadd:{pid}:—")
        kb.button(text="⬅️ Назад до товару", callback_data=f"detail:{pid}:0")
        kb.adjust(1)
        await callback.message.answer(f"Додати «{product[1]}» до кошика?", reply_markup=kb.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("orderadd:"))
async def add_to_cart(callback: CallbackQuery):
    _, pid_s, size = callback.data.split(":", 2)
    pid = int(pid_s)
    if not get_product(pid):
        await callback.answer("Товар більше недоступний.", show_alert=True)
        return
    with closing(db()) as con:
        con.execute("""INSERT INTO cart(user_id,product_id,size,quantity) VALUES(?,?,?,1)
            ON CONFLICT(user_id,product_id,size) DO UPDATE SET quantity=quantity+1""",
            (callback.from_user.id, pid, size))
        con.commit()
    await callback.answer("Додано до кошика 🛒")
    await delete_callback_message(callback)
    await callback.message.answer("Товар додано до кошика.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Відкрити кошик", callback_data="cart")],
        [InlineKeyboardButton(text="⬅️ Продовжити покупки", callback_data="back:categories")]
    ]))

def get_cart(user_id):
    with closing(db()) as con:
        return con.execute("""SELECT c.product_id,c.size,c.quantity,p.name,p.price,p.category
            FROM cart c JOIN products p ON p.id=c.product_id
            WHERE c.user_id=? AND p.active=1 AND p.stock>0 ORDER BY p.name""", (user_id,)).fetchall()

async def send_cart(target, user_id):
    items = get_cart(user_id)
    if not items:
        text = "Ваш кошик поки порожній."
        if isinstance(target, CallbackQuery):
            await target.message.answer(text)
        else:
            await target.answer(text)
        return
    total = sum(qty * price for _, _, qty, _, price, _category in items)
    lines = ["🛒 <b>Ваш кошик</b>\n"]
    kb = InlineKeyboardBuilder()
    for pid, size, qty, name, price, category in items:
        lines.append(f"• {CATEGORIES.get(category, ('Одяг', ''))[0]}: {html.escape(name)} / {html.escape(size)} × {qty} — {money(qty*price)}")
        kb.button(text=f"❌ {name} · {size}", callback_data=f"remove:{pid}:{size}")
    lines.append(f"\n<b>Разом: {money(total)}</b>")
    kb.button(text="✅ Оформити замовлення", callback_data="checkout")
    kb.button(text="🗑 Очистити кошик", callback_data="clear_cart")
    kb.button(text="⬅️ Продовжити покупки", callback_data="back:categories")
    kb.adjust(1)
    text = "\n".join(lines)
    if isinstance(target, CallbackQuery):
        msg = await target.message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")
        remember_screen(user_id, msg)
    else:
        msg = await target.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")
        remember_screen(user_id, msg)

@dp.message(F.text == "🛒 Кошик")
async def cart_cmd(message: Message):
    await clear_user_screens(message.from_user.id)
    await send_cart(message, message.from_user.id)

@dp.callback_query(F.data == "cart")
async def cart_callback(callback: CallbackQuery):
    await delete_callback_message(callback)
    await send_cart(callback, callback.from_user.id)
    await callback.answer()

@dp.callback_query(F.data.startswith("remove:"))
async def remove_item(callback: CallbackQuery):
    _, pid_s, size = callback.data.split(":", 2)
    with closing(db()) as con:
        con.execute("DELETE FROM cart WHERE user_id=? AND product_id=? AND size=?",
                    (callback.from_user.id, int(pid_s), size))
        con.commit()
    await callback.answer("Товар видалено.")
    await delete_callback_message(callback)
    await send_cart(callback, callback.from_user.id)

@dp.callback_query(F.data == "clear_cart")
async def clear_cart(callback: CallbackQuery):
    with closing(db()) as con:
        con.execute("DELETE FROM cart WHERE user_id=?", (callback.from_user.id,))
        con.commit()
    await callback.answer("Кошик очищено.")
    await delete_callback_message(callback)
    await callback.message.answer("Кошик очищено.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛍️ До каталогу", callback_data="back:categories")]
    ]))

@dp.callback_query(F.data == "checkout")
async def checkout(callback: CallbackQuery, state: FSMContext):
    if not get_cart(callback.from_user.id):
        await callback.answer("Кошик порожній.", show_alert=True)
        return
    await state.set_state(ContactForm.name)
    await delete_callback_message(callback)
    await callback.message.answer("Як до вас звертатися? Введіть ім’я:\n\nДля скасування введіть /cancel", reply_markup=ReplyKeyboardRemove())
    await callback.answer()

@dp.message(ContactForm.name)
async def checkout_name(message: Message, state: FSMContext):
    name = message.text.strip()[:100] if message.text else ""
    if len(name) < 2:
        await message.answer("Введіть, будь ласка, ім’я (щонайменше 2 символи).")
        return
    await state.update_data(customer_name=name)
    await state.set_state(ContactForm.phone)
    await message.answer("Введіть номер телефону для зв’язку:")

@dp.message(ContactForm.phone)
async def checkout_phone(message: Message, state: FSMContext):
    phone = message.text.strip()[:40] if message.text else ""
    if len(phone) < 6:
        await message.answer("Введіть коректний номер телефону ще раз.")
        return
    await state.update_data(phone=phone)
    await state.set_state(ContactForm.address)
    await message.answer("Вкажіть місто та відділення Нової пошти або натисніть кнопку, щоб узгодити доставку з продавцем:", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="💬 Узгодити з продавцем")]], resize_keyboard=True, one_time_keyboard=True))

@dp.message(ContactForm.address)
async def checkout_address(message: Message, state: FSMContext):
    address = message.text.strip()[:250] if message.text else ""
    if address == "💬 Узгодити з продавцем":
        address = "Узгодити з продавцем"
    if len(address) < 3:
        await message.answer("Вкажіть дані доставки або «узгодити з продавцем».")
        return
    data = await state.get_data()
    items = get_cart(message.from_user.id)
    if not items:
        await state.clear()
        await message.answer("Кошик порожній.", reply_markup=main_menu(message.from_user.id))
        return
    # Verify stock at checkout; quantities are kept private from customers.
    total = sum(qty * price for _, _, qty, _, price, _category in items)
    items_text = "\n".join(f"{CATEGORIES.get(category, ('Одяг', ''))[0]} — {name} / {size} × {qty} — {money(qty*price)}" for _, size, qty, name, price, category in items)
    created = datetime.now(KYIV_TZ).strftime("%d.%m.%Y %H:%M")
    order_id = None
    unavailable_name = None
    # Reserve stock atomically to avoid two simultaneous orders overselling the same item.
    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")
        for pid, size, qty, name, price, category in items:
            stock_row = con.execute("SELECT stock FROM products WHERE id=? AND active=1", (pid,)).fetchone()
            if not stock_row or stock_row[0] < qty:
                unavailable_name = name
                break
        if unavailable_name is None:
            cur = con.execute("""INSERT INTO orders(user_id,customer_name,phone,address,items,total,status,created_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (message.from_user.id, data["customer_name"], data["phone"], address, items_text, total, "Очікує уточнення", created))
            order_id = cur.lastrowid
            for pid, size, qty, name, price, category in items:
                con.execute("UPDATE products SET stock=stock-? WHERE id=?", (qty, pid))
                con.execute("INSERT INTO order_items(order_id,product_id,size,quantity) VALUES(?,?,?,?)", (order_id, pid, size, qty))
            con.execute("DELETE FROM cart WHERE user_id=?", (message.from_user.id,))
            con.commit()
        else:
            con.rollback()
    if unavailable_name is not None:
        await state.clear()
        await message.answer(f"На жаль, товар «{unavailable_name}» більше не має потрібної кількості. Кошик залишено без оформлення.", reply_markup=main_menu(message.from_user.id))
        return
    await state.clear()
    await message.answer(
        f"✅ Замовлення №{order_id} оформлено!\n\n{items_text}\n\nРазом: {money(total)}\n"
        "Статус: очікує уточнення. Продавець зв’яжеться з вами для підтвердження деталей.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Уточнити в адміністратора", callback_data=f"contactadmin:{order_id}")]
        ]))
    await message.answer("Головне меню:", reply_markup=main_menu(message.from_user.id))
    if ADMIN_ID:
        admin_text = (f"🆕 <b>Нове замовлення №{order_id}</b>\nПокупець: {html.escape(data['customer_name'])}\n"
            f"Телефон: {html.escape(data['phone'])}\nДоставка: {html.escape(address)}\nTelegram: <a href=\"tg://user?id={message.from_user.id}\">відкрити чат покупця</a>\n\n"
            f"{html.escape(items_text)}\n\nРазом: {money(total)}")
        kb = order_status_keyboard(order_id, "Очікує уточнення")
        try:
            await bot.send_message(ADMIN_ID, admin_text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            logging.exception("Не вдалося надіслати замовлення адміністратору")

@dp.callback_query(F.data.startswith("contactadmin:"))
async def contact_admin_for_order(callback: CallbackQuery):
    order_id = int(callback.data.split(":", 1)[1])
    with closing(db()) as con:
        row = con.execute("SELECT user_id,customer_name,phone,items,total,status FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row or row[0] != callback.from_user.id:
        await callback.answer("Замовлення не знайдено.", show_alert=True)
        return
    if not ADMIN_ID or not bot:
        await callback.answer("Зв’язок із магазином тимчасово недоступний.", show_alert=True)
        return
    user_id, customer_name, phone, items, total, status = row
    text = (f"💬 Покупець просить уточнити замовлення №{order_id}.\n"
            f"Ім’я: {html.escape(customer_name)}\nТелефон: {html.escape(phone)}\n"
            f"Telegram: <a href=\"tg://user?id={user_id}\">відкрити чат покупця</a>\n\n"
            f"{html.escape(items)}\nРазом: {money(total)}\nСтатус: {html.escape(status)}")
    try:
        await bot.send_message(ADMIN_ID, text, parse_mode="HTML")
        await callback.answer("Запит надіслано адміністратору.", show_alert=True)
    except Exception:
        logging.exception("Не вдалося надіслати запит адміністратору")
        await callback.answer("Не вдалося зв’язатися з адміністратором. Спробуйте пізніше.", show_alert=True)

@dp.message(F.text == "📦 Мої замовлення")
async def my_orders(message: Message):
    await clear_user_screens(message.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🕓 Очікується", callback_data="clientorders:pending")],
        [InlineKeyboardButton(text="✅ Підтверджено", callback_data="clientorders:accepted")],
        [InlineKeyboardButton(text="❌ Відхилено / скасовано", callback_data="clientorders:cancelled")],
    ])
    msg = await message.answer("📦 <b>Мої замовлення</b>\nОберіть статус:", reply_markup=kb, parse_mode="HTML")
    remember_screen(message.from_user.id, msg)

@dp.callback_query(F.data.startswith("clientorders:"))
async def client_orders_by_status(callback: CallbackQuery):
    group = callback.data.split(":", 1)[1]
    if group == "home":
        await delete_callback_message(callback)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🕓 Очікується", callback_data="clientorders:pending")],
            [InlineKeyboardButton(text="✅ Підтверджено", callback_data="clientorders:accepted")],
            [InlineKeyboardButton(text="❌ Відхилено / скасовано", callback_data="clientorders:cancelled")],
        ])
        msg = await callback.message.answer("📦 <b>Мої замовлення</b>\nОберіть статус:", reply_markup=kb, parse_mode="HTML")
        remember_screen(callback.from_user.id, msg)
        await callback.answer()
        return
    if group == "pending":
        statuses = ("Очікує уточнення", "Очікує підтвердження")
        title = "🕓 Замовлення, які очікують підтвердження"
    elif group == "accepted":
        statuses = ("Підтверджено", "Відправлено")
        title = "✅ Підтверджені замовлення"
    elif group == "cancelled":
        statuses = ("Скасовано",)
        title = "❌ Відхилені / скасовані замовлення"
    else:
        await callback.answer("Невідомий статус.", show_alert=True)
        return
    placeholders = ",".join("?" for _ in statuses)
    with closing(db()) as con:
        rows = con.execute(f"SELECT id,items,total,status,created_at FROM orders WHERE user_id=? AND status IN ({placeholders}) ORDER BY id DESC LIMIT 20", (callback.from_user.id, *statuses)).fetchall()
    await delete_callback_message(callback)
    if not rows:
        text = f"{title}\n\nЗамовлень у цьому розділі немає."
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="clientorders:home")]])
        msg = await callback.message.answer(text, reply_markup=kb)
        remember_screen(callback.from_user.id, msg)
    else:
        for oid, items, total, status, created in rows:
            icon = status_icon_fn(status)
            lines = [f"{icon} <b>Замовлення №{oid}</b>", html.escape(items), f"Разом: {money(total)}", f"Статус: {html.escape(status)}", f"Дата: {html.escape(created)}"]
            buttons = []
            if status in ("Очікує уточнення", "Очікує підтвердження"):
                buttons.append([InlineKeyboardButton(text="❌ Відмовитися від замовлення", callback_data=f"clientcancel:{oid}")])
            buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="clientorders:home")])
            msg = await callback.message.answer("\n\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
            remember_screen(callback.from_user.id, msg)
    await callback.answer()

@dp.callback_query(F.data == "clientorders:home")
async def client_orders_home(callback: CallbackQuery):
    await delete_callback_message(callback)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🕓 Очікується", callback_data="clientorders:pending")],
        [InlineKeyboardButton(text="✅ Підтверджено", callback_data="clientorders:accepted")],
        [InlineKeyboardButton(text="❌ Відхилено / скасовано", callback_data="clientorders:cancelled")],
    ])
    msg = await callback.message.answer("📦 <b>Мої замовлення</b>\nОберіть статус:", reply_markup=kb, parse_mode="HTML")
    remember_screen(callback.from_user.id, msg)
    await callback.answer()

@dp.callback_query(F.data.startswith("clientcancel:"))
async def client_cancel_order(callback: CallbackQuery):
    order_id = int(callback.data.split(":", 1)[1])
    with closing(db()) as con:
        row = con.execute("SELECT user_id,status,stock_restored FROM orders WHERE id=?", (order_id,)).fetchone()
        if not row or row[0] != callback.from_user.id:
            await callback.answer("Замовлення не знайдено.", show_alert=True)
            return
        if row[1] not in ("Очікує уточнення", "Очікує підтвердження"):
            await callback.answer("Це замовлення вже не можна скасувати тут.", show_alert=True)
            return
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT status,stock_restored FROM orders WHERE id=?", (order_id,)).fetchone()
        if not row or row[0] not in ("Очікує уточнення", "Очікує підтвердження"):
            con.rollback()
            await callback.answer("Статус замовлення вже змінився.", show_alert=True)
            return
        if not row[1]:
            lines = con.execute("SELECT product_id,quantity FROM order_items WHERE order_id=?", (order_id,)).fetchall()
            for product_id, quantity in lines:
                con.execute("UPDATE products SET stock=stock+? WHERE id=?", (quantity, product_id))
        con.execute("UPDATE orders SET status='Скасовано', stock_restored=1, cancel_reason='Скасовано покупцем' WHERE id=?", (order_id,))
        con.commit()
    if ADMIN_ID:
        try:
            await bot.send_message(ADMIN_ID, f"❌ Покупець самостійно скасував замовлення №{order_id}.")
        except Exception:
            logging.exception("Не вдалося повідомити адміністратора про скасування покупцем")
    await callback.message.edit_text(f"❌ Замовлення №{order_id} скасовано. Товар повернуто до залишків.")
    await callback.answer("Замовлення скасовано.")

@dp.callback_query(F.data.startswith("orderstatus:"))
async def set_order_status(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Немає доступу.", show_alert=True)
        return
    _, oid_s, status = callback.data.split(":", 2)
    order_id = int(oid_s)
    if status not in ("Підтверджено", "Відправлено", "Скасовано"):
        await callback.answer("Невідомий статус.", show_alert=True)
        return
    if status == "Скасовано":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Так, відмовитися", callback_data=f"rejectconfirm:{order_id}:yes")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"rejectconfirm:{order_id}:no")],
        ])
        await callback.message.answer(f"Ви точно хочете відмовитися від замовлення №{order_id}?", reply_markup=kb)
        await callback.answer()
        return
    if status == "Підтверджено":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Так, підтвердити", callback_data=f"acceptconfirm:{order_id}:yes")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"acceptconfirm:{order_id}:no")],
        ])
        await callback.message.answer(f"Ви точно хочете підтвердити замовлення №{order_id}?", reply_markup=kb)
        await callback.answer()
        return
    await update_order_status(order_id, status, callback.message, callback)

async def update_order_status(order_id: int, status: str, message: Message, callback=None):
    with closing(db()) as con:
        row = con.execute("SELECT user_id,status FROM orders WHERE id=?", (order_id,)).fetchone()
        if not row:
            if callback:
                await callback.answer("Замовлення не знайдено.", show_alert=True)
            else:
                await message.answer("Замовлення не знайдено.")
            return
        user_id, old_status = row
        if old_status == "Скасовано":
            if callback:
                await callback.answer("Замовлення вже скасовано.", show_alert=True)
            return
        con.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
        con.commit()
    try:
        await bot.send_message(user_id, f"{status_icon_fn(status)} Замовлення №{order_id}: статус — {status}.")
    except Exception:
        logging.exception("Не вдалося повідомити покупця про статус замовлення")
    if callback:
        await callback.answer(f"Статус: {status}")
    try:
        await message.edit_reply_markup(reply_markup=order_status_keyboard(order_id, status))
    except Exception:
        pass

@dp.callback_query(F.data.startswith("acceptconfirm:"))
async def confirm_order_acceptance(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Немає доступу.", show_alert=True)
        return
    _, oid_s, answer = callback.data.split(":", 2)
    order_id = int(oid_s)
    if answer == "no":
        await callback.message.edit_text(f"Підтвердження замовлення №{order_id} скасовано. Замовлення не змінено.")
        await callback.answer("Замовлення залишилося без змін.")
        return
    await callback.message.edit_text(f"Підтвердження замовлення №{order_id}...")
    await update_order_status(order_id, "Підтверджено", callback.message, callback)

@dp.callback_query(F.data.startswith("rejectconfirm:"))
async def confirm_order_rejection(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Немає доступу.", show_alert=True)
        return
    _, oid_s, answer = callback.data.split(":", 2)
    order_id = int(oid_s)
    if answer == "no":
        await callback.message.edit_text(f"Відмову від замовлення №{order_id} скасовано. Замовлення не змінено.")
        await callback.answer("Замовлення залишилося без змін.")
        return
    with closing(db()) as con:
        row = con.execute("SELECT status FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row:
        await callback.answer("Замовлення не знайдено.", show_alert=True)
        return
    if row[0] == "Скасовано":
        await callback.message.edit_text(f"Замовлення №{order_id} вже скасовано.")
        await callback.answer()
        return
    PENDING_REJECTIONS[callback.from_user.id] = order_id
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="- Без причини", callback_data=f"rejectreasonnone:{order_id}")],
    ])
    await callback.message.edit_text(
        f"Вкажіть причину відмови від замовлення №{order_id}. Покупець її побачить.\n"
        "Якщо не хочете вказувати причину, натисніть «- Без причини» або надішліть символ `-`.",
        reply_markup=kb,
        parse_mode="Markdown",
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("rejectreasonnone:"))
async def reject_order_without_reason(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Немає доступу.", show_alert=True)
        return
    order_id = int(callback.data.split(":", 1)[1])
    if PENDING_REJECTIONS.get(callback.from_user.id) != order_id:
        await callback.answer("Цей запит уже неактивний.", show_alert=True)
        return
    await finish_order_rejection(callback.from_user.id, order_id, "", callback.message)
    await callback.answer("Замовлення скасовано без зазначення причини.")

async def finish_order_rejection(admin_id: int, order_id: int, reason: str, message: Message):
    PENDING_REJECTIONS.pop(admin_id, None)
    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT user_id,status,stock_restored FROM orders WHERE id=?", (order_id,)).fetchone()
        if not row:
            con.rollback()
            await message.answer("Замовлення не знайдено.")
            return
        user_id, old_status, stock_restored = row
        if old_status == "Скасовано":
            con.rollback()
            await message.answer("Це замовлення вже скасовано.")
            return
        if not stock_restored:
            lines = con.execute("SELECT product_id,quantity FROM order_items WHERE order_id=?", (order_id,)).fetchall()
            for product_id, quantity in lines:
                con.execute("UPDATE products SET stock=stock+? WHERE id=?", (quantity, product_id))
            con.execute("UPDATE orders SET stock_restored=1 WHERE id=?", (order_id,))
        con.execute("UPDATE orders SET status='Скасовано', cancel_reason=? WHERE id=?", (reason, order_id))
        con.commit()
    customer_text = f"❌ Замовлення №{order_id} скасовано."
    if reason:
        customer_text += f"\nПричина: {html.escape(reason)}"
    try:
        await bot.send_message(user_id, customer_text, parse_mode="HTML")
    except Exception:
        logging.exception("Не вдалося повідомити покупця про скасування")
    await message.answer(f"Замовлення №{order_id} скасовано." + (" Причину надіслано покупцю." if reason else " Причину не вказано."))

@dp.message(F.text == "ℹ️ Про магазин")
async def about(message: Message):
    await message.answer("Вітаємо! Це магазин одягу.")

@dp.message(F.text == "🚚 Доставка й оплата")
async def shipping(message: Message):
    await message.answer("🚚 <b>Доставка й оплата</b>\n\nДоставку та спосіб оплати узгоджуємо після оформлення замовлення. Продавець підтвердить наявність, вартість доставки й оплату.", parse_mode="HTML")

@dp.message(F.text == "📞 Контакти")
async def contacts(message: Message):
    await message.answer("📞 Для зв’язку з продавцем напишіть у цей чат або оформіть замовлення — продавець зв’яжеться з вами.")

@dp.message(F.text == "⚙️ Адмін-панель")
async def admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        return
    await clear_user_screens(message.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Додати товар", callback_data="admin:add")],
        [InlineKeyboardButton(text="📋 Список товарів / редагування", callback_data="admin:list")],
        [InlineKeyboardButton(text="📦 Замовлення", callback_data="admin:orders")],
        [InlineKeyboardButton(text="⬅️ До головного меню", callback_data="admin:exit")],
    ])
    msg = await message.answer("⚙️ Адмін-панель", reply_markup=kb)
    remember_screen(message.from_user.id, msg)

@dp.callback_query(F.data.startswith("admin:"))
async def admin_actions(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Немає доступу.", show_alert=True)
        return
    action = callback.data.split(":")[1]
    if action == "exit":
        await state.clear()
        await delete_callback_message(callback)
        await clear_user_screens(callback.from_user.id)
        msg = await callback.message.answer("Головне меню:", reply_markup=main_menu(callback.from_user.id))
        remember_screen(callback.from_user.id, msg)
        await callback.answer()
        return
    await delete_callback_message(callback)
    if action == "add":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"admincat:{key}")]
            for key, (label, _) in CATEGORIES.items()
        ] + [[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:home")]])
        await state.set_state(ProductForm.category)
        msg = await callback.message.answer("Оберіть категорію нового товару:", reply_markup=kb)
        remember_screen(callback.from_user.id, msg)
    elif action == "list":
        with closing(db()) as con:
            rows = con.execute("SELECT id,name,price,active,category,stock FROM products ORDER BY id DESC LIMIT 50").fetchall()
        if not rows:
            await callback.message.answer("Товарів ще немає.")
        for pid, name, price, active, category, stock in rows:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✏️ Редагувати", callback_data=f"editproduct:{pid}")],
                [InlineKeyboardButton(text="🙈 Приховати" if active else "👁 Показати", callback_data=f"toggleproduct:{pid}:{0 if active else 1}"),
                 InlineKeyboardButton(text="🗑 Видалити", callback_data=f"deleteproduct:{pid}")],
                [InlineKeyboardButton(text="📦 Змінити залишок", callback_data=f"stockproduct:{pid}")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:home")]
            ])
            msg = await callback.message.answer(f"#{pid} · {name} · {money(price)} · {CATEGORIES.get(category, ('Худі',''))[0]} · {'активний' if active else 'прихований'} · залишок: {stock}", reply_markup=kb)
            remember_screen(callback.from_user.id, msg)
    elif action == "orders":
        msg = await callback.message.answer("📦 Замовлення магазину. Оберіть статус:", reply_markup=admin_orders_menu())
        remember_screen(callback.from_user.id, msg)
    elif action == "home":
        msg = await callback.message.answer("⚙️ Адмін-панель", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Додати товар", callback_data="admin:add")],
            [InlineKeyboardButton(text="📋 Список товарів / редагування", callback_data="admin:list")],
            [InlineKeyboardButton(text="📦 Замовлення", callback_data="admin:orders")],
            [InlineKeyboardButton(text="⬅️ До головного меню", callback_data="admin:exit")],
        ]))
        remember_screen(callback.from_user.id, msg)
    await callback.answer()

@dp.callback_query(F.data.startswith("admincat:"))
async def admin_category(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Немає доступу.", show_alert=True)
        return
    category = callback.data.split(":", 1)[1]
    if category not in CATEGORIES:
        await callback.answer("Категорію не знайдено.", show_alert=True)
        return
    await delete_callback_message(callback)
    await state.update_data(category=category, photo_ids=[])
    await state.set_state(ProductForm.name)
    msg = await callback.message.answer(f"Категорія: {CATEGORIES[category][0]}\nВведіть назву товару:")
    remember_screen(callback.from_user.id, msg)
    await callback.answer()

@dp.callback_query(F.data.startswith("editproduct:"))
async def edit_product_menu(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Немає доступу.", show_alert=True); return
    pid = int(callback.data.split(":")[1])
    with closing(db()) as con:
        row = con.execute("SELECT id,name,price,description,sizes,category FROM products WHERE id=?", (pid,)).fetchone()
    if not row:
        await callback.answer("Товар не знайдено.", show_alert=True); return
    await delete_callback_message(callback)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Назва", callback_data=f"editfield:{pid}:name"), InlineKeyboardButton(text="Ціна", callback_data=f"editfield:{pid}:price")],
        [InlineKeyboardButton(text="Опис", callback_data=f"editfield:{pid}:description"), InlineKeyboardButton(text="Розміри", callback_data=f"editfield:{pid}:sizes")],
        [InlineKeyboardButton(text="Фотографії", callback_data=f"editfield:{pid}:photos")],
        [InlineKeyboardButton(text="Категорія", callback_data=f"editfield:{pid}:category")]
    ])
    await callback.message.answer(f"Редагування товару #{pid}: {row[1]}\nОберіть поле:", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("editfield:"))
async def edit_product_field(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Немає доступу.", show_alert=True); return
    _, pid, field = callback.data.split(":")
    await delete_callback_message(callback)
    await state.update_data(edit_pid=int(pid), edit_field=field)
    if field == "category":
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=f"editcat:{pid}:{key}")] for key,(label,_) in CATEGORIES.items()])
        await callback.message.answer("Оберіть нову категорію:", reply_markup=kb)
    elif field == "photos":
        await state.update_data(edit_photo_ids=[])
        await state.set_state(EditProductForm.photos)
        await callback.message.answer("Надсилайте нові фото по одному. Коли закінчите, напишіть «Готово». Напишіть «-», щоб видалити всі фото.")
    else:
        await state.set_state(EditProductForm.value)
        await callback.message.answer({"name":"Введіть нову назву:","price":"Введіть нову ціну в гривнях:","description":"Введіть новий опис (або - щоб очистити):","sizes":"Введіть розміри через кому (або - щоб очистити):"}.get(field,"Введіть значення:"))
    await callback.answer()

@dp.callback_query(F.data.startswith("editcat:"))
async def edit_product_category(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Немає доступу.", show_alert=True); return
    _, pid, category = callback.data.split(":")
    if category not in CATEGORIES:
        await callback.answer("Категорію не знайдено.", show_alert=True)
        return
    await delete_callback_message(callback)
    with closing(db()) as con:
        con.execute("UPDATE products SET category=? WHERE id=?", (category, int(pid))); con.commit()
    await callback.answer("Категорію змінено.")
    await callback.message.answer("✅ Категорію товару оновлено.")

@dp.message(EditProductForm.photos)
async def save_edited_photos(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    photo_ids = data.get("edit_photo_ids", [])
    text = (message.text or "").strip().lower()
    if message.photo:
        photo_ids.append(message.photo[-1].file_id)
        await state.update_data(edit_photo_ids=photo_ids)
        return
    if text == "-" and not photo_ids:
        pid = data["edit_pid"]
        with closing(db()) as con:
            con.execute("UPDATE products SET photos='[]', photo_file_id='' WHERE id=?", (pid,))
            con.commit()
        await state.clear()
        await message.answer("✅ Усі фото товару видалено.", reply_markup=main_menu(message.from_user.id))
        return
    if text in ("готово", "готово!", "done"):
        if not photo_ids:
            await message.answer("Надішліть хоча б одне фото або напишіть «-», щоб видалити всі фото.")
            return
        pid = data["edit_pid"]
        with closing(db()) as con:
            con.execute("UPDATE products SET photos=?, photo_file_id=? WHERE id=?", (json.dumps(photo_ids), photo_ids[0], pid))
            con.commit()
        await state.clear()
        await message.answer("✅ Фотографії товару оновлено.", reply_markup=main_menu(message.from_user.id))
        return
    await message.answer("Надішліть фото, напишіть «Готово» або «-» для видалення всіх фото.")

@dp.message(EditProductForm.value)
async def save_product_field(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear(); return
    data = await state.get_data(); field = data["edit_field"]; pid = data["edit_pid"]
    value = (message.text or "").strip()
    if field == "name":
        if not value: await message.answer("Назва не може бути порожньою."); return
        value = value[:120]
    elif field == "price":
        try:
            value = float(value.replace(",", "."))
            if value < 0: raise ValueError
        except ValueError:
            await message.answer("Введіть коректну ціну, наприклад 899."); return
    elif field in ("description", "sizes"):
        value = "" if value == "-" else value[:1000 if field == "description" else 200]
    with closing(db()) as con:
        con.execute(f"UPDATE products SET {field}=? WHERE id=?", (value, pid)); con.commit()
    await state.clear()
    await message.answer("✅ Дані товару оновлено.", reply_markup=main_menu(message.from_user.id))

@dp.callback_query(F.data.startswith("deleteproduct:"))
async def ask_delete_product(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Немає доступу.", show_alert=True); return
    pid = int(callback.data.split(":")[1])
    await delete_callback_message(callback)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Так, видалити", callback_data=f"confirmdelete:{pid}"), InlineKeyboardButton(text="Скасувати", callback_data="admin:list")]])
    await callback.message.answer(f"Точно видалити товар #{pid}? Дію не можна скасувати.", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("confirmdelete:"))
async def confirm_delete_product(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Немає доступу.", show_alert=True); return
    pid = int(callback.data.split(":")[1])
    with closing(db()) as con:
        con.execute("DELETE FROM cart WHERE product_id=?", (pid,))
        con.execute("DELETE FROM products WHERE id=?", (pid,)); con.commit()
    await callback.answer("Товар видалено.")
    await callback.message.edit_text(f"✅ Товар #{pid} видалено.")

@dp.callback_query(F.data.startswith("stockproduct:"))
async def ask_stock(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Немає доступу.", show_alert=True); return
    pid = int(callback.data.split(":")[1])
    await delete_callback_message(callback)
    await state.update_data(stock_pid=pid)
    await state.set_state(EditProductForm.field)
    await callback.message.answer(f"Введіть нову кількість на складі для товару #{pid}:")
    await callback.answer()

@dp.message(EditProductForm.field)
async def save_stock(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): await state.clear(); return
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) > 100000:
        await message.answer("Введіть ціле число від 0 до 100000."); return
    data = await state.get_data(); pid = data["stock_pid"]; stock = int(raw)
    with closing(db()) as con:
        con.execute("UPDATE products SET stock=? WHERE id=?", (stock, pid)); con.commit()
    await state.clear()
    await message.answer(f"✅ Залишок товару #{pid}: {stock}. {'Товар не відображатиметься в каталозі, доки залишок 0.' if stock == 0 else 'Видимість залежить від налаштування приховування.'}", reply_markup=main_menu(message.from_user.id))

@dp.callback_query(F.data.startswith("toggleproduct:"))
async def toggle_product(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Немає доступу.", show_alert=True)
        return
    _, pid_s, active_s = callback.data.split(":")
    with closing(db()) as con:
        if int(active_s) == 1:
            con.execute("UPDATE products SET active=CASE WHEN stock>0 THEN 1 ELSE 0 END WHERE id=?", (int(pid_s),))
        else:
            con.execute("UPDATE products SET active=0 WHERE id=?", (int(pid_s),))
        con.commit()
    await callback.answer("Зміни збережено.")
    await delete_callback_message(callback)
    await callback.message.answer("✅ Видимість товару оновлено.")

@dp.callback_query(F.data.startswith("orderslist:"))
async def show_orders_by_status(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Немає доступу.", show_alert=True)
        return
    group = callback.data.split(":", 1)[1]
    if group == "cancelled":
        statuses = ("Скасовано",)
        title = "❌ Скасовані замовлення"
    elif group == "accepted":
        statuses = ("Підтверджено", "Відправлено")
        title = "✅ Прийняті замовлення"
    elif group == "pending":
        statuses = ("Очікує уточнення", "Очікує підтвердження")
        title = "🕓 Замовлення, що очікують підтвердження"
    else:
        await callback.answer("Невідома категорія.", show_alert=True)
        return
    placeholders = ",".join("?" for _ in statuses)
    with closing(db()) as con:
        rows = con.execute(f"SELECT id,user_id,customer_name,phone,address,items,total,status,created_at,cancel_reason FROM orders WHERE status IN ({placeholders}) ORDER BY id DESC LIMIT 10", statuses).fetchall()
    lines = [title, ""]
    buttons = []
    for oid, user_id, name, phone, address, items, total, status, created, reason in rows:
        lines.append(f"{status_icon_fn(status)} Замовлення №{oid} · {html.escape(created)}\nПокупець: {html.escape(name)} · {html.escape(phone)}\nДоставка: {html.escape(address)}\n<a href=\"tg://user?id={user_id}\">💬 Відкрити чат покупця</a>\n{html.escape(items)}\nРазом: {money(total)}\nСтатус: {html.escape(status)}" + (f"\nПричина: {html.escape(reason)}" if reason else ""))
        if status in ("Очікує уточнення", "Очікує підтвердження"):
            buttons.append([InlineKeyboardButton(text=f"✅ Прийняти №{oid}", callback_data=f"orderstatus:{oid}:Підтверджено")])
            buttons.append([InlineKeyboardButton(text=f"❌ Відмовити №{oid}", callback_data=f"orderstatus:{oid}:Скасовано")])
        elif status == "Підтверджено":
            buttons.append([InlineKeyboardButton(text=f"📦 Відправлено №{oid}", callback_data=f"orderstatus:{oid}:Відправлено")])
            buttons.append([InlineKeyboardButton(text=f"❌ Відмовити №{oid}", callback_data=f"orderstatus:{oid}:Скасовано")])
    if not rows:
        lines.append("Замовлень у цій категорії немає.")
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:orders")])
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    await delete_callback_message(callback)
    msg = await callback.message.answer("\n\n────────────\n\n".join(lines)[:4000], reply_markup=markup, parse_mode="HTML")
    remember_screen(callback.from_user.id, msg)
    await callback.answer()

@dp.message(ProductForm.name)
async def add_name(message: Message, state: FSMContext):
    name = message.text.strip()[:120] if message.text else ""
    if not name:
        await message.answer("Назва не може бути порожньою.")
        return
    await state.update_data(name=name)
    await state.set_state(ProductForm.price)
    msg = await message.answer("Введіть ціну в гривнях (наприклад: 899 або 899.50):")
    remember_screen(message.from_user.id, msg)

@dp.message(ProductForm.price)
async def add_price(message: Message, state: FSMContext):
    try:
        price = float((message.text or "").strip().replace(",", "."))
        if price < 0 or price > 10_000_000:
            raise ValueError
    except ValueError:
        await message.answer("Введіть коректну ціну, наприклад 899.")
        return
    await state.update_data(price=price)
    await state.set_state(ProductForm.description)
    msg = await message.answer("Введіть опис товару або «-», якщо опису немає:")
    remember_screen(message.from_user.id, msg)

@dp.message(ProductForm.description)
async def add_description(message: Message, state: FSMContext):
    description = (message.text or "").strip()
    await state.update_data(description="" if description == "-" else description[:1000])
    await state.set_state(ProductForm.photos)
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⏭ Пропустити")]], resize_keyboard=True, one_time_keyboard=True)
    msg = await message.answer("Надішліть фото товару по одному. Після додавання всіх фото натисніть «✅ Готово». Якщо фото не потрібні — натисніть «⏭ Пропустити».", reply_markup=kb)
    remember_screen(message.from_user.id, msg)

@dp.message(ProductForm.photos)
async def add_photos(message: Message, state: FSMContext):
    data = await state.get_data()
    photo_ids = data.get("photo_ids", [])
    text = (message.text or "").strip().lower()
    if message.photo:
        photo_ids.append(message.photo[-1].file_id)
        await state.update_data(photo_ids=photo_ids)
        kb = ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="✅ Готово")],
            [KeyboardButton(text="⏭ Пропустити")],
        ], resize_keyboard=True, one_time_keyboard=True)
        msg = await message.answer(f"Фото додано ({len(photo_ids)}). Надішліть ще фото або натисніть «✅ Готово».", reply_markup=kb)
        remember_screen(message.from_user.id, msg)
        return
    if text in ("⏭ пропустити", "пропустити", "-"):
        await state.update_data(photo_ids=[])
        await state.set_state(ProductForm.sizes)
        kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⏭ Пропустити")]], resize_keyboard=True, one_time_keyboard=True)
        msg = await message.answer("Введіть розміри через кому (наприклад: S, M, L, XL) або натисніть «⏭ Пропустити».", reply_markup=kb)
        remember_screen(message.from_user.id, msg)
        return
    if text in ("✅ готово", "готово", "готово!", "done"):
        if not photo_ids:
            await message.answer("Спочатку додайте фото або натисніть «⏭ Пропустити».")
            return
        await state.set_state(ProductForm.sizes)
        kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⏭ Пропустити")]], resize_keyboard=True, one_time_keyboard=True)
        msg = await message.answer("Введіть розміри через кому (наприклад: S, M, L, XL) або натисніть «⏭ Пропустити».", reply_markup=kb)
        remember_screen(message.from_user.id, msg)
        return
    await message.answer("Надішліть фото або скористайтеся кнопками «✅ Готово» чи «⏭ Пропустити».")

@dp.message(ProductForm.sizes)
async def add_sizes(message: Message, state: FSMContext):
    sizes = (message.text or "").strip()
    if not sizes:
        await message.answer("Введіть розміри або натисніть «⏭ Пропустити».")
        return
    if sizes in ("-", "⏭ Пропустити"):
        sizes = ""
    await state.update_data(sizes=sizes[:200])
    await state.set_state(ProductForm.stock)
    msg = await message.answer("Вкажіть кількість товару на складі (ці цифри покупцям не показуються). Якщо товару немає — введіть 0:", reply_markup=ReplyKeyboardRemove())
    remember_screen(message.from_user.id, msg)

@dp.message(ProductForm.stock)
async def add_stock(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) < 0 or int(raw) > 100000:
        await message.answer("Введіть ціле число від 0 до 100000.")
        return
    data = await state.get_data()
    stock = int(raw)
    photos = data.get("photo_ids", [])
    first_photo = photos[0] if photos else ""
    with closing(db()) as con:
        cur = con.execute("""INSERT INTO products(name,price,description,photo_file_id,sizes,active,created_at,category,photos,stock)
            VALUES(?,?,?,?,?, ?,?,?,?,?)""",
            (data["name"], data["price"], data["description"], first_photo, data["sizes"],
             1 if stock > 0 else 0, datetime.now(KYIV_TZ).strftime("%d.%m.%Y %H:%M"), data["category"], json.dumps(photos), stock))
        pid = cur.lastrowid
        con.commit()
    await state.clear()
    await clear_user_screens(message.from_user.id)
    msg = await message.answer(f"✅ Товар «{data['name']}» додано до категорії «{CATEGORIES[data['category']][0]}» (ID {pid}).\nКількість на складі: {stock}.",
                               reply_markup=main_menu(message.from_user.id))
    remember_screen(message.from_user.id, msg)

@dp.message(lambda message: bool(message.from_user and message.from_user.id in PENDING_REJECTIONS))
async def admin_rejection_reason(message: Message):
    if not is_admin(message.from_user.id) or message.from_user.id not in PENDING_REJECTIONS:
        return
    reason = (message.text or "").strip()[:500]
    if reason == "-":
        await finish_order_rejection(message.from_user.id, PENDING_REJECTIONS[message.from_user.id], "", message)
        return
    if len(reason) < 3:
        await message.answer("Напишіть причину відмови (щонайменше 3 символи) або надішліть «-» без причини.")
        return
    await finish_order_rejection(message.from_user.id, PENDING_REJECTIONS[message.from_user.id], reason, message)

@dp.message()
async def fallback(message: Message):
    await message.answer("Оберіть дію в меню нижче 👇", reply_markup=main_menu(message.from_user.id))

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задано BOT_TOKEN у файлі .env")
    init_db()
    if not ADMIN_ID:
        logging.warning("ADMIN_ID не задано: адмін-панель недоступна, замовлення не пересилатимуться.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
