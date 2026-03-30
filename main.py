import sqlite3
import threading
import requests
import telebot
from telebot import types
import time
from datetime import datetime, timedelta
import re

# Конфигурация
BOT_TOKEN = "8618455006:AAHBaSpzt666owEAbbn_D4ZJ75DqcSwR678"
ADMIN_IDS = [8282537975]  # ID администратора

# Настройки Telegram канала для проверки подписки
REQUIRED_CHANNEL = "@bestgold_official"
CHANNEL_ID = "-1003841020907"

# Курсы валют
GOLD_PRICE_RUB = 0.68

# Инициализация бота с настройками для работы через прокси (если нужно)
# bot = telebot.TeleBot(BOT_TOKEN)
# Если нужен прокси, раскомментируйте:
# from telebot import apihelper
# apihelper.proxy = {'http': 'http://your_proxy:8080', 'https': 'http://your_proxy:8080'}
bot = telebot.TeleBot(BOT_TOKEN)

# Состояния пользователей
user_state = {}

# Блокировка для работы с БД
db_lock = threading.Lock()

# ---------- Функция проверки подписки ----------
def check_subscription(user_id):
    try:
        chat_member = bot.get_chat_member(CHANNEL_ID, user_id)
        return chat_member.status in ['member', 'administrator', 'creator']
    except Exception as e:
        print(f"Error checking subscription: {e}")
        return False

def require_subscription(func):
    def wrapper(message):
        user_id = message.from_user.id
        if not check_subscription(user_id):
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(
                types.InlineKeyboardButton("📢 Подписаться на канал", url=f"https://t.me/{REQUIRED_CHANNEL.replace('@', '')}"),
                types.InlineKeyboardButton("🔄 Проверить подписку", callback_data="check_subscription")
            )
            bot.send_message(
                user_id,
                f"❌ Для использования бота необходимо подписаться на наш канал {REQUIRED_CHANNEL}.\n\nПодпишитесь и нажмите 'Проверить подписку'.",
                reply_markup=markup
            )
            return
        return func(message)
    return wrapper

# ---------- Работа с базой данных ----------
def init_db():
    with db_lock:
        conn = sqlite3.connect('bot_database.db')
        c = conn.cursor()
        
        # Таблица users
        c.execute('''CREATE TABLE IF NOT EXISTS users
                     (user_id INTEGER PRIMARY KEY,
                      username TEXT,
                      first_name TEXT,
                      balance INTEGER DEFAULT 0,
                      total_orders INTEGER DEFAULT 0,
                      total_withdrawn INTEGER DEFAULT 0,
                      is_blocked INTEGER DEFAULT 0,
                      referrer_id INTEGER,
                      referral_earnings INTEGER DEFAULT 0,
                      phone_verified INTEGER DEFAULT 0,
                      geo_verified INTEGER DEFAULT 0,
                      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        
        # Таблица orders
        c.execute('''CREATE TABLE IF NOT EXISTS orders
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      user_id INTEGER,
                      amount_rub INTEGER,
                      amount_gold INTEGER,
                      amount_usdt REAL,
                      payment_method TEXT,
                      status TEXT DEFAULT 'pending',
                      screenshot_file_id TEXT,
                      invoice_id TEXT,
                      admin_note TEXT,
                      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        
        # Таблица withdrawals
        c.execute('''CREATE TABLE IF NOT EXISTS withdrawals
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      user_id INTEGER,
                      amount INTEGER,
                      amount_with_comission INTEGER,
                      status TEXT DEFAULT 'pending',
                      screenshot_file_id TEXT,
                      admin_note TEXT,
                      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        
        # Проверяем наличие колонок
        c.execute("PRAGMA table_info(users)")
        columns = [col[1] for col in c.fetchall()]
        if 'total_withdrawn' not in columns:
            c.execute("ALTER TABLE users ADD COLUMN total_withdrawn INTEGER DEFAULT 0")
        if 'referrer_id' not in columns:
            c.execute("ALTER TABLE users ADD COLUMN referrer_id INTEGER")
        if 'referral_earnings' not in columns:
            c.execute("ALTER TABLE users ADD COLUMN referral_earnings INTEGER DEFAULT 0")
        if 'phone_verified' not in columns:
            c.execute("ALTER TABLE users ADD COLUMN phone_verified INTEGER DEFAULT 0")
        if 'geo_verified' not in columns:
            c.execute("ALTER TABLE users ADD COLUMN geo_verified INTEGER DEFAULT 0")
        
        c.execute("PRAGMA table_info(orders)")
        columns = [col[1] for col in c.fetchall()]
        if 'amount_usdt' not in columns:
            c.execute("ALTER TABLE orders ADD COLUMN amount_usdt REAL")
        if 'invoice_id' not in columns:
            c.execute("ALTER TABLE orders ADD COLUMN invoice_id TEXT")
        
        conn.commit()
        conn.close()

def get_user(user_id):
    with db_lock:
        conn = sqlite3.connect('bot_database.db')
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user = c.fetchone()
        if not user:
            c.execute("INSERT INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
                      (user_id, None, None))
            conn.commit()
            c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            user = c.fetchone()
        conn.close()
        return user

def update_balance(user_id, delta, add_to_withdrawn=0):
    with db_lock:
        conn = sqlite3.connect('bot_database.db')
        c = conn.cursor()
        c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (delta, user_id))
        if add_to_withdrawn > 0:
            c.execute("UPDATE users SET total_withdrawn = total_withdrawn + ? WHERE user_id = ?", (add_to_withdrawn, user_id))
        conn.commit()
        conn.close()

def add_order(user_id, amount_rub, amount_gold, amount_usdt, payment_method, screenshot_file_id=None, invoice_id=None):
    with db_lock:
        conn = sqlite3.connect('bot_database.db')
        c = conn.cursor()
        c.execute('''INSERT INTO orders 
                     (user_id, amount_rub, amount_gold, amount_usdt, payment_method, screenshot_file_id, invoice_id)
                     VALUES (?, ?, ?, ?, ?, ?, ?)''',
                  (user_id, amount_rub, amount_gold, amount_usdt, payment_method, screenshot_file_id, invoice_id))
        order_id = c.lastrowid
        conn.commit()
        conn.close()
        return order_id

def add_withdrawal(user_id, amount, amount_with_comission, screenshot_file_id):
    with db_lock:
        conn = sqlite3.connect('bot_database.db')
        c = conn.cursor()
        c.execute('''INSERT INTO withdrawals (user_id, amount, amount_with_comission, screenshot_file_id)
                     VALUES (?, ?, ?, ?)''',
                  (user_id, amount, amount_with_comission, screenshot_file_id))
        withdrawal_id = c.lastrowid
        conn.commit()
        conn.close()
        return withdrawal_id

def get_order(order_id):
    with db_lock:
        conn = sqlite3.connect('bot_database.db')
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
        order = c.fetchone()
        conn.close()
        return order

def get_withdrawal(withdrawal_id):
    with db_lock:
        conn = sqlite3.connect('bot_database.db')
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM withdrawals WHERE id = ?", (withdrawal_id,))
        withdrawal = c.fetchone()
        conn.close()
        return withdrawal

def update_order_status(order_id, status, admin_note=''):
    with db_lock:
        conn = sqlite3.connect('bot_database.db')
        c = conn.cursor()
        c.execute("UPDATE orders SET status = ?, admin_note = ? WHERE id = ?", (status, admin_note, order_id))
        conn.commit()
        conn.close()

def update_withdrawal_status(withdrawal_id, status, admin_note=''):
    with db_lock:
        conn = sqlite3.connect('bot_database.db')
        c = conn.cursor()
        c.execute("UPDATE withdrawals SET status = ?, admin_note = ? WHERE id = ?", (status, admin_note, withdrawal_id))
        conn.commit()
        conn.close()

def get_user_orders_count(user_id):
    with db_lock:
        conn = sqlite3.connect('bot_database.db')
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM orders WHERE user_id = ?", (user_id,))
        count = c.fetchone()[0]
        conn.close()
        return count

def block_user(user_id):
    with db_lock:
        conn = sqlite3.connect('bot_database.db')
        c = conn.cursor()
        c.execute("UPDATE users SET is_blocked = 1 WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()

def unblock_user(user_id):
    with db_lock:
        conn = sqlite3.connect('bot_database.db')
        c = conn.cursor()
        c.execute("UPDATE users SET is_blocked = 0 WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()

def get_all_users():
    with db_lock:
        conn = sqlite3.connect('bot_database.db')
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM users ORDER BY created_at DESC")
        users = c.fetchall()
        conn.close()
        return users

def get_pending_orders():
    with db_lock:
        conn = sqlite3.connect('bot_database.db')
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM orders WHERE status = 'pending' ORDER BY created_at DESC")
        orders = c.fetchall()
        conn.close()
        return orders

def get_pending_withdrawals():
    with db_lock:
        conn = sqlite3.connect('bot_database.db')
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM withdrawals WHERE status = 'pending' ORDER BY created_at DESC")
        withdrawals = c.fetchall()
        conn.close()
        return withdrawals

def get_stats():
    with db_lock:
        conn = sqlite3.connect('bot_database.db')
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM users")
        total_users = c.fetchone()[0]
        c.execute("SELECT SUM(amount_gold) FROM orders WHERE status = 'approved'")
        total_gold_sold = c.fetchone()[0] or 0
        c.execute("SELECT SUM(amount) FROM withdrawals WHERE status = 'approved'")
        total_gold_withdrawn = c.fetchone()[0] or 0
        c.execute("SELECT SUM(amount_rub) FROM orders WHERE status = 'approved'")
        total_rub_earned = c.fetchone()[0] or 0
        conn.close()
        return {
            'total_users': total_users,
            'total_gold_sold': total_gold_sold,
            'total_gold_withdrawn': total_gold_withdrawn,
            'total_rub_earned': total_rub_earned
        }

# ---------- Реферальная система ----------
def add_referral(user_id, referrer_id):
    with db_lock:
        conn = sqlite3.connect('bot_database.db')
        c = conn.cursor()
        c.execute("UPDATE users SET referrer_id = ? WHERE user_id = ?", (referrer_id, user_id))
        c.execute("UPDATE users SET balance = balance + 3, referral_earnings = referral_earnings + 3 WHERE user_id = ?", (referrer_id,))
        conn.commit()
        conn.close()
    bot.send_message(referrer_id, f"🎉 По вашему приглашению зарегистрировался новый пользователь!\nВы получили +3 голды на баланс!")

def add_referral_bonus(user_id, amount_gold):
    with db_lock:
        conn = sqlite3.connect('bot_database.db')
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT referrer_id FROM users WHERE user_id = ?", (user_id,))
        user = c.fetchone()
        if user and user['referrer_id']:
            bonus = int(amount_gold * 0.2)
            if bonus > 0:
                c.execute("UPDATE users SET balance = balance + ?, referral_earnings = referral_earnings + ? WHERE user_id = ?", 
                         (bonus, bonus, user['referrer_id']))
                bot.send_message(user['referrer_id'], f"🎁 Ваш реферал совершил покупку на {amount_gold} голды!\nВы получили +{bonus} голды (20%)")
        conn.commit()
        conn.close()

# ---------- Рассылка ----------
def send_broadcast(message_text, message_id=None):
    users = get_all_users()
    success_count = 0
    fail_count = 0
    
    for user in users:
        try:
            if message_id:
                bot.forward_message(user['user_id'], message_id, message_id)
            else:
                bot.send_message(user['user_id'], message_text)
            success_count += 1
            time.sleep(0.05)  # Чтобы не превысить лимиты
        except:
            fail_count += 1
    
    return success_count, fail_count

# ---------- Crypto Pay API ----------
def create_crypto_invoice(amount_usdt, description):
    url = "https://pay.crypt.bot/api/createInvoice"
    headers = {
        "Crypto-Pay-API-Token": "557593:AAbJ3WIbLI2ox4Fo2UdaebiLzEq2rsVcbLC",
        "Content-Type": "application/json"
    }
    payload = {
        "asset": "USDT",
        "amount": str(amount_usdt),
        "description": description,
        "hidden_message": "Оплата за голду",
        "paid_btn_name": "openBot",
        "paid_btn_url": f"https://t.me/{bot.get_me().username}"
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        result = response.json()
        if result.get("ok"):
            data = result["result"]
            return data["invoice_id"], data["bot_invoice_url"]
        else:
            print(f"Crypto Pay error: {result}")
            return None, None
    except Exception as e:
        print(f"Error creating invoice: {e}")
        return None, None

def check_invoice_status(invoice_id):
    url = "https://pay.crypt.bot/api/getInvoices"
    headers = {
        "Crypto-Pay-API-Token": "557593:AAbJ3WIbLI2ox4Fo2UdaebiLzEq2rsVcbLC",
        "Content-Type": "application/json"
    }
    params = {"invoice_ids": invoice_id}
    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        result = response.json()
        if result.get("ok"):
            invoices = result["result"]["items"]
            if invoices:
                return invoices[0]["status"] == "paid"
        return False
    except Exception as e:
        print(f"Error checking invoice: {e}")
        return False

def get_usdt_rate():
    try:
        response = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=rub", timeout=5)
        data = response.json()
        return data['tether']['rub']
    except:
        return 90

# ---------- Клавиатуры (компактные) ----------
def main_menu_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    btn1 = types.KeyboardButton("🪙 Купить голду")
    btn2 = types.KeyboardButton("💸 Вывести голду")
    btn3 = types.KeyboardButton("👤 Профиль")
    btn4 = types.KeyboardButton("📊 Курс")
    btn5 = types.KeyboardButton("ℹ️ Поддержка")
    btn6 = types.KeyboardButton("📰 Новости")
    btn7 = types.KeyboardButton("🎮 Игры")
    btn8 = types.KeyboardButton("⚙️ Админка")
    markup.add(btn1, btn2, btn3, btn4, btn5, btn6, btn7)
    if message.from_user.id in ADMIN_IDS:
        markup.add(btn8)
    return markup

def admin_menu_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("📊 Статистика", callback_data="admin_stats"),
        types.InlineKeyboardButton("📋 Заказы", callback_data="admin_orders"),
        types.InlineKeyboardButton("💸 Выводы", callback_data="admin_withdrawals"),
        types.InlineKeyboardButton("👥 Пользователи", callback_data="admin_users"),
        types.InlineKeyboardButton("📢 Рассылка", callback_data="admin_broadcast"),
        types.InlineKeyboardButton("🔓 Разблокировать", callback_data="admin_unblock"),
        types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")
    )
    return markup

def inline_buy_amount_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("💰 В голде", callback_data="buy_enter_gold"),
        types.InlineKeyboardButton("💵 В рублях", callback_data="buy_enter_rub")
    )
    markup.add(types.InlineKeyboardButton("❌ Отменить", callback_data="cancel"))
    return markup

def inline_payment_method_keyboard(amount_gold, amount_rub, amount_usdt):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("💳 ЮMoney", callback_data="pay_sber"),
        types.InlineKeyboardButton("🏦 Сбербанк", callback_data="pay_alfa"),
        types.InlineKeyboardButton("🏦 Ozonbank", callback_data="pay_ozon")
    )
    markup.add(types.InlineKeyboardButton(f"🪙 Crypto ({amount_usdt:.2f} USDT)", callback_data="pay_crypto"))
    markup.add(
        types.InlineKeyboardButton("✏️ Изменить", callback_data="buy_change_amount"),
        types.InlineKeyboardButton("❌ Отменить", callback_data="cancel")
    )
    return markup

def inline_screenshot_options_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✏️ Изменить", callback_data="buy_change_amount"),
        types.InlineKeyboardButton("💳 Другой способ", callback_data="buy_change_payment"),
        types.InlineKeyboardButton("❌ Отменить", callback_data="cancel")
    )
    return markup

def inline_withdraw_amount_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("❌ Отменить", callback_data="cancel_withdraw"))
    return markup

def inline_withdraw_screenshot_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("✏️ Изменить", callback_data="withdraw_change_amount"),
        types.InlineKeyboardButton("❌ Отменить", callback_data="cancel_withdraw")
    )
    return markup

def admin_order_keyboard(order_id, user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"approve_order:{order_id}"),
        types.InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_order:{order_id}"),
        types.InlineKeyboardButton("🚫 Заблокировать", callback_data=f"block_user:{user_id}")
    )
    return markup

def admin_withdrawal_keyboard(withdrawal_id, user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"approve_withdraw:{withdrawal_id}"),
        types.InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_withdraw:{withdrawal_id}"),
        types.InlineKeyboardButton("🚫 Заблокировать", callback_data=f"block_user:{user_id}")
    )
    return markup

def crypto_payment_keyboard(invoice_url, order_id):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("💳 Оплатить в USDT", url=invoice_url))
    markup.add(types.InlineKeyboardButton("🔄 Проверить", callback_data=f"check_crypto:{order_id}"))
    markup.add(types.InlineKeyboardButton("❌ Отменить", callback_data="cancel"))
    return markup

def games_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("📱 Номер телефона (+3 голды)", callback_data="verify_phone"),
        types.InlineKeyboardButton("📍 Геолокация (+7 голды)", callback_data="verify_geo"),
        types.InlineKeyboardButton("🔗 Реферальная ссылка", callback_data="referral_link")
    )
    return markup

# ---------- Обработчики команд ----------
@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    user = get_user(user_id)
    
    # Реферальная система
    args = message.text.split()
    if len(args) > 1 and args[1].isdigit():
        referrer_id = int(args[1])
        if referrer_id != user_id and user['referrer_id'] is None:
            add_referral(user_id, referrer_id)
    
    if check_subscription(user_id):
        bot.send_message(user_id, "Добро пожаловать в бот!\n\nКурс: 1 голда = 0.68 рубля", reply_markup=main_menu_keyboard(message))
    else:
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton("📢 Подписаться на канал", url=f"https://t.me/{REQUIRED_CHANNEL.replace('@', '')}"),
            types.InlineKeyboardButton("🔄 Проверить подписку", callback_data="check_subscription")
        )
        bot.send_message(
            user_id,
            f"❌ Для использования бота необходимо подписаться на наш канал {REQUIRED_CHANNEL}.\n\nПодпишитесь и нажмите 'Проверить подписку'.",
            reply_markup=markup
        )

@bot.message_handler(commands=['geo'])
def geo_command(message):
    keyboard = types.ReplyKeyboardMarkup(row_width=1, resize_keyboard=True)
    button_geo = types.KeyboardButton(text="📍 Отправить геолокацию", request_location=True)
    keyboard.add(button_geo)
    bot.send_message(
        message.chat.id, 
        "Нажмите на кнопку, чтобы передать свое местоположение.\nЗа это вы получите +7 голды!",
        reply_markup=keyboard
    )

# ---------- Обработчики текстовых сообщений ----------
@bot.message_handler(func=lambda message: True)
def handle_text(message):
    user_id = message.from_user.id
    text = message.text
    
    # Проверяем, нужно ли показывать админку
    if text == "⚙️ Админка" and user_id in ADMIN_IDS:
        bot.send_message(user_id, "⚙️ <b>Панель администратора</b>\n\nВыберите действие:", 
                        parse_mode='HTML', reply_markup=admin_menu_keyboard())
        return

    user = get_user(user_id)
    if user['is_blocked']:
        bot.send_message(user_id, "Вы заблокированы.")
        return

    if text == "🪙 Купить голду":
        if not check_subscription(user_id):
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(
                types.InlineKeyboardButton("📢 Подписаться", url=f"https://t.me/{REQUIRED_CHANNEL.replace('@', '')}"),
                types.InlineKeyboardButton("🔄 Проверить", callback_data="check_subscription")
            )
            bot.send_message(user_id, f"❌ Подпишитесь на {REQUIRED_CHANNEL}", reply_markup=markup)
            return
        bot.send_message(user_id, "Выберите способ ввода суммы:", reply_markup=inline_buy_amount_keyboard())
        user_state[user_id] = {'state': 'buy_awaiting_choice', 'data': {}}
        return

    if text == "💸 Вывести голду":
        if not check_subscription(user_id):
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(
                types.InlineKeyboardButton("📢 Подписаться", url=f"https://t.me/{REQUIRED_CHANNEL.replace('@', '')}"),
                types.InlineKeyboardButton("🔄 Проверить", callback_data="check_subscription")
            )
            bot.send_message(user_id, f"❌ Подпишитесь на {REQUIRED_CHANNEL}", reply_markup=markup)
            return
        balance = user['balance']
        bot.send_message(user_id, f"Введите сумму для вывода (в голде). У вас {balance} голды.",
                         reply_markup=inline_withdraw_amount_keyboard())
        user_state[user_id] = {'state': 'waiting_for_withdraw_amount', 'data': {}}
        return

    if text == "👤 Профиль":
        if not check_subscription(user_id):
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(
                types.InlineKeyboardButton("📢 Подписаться", url=f"https://t.me/{REQUIRED_CHANNEL.replace('@', '')}"),
                types.InlineKeyboardButton("🔄 Проверить", callback_data="check_subscription")
            )
            bot.send_message(user_id, f"❌ Подпишитесь на {REQUIRED_CHANNEL}", reply_markup=markup)
            return
        balance = user['balance']
        total_withdrawn = user['total_withdrawn'] or 0
        orders_count = get_user_orders_count(user_id)
        referral_earnings = user['referral_earnings'] or 0
        
        with db_lock:
            conn = sqlite3.connect('bot_database.db')
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (user_id,))
            referrals_count = c.fetchone()[0]
            conn.close()
        
        username = message.from_user.username or "Не указан"
        first_name = message.from_user.first_name or "Не указано"
        
        text = (f"👤 <b>Профиль</b>\n\n"
                f"🆔 ID: <code>{user_id}</code>\n"
                f"👤 Имя: {first_name}\n"
                f"📝 Username: @{username}\n"
                f"💰 Баланс: <b>{balance}</b> голды\n"
                f"📈 Всего заказов: {orders_count}\n"
                f"💸 Выведено: {total_withdrawn} голды\n"
                f"🤝 Рефералов: {referrals_count}\n"
                f"🎁 С рефералов: {referral_earnings} голды")
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔗 Реферальная ссылка", callback_data="referral_link"))
        
        bot.send_message(user_id, text, parse_mode='HTML', reply_markup=markup)
        return

    if text == "📊 Курс":
        usdt_rate = get_usdt_rate()
        text = (f"📈 <b>Текущий курс</b>\n\n"
                f"💰 1 голда = {GOLD_PRICE_RUB} руб.\n"
                f"💵 1 USDT = {usdt_rate} руб.\n\n"
                f"🪙 Покупка: от 50 до 5000 голды\n"
                f"💸 Вывод: комиссия 25%")
        bot.send_message(message.chat.id, text, parse_mode='HTML', reply_markup=main_menu_keyboard(message))
        return

    if text == "ℹ️ Поддержка":
        text = (f"📞 <b>Служба поддержки</b>\n\n"
                f"По всем вопросам обращайтесь:\n"
                f"📱 Telegram: @support_username\n"
                f"⏰ Время работы: 10:00 - 22:00 МСК")
        bot.send_message(message.chat.id, text, parse_mode='HTML', reply_markup=main_menu_keyboard(message))
        return

    if text == "📰 Новости":
        text = (f"📰 <b>Новости</b>\n\n"
                f"Подпишитесь на наш канал: {REQUIRED_CHANNEL}\n\n"
                f"Там вы узнаете о:\n"
                f"• Акциях и бонусах\n"
                f"• Обновлениях бота\n"
                f"• Конкурсах и розыгрышах")
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📢 Перейти на канал", url=f"https://t.me/{REQUIRED_CHANNEL.replace('@', '')}"))
        bot.send_message(message.chat.id, text, parse_mode='HTML', reply_markup=markup)
        return

    if text == "🎮 Игры":
        bot.send_message(message.chat.id, "🎮 <b>Игры и задания</b>\n\nВыполняйте задания и получайте голду!", 
                         parse_mode='HTML', reply_markup=games_keyboard())
        return

    # Обработка ввода суммы
    if user_id in user_state:
        state = user_state[user_id].get('state')
        data = user_state[user_id].get('data', {})

        if state == 'buy_awaiting_gold_amount':
            try:
                amount_gold = int(text)
            except ValueError:
                bot.send_message(user_id, "Пожалуйста, введите целое число.", 
                               reply_markup=inline_cancel_keyboard())
                return

            if amount_gold < 50:
                bot.send_message(user_id, "❌ Минимальное количество: 50 голды.", 
                               reply_markup=inline_cancel_keyboard())
                return
            if amount_gold > 5000:
                bot.send_message(user_id, "❌ Максимум: 5000 голды.", 
                               reply_markup=inline_cancel_keyboard())
                return

            amount_rub = int(amount_gold * GOLD_PRICE_RUB)
            usdt_rate = get_usdt_rate()
            amount_usdt = amount_rub / usdt_rate
            
            bot.send_message(user_id,
                             f"✅ {amount_gold} голды = {amount_rub} руб. ({amount_usdt:.2f} USDT)\n\nВыберите способ оплаты:",
                             reply_markup=inline_payment_method_keyboard(amount_gold, amount_rub, amount_usdt))
            user_state[user_id]['state'] = 'buy_awaiting_payment'
            user_state[user_id]['data']['amount_gold'] = amount_gold
            user_state[user_id]['data']['amount_rub'] = amount_rub
            return

        elif state == 'buy_awaiting_rub_amount':
            try:
                amount_rub = int(text)
            except ValueError:
                bot.send_message(user_id, "Пожалуйста, введите целое число.", 
                               reply_markup=inline_cancel_keyboard())
                return

            if amount_rub < 34:
                bot.send_message(user_id, "❌ Минимальная сумма: 34 руб.", 
                               reply_markup=inline_cancel_keyboard())
                return
            if amount_rub > 3400:
                bot.send_message(user_id, "❌ Максимум: 3400 руб.", 
                               reply_markup=inline_cancel_keyboard())
                return

            amount_gold = int(amount_rub / GOLD_PRICE_RUB)
            usdt_rate = get_usdt_rate()
            amount_usdt = amount_rub / usdt_rate
            
            bot.send_message(user_id,
                             f"✅ {amount_rub} руб. = {amount_gold} голды ({amount_usdt:.2f} USDT)\n\nВыберите способ оплаты:",
                             reply_markup=inline_payment_method_keyboard(amount_gold, amount_rub, amount_usdt))
            user_state[user_id]['state'] = 'buy_awaiting_payment'
            user_state[user_id]['data']['amount_gold'] = amount_gold
            user_state[user_id]['data']['amount_rub'] = amount_rub
            return

        elif state == 'waiting_for_withdraw_amount':
            try:
                amount = int(text)
            except ValueError:
                bot.send_message(user_id, "Пожалуйста, введите число.", 
                               reply_markup=inline_cancel_keyboard())
                return

            balance = user['balance']
            if amount <= 0:
                bot.send_message(user_id, "Сумма должна быть положительной.", 
                               reply_markup=inline_cancel_keyboard())
            elif amount > balance:
                bot.send_message(user_id, f"Недостаточно голды. Баланс: {balance}.", 
                               reply_markup=inline_cancel_keyboard())
            else:
                amount_with_comission = int(amount * 1.25)
                bot.send_message(user_id,
                                 f"Выставьте скин за {amount} + 25% = {amount_with_comission} голды\n\n"
                                 f"Сумма вывода: {amount} голды\n"
                                 f"Комиссия: {amount_with_comission - amount} голды",
                                 reply_markup=inline_withdraw_screenshot_keyboard())
                user_state[user_id]['state'] = 'waiting_for_withdraw_screenshot'
                user_state[user_id]['data']['amount'] = amount
                user_state[user_id]['data']['amount_with_comission'] = amount_with_comission
            return

        else:
            bot.send_message(user_id, "Используйте кнопки меню.", reply_markup=main_menu_keyboard(message))
            if user_id in user_state:
                del user_state[user_id]
    else:
        bot.send_message(user_id, "Используйте кнопки меню.", reply_markup=main_menu_keyboard(message))

def inline_cancel_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("❌ Отменить", callback_data="cancel"))
    return markup

# ---------- Обработчики колбэков ----------
@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    user_id = call.from_user.id
    data = call.data

    try:
        if data == "check_subscription":
            if check_subscription(user_id):
                bot.answer_callback_query(call.id, "✅ Подписка подтверждена!")
                bot.delete_message(user_id, call.message.message_id)
                bot.send_message(user_id, "Добро пожаловать!", reply_markup=main_menu_keyboard(call.message))
            else:
                bot.answer_callback_query(call.id, "❌ Вы не подписаны", show_alert=True)
            return

        if data == "referral_link":
            bot.answer_callback_query(call.id)
            referral_link = f"https://t.me/{bot.get_me().username}?start={user_id}"
            text = (f"🔗 <b>Ваша реферальная ссылка</b>\n\n"
                    f"<code>{referral_link}</code>\n\n"
                    f"📋 <b>Условия:</b>\n"
                    f"• +3 голды за каждого приглашенного\n"
                    f"• 20% от заказов рефералов")
            bot.send_message(user_id, text, parse_mode='HTML')
            return

        if data == "verify_phone":
            bot.answer_callback_query(call.id)
            keyboard = types.ReplyKeyboardMarkup(row_width=1, resize_keyboard=True)
            button_phone = types.KeyboardButton(text="📱 Отправить номер", request_contact=True)
            keyboard.add(button_phone)
            bot.send_message(user_id, "Нажмите кнопку для отправки номера телефона.\nЗа это +3 голды!", 
                             reply_markup=keyboard)
            return

        if data == "verify_geo":
            bot.answer_callback_query(call.id)
            keyboard = types.ReplyKeyboardMarkup(row_width=1, resize_keyboard=True)
            button_geo = types.KeyboardButton(text="📍 Отправить геолокацию", request_location=True)
            keyboard.add(button_geo)
            bot.send_message(user_id, "Нажмите кнопку для отправки геолокации.\nЗа это +7 голды!", 
                             reply_markup=keyboard)
            return

        if data == "cancel" or data == "cancel_withdraw":
            bot.answer_callback_query(call.id)
            bot.delete_message(user_id, call.message.message_id)
            bot.send_message(user_id, "Действие отменено.", reply_markup=main_menu_keyboard(call.message))
            if user_id in user_state:
                del user_state[user_id]
            return

        if data == "buy_enter_gold":
            bot.answer_callback_query(call.id)
            bot.edit_message_text("Введите количество голды:", user_id, call.message.message_id)
            user_state[user_id] = {'state': 'buy_awaiting_gold_amount', 'data': {}}
            return

        if data == "buy_enter_rub":
            bot.answer_callback_query(call.id)
            bot.edit_message_text("Введите сумму в рублях:", user_id, call.message.message_id)
            user_state[user_id] = {'state': 'buy_awaiting_rub_amount', 'data': {}}
            return

        if data == "buy_change_amount":
            bot.answer_callback_query(call.id)
            bot.edit_message_text("Выберите способ ввода:", user_id, call.message.message_id, 
                                 reply_markup=inline_buy_amount_keyboard())
            user_state[user_id] = {'state': 'buy_awaiting_choice', 'data': {}}
            return

        if data == "buy_change_payment":
            bot.answer_callback_query(call.id)
            if user_id in user_state and 'amount_gold' in user_state[user_id]['data']:
                amount_gold = user_state[user_id]['data']['amount_gold']
                amount_rub = user_state[user_id]['data']['amount_rub']
                usdt_rate = get_usdt_rate()
                amount_usdt = amount_rub / usdt_rate
                bot.edit_message_text(f"Выберите способ оплаты для {amount_gold} голды ({amount_rub} руб.):",
                                      user_id, call.message.message_id,
                                      reply_markup=inline_payment_method_keyboard(amount_gold, amount_rub, amount_usdt))
                user_state[user_id]['state'] = 'buy_awaiting_payment'
            return

        # Способы оплаты
        if data == "pay_sber":
            bot.answer_callback_query(call.id)
            if user_id in user_state and user_state[user_id].get('state') == 'buy_awaiting_payment':
                amount_gold = user_state[user_id]['data']['amount_gold']
                amount_rub = user_state[user_id]['data']['amount_rub']
                payment_details = "ЮMoney: 4100118451271375\n+79029732829"
                bot.edit_message_text(
                    f"💳 <b>Оплата через ЮMoney</b>\n\n{payment_details}\nСумма: {amount_rub} руб.\n\n✅ После оплаты отправьте скриншот.",
                    user_id, call.message.message_id, parse_mode='HTML',
                    reply_markup=inline_screenshot_options_keyboard()
                )
                user_state[user_id]['state'] = 'waiting_for_buy_screenshot'
                user_state[user_id]['data']['payment_method'] = 'ЮMoney'
            return

        if data == "pay_alfa":
            bot.answer_callback_query(call.id)
            if user_id in user_state and user_state[user_id].get('state') == 'buy_awaiting_payment':
                amount_gold = user_state[user_id]['data']['amount_gold']
                amount_rub = user_state[user_id]['data']['amount_rub']
                payment_details = "Сбербанк: 2202206195858209\n+79135595347"
                bot.edit_message_text(
                    f"🏦 <b>Оплата через Сбербанк</b>\n\n{payment_details}\nСумма: {amount_rub} руб.\n\n✅ После оплаты отправьте скриншот.",
                    user_id, call.message.message_id, parse_mode='HTML',
                    reply_markup=inline_screenshot_options_keyboard()
                )
                user_state[user_id]['state'] = 'waiting_for_buy_screenshot'
                user_state[user_id]['data']['payment_method'] = 'Сбербанк'
            return

        if data == "pay_ozon":
            bot.answer_callback_query(call.id)
            if user_id in user_state and user_state[user_id].get('state') == 'buy_awaiting_payment':
                amount_gold = user_state[user_id]['data']['amount_gold']
                amount_rub = user_state[user_id]['data']['amount_rub']
                payment_details = "Ozon Bank: 2200 1234 5678 9012\nМатвей Л."
                bot.edit_message_text(
                    f"🏦 <b>Оплата через Ozonbank</b>\n\n{payment_details}\nСумма: {amount_rub} руб.\n\n✅ После оплаты отправьте скриншот.",
                    user_id, call.message.message_id, parse_mode='HTML',
                    reply_markup=inline_screenshot_options_keyboard()
                )
                user_state[user_id]['state'] = 'waiting_for_buy_screenshot'
                user_state[user_id]['data']['payment_method'] = 'Ozonbank'
            return

        if data == "pay_crypto":
            bot.answer_callback_query(call.id)
            if user_id in user_state and user_state[user_id].get('state') == 'buy_awaiting_payment':
                amount_gold = user_state[user_id]['data']['amount_gold']
                amount_rub = user_state[user_id]['data']['amount_rub']
                usdt_rate = get_usdt_rate()
                amount_usdt = round(amount_rub / usdt_rate, 2)
                
                invoice_id, invoice_url = create_crypto_invoice(amount_usdt, f"Покупка {amount_gold} голды")
                if invoice_id and invoice_url:
                    order_id = add_order(user_id, amount_rub, amount_gold, amount_usdt, 'Crypto', invoice_id=invoice_id)
                    user_state[user_id]['data']['order_id'] = order_id
                    
                    bot.edit_message_text(
                        f"🪙 <b>Оплата через Crypto Pay</b>\n\n"
                        f"Сумма: {amount_usdt} USDT\n"
                        f"Курс: 1 USDT = {usdt_rate} руб.\n\n"
                        f"После оплаты нажмите 'Проверить'.",
                        user_id, call.message.message_id, parse_mode='HTML',
                        reply_markup=crypto_payment_keyboard(invoice_url, order_id)
                    )
                    user_state[user_id]['state'] = 'buy_crypto_waiting_payment'
                else:
                    bot.edit_message_text("❌ Ошибка создания счёта.", user_id, call.message.message_id)
            return

        if data.startswith("check_crypto:"):
            order_id = int(data.split(":")[1])
            order = get_order(order_id)
            if not order:
                bot.answer_callback_query(call.id, "Заказ не найден")
                return
            
            if check_invoice_status(order['invoice_id']):
                if order['status'] != 'approved':
                    update_order_status(order_id, 'approved')
                    update_balance(user_id, order['amount_gold'])
                    add_referral_bonus(user_id, order['amount_gold'])
                    bot.send_message(user_id, f"✅ Оплата получена! +{order['amount_gold']} голды.", 
                                    reply_markup=main_menu_keyboard(call.message))
                    bot.answer_callback_query(call.id, "✅ Оплата подтверждена!")
                    bot.edit_message_reply_markup(user_id, call.message.message_id, reply_markup=None)
                    if user_id in user_state:
                        del user_state[user_id]
                else:
                    bot.answer_callback_query(call.id, "Заказ уже обработан")
            else:
                bot.answer_callback_query(call.id, "⏳ Оплата не получена", show_alert=True)
            return

        if data == "withdraw_change_amount":
            bot.answer_callback_query(call.id)
            bot.edit_message_text("Введите новую сумму:", user_id, call.message.message_id)
            user_state[user_id] = {'state': 'waiting_for_withdraw_amount', 'data': {}}
            return

        if data == "buy_again":
            bot.answer_callback_query(call.id)
            buy_gold(call.message)
            return

        if data == "back_to_menu":
            bot.answer_callback_query(call.id)
            bot.delete_message(user_id, call.message.message_id)
            bot.send_message(user_id, "Главное меню:", reply_markup=main_menu_keyboard(call.message))
            return

        # Админские команды
        if data == "admin_stats" and user_id in ADMIN_IDS:
            stats = get_stats()
            text = (f"📊 <b>Статистика</b>\n\n"
                    f"👥 Пользователей: {stats['total_users']}\n"
                    f"🪙 Продано голды: {stats['total_gold_sold']}\n"
                    f"💸 Выведено голды: {stats['total_gold_withdrawn']}\n"
                    f"💰 Выручка: {stats['total_rub_earned']} руб.")
            bot.edit_message_text(text, user_id, call.message.message_id, parse_mode='HTML', 
                                 reply_markup=admin_menu_keyboard())
            return

        if data == "admin_orders" and user_id in ADMIN_IDS:
            orders = get_pending_orders()
            if not orders:
                text = "📋 Нет pending заказов"
            else:
                text = "📋 <b>Pending заказы</b>\n\n"
                for order in orders[:10]:
                    text += f"🆔 {order['id']} | {order['amount_gold']} голды | {order['payment_method']}\n"
            bot.edit_message_text(text, user_id, call.message.message_id, parse_mode='HTML',
                                 reply_markup=admin_menu_keyboard())
            return

        if data == "admin_withdrawals" and user_id in ADMIN_IDS:
            withdrawals = get_pending_withdrawals()
            if not withdrawals:
                text = "💸 Нет pending выводов"
            else:
                text = "💸 <b>Pending выводы</b>\n\n"
                for w in withdrawals[:10]:
                    text += f"🆔 {w['id']} | {w['amount']} голды\n"
            bot.edit_message_text(text, user_id, call.message.message_id, parse_mode='HTML',
                                 reply_markup=admin_menu_keyboard())
            return

        if data == "admin_users" and user_id in ADMIN_IDS:
            users = get_all_users()
            text = f"👥 <b>Все пользователи</b> ({len(users)})\n\n"
            for u in users[:20]:
                text += f"🆔 {u['user_id']} | {u['balance']} голды\n"
            bot.edit_message_text(text, user_id, call.message.message_id, parse_mode='HTML',
                                 reply_markup=admin_menu_keyboard())
            return

        if data == "admin_broadcast" and user_id in ADMIN_IDS:
            bot.edit_message_text("📢 Введите текст для рассылки (или перешлите сообщение):", 
                                 user_id, call.message.message_id,
                                 reply_markup=types.InlineKeyboardMarkup().add(
                                     types.InlineKeyboardButton("❌ Отмена", callback_data="back_to_menu")
                                 ))
            user_state[user_id] = {'state': 'admin_broadcast_waiting'}
            return

        if data == "admin_unblock" and user_id in ADMIN_IDS:
            bot.edit_message_text("Введите ID пользователя для разблокировки:", 
                                 user_id, call.message.message_id,
                                 reply_markup=types.InlineKeyboardMarkup().add(
                                     types.InlineKeyboardButton("❌ Отмена", callback_data="back_to_menu")
                                 ))
            user_state[user_id] = {'state': 'admin_unblock_waiting'}
            return

        # Обработка заказов
        if data.startswith("approve_order:"):
            order_id = int(data.split(":")[1])
            order = get_order(order_id)
            if order and order['status'] == 'pending':
                update_order_status(order_id, 'approved')
                update_balance(order['user_id'], order['amount_gold'])
                add_referral_bonus(order['user_id'], order['amount_gold'])
                bot.send_message(order['user_id'], f"✅ Заказ #{order_id} подтвержден! +{order['amount_gold']} голды.")
                bot.answer_callback_query(call.id, "Заказ подтвержден")
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            return

        if data.startswith("reject_order:"):
            order_id = int(data.split(":")[1])
            order = get_order(order_id)
            if order:
                update_order_status(order_id, 'rejected')
                bot.send_message(order['user_id'], f"❌ Заказ #{order_id} отклонен.")
                bot.answer_callback_query(call.id, "Заказ отклонен")
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            return

        if data.startswith("approve_withdraw:"):
            withdrawal_id = int(data.split(":")[1])
            withdrawal = get_withdrawal(withdrawal_id)
            if withdrawal and withdrawal['status'] == 'pending':
                update_withdrawal_status(withdrawal_id, 'approved')
                update_balance(withdrawal['user_id'], -withdrawal['amount'], withdrawal['amount'])
                bot.send_message(withdrawal['user_id'], f"✅ Вывод #{withdrawal_id} подтвержден! -{withdrawal['amount']} голды.")
                bot.answer_callback_query(call.id, "Вывод подтвержден")
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            return

        if data.startswith("reject_withdraw:"):
            withdrawal_id = int(data.split(":")[1])
            withdrawal = get_withdrawal(withdrawal_id)
            if withdrawal:
                update_withdrawal_status(withdrawal_id, 'rejected')
                bot.send_message(withdrawal['user_id'], f"❌ Вывод #{withdrawal_id} отклонен.")
                bot.answer_callback_query(call.id, "Вывод отклонен")
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            return

        if data.startswith("block_user:"):
            user_id_block = int(data.split(":")[1])
            block_user(user_id_block)
            bot.answer_callback_query(call.id, "Пользователь заблокирован")
            bot.send_message(call.message.chat.id, f"Пользователь {user_id_block} заблокирован.")
            return

    except Exception as e:
        print(f"Error in callback: {e}")
        bot.answer_callback_query(call.id, "Ошибка", show_alert=True)

# ---------- Обработчик фотографий ----------
@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    user_id = message.from_user.id
    user = get_user(user_id)
    if user['is_blocked']:
        bot.send_message(user_id, "Вы заблокированы.")
        return

    if not check_subscription(user_id):
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton("📢 Подписаться", url=f"https://t.me/{REQUIRED_CHANNEL.replace('@', '')}"),
            types.InlineKeyboardButton("🔄 Проверить", callback_data="check_subscription")
        )
        bot.send_message(user_id, f"❌ Подпишитесь на {REQUIRED_CHANNEL}", reply_markup=markup)
        return

    if user_id in user_state:
        state = user_state[user_id].get('state')
        data = user_state[user_id].get('data', {})

        if state == 'waiting_for_buy_screenshot':
            file_id = message.photo[-1].file_id
            amount_gold = data.get('amount_gold')
            amount_rub = data.get('amount_rub')
            payment_method = data.get('payment_method')
            
            if not all([amount_gold, amount_rub, payment_method]):
                bot.send_message(user_id, "Ошибка. Начните заново.", reply_markup=main_menu_keyboard(message))
                if user_id in user_state:
                    del user_state[user_id]
                return

            order_id = add_order(user_id, amount_rub, amount_gold, 0, payment_method, file_id)

            caption = (f"🆕 <b>Новый заказ</b>\n"
                       f"👤 {user_id} (@{message.from_user.username or 'no'})\n"
                       f"💰 {amount_rub} руб.\n"
                       f"🪙 {amount_gold} голды\n"
                       f"💳 {payment_method}\n"
                       f"📝 #{order_id}")
            for admin_id in ADMIN_IDS:
                bot.send_photo(admin_id, file_id, caption=caption, parse_mode='HTML',
                               reply_markup=admin_order_keyboard(order_id, user_id))

            bot.send_message(user_id, f"✅ Чек отправлен! Заказ #{order_id}", reply_markup=main_menu_keyboard(message))
            if user_id in user_state:
                del user_state[user_id]
            return

        elif state == 'waiting_for_withdraw_screenshot':
            file_id = message.photo[-1].file_id
            amount = data.get('amount')
            amount_with_comission = data.get('amount_with_comission')
            
            if not all([amount, amount_with_comission]):
                bot.send_message(user_id, "Ошибка. Начните заново.", reply_markup=main_menu_keyboard(message))
                if user_id in user_state:
                    del user_state[user_id]
                return

            withdrawal_id = add_withdrawal(user_id, amount, amount_with_comission, file_id)

            caption = (f"🆕 <b>Новая заявка на вывод</b>\n"
                       f"👤 {user_id} (@{message.from_user.username or 'no'})\n"
                       f"💸 {amount} голды\n"
                       f"📈 +25%: {amount_with_comission}\n"
                       f"📝 #{withdrawal_id}")
            for admin_id in ADMIN_IDS:
                bot.send_photo(admin_id, file_id, caption=caption, parse_mode='HTML',
                               reply_markup=admin_withdrawal_keyboard(withdrawal_id, user_id))

            bot.send_message(user_id, f"✅ Заявка #{withdrawal_id} отправлена!", reply_markup=main_menu_keyboard(message))
            if user_id in user_state:
                del user_state[user_id]
            return

# ---------- Обработчик контактов и геолокации ----------
@bot.message_handler(content_types=['contact'])
def handle_contact(message):
    user_id = message.from_user.id
    user = get_user(user_id)
    
    if user['phone_verified'] == 0:
        update_balance(user_id, 3)
        with db_lock:
            conn = sqlite3.connect('bot_database.db')
            c = conn.cursor()
            c.execute("UPDATE users SET phone_verified = 1 WHERE user_id = ?", (user_id,))
            conn.commit()
            conn.close()
        bot.send_message(user_id, "✅ Номер подтвержден! +3 голды.", reply_markup=main_menu_keyboard(message))
    else:
        bot.send_message(user_id, "Вы уже получали бонус.", reply_markup=main_menu_keyboard(message))

@bot.message_handler(content_types=['location'])
def handle_location(message):
    user_id = message.from_user.id
    user = get_user(user_id)
    
    if user['geo_verified'] == 0:
        update_balance(user_id, 7)
        with db_lock:
            conn = sqlite3.connect('bot_database.db')
            c = conn.cursor()
            c.execute("UPDATE users SET geo_verified = 1 WHERE user_id = ?", (user_id,))
            conn.commit()
            conn.close()
        bot.send_message(user_id, "✅ Геолокация подтверждена! +7 голды.", reply_markup=main_menu_keyboard(message))
    else:
        bot.send_message(user_id, "Вы уже получали бонус.", reply_markup=main_menu_keyboard(message))

# ---------- Обработчик текста для админки ----------
@bot.message_handler(func=lambda message: message.from_user.id in ADMIN_IDS and user_state.get(message.from_user.id, {}).get('state') == 'admin_broadcast_waiting')
def handle_broadcast(message):
    user_id = message.from_user.id
    
    if message.text:
        success, fail = send_broadcast(message.text)
        bot.send_message(user_id, f"📢 Рассылка завершена!\n✅ Успешно: {success}\n❌ Ошибок: {fail}")
    elif message.forward_from_chat or message.forward_from:
        success, fail = send_broadcast(None, message.message_id)
        bot.send_message(user_id, f"📢 Рассылка завершена!\n✅ Успешно: {success}\n❌ Ошибок: {fail}")
    
    if user_id in user_state:
        del user_state[user_id]
    bot.send_message(user_id, "Главное меню:", reply_markup=main_menu_keyboard(message))

@bot.message_handler(func=lambda message: message.from_user.id in ADMIN_IDS and user_state.get(message.from_user.id, {}).get('state') == 'admin_unblock_waiting')
def handle_unblock(message):
    user_id = message.from_user.id
    
    try:
        user_to_unblock = int(message.text)
        unblock_user(user_to_unblock)
        bot.send_message(user_id, f"✅ Пользователь {user_to_unblock} разблокирован.")
    except:
        bot.send_message(user_id, "❌ Введите корректный ID.")
    
    if user_id in user_state:
        del user_state[user_id]
    bot.send_message(user_id, "Главное меню:", reply_markup=main_menu_keyboard(message))

# ---------- Запуск бота ----------
if __name__ == "__main__":
    init_db()
    print("🤖 Бот запущен...")
    
    # Бесконечный цикл с переподключением
    while True:
        try:
            bot.polling(none_stop=True, interval=1, timeout=30)
        except Exception as e:
            print(f"Ошибка: {e}")
            print("Переподключение через 5 секунд...")
            time.sleep(5)
