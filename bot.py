import asyncio
import json
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime

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
DB_PATH = os.getenv("DB_PATH", "/data/shop.sqlite3")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()

CATEGORIES = {
    "hoodie": ("Худі", "https://images.unsplash.com/photo-1556821840-3a63f95609a7?w=900"),
    "longsleeve": ("Лонгслів", "https://images.unsplash.com/photo-1521572163474-6864f9cf17ab?w=900"),
    "sweatshirt": ("Світшот", "https://images.unsplash.com/photo-1578681994506-b8f463449011?w=900"),
    "tshirt": ("Футболка", "https://images.unsplash.com/photo-1521572163474-6864f9cf17ab?w=900"),
}

class ProductForm(StatesGroup):
    category = State()
    name = State()
    price = State()
    description = State()
    photos = State()
    sizes = State()

class ContactForm(StatesGroup):
    name = State()
    phone = State()
    address = State()

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
            category TEXT NOT NULL DEFAULT 'hoodie', photos TEXT NOT NULL DEFAULT '[]'
        )""")
        cols = {r[1] for r in con.execute("PRAGMA table_info(products)").fetchall()}
        if "category" not in cols:
            con.execute("ALTER TABLE products ADD COLUMN category TEXT NOT NULL DEFAULT 'hoodie'")
        if "photos" not in cols:
            con.execute("ALTER TABLE products ADD COLUMN photos TEXT NOT NULL DEFAULT '[]'")
        con.execute("""CREATE TABLE IF NOT EXISTS cart (
            user_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
            size TEXT NOT NULL DEFAULT '', quantity INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(user_id, product_id, size)
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            customer_name TEXT NOT NULL, phone TEXT NOT NULL, address TEXT NOT NULL,
            items TEXT NOT NULL, total REAL NOT NULL, status TEXT NOT NULL DEFAULT 'Очікує уточнення',
            created_at TEXT NOT NULL
        )""")
        con.commit()

def money(value):
    return f"{value:g} грн"

def is_admin(user_id):
    return bool(ADMIN_ID and user_id == ADMIN_ID)

def main_menu(user_id):
    rows = [
        [KeyboardButton(text="🛍 Каталог"), KeyboardButton(text="🛒 Кошик")],
        [KeyboardButton(text="📦 Мої замовлення"), KeyboardButton(text="ℹ️ Про нас")],
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
            FROM products WHERE active=1 AND category=? ORDER BY id DESC""", (category,)).fetchall()

def get_product(pid):
    with closing(db()) as con:
        row = con.execute("""SELECT id,name,price,description,photo_file_id,sizes,category,photos
            FROM products WHERE id=? AND active=1""", (pid,)).fetchone()
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
        kb.button(text=f"🔎 Детально · {name} — {money(price)}", callback_data=f"detail:{pid}:0")
    kb.button(text="⬅️ Назад до категорій", callback_data="back:categories")
    kb.adjust(1)
    return kb.as_markup()

async def show_categories(message: Message):
    # Telegram cannot attach a separate button to each image in a 4-photo album.
    # Show the four category images as an album, then a compact keyboard with category names.
    from aiogram.types import InputMediaPhoto
    media = [
        InputMediaPhoto(media=photo, caption=label if i == 0 else None)
        for i, (key, (label, photo)) in enumerate(CATEGORIES.items())
    ]
    try:
        await message.answer_media_group(media)
    except Exception:
        await message.answer("🛍 Каталог одягу")
    await message.answer("Оберіть категорію:", reply_markup=category_keyboard())

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
    caption = f"👕 <b>{name}</b>\n💰 {money(price)}"
    if description:
        caption += f"\n\n{description}"
    if sizes:
        caption += f"\n\n📏 Розміри: {sizes}"
    if photo_count:
        caption += f"\n\nФото {index+1}/{photo_count}"
    kb = InlineKeyboardBuilder()
    arrows = []
    if photo_count > 1:
        # At the first photo, previous arrow remains visible but does nothing.
        arrows.append(InlineKeyboardButton(text="⬅️", callback_data=f"detail:{pid}:{max(0,index-1)}"))
        arrows.append(InlineKeyboardButton(text="➡️", callback_data=f"detail:{pid}:{min(photo_count-1,index+1)}"))
        kb.row(*arrows)
    kb.button(text="🛒 Замовити", callback_data=f"buy:{pid}")
    kb.button(text="⬅️ Назад до товарів", callback_data=f"category:{category}")
    kb.button(text="🏠 До категорій", callback_data="back:categories")
    kb.adjust(2, 1, 1)
    markup = kb.as_markup()
    if isinstance(target, CallbackQuery):
        msg = target.message
        try:
            if photo:
                await msg.edit_media(media=InputMediaPhoto(media=photo, caption=caption, parse_mode="HTML"), reply_markup=markup)
            else:
                await msg.edit_text(caption, reply_markup=markup, parse_mode="HTML")
        except Exception:
            # Same content may trigger Telegram "message is not modified"; ignore.
            pass
    else:
        if photo:
            await target.answer_photo(photo, caption=caption, reply_markup=markup, parse_mode="HTML")
        else:
            await target.answer(caption, reply_markup=markup, parse_mode="HTML")

@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "Вітаємо в магазині одягу! 👕\n\nОберіть категорію, перегляньте фото товарів і оформіть замовлення. "
        "Продавець зв’яжеться з вами, щоб уточнити деталі.",
        reply_markup=main_menu(message.from_user.id)
    )

@dp.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer("Скористайтеся кнопками меню. Для замовлення відкрийте каталог або кошик.")

@dp.message(F.text == "🛍 Каталог")
async def catalog(message: Message):
    await show_categories(message)

@dp.callback_query(F.data == "back:categories")
async def back_categories(callback: CallbackQuery):
    await callback.message.answer("🛍 Оберіть категорію:", reply_markup=category_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "back:menu")
async def back_menu(callback: CallbackQuery):
    await callback.message.answer("Головне меню:", reply_markup=main_menu(callback.from_user.id))
    await callback.answer()

@dp.callback_query(F.data.startswith("category:"))
async def category_open(callback: CallbackQuery):
    category = callback.data.split(":", 1)[1]
    if category not in CATEGORIES:
        await callback.answer("Категорію не знайдено.", show_alert=True)
        return
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

@dp.callback_query(F.data.startswith("add:"))
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
    await callback.message.answer("Товар додано до кошика.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Відкрити кошик", callback_data="cart")],
        [InlineKeyboardButton(text="⬅️ Продовжити покупки", callback_data="back:categories")]
    ]))

def get_cart(user_id):
    with closing(db()) as con:
        return con.execute("""SELECT c.product_id,c.size,c.quantity,p.name,p.price
            FROM cart c JOIN products p ON p.id=c.product_id
            WHERE c.user_id=? AND p.active=1 ORDER BY p.name""", (user_id,)).fetchall()

async def send_cart(target, user_id):
    items = get_cart(user_id)
    if not items:
        text = "Ваш кошик поки порожній."
        if isinstance(target, CallbackQuery):
            await target.message.answer(text)
        else:
            await target.answer(text)
        return
    total = sum(qty * price for _, _, qty, _, price in items)
    lines = ["🛒 <b>Ваш кошик</b>\n"]
    kb = InlineKeyboardBuilder()
    for pid, size, qty, name, price in items:
        lines.append(f"• {name} / {size} × {qty} — {money(qty*price)}")
        kb.button(text=f"❌ {name} · {size}", callback_data=f"remove:{pid}:{size}")
    lines.append(f"\n<b>Разом: {money(total)}</b>")
    kb.button(text="✅ Оформити замовлення", callback_data="checkout")
    kb.button(text="🗑 Очистити кошик", callback_data="clear_cart")
    kb.button(text="⬅️ Продовжити покупки", callback_data="back:categories")
    kb.adjust(1)
    text = "\n".join(lines)
    if isinstance(target, CallbackQuery):
        await target.message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.message(F.text == "🛒 Кошик")
async def cart_cmd(message: Message):
    await send_cart(message, message.from_user.id)

@dp.callback_query(F.data == "cart")
async def cart_callback(callback: CallbackQuery):
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
    await send_cart(callback, callback.from_user.id)

@dp.callback_query(F.data == "clear_cart")
async def clear_cart(callback: CallbackQuery):
    with closing(db()) as con:
        con.execute("DELETE FROM cart WHERE user_id=?", (callback.from_user.id,))
        con.commit()
    await callback.answer("Кошик очищено.")
    await callback.message.answer("Кошик очищено.")

@dp.callback_query(F.data == "checkout")
async def checkout(callback: CallbackQuery, state: FSMContext):
    if not get_cart(callback.from_user.id):
        await callback.answer("Кошик порожній.", show_alert=True)
        return
    await state.set_state(ContactForm.name)
    await callback.message.answer("Як до вас звертатися? Введіть ім’я:", reply_markup=ReplyKeyboardRemove())
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
    await message.answer("Вкажіть місто та відділення Нової пошти або напишіть «узгодити з продавцем»:")

@dp.message(ContactForm.address)
async def checkout_address(message: Message, state: FSMContext):
    address = message.text.strip()[:250] if message.text else ""
    if len(address) < 3:
        await message.answer("Вкажіть дані доставки або «узгодити з продавцем».")
        return
    data = await state.get_data()
    items = get_cart(message.from_user.id)
    if not items:
        await state.clear()
        await message.answer("Кошик порожній.", reply_markup=main_menu(message.from_user.id))
        return
    total = sum(qty * price for _, _, qty, _, price in items)
    items_text = "\n".join(f"{name} / {size} × {qty} — {money(qty*price)}" for _, size, qty, name, price in items)
    created = datetime.now().strftime("%Y-%m-%d %H:%M")
    with closing(db()) as con:
        cur = con.execute("""INSERT INTO orders(user_id,customer_name,phone,address,items,total,status,created_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (message.from_user.id, data["customer_name"], data["phone"], address, items_text, total, "Очікує уточнення", created))
        order_id = cur.lastrowid
        con.execute("DELETE FROM cart WHERE user_id=?", (message.from_user.id,))
        con.commit()
    await state.clear()
    await message.answer(
        f"✅ Замовлення №{order_id} оформлено!\n\n{items_text}\n\nРазом: {money(total)}\n"
        "Статус: очікує уточнення. Продавець зв’яжеться з вами для підтвердження деталей.",
        reply_markup=main_menu(message.from_user.id))
    if ADMIN_ID:
        admin_text = (f"🆕 <b>Нове замовлення №{order_id}</b>\nПокупець: {data['customer_name']}\n"
            f"Телефон: {data['phone']}\nДоставка: {address}\nTelegram ID: {message.from_user.id}\n\n"
            f"{items_text}\n\nРазом: {money(total)}")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Підтвердити", callback_data=f"orderstatus:{order_id}:Підтверджено"),
             InlineKeyboardButton(text="📦 Відправлено", callback_data=f"orderstatus:{order_id}:Відправлено")],
            [InlineKeyboardButton(text="❌ Скасувати", callback_data=f"orderstatus:{order_id}:Скасовано")]
        ])
        try:
            await bot.send_message(ADMIN_ID, admin_text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            logging.exception("Не вдалося надіслати замовлення адміністратору")

@dp.message(F.text == "📦 Мої замовлення")
async def my_orders(message: Message):
    with closing(db()) as con:
        rows = con.execute("""SELECT id,items,total,status,created_at FROM orders
            WHERE user_id=? ORDER BY id DESC LIMIT 20""", (message.from_user.id,)).fetchall()
    if not rows:
        await message.answer("У вас ще немає замовлень.")
        return
    for oid, items, total, status, created in rows:
        await message.answer(f"📦 <b>Замовлення №{oid}</b>\n{items}\n\nРазом: {money(total)}\nСтатус: {status}\nДата: {created}", parse_mode="HTML")

@dp.callback_query(F.data.startswith("orderstatus:"))
async def set_order_status(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Немає доступу.", show_alert=True)
        return
    _, oid_s, status = callback.data.split(":", 2)
    with closing(db()) as con:
        row = con.execute("SELECT user_id FROM orders WHERE id=?", (int(oid_s),)).fetchone()
        if not row:
            await callback.answer("Замовлення не знайдено.", show_alert=True)
            return
        con.execute("UPDATE orders SET status=? WHERE id=?", (status, int(oid_s)))
        con.commit()
    try:
        await bot.send_message(row[0], f"Оновлення замовлення №{oid_s}: статус — {status}.")
    except Exception:
        pass
    await callback.answer(f"Статус: {status}")
    await callback.message.edit_reply_markup(reply_markup=None)

@dp.message(F.text == "ℹ️ Про нас")
async def about(message: Message):
    await message.answer("Здравствуй! Это магазин одежды.")

@dp.message(F.text == "🚚 Доставка й оплата")
async def shipping(message: Message):
    await message.answer("🚚 <b>Доставка й оплата</b>\n\nДоставку та спосіб оплати узгоджуємо після оформлення замовлення. Продавець підтвердить наявність, вартість доставки й оплату.", parse_mode="HTML")

@dp.message(F.text == "📞 Контакти")
async def contacts(message: Message):
    await message.answer("📞 Напишіть продавцю в Telegram. Додайте свій контакт у текст цієї функції перед запуском магазину.")

@dp.message(F.text == "⚙️ Адмін-панель")
async def admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Додати товар", callback_data="admin:add")],
        [InlineKeyboardButton(text="📋 Список товарів", callback_data="admin:list")],
        [InlineKeyboardButton(text="📦 Останні замовлення", callback_data="admin:orders")],
    ])
    await message.answer("⚙️ Адмін-панель", reply_markup=kb)

@dp.callback_query(F.data.startswith("admin:"))
async def admin_actions(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Немає доступу.", show_alert=True)
        return
    action = callback.data.split(":")[1]
    if action == "add":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"admincat:{key}")]
            for key, (label, _) in CATEGORIES.items()
        ])
        await state.set_state(ProductForm.category)
        await callback.message.answer("Оберіть категорію нового товару:", reply_markup=kb)
    elif action == "list":
        with closing(db()) as con:
            rows = con.execute("SELECT id,name,price,active,category FROM products ORDER BY id DESC LIMIT 50").fetchall()
        if not rows:
            await callback.message.answer("Товарів ще немає.")
        for pid, name, price, active, category in rows:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🗑 Приховати" if active else "♻️ Відновити",
                                     callback_data=f"toggleproduct:{pid}:{0 if active else 1}")
            ]])
            await callback.message.answer(f"#{pid} · {name} · {money(price)} · {CATEGORIES.get(category, ('Худі',''))[0]} · {'активний' if active else 'прихований'}", reply_markup=kb)
    elif action == "orders":
        with closing(db()) as con:
            rows = con.execute("SELECT id,customer_name,phone,items,total,status,created_at FROM orders ORDER BY id DESC LIMIT 10").fetchall()
        if not rows:
            await callback.message.answer("Замовлень ще немає.")
        for oid, name, phone, items, total, status, created in rows:
            await callback.message.answer(f"№{oid} · {created}\n{name} · {phone}\n{items}\nРазом: {money(total)}\nСтатус: {status}")
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
    await state.update_data(category=category, photo_ids=[])
    await state.set_state(ProductForm.name)
    await callback.message.answer(f"Категорія: {CATEGORIES[category][0]}\nВведіть назву товару:")
    await callback.answer()

@dp.callback_query(F.data.startswith("toggleproduct:"))
async def toggle_product(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Немає доступу.", show_alert=True)
        return
    _, pid_s, active_s = callback.data.split(":")
    with closing(db()) as con:
        con.execute("UPDATE products SET active=? WHERE id=?", (int(active_s), int(pid_s)))
        con.commit()
    await callback.answer("Зміни збережено.")
    await callback.message.edit_reply_markup(reply_markup=None)

@dp.message(ProductForm.name)
async def add_name(message: Message, state: FSMContext):
    name = message.text.strip()[:120] if message.text else ""
    if not name:
        await message.answer("Назва не може бути порожньою.")
        return
    await state.update_data(name=name)
    await state.set_state(ProductForm.price)
    await message.answer("Введіть ціну в гривнях (наприклад: 899 або 899.50):")

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
    await message.answer("Введіть опис товару або «-», якщо опису немає:")

@dp.message(ProductForm.description)
async def add_description(message: Message, state: FSMContext):
    description = (message.text or "").strip()
    await state.update_data(description="" if description == "-" else description[:1000])
    await state.set_state(ProductForm.photos)
    await message.answer("Надсилайте фото товару по одному. Коли додасте всі фото, напишіть «Готово». Мінімум одне фото; щоб пропустити — напишіть «-».")

@dp.message(ProductForm.photos)
async def add_photos(message: Message, state: FSMContext):
    data = await state.get_data()
    photo_ids = data.get("photo_ids", [])
    text = (message.text or "").strip().lower()
    if message.photo:
        photo_ids.append(message.photo[-1].file_id)
        await state.update_data(photo_ids=photo_ids)
        await message.answer(f"Фото додано ({len(photo_ids)}). Надішліть наступне або напишіть «Готово».")
        return
    if text in ("готово", "готово!", "done"):
        if not photo_ids:
            await message.answer("Додайте хоча б одне фото або напишіть «-», щоб пропустити.")
            return
        await state.set_state(ProductForm.sizes)
        await message.answer("Введіть розміри через кому (наприклад: S, M, L, XL) або «-»:")
        return
    if text == "-" and not photo_ids:
        await state.set_state(ProductForm.sizes)
        await message.answer("Введіть розміри через кому (наприклад: S, M, L, XL) або «-»:")
        return
    await message.answer("Надішліть фото або напишіть «Готово» після всіх фото.")

@dp.message(ProductForm.sizes)
async def add_sizes(message: Message, state: FSMContext):
    sizes = (message.text or "").strip()
    if not sizes:
        await message.answer("Введіть розміри або «-».")
        return
    if sizes == "-":
        sizes = ""
    data = await state.get_data()
    photos = data.get("photo_ids", [])
    first_photo = photos[0] if photos else ""
    with closing(db()) as con:
        cur = con.execute("""INSERT INTO products(name,price,description,photo_file_id,sizes,active,created_at,category,photos)
            VALUES(?,?,?,?,?,1,?,?,?)""",
            (data["name"], data["price"], data["description"], first_photo, sizes[:200],
             datetime.now().strftime("%Y-%m-%d %H:%M"), data["category"], json.dumps(photos)))
        pid = cur.lastrowid
        con.commit()
    await state.clear()
    await message.answer(f"✅ Товар «{data['name']}» додано до категорії «{CATEGORIES[data['category']][0]}» (ID {pid}).",
                         reply_markup=main_menu(message.from_user.id))

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
