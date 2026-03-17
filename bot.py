import os
import sqlite3
import logging
import time
from datetime import datetime
from telebot import TeleBot, types
from telebot.apihelper import ApiTelegramException
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv('BOT_TOKEN', '').strip()
ADMIN_IDS = [int(x) for x in os.getenv('ADMIN_IDS', '').replace(' ', '').split(',') if x.strip().isdigit()]
BOT_USERNAME = os.getenv('BOT_USERNAME', 'MyShopBot')
DB_PATH = os.getenv('DB_PATH', 'shop.db')
CURRENCY_NAME = os.getenv('CURRENCY_NAME', 'نقطة')
SUPPORT_LINK = os.getenv('SUPPORT_LINK', '')

if not TOKEN:
    raise SystemExit('BOT_TOKEN is missing in environment variables.')
if not ADMIN_IDS:
    raise SystemExit('ADMIN_IDS is missing. Add at least one admin id.')

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger(__name__)

bot = TeleBot(TOKEN, parse_mode='HTML')
user_states = {}

ORDER_STATUSES = {
    'pending': 'انتظار',
    'preparing': 'تجهيز الطلب',
    'in_delivery': 'قيد التسليم',
    'delivered': 'تم التسليم',
    'failed_delivery': 'لم يتم التسليم',
    'completed': 'مكتمل',
    'cancelled': 'ملغي',
}

STATUS_EMOJIS = {
    'pending': '🟠',
    'preparing': '🔵',
    'in_delivery': '🟢',
    'delivered': '✅',
    'failed_delivery': '⚠️',
    'completed': '☑️',
    'cancelled': '🔴',
}

STATUS_BUTTON_ORDER = ['pending', 'preparing', 'in_delivery', 'completed', 'cancelled']


def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def now_str():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def init_db():
    conn = db()
    cur = conn.cursor()
    cur.executescript('''
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id INTEGER UNIQUE NOT NULL,
        full_name TEXT,
        username TEXT,
        points INTEGER DEFAULT 0,
        is_blocked INTEGER DEFAULT 0,
        created_at TEXT,
        last_seen TEXT
    );

    CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        sort_order INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category_id INTEGER,
        name TEXT NOT NULL,
        description TEXT,
        price_points INTEGER NOT NULL DEFAULT 0,
        stock_count INTEGER DEFAULT 0,
        auto_delivery INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1,
        created_at TEXT,
        FOREIGN KEY(category_id) REFERENCES categories(id)
    );

    CREATE TABLE IF NOT EXISTS product_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL,
        code_text TEXT NOT NULL,
        is_used INTEGER DEFAULT 0,
        used_in_order_id INTEGER,
        created_at TEXT,
        FOREIGN KEY(product_id) REFERENCES products(id)
    );

    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        product_name TEXT NOT NULL,
        unit_price INTEGER NOT NULL,
        quantity INTEGER NOT NULL DEFAULT 1,
        total_price INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        delivery_text TEXT,
        admin_note TEXT,
        user_note TEXT,
        created_at TEXT,
        updated_at TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id),
        FOREIGN KEY(product_id) REFERENCES products(id)
    );

    CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        message TEXT NOT NULL,
        status TEXT DEFAULT 'open',
        related_order_id INTEGER,
        created_at TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    ''')
    try:
        cur.execute('ALTER TABLE orders ADD COLUMN refunded_points INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute('ALTER TABLE tickets ADD COLUMN admin_reply TEXT')
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE orders ADD COLUMN delivery_mode TEXT DEFAULT 'manual'")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    cur.execute("UPDATE orders SET delivery_mode='auto' WHERE (delivery_text IS NOT NULL AND TRIM(delivery_text) != '') AND (delivery_mode IS NULL OR delivery_mode='')")
    cur.execute("UPDATE orders SET delivery_mode='manual' WHERE delivery_mode IS NULL OR delivery_mode=''")
    conn.commit()
    conn.close()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def escape(s):
    return str(s or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def get_user_row(tg_id: int):
    conn = db()
    row = conn.execute('SELECT * FROM users WHERE tg_id=?', (tg_id,)).fetchone()
    conn.close()
    return row


def get_user_by_internal(user_id: int):
    conn = db()
    row = conn.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
    conn.close()
    return row


def upsert_user(message_user):
    conn = db()
    existing = conn.execute('SELECT * FROM users WHERE tg_id=?', (message_user.id,)).fetchone()
    if existing:
        conn.execute(
            'UPDATE users SET full_name=?, username=?, last_seen=? WHERE tg_id=?',
            (message_user.full_name, message_user.username or '', now_str(), message_user.id)
        )
        conn.commit()
        conn.close()
        return False
    conn.execute(
        'INSERT INTO users (tg_id, full_name, username, points, created_at, last_seen) VALUES (?,?,?,?,?,?)',
        (message_user.id, message_user.full_name, message_user.username or '', 0, now_str(), now_str())
    )
    conn.commit()
    conn.close()
    return True


def admin_menu():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton('🛍 إدارة المنتجات', callback_data='admin_products'),
        types.InlineKeyboardButton('📂 إدارة الأقسام', callback_data='admin_categories'),
        types.InlineKeyboardButton('📦 إدارة الطلبات', callback_data='admin_orders'),
        types.InlineKeyboardButton('📥 إدارة المخزون التلقائي', callback_data='admin_stock_manage'),
        types.InlineKeyboardButton('🔒 الاشتراك الإجباري', callback_data='admin_force_sub'),
        types.InlineKeyboardButton('💳 شحن نقاط يدوي', callback_data='admin_charge_points'),
        types.InlineKeyboardButton('📢 إذاعة', callback_data='admin_broadcast'),
        types.InlineKeyboardButton('📊 إحصائيات', callback_data='admin_stats'),
    )
    return kb


def main_menu(user_id):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row('🛒 المنتجات', '📦 طلباتي')
    kb.row('💰 رصيدي', '➕ شحن نقاط')
    kb.row('☎️ التواصل مع الإدارة')
    if is_admin(user_id):
        kb.row('⚙️ لوحة التحكم')
    return kb


def categories_keyboard(prefix='cat'):
    conn = db()
    rows = conn.execute('SELECT * FROM categories WHERE is_active=1 ORDER BY sort_order, id').fetchall()
    conn.close()
    kb = types.InlineKeyboardMarkup(row_width=2)
    for row in rows:
        kb.add(types.InlineKeyboardButton(f"📁 {row['name']}", callback_data=f'{prefix}:{row["id"]}'))
    return kb


def products_keyboard(category_id):
    conn = db()
    rows = conn.execute('SELECT * FROM products WHERE category_id=? AND is_active=1 ORDER BY id DESC', (category_id,)).fetchall()
    conn.close()
    kb = types.InlineKeyboardMarkup(row_width=1)
    for row in rows:
        suffix = ' - تلقائي' if row['auto_delivery'] else ''
        kb.add(types.InlineKeyboardButton(
            f"{row['name']} | {row['price_points']} {CURRENCY_NAME}{suffix}",
            callback_data=f'product:{row["id"]}'
        ))
    kb.add(types.InlineKeyboardButton('⬅️ رجوع', callback_data='back_categories'))
    return kb


def order_status_keyboard(order_id, current_status=None):
    kb = types.InlineKeyboardMarkup(row_width=2)
    if current_status not in ('completed', 'cancelled'):
        for code in STATUS_BUTTON_ORDER:
            kb.add(types.InlineKeyboardButton(status_text(code), callback_data=f'orderstatus:{order_id}:{code}'))
    kb.add(types.InlineKeyboardButton('💬 الرد على الطلب', callback_data=f'admin_reply_order:{order_id}'))
    return kb


def ticket_reply_keyboard(ticket_id):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton('💬 الرد على الرسالة', callback_data=f'admin_reply_ticket:{ticket_id}'))
    return kb


def send_admin_alert(text, reply_markup=None):
    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(admin_id, text, reply_markup=reply_markup)
        except Exception as e:
            logger.warning('Failed to send admin alert to %s: %s', admin_id, e)


def set_state(user_id, state, data=None):
    user_states[user_id] = {'state': state, 'data': data or {}}


def get_state(user_id):
    return user_states.get(user_id)


def clear_state(user_id):
    user_states.pop(user_id, None)


def ensure_points(user_tg_id, amount):
    row = get_user_row(user_tg_id)
    return row and row['points'] >= amount


def adjust_points(user_tg_id, amount):
    conn = db()
    conn.execute('UPDATE users SET points = points + ? WHERE tg_id=?', (amount, user_tg_id))
    conn.commit()
    conn.close()


def get_setting(key, default=''):
    conn = db()
    row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    conn.close()
    return row['value'] if row and row['value'] is not None else default


def set_setting(key, value):
    conn = db()
    conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, value))
    conn.commit()
    conn.close()


def get_force_sub_channels():
    raw = get_setting('force_sub_channels', '')
    return [x.strip() for x in raw.split('\n') if x.strip()]


def save_force_sub_channels(channels):
    cleaned = []
    seen = set()
    for ch in channels:
        ch = ch.strip()
        if not ch:
            continue
        if not ch.startswith('@'):
            ch = '@' + ch.lstrip('@')
        if ch not in seen:
            seen.add(ch)
            cleaned.append(ch)
    set_setting('force_sub_channels', '\n'.join(cleaned))


def is_force_sub_enabled():
    return get_setting('force_sub_enabled', '0') == '1'


def forced_sub_text():
    channels = get_force_sub_channels()
    lines = '\n'.join([f'• {escape(ch)}' for ch in channels]) if channels else 'لا توجد قنوات محددة حالياً.'
    return '🔒 للاستخدام يجب الاشتراك أولاً في القنوات التالية:\n' + lines


def check_force_sub(user_id):
    if is_admin(user_id):
        return True, ''
    if not is_force_sub_enabled():
        return True, ''
    channels = get_force_sub_channels()
    if not channels:
        return True, ''
    for ch in channels:
        try:
            member = bot.get_chat_member(ch, user_id)
            if member.status in ('left', 'kicked'):
                return False, forced_sub_text()
        except Exception:
            return False, forced_sub_text() + '\n\nتعذر التحقق من الاشتراك، تأكد أن البوت داخل القناة.'
    return True, ''


def ensure_force_sub_or_send(chat_id, user_id):
    ok, text = check_force_sub(user_id)
    if not ok:
        bot.send_message(chat_id, text)
    return ok


def add_category(name):
    conn = db()
    conn.execute('INSERT INTO categories (name, sort_order, is_active) VALUES (?, 0, 1)', (name,))
    conn.commit()
    conn.close()


def add_product(category_id, name, description, price_points, auto_delivery):
    conn = db()
    conn.execute(
        'INSERT INTO products (category_id, name, description, price_points, auto_delivery, stock_count, is_active, created_at) VALUES (?,?,?,?,?,?,1,?)',
        (category_id, name, description, price_points, 1 if auto_delivery else 0, 0, now_str())
    )
    conn.commit()
    conn.close()


def get_product(product_id):
    conn = db()
    row = conn.execute('SELECT * FROM products WHERE id=?', (product_id,)).fetchone()
    conn.close()
    return row


def deliver_automatic_item(product_id, order_id):
    conn = db()
    item = conn.execute(
        'SELECT * FROM product_items WHERE product_id=? AND is_used=0 ORDER BY id LIMIT 1',
        (product_id,)
    ).fetchone()
    if not item:
        conn.close()
        return None
    conn.execute('UPDATE product_items SET is_used=1, used_in_order_id=? WHERE id=?', (order_id, item['id']))
    conn.execute('UPDATE products SET stock_count = CASE WHEN stock_count > 0 THEN stock_count - 1 ELSE 0 END WHERE id=?', (product_id,))
    conn.commit()
    conn.close()
    return item['code_text']


def create_order(user_tg_id, product_id, quantity=1):
    user = get_user_row(user_tg_id)
    product = get_product(product_id)
    if not user or not product:
        return False, 'المنتج أو المستخدم غير موجود.'

    if product['auto_delivery']:
        conn = db()
        available = conn.execute(
            'SELECT COUNT(*) c FROM product_items WHERE product_id=? AND is_used=0',
            (product_id,)
        ).fetchone()['c']
        conn.close()
        if available < quantity:
            return False, 'نفد المخزون التلقائي حالياً، لا يمكنك تقديم طلب الآن.'

    total_price = product['price_points'] * quantity
    if user['points'] < total_price:
        return False, f'رصيدك غير كافٍ. المطلوب {total_price} {CURRENCY_NAME}.'

    conn = db()
    cur = conn.cursor()
    cur.execute('UPDATE users SET points = points - ? WHERE tg_id=? AND points >= ?', (total_price, user_tg_id, total_price))
    if cur.rowcount == 0:
        conn.rollback()
        conn.close()
        return False, 'تعذر خصم الرصيد، حاول مجدداً.'

    created_at = now_str()
    delivery_mode = 'auto' if product['auto_delivery'] else 'manual'
    cur.execute(
        'INSERT INTO orders (user_id, product_id, product_name, unit_price, quantity, total_price, status, delivery_mode, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
        (user['id'], product_id, product['name'], product['price_points'], quantity, total_price, 'pending', delivery_mode, created_at, created_at)
    )
    order_id = cur.lastrowid
    delivery_text = None
    status = 'pending'

    if product['auto_delivery']:
        item = conn.execute('SELECT * FROM product_items WHERE product_id=? AND is_used=0 ORDER BY id LIMIT 1', (product_id,)).fetchone()
        if item:
            conn.execute('UPDATE product_items SET is_used=1, used_in_order_id=? WHERE id=?', (order_id, item['id']))
            conn.execute('UPDATE products SET stock_count = CASE WHEN stock_count > 0 THEN stock_count - 1 ELSE 0 END WHERE id=?', (product_id,))
            delivery_text = item['code_text']
            status = 'delivered'
        else:
            conn.rollback()
            conn.close()
            return False, 'نفد المخزون التلقائي حالياً، لا يمكنك تقديم طلب الآن.'

    conn.execute('UPDATE orders SET status=?, delivery_text=?, updated_at=? WHERE id=?', (status, delivery_text, now_str(), order_id))
    conn.commit()
    conn.close()
    return True, {
        'order_id': order_id,
        'status': status,
        'delivery_text': delivery_text,
        'total_price': total_price,
        'product_name': product['name'],
        'product_description': product['description'] or 'لا يوجد وصف',
        'unit_price': product['price_points'],
        'created_at': created_at,
        'delivery_mode': delivery_mode,
    }


def product_caption(product):
    mode = 'تلقائي' if product['auto_delivery'] else 'يدوي'
    stock_line = f"\nالمخزون: {product['stock_count']}" if product['auto_delivery'] else ''
    return (
        f"<b>{escape(product['name'])}</b>\n"
        f"السعر: <b>{product['price_points']} {CURRENCY_NAME}</b>\n"
        f"التسليم: <b>{mode}</b>{stock_line}\n\n"
        f"{escape(product['description'] or 'لا يوجد وصف')}"
    )


def delivery_mode_label(mode):
    return 'تلقائي' if mode == 'auto' else 'يدوي'


def status_text(code):
    label = ORDER_STATUSES.get(code, code)
    emoji = STATUS_EMOJIS.get(code, 'ℹ️')
    return f'{emoji} {label}'


def user_identity_text(full_name, username, tg_id):
    return (
        f"الاسم: {escape(full_name or 'بدون')}\n"
        f"اليوزر: @{escape(username or 'بدون')}\n"
        f"الايدي: <code>{tg_id}</code>"
    )


def order_admin_text(order):
    mode = delivery_mode_label(order['delivery_mode'] or 'manual')
    description = order['description'] if 'description' in order.keys() and order['description'] else 'لا يوجد وصف'
    return (
        f"<b>إدارة الطلب #{order['id']}</b>\n"
        f"{user_identity_text(order['full_name'], order['username'], order['tg_id'])}\n"
        f"المنتج: {escape(order['product_name'])}\n"
        f"السعر: {order['total_price']} {CURRENCY_NAME}\n"
        f"تاريخ الإنشاء: {order['created_at']}\n"
        f"نوع التسليم: <b>{mode}</b>\n"
        f"وصف المنتج: {escape(description)}\n"
        f"الحالة الحالية: <b>{status_text(order['status'])}</b>"
        + (f"\n\n<b>بيانات التسليم:</b>\n<code>{escape(order['delivery_text'])}</code>" if order['delivery_text'] else ("\n\n<b>بيانات التسليم:</b> لا توجد" if mode == 'تلقائي' else ''))
        + (f"\n\nملاحظة العميل: {escape(order['user_note'])}" if order['user_note'] else '')
        + (f"\n\nملاحظة الإدارة: {escape(order['admin_note'])}" if order['admin_note'] else '')
    )


def order_user_text(order):
    mode = delivery_mode_label(order['delivery_mode'] or 'manual')
    description = order['description'] if 'description' in order.keys() and order['description'] else 'لا يوجد وصف'
    return (
        f"<b>طلب رقم #{order['id']}</b>\n"
        f"المنتج: {escape(order['product_name'])}\n"
        f"الكمية: {order['quantity']}\n"
        f"السعر: {order['total_price']} {CURRENCY_NAME}\n"
        f"نوع التسليم: <b>{mode}</b>\n"
        f"وصف المنتج: {escape(description)}\n"
        f"الحالة: <b>{status_text(order['status'])}</b>\n"
        f"التاريخ: {order['created_at']}"
        + (f"\n\n<b>بيانات التسليم:</b>\n<code>{escape(order['delivery_text'])}</code>" if order['delivery_text'] else '')
        + (f"\n\nملاحظة الإدارة: {escape(order['admin_note'])}" if order['admin_note'] else '')
        + (f"\n\nملاحظتك: {escape(order['user_note'])}" if order['user_note'] else '')
    )


@bot.message_handler(commands=['start'])
def start_handler(message):
    is_new = upsert_user(message.from_user)
    if is_new:
        send_admin_alert(
            '🚨 <b>عضو جديد دخل البوت</b>\n'
            f'الاسم: {escape(message.from_user.full_name)}\n'
            f'اليوزر: @{escape(message.from_user.username or "بدون") }\n'
            f'الايدي: <code>{message.from_user.id}</code>'
        )
    bot.send_message(
        message.chat.id,
        'أهلاً بك في المتجر الإلكتروني عبر تيليجرام.\nاختر من القائمة بالأسفل.',
        reply_markup=main_menu(message.from_user.id)
    )


@bot.message_handler(func=lambda m: m.text == '⚙️ لوحة التحكم' and is_admin(m.from_user.id))
def admin_panel(message):
    bot.send_message(message.chat.id, 'لوحة التحكم:', reply_markup=admin_menu())


@bot.message_handler(func=lambda m: m.text == '🛒 المنتجات')
def show_categories(message):
    if not ensure_force_sub_or_send(message.chat.id, message.from_user.id):
        return
    kb = categories_keyboard('cat')
    if not kb.keyboard:
        bot.send_message(message.chat.id, 'لا توجد أقسام حالياً.')
        return
    bot.send_message(message.chat.id, 'اختر القسم:', reply_markup=kb)


@bot.message_handler(func=lambda m: m.text == '💰 رصيدي')
def show_balance(message):
    user = get_user_row(message.from_user.id)
    bot.send_message(message.chat.id, f'رصيدك الحالي: <b>{user["points"]} {CURRENCY_NAME}</b>')


@bot.message_handler(func=lambda m: m.text == '📦 طلباتي')
def my_orders(message):
    user = get_user_row(message.from_user.id)
    conn = db()
    rows = conn.execute('SELECT o.*, p.description FROM orders o LEFT JOIN products p ON p.id = o.product_id WHERE o.user_id=? ORDER BY o.id ASC LIMIT 10', (user['id'],)).fetchall()
    conn.close()
    if not rows:
        bot.send_message(message.chat.id, 'لا توجد لديك طلبات بعد.')
        return
    for order in rows:
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton('📝 إرسال ملاحظة للإدارة حول الطلب', callback_data=f'user_note_order:{order["id"]}'))
        bot.send_message(message.chat.id, order_user_text(order), reply_markup=kb)


@bot.message_handler(func=lambda m: m.text == '➕ شحن نقاط')
def recharge_request(message):
    set_state(message.from_user.id, 'wait_recharge_photo')
    bot.send_message(message.chat.id, 'أرسل الآن صورة سند التحويل، ويمكنك كتابة ملاحظة معها في نفس الرسالة.')


@bot.message_handler(func=lambda m: m.text == '☎️ التواصل مع الإدارة')
def contact_admin(message):
    set_state(message.from_user.id, 'wait_support_message')
    txt = 'أرسل رسالتك وسيتم تحويلها للإدارة.'
    if SUPPORT_LINK:
        txt += f'\nرابط الدعم: {escape(SUPPORT_LINK)}'
    bot.send_message(message.chat.id, txt)


@bot.callback_query_handler(func=lambda c: True)
def callback_router(call):
    data = call.data
    uid = call.from_user.id

    if data.startswith(('cat:', 'product:', 'buy:', 'back_categories', 'user_note_order:')):
        ok, text_sub = check_force_sub(uid)
        if not ok:
            bot.answer_callback_query(call.id, 'يجب الاشتراك أولاً.', show_alert=True)
            try:
                bot.send_message(call.message.chat.id, text_sub)
            except Exception:
                pass
            return

    if data.startswith('cat:'):
        cat_id = int(data.split(':')[1])
        bot.edit_message_text('اختر المنتج:', call.message.chat.id, call.message.message_id, reply_markup=products_keyboard(cat_id))
        return

    if data == 'back_categories':
        bot.edit_message_text('اختر القسم:', call.message.chat.id, call.message.message_id, reply_markup=categories_keyboard('cat'))
        return

    if data.startswith('product:'):
        product_id = int(data.split(':')[1])
        product = get_product(product_id)
        if not product:
            bot.answer_callback_query(call.id, 'المنتج غير موجود')
            return
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton('✅ شراء الآن', callback_data=f'buy:{product_id}'))
        kb.add(types.InlineKeyboardButton('⬅️ رجوع', callback_data=f'cat:{product["category_id"]}'))
        bot.edit_message_text(product_caption(product), call.message.chat.id, call.message.message_id, reply_markup=kb)
        return

    if data.startswith('buy:'):
        product_id = int(data.split(':')[1])
        ok, result = create_order(uid, product_id, 1)
        if not ok:
            bot.answer_callback_query(call.id, result, show_alert=True)
            return
        mode = delivery_mode_label(result['delivery_mode'])
        msg = (
            f"✅ تم إنشاء الطلب رقم #{result['order_id']}\n"
            f"المنتج: {escape(result['product_name'])}\n"
            f"وصف المنتج: {escape(result['product_description'])}\n"
            f"الإجمالي: {result['total_price']} {CURRENCY_NAME}\n"
            f"تاريخ الإنشاء: {result['created_at']}\n"
            f"نوع التسليم: <b>{mode}</b>\n"
            f"الحالة: {status_text(result['status'])}"
        )
        if result['delivery_text']:
            msg += f"\n\n<b>بيانات التسليم:</b>\n<code>{escape(result['delivery_text'])}</code>"
        bot.send_message(call.message.chat.id, msg)
        user_label = f"@{escape(call.from_user.username)}" if call.from_user.username else 'بدون'
        admin_text = (
            '🛒 <b>طلب جديد</b>\n'
            f'الاسم: {escape(call.from_user.full_name)}\n'
            f'اليوزر: {user_label}\n'
            f'الايدي: <code>{uid}</code>\n'
            f'رقم الطلب: <code>{result["order_id"]}</code>\n'
            f'المنتج: {escape(result["product_name"])}\n'
            f'السعر: {result["total_price"]} {CURRENCY_NAME}\n'
            f'تاريخ الإنشاء: {result["created_at"]}\n'
            f'نوع التسليم: <b>{mode}</b>\n'
            f'وصف المنتج: {escape(result["product_description"])}\n'
            f'الحالة: {status_text(result["status"])}'
        )
        if result['delivery_text']:
            admin_text += f"\n\n<b>بيانات التسليم:</b>\n<code>{escape(result['delivery_text'])}</code>"
        send_admin_alert(
            admin_text,
            reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton('فتح إدارة الطلب', callback_data=f'admin_order_open:{result["order_id"]}'))
        )
        return

    if data == 'admin_products' and is_admin(uid):
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton('➕ إضافة منتج', callback_data='admin_add_product'),
            types.InlineKeyboardButton('✏️ تعديل منتج', callback_data='admin_edit_product'),
            types.InlineKeyboardButton('🗑 حذف منتج', callback_data='admin_delete_product'),
            types.InlineKeyboardButton('📥 إضافة مخزون تلقائي', callback_data='admin_add_stock'),
            types.InlineKeyboardButton('🧰 إدارة المخزون التلقائي', callback_data='admin_stock_manage'),
        )
        bot.edit_message_text('إدارة المنتجات:', call.message.chat.id, call.message.message_id, reply_markup=kb)
        return

    if data == 'admin_categories' and is_admin(uid):
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton('➕ إضافة قسم', callback_data='admin_add_category'),
            types.InlineKeyboardButton('✏️ تعديل قسم', callback_data='admin_edit_category'),
            types.InlineKeyboardButton('🗑 حذف قسم', callback_data='admin_delete_category'),
        )
        bot.edit_message_text('إدارة الأقسام:', call.message.chat.id, call.message.message_id, reply_markup=kb)
        return

    if data == 'admin_orders' and is_admin(uid):
        conn = db()
        rows = conn.execute("""
            SELECT o.*, u.tg_id, u.full_name, u.username, p.description
            FROM orders o
            JOIN users u ON u.id = o.user_id
            LEFT JOIN products p ON p.id = o.product_id
            ORDER BY o.id ASC LIMIT 10
        """).fetchall()
        conn.close()
        if not rows:
            bot.send_message(uid, 'لا توجد طلبات حالياً.')
            return
        for order in rows:
            bot.send_message(uid, order_admin_text(order), reply_markup=order_status_keyboard(order['id'], order['status']))
        return

    if data == 'admin_stock_manage' and is_admin(uid):
        conn = db()
        rows = conn.execute('SELECT * FROM products WHERE auto_delivery=1 ORDER BY id DESC LIMIT 50').fetchall()
        conn.close()
        if not rows:
            bot.send_message(uid, 'لا توجد منتجات تسليم تلقائي.')
            return
        kb = types.InlineKeyboardMarkup(row_width=1)
        for row in rows:
            kb.add(types.InlineKeyboardButton(f"📦 {row['name']} | المتبقي {row['stock_count']}", callback_data=f'stockprod:{row["id"]}'))
        bot.send_message(uid, 'اختر المنتج لإدارة المخزون التلقائي:', reply_markup=kb)
        return

    if data.startswith('stockprod:') and is_admin(uid):
        pid = int(data.split(':')[1])
        product = get_product(pid)
        if not product:
            bot.answer_callback_query(call.id, 'المنتج غير موجود')
            return
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton('➕ إضافة مخزون', callback_data=f'addstock:{pid}'),
            types.InlineKeyboardButton('🗑 حذف عنصر', callback_data=f'delstockpick:{pid}'),
            types.InlineKeyboardButton('🧹 حذف كل المخزون', callback_data=f'delstockall:{pid}')
        )
        bot.send_message(uid, f"إدارة مخزون المنتج: {escape(product['name'])}\nالمخزون الحالي: {product['stock_count']}", reply_markup=kb)
        return

    if data.startswith('delstockpick:') and is_admin(uid):
        pid = int(data.split(':')[1])
        conn = db()
        rows = conn.execute('SELECT * FROM product_items WHERE product_id=? AND is_used=0 ORDER BY id ASC LIMIT 30', (pid,)).fetchall()
        conn.close()
        if not rows:
            bot.send_message(uid, 'لا يوجد مخزون غير مستخدم لهذا المنتج.')
            return
        kb = types.InlineKeyboardMarkup(row_width=1)
        for row in rows:
            preview = row['code_text'].replace('\n', ' / ')[:45]
            kb.add(types.InlineKeyboardButton(f"🗑 #{row['id']} | {preview}", callback_data=f'delstockitem:{row["id"]}:{pid}'))
        bot.send_message(uid, 'اختر عنصر المخزون للحذف:', reply_markup=kb)
        return

    if data.startswith('delstockitem:') and is_admin(uid):
        _, item_id, pid = data.split(':')
        item_id = int(item_id)
        pid = int(pid)
        conn = db()
        row = conn.execute('SELECT * FROM product_items WHERE id=? AND is_used=0', (item_id,)).fetchone()
        if not row:
            conn.close()
            bot.answer_callback_query(call.id, 'العنصر غير موجود أو مستخدم')
            return
        conn.execute('DELETE FROM product_items WHERE id=?', (item_id,))
        conn.execute('UPDATE products SET stock_count = CASE WHEN stock_count > 0 THEN stock_count - 1 ELSE 0 END WHERE id=?', (pid,))
        conn.commit()
        conn.close()
        bot.answer_callback_query(call.id, 'تم حذف عنصر المخزون')
        return

    if data.startswith('delstockall:') and is_admin(uid):
        pid = int(data.split(':')[1])
        conn = db()
        count = conn.execute('SELECT COUNT(*) c FROM product_items WHERE product_id=? AND is_used=0', (pid,)).fetchone()['c']
        conn.execute('DELETE FROM product_items WHERE product_id=? AND is_used=0', (pid,))
        conn.execute('UPDATE products SET stock_count = 0 WHERE id=?', (pid,))
        conn.commit()
        conn.close()
        bot.answer_callback_query(call.id, f'تم حذف {count} عنصر من المخزون')
        return

    if data == 'admin_force_sub' and is_admin(uid):
        channels = get_force_sub_channels()
        status_label = 'مفعل' if is_force_sub_enabled() else 'معطل'
        channels_text = '\n'.join([f'• {escape(ch)}' for ch in channels]) if channels else 'لا توجد قنوات مضافة.'
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton('➕ إضافة قناة', callback_data='forcesub_add'),
            types.InlineKeyboardButton('🗑 حذف قناة', callback_data='forcesub_delete'),
            types.InlineKeyboardButton('✅ تفعيل', callback_data='forcesub_enable'),
            types.InlineKeyboardButton('⛔ تعطيل', callback_data='forcesub_disable')
        )
        bot.send_message(uid, f'🔒 الاشتراك الإجباري\nالحالة: <b>{status_label}</b>\n\nالقنوات:\n{channels_text}', reply_markup=kb)
        return

    if data == 'forcesub_add' and is_admin(uid):
        set_state(uid, 'admin_force_sub_add')
        bot.send_message(uid, 'أرسل معرف القناة بهذا الشكل: @channelusername')
        return

    if data == 'forcesub_delete' and is_admin(uid):
        channels = get_force_sub_channels()
        if not channels:
            bot.send_message(uid, 'لا توجد قنوات لحذفها.')
            return
        kb = types.InlineKeyboardMarkup(row_width=1)
        for i, ch in enumerate(channels):
            kb.add(types.InlineKeyboardButton(f'🗑 {ch}', callback_data=f'forcesubdel:{i}'))
        bot.send_message(uid, 'اختر القناة المراد حذفها:', reply_markup=kb)
        return

    if data.startswith('forcesubdel:') and is_admin(uid):
        idx = int(data.split(':')[1])
        channels = get_force_sub_channels()
        if 0 <= idx < len(channels):
            removed = channels.pop(idx)
            save_force_sub_channels(channels)
            bot.answer_callback_query(call.id, f'تم حذف {removed}')
        else:
            bot.answer_callback_query(call.id, 'القناة غير موجودة')
        return

    if data == 'forcesub_enable' and is_admin(uid):
        set_setting('force_sub_enabled', '1')
        bot.answer_callback_query(call.id, 'تم تفعيل الاشتراك الإجباري')
        return

    if data == 'forcesub_disable' and is_admin(uid):
        set_setting('force_sub_enabled', '0')
        bot.answer_callback_query(call.id, 'تم تعطيل الاشتراك الإجباري')
        return

    if data == 'admin_charge_points' and is_admin(uid):
        set_state(uid, 'admin_charge_points')
        bot.send_message(uid, 'أرسل بالشكل التالي:\n<code>ايدي عدد_النقاط</code>\nمثال: <code>123456789 500</code>')
        return

    if data == 'admin_broadcast' and is_admin(uid):
        set_state(uid, 'admin_broadcast')
        bot.send_message(uid, 'أرسل نص الإذاعة الآن.')
        return

    if data == 'admin_stats' and is_admin(uid):
        conn = db()
        users_count = conn.execute('SELECT COUNT(*) c FROM users').fetchone()['c']
        orders_count = conn.execute('SELECT COUNT(*) c FROM orders').fetchone()['c']
        products_count = conn.execute('SELECT COUNT(*) c FROM products').fetchone()['c']
        pending_count = conn.execute("SELECT COUNT(*) c FROM orders WHERE status IN ('pending','preparing')").fetchone()['c']
        conn.close()
        bot.send_message(uid, f'👥 المستخدمون: {users_count}\n🛍 المنتجات: {products_count}\n📦 الطلبات: {orders_count}\n⏳ قيد المتابعة: {pending_count}')
        return

    if data == 'admin_add_category' and is_admin(uid):
        set_state(uid, 'admin_add_category')
        bot.send_message(uid, 'أرسل اسم القسم الجديد.')
        return

    if data == 'admin_delete_category' and is_admin(uid):
        conn = db()
        rows = conn.execute('SELECT * FROM categories ORDER BY id DESC').fetchall()
        conn.close()
        kb = types.InlineKeyboardMarkup(row_width=1)
        for row in rows:
            kb.add(types.InlineKeyboardButton(f"🗑 {row['name']}", callback_data=f'delcat:{row["id"]}'))
        bot.send_message(uid, 'اختر القسم للحذف:', reply_markup=kb)
        return

    if data.startswith('delcat:') and is_admin(uid):
        cat_id = int(data.split(':')[1])
        conn = db()
        conn.execute('DELETE FROM categories WHERE id=?', (cat_id,))
        conn.commit()
        conn.close()
        bot.answer_callback_query(call.id, 'تم حذف القسم')
        return

    if data == 'admin_edit_category' and is_admin(uid):
        conn = db()
        rows = conn.execute('SELECT * FROM categories ORDER BY id DESC').fetchall()
        conn.close()
        kb = types.InlineKeyboardMarkup(row_width=1)
        for row in rows:
            kb.add(types.InlineKeyboardButton(f"✏️ {row['name']}", callback_data=f'editcat:{row["id"]}'))
        bot.send_message(uid, 'اختر القسم للتعديل:', reply_markup=kb)
        return

    if data.startswith('editcat:') and is_admin(uid):
        cat_id = int(data.split(':')[1])
        set_state(uid, 'admin_edit_category_name', {'category_id': cat_id})
        bot.send_message(uid, 'أرسل الاسم الجديد للقسم.')
        return

    if data == 'admin_add_product' and is_admin(uid):
        conn = db()
        rows = conn.execute('SELECT * FROM categories WHERE is_active=1 ORDER BY id DESC').fetchall()
        conn.close()
        if not rows:
            bot.send_message(uid, 'أضف قسماً أولاً.')
            return
        kb = types.InlineKeyboardMarkup(row_width=1)
        for row in rows:
            kb.add(types.InlineKeyboardButton(row['name'], callback_data=f'addproductcat:{row["id"]}'))
        bot.send_message(uid, 'اختر القسم الذي سيتم إضافة المنتج بداخله:', reply_markup=kb)
        return

    if data.startswith('addproductcat:') and is_admin(uid):
        cat_id = int(data.split(':')[1])
        set_state(uid, 'admin_add_product_form', {'category_id': cat_id})
        bot.send_message(uid, 'أرسل بيانات المنتج بهذا الشكل:\n<code>الاسم | السعر | تلقائي 0 أو 1 | الوصف</code>\nمثال:\n<code>Netflix 1 month | 2500 | 1 | حساب نتفلكس شهر كامل</code>')
        return

    if data == 'admin_delete_product' and is_admin(uid):
        conn = db()
        rows = conn.execute('SELECT * FROM products ORDER BY id DESC LIMIT 50').fetchall()
        conn.close()
        kb = types.InlineKeyboardMarkup(row_width=1)
        for row in rows:
            kb.add(types.InlineKeyboardButton(f"🗑 {row['name']}", callback_data=f'delprod:{row["id"]}'))
        bot.send_message(uid, 'اختر المنتج للحذف:', reply_markup=kb)
        return

    if data.startswith('delprod:') and is_admin(uid):
        pid = int(data.split(':')[1])
        conn = db()
        conn.execute('DELETE FROM products WHERE id=?', (pid,))
        conn.execute('DELETE FROM product_items WHERE product_id=?', (pid,))
        conn.commit()
        conn.close()
        bot.answer_callback_query(call.id, 'تم حذف المنتج')
        return

    if data == 'admin_edit_product' and is_admin(uid):
        conn = db()
        rows = conn.execute('SELECT * FROM products ORDER BY id DESC LIMIT 50').fetchall()
        conn.close()
        kb = types.InlineKeyboardMarkup(row_width=1)
        for row in rows:
            kb.add(types.InlineKeyboardButton(f"✏️ {row['name']}", callback_data=f'editprod:{row["id"]}'))
        bot.send_message(uid, 'اختر المنتج للتعديل:', reply_markup=kb)
        return

    if data.startswith('editprod:') and is_admin(uid):
        pid = int(data.split(':')[1])
        set_state(uid, 'admin_edit_product_form', {'product_id': pid})
        bot.send_message(uid, 'أرسل البيانات الجديدة بنفس الشكل:\n<code>الاسم | السعر | تلقائي 0 أو 1 | الوصف</code>')
        return

    if data == 'admin_add_stock' and is_admin(uid):
        conn = db()
        rows = conn.execute('SELECT * FROM products WHERE auto_delivery=1 ORDER BY id DESC LIMIT 50').fetchall()
        conn.close()
        if not rows:
            bot.send_message(uid, 'لا توجد منتجات تسليم تلقائي.')
            return
        kb = types.InlineKeyboardMarkup(row_width=1)
        for row in rows:
            kb.add(types.InlineKeyboardButton(f"📥 {row['name']}", callback_data=f'addstock:{row["id"]}'))
        bot.send_message(uid, 'اختر المنتج لإضافة المخزون:', reply_markup=kb)
        return

    if data.startswith('addstock:') and is_admin(uid):
        pid = int(data.split(':')[1])
        set_state(uid, 'admin_add_stock_codes', {'product_id': pid})
        bot.send_message(uid, 'أرسل المخزون الآن. يمكنك كتابة كل عنصر بعدة أسطر، وافصل بين كل عنصر والذي بعده بسطر فارغ.')
        return

    if data.startswith('admin_order_open:') and is_admin(uid):
        order_id = int(data.split(':')[1])
        conn = db()
        order = conn.execute("""
            SELECT o.*, u.tg_id, u.full_name, u.username, p.description
            FROM orders o
            JOIN users u ON u.id = o.user_id
            LEFT JOIN products p ON p.id = o.product_id
            WHERE o.id=?
        """, (order_id,)).fetchone()
        conn.close()
        if not order:
            bot.answer_callback_query(call.id, 'الطلب غير موجود')
            return
        bot.send_message(uid, order_admin_text(order), reply_markup=order_status_keyboard(order_id, order['status']))
        return

    if data.startswith('orderstatus:') and is_admin(uid):
        _, order_id, status = data.split(':')
        order_id = int(order_id)
        conn = db()
        order = conn.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
        if not order:
            conn.close()
            bot.answer_callback_query(call.id, 'الطلب غير موجود')
            return
        if order['status'] in ('completed', 'cancelled'):
            conn.close()
            bot.answer_callback_query(call.id, 'تم إغلاق حالات هذا الطلب نهائياً')
            return

        if status == 'in_delivery':
            conn.execute('UPDATE orders SET status=?, updated_at=? WHERE id=?', ('in_delivery', now_str(), order_id))
            order_view = conn.execute('''
                SELECT o.*, u.tg_id, u.full_name, u.username, p.description
                FROM orders o
                JOIN users u ON u.id = o.user_id
                LEFT JOIN products p ON p.id = o.product_id
                WHERE o.id=?
            ''', (order_id,)).fetchone()
            conn.commit()
            conn.close()
            set_state(uid, 'admin_delivery_message', {'order_id': order_id})
            try:
                bot.edit_message_text(
                    order_admin_text(order_view),
                    call.message.chat.id,
                    call.message.message_id,
                    reply_markup=order_status_keyboard(order_id, 'in_delivery')
                )
            except Exception:
                try:
                    bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=order_status_keyboard(order_id, 'in_delivery'))
                except Exception:
                    pass
            bot.answer_callback_query(call.id, 'أرسل الآن رسالة أو رد بخصوص التسليم للمستخدم')
            bot.send_message(uid, f'أرسل الآن رسالة أو رد بخصوص التسليم للطلب #{order_id}.')
            return

        refund_msg = ''
        if status == 'cancelled' and not order['refunded_points']:
            conn.execute('UPDATE users SET points = points + ? WHERE id=?', (order['total_price'], order['user_id']))
            conn.execute('UPDATE orders SET refunded_points=1 WHERE id=?', (order_id,))
            refund_msg = f'\n💰 تم استرجاع {order["total_price"]} {CURRENCY_NAME} إلى رصيدك.'

        updated_at = now_str()
        conn.execute('UPDATE orders SET status=?, updated_at=? WHERE id=?', (status, updated_at, order_id))
        user_row = conn.execute('SELECT * FROM users WHERE id=?', (order['user_id'],)).fetchone()
        order_view = conn.execute('''
            SELECT o.*, u.tg_id, u.full_name, u.username, p.description
            FROM orders o
            JOIN users u ON u.id = o.user_id
            LEFT JOIN products p ON p.id = o.product_id
            WHERE o.id=?
        ''', (order_id,)).fetchone()
        conn.commit()
        conn.close()
        try:
            bot.send_message(user_row['tg_id'], f'📦 تم تحديث حالة طلبك رقم #{order_id} إلى: <b>{status_text(status)}</b>{refund_msg}')
        except Exception:
            pass
        try:
            bot.edit_message_text(
                order_admin_text(order_view),
                call.message.chat.id,
                call.message.message_id,
                reply_markup=order_status_keyboard(order_id, status)
            )
        except Exception:
            try:
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=order_status_keyboard(order_id, status))
            except Exception:
                pass
        bot.answer_callback_query(call.id, 'تم تحديث الحالة')
        return

    if data.startswith('confirmdelivery:') and is_admin(uid):
        _, order_id, result = data.split(':')
        order_id = int(order_id)
        final_status = 'delivered' if result == 'yes' else 'failed_delivery'
        conn = db()
        order = conn.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
        if not order:
            conn.close()
            bot.answer_callback_query(call.id, 'الطلب غير موجود')
            return
        conn.execute('UPDATE orders SET status=?, updated_at=? WHERE id=?', (final_status, now_str(), order_id))
        user_row = conn.execute('SELECT * FROM users WHERE id=?', (order['user_id'],)).fetchone()
        order_view = conn.execute('''
            SELECT o.*, u.tg_id, u.full_name, u.username, p.description
            FROM orders o
            JOIN users u ON u.id = o.user_id
            LEFT JOIN products p ON p.id = o.product_id
            WHERE o.id=?
        ''', (order_id,)).fetchone()
        conn.commit()
        conn.close()
        try:
            bot.send_message(user_row['tg_id'], f'📦 تم تحديث حالة طلبك رقم #{order_id} إلى: <b>{status_text(final_status)}</b>')
        except Exception:
            pass
        try:
            bot.edit_message_text(
                f'هل تم التسليم بنجاح للطلب #{order_id}؟\n\nالنتيجة: <b>{status_text(final_status)}</b>',
                call.message.chat.id,
                call.message.message_id
            )
        except Exception:
            pass
        bot.send_message(uid, order_admin_text(order_view), reply_markup=order_status_keyboard(order_id, final_status))
        bot.answer_callback_query(call.id, 'تم حفظ نتيجة التسليم')
        return

    if data.startswith('admin_reply_order:') and is_admin(uid):
        order_id = int(data.split(':')[1])
        set_state(uid, 'admin_reply_order', {'order_id': order_id})
        bot.send_message(uid, 'أرسل ردك على هذا الطلب.')
        return

    if data.startswith('admin_reply_ticket:') and is_admin(uid):
        ticket_id = int(data.split(':')[1])
        set_state(uid, 'admin_reply_ticket', {'ticket_id': ticket_id})
        bot.send_message(uid, 'أرسل ردك على هذه الرسالة.')
        return

    if data.startswith('user_note_order:'):
        order_id = int(data.split(':')[1])
        set_state(uid, 'user_note_order', {'order_id': order_id})
        bot.send_message(uid, 'أرسل ملاحظتك حول الطلب.')
        return

    bot.answer_callback_query(call.id, 'أمر غير معروف')




@bot.message_handler(content_types=['photo'])
def photo_router(message):
    user = message.from_user
    upsert_user(user)
    state = get_state(user.id)
    if not state:
        return

    st = state['state']
    if st != 'wait_recharge_photo':
        return

    row = get_user_row(user.id)
    caption = message.caption or 'بدون ملاحظة'
    file_id = message.photo[-1].file_id

    conn = db()
    cur = conn.cursor()
    cur.execute('INSERT INTO tickets (user_id, kind, message, created_at) VALUES (?,?,?,?)', (row['id'], 'recharge', caption, now_str()))
    ticket_id = cur.lastrowid
    conn.commit()
    conn.close()

    for admin_id in ADMIN_IDS:
        try:
            bot.send_photo(
                admin_id,
                file_id,
                caption=(
                    '💳 <b>طلب شحن نقاط جديد</b>\n'
                    f'الاسم: {escape(user.full_name)}\n'
                    f'اليوزر: @{escape(user.username or "بدون")}\n'
                    f'ايدي: <code>{user.id}</code>\n\n'
                    f'الملاحظة: {escape(caption)}'
                ),
                reply_markup=ticket_reply_keyboard(ticket_id)
            )
        except Exception as e:
            logger.warning('Failed to send recharge photo to %s: %s', admin_id, e)

    clear_state(user.id)
    bot.send_message(message.chat.id, 'تم إرسال سند التحويل للإدارة، انتظر المراجعة.')

@bot.message_handler(func=lambda m: True, content_types=['text'])
def text_router(message):
    user = message.from_user
    upsert_user(user)
    state = get_state(user.id)

    if not state:
        return

    st = state['state']
    data = state.get('data', {})

    if st == 'wait_support_message':
        row = get_user_row(user.id)
        conn = db()
        cur = conn.cursor()
        cur.execute('INSERT INTO tickets (user_id, kind, message, created_at) VALUES (?,?,?,?)', (row['id'], 'support', message.text, now_str()))
        ticket_id = cur.lastrowid
        conn.commit()
        conn.close()
        for admin_id in ADMIN_IDS:
            try:
                bot.send_message(
                    admin_id,
                    '📩 <b>رسالة جديدة من مستخدم</b>\n'
                    f'الاسم: {escape(user.full_name)}\n'
                    f'اليوزر: @{escape(user.username or "بدون")}\n'
                    f'ايدي: <code>{user.id}</code>\n\n'
                    f'{escape(message.text)}',
                    reply_markup=ticket_reply_keyboard(ticket_id)
                )
            except Exception as e:
                logger.warning('Failed to send support message to %s: %s', admin_id, e)
        clear_state(user.id)
        bot.send_message(message.chat.id, 'تم إرسال رسالتك للإدارة.')
        return

    if st == 'wait_recharge_note':
        row = get_user_row(user.id)
        conn = db()
        conn.execute('INSERT INTO tickets (user_id, kind, message, created_at) VALUES (?,?,?,?)', (row['id'], 'recharge', message.text, now_str()))
        conn.commit()
        conn.close()
        send_admin_alert(
            '💳 <b>طلب شحن نقاط</b>\n'
            f'الاسم: {escape(user.full_name)}\n'
            f'ايدي: <code>{user.id}</code>\n\n'
            f'{escape(message.text)}'
        )
        clear_state(user.id)
        bot.send_message(message.chat.id, 'تم إرسال طلب الشحن للإدارة، انتظر المراجعة.')
        return


    if st == 'admin_force_sub_add' and is_admin(user.id):
        channel = message.text.strip()
        if not channel.startswith('@'):
            channel = '@' + channel.lstrip('@')
        channels = get_force_sub_channels()
        if channel not in channels:
            channels.append(channel)
            save_force_sub_channels(channels)
        clear_state(user.id)
        bot.send_message(message.chat.id, f'تمت إضافة القناة {escape(channel)} للاشتراك الإجباري.')
        return

    if st == 'admin_charge_points' and is_admin(user.id):
        try:
            tg_id, amount = message.text.split()
            tg_id = int(tg_id)
            amount = int(amount)
        except ValueError:
            bot.send_message(message.chat.id, 'صيغة غير صحيحة.')
            return
        conn = db()
        target = conn.execute('SELECT * FROM users WHERE tg_id=?', (tg_id,)).fetchone()
        if not target:
            conn.close()
            bot.send_message(message.chat.id, 'المستخدم غير موجود.')
            return
        conn.execute('UPDATE users SET points = points + ? WHERE tg_id=?', (amount, tg_id))
        conn.commit()
        conn.close()
        clear_state(user.id)
        bot.send_message(message.chat.id, f'تم شحن {amount} {CURRENCY_NAME} للمستخدم {tg_id}.')
        try:
            bot.send_message(tg_id, f'✅ تم شحن رصيدك بمقدار <b>{amount} {CURRENCY_NAME}</b>')
        except Exception:
            pass
        return

    if st == 'admin_broadcast' and is_admin(user.id):
        conn = db()
        rows = conn.execute('SELECT tg_id FROM users WHERE is_blocked=0').fetchall()
        conn.close()
        success = 0
        fail = 0
        for row in rows:
            try:
                bot.send_message(row['tg_id'], message.text)
                success += 1
                time.sleep(0.03)
            except ApiTelegramException:
                fail += 1
            except Exception:
                fail += 1
        clear_state(user.id)
        bot.send_message(message.chat.id, f'انتهت الإذاعة. نجح: {success} | فشل: {fail}')
        return

    if st == 'admin_add_category' and is_admin(user.id):
        add_category(message.text.strip())
        clear_state(user.id)
        bot.send_message(message.chat.id, 'تمت إضافة القسم.')
        return

    if st == 'admin_edit_category_name' and is_admin(user.id):
        conn = db()
        conn.execute('UPDATE categories SET name=? WHERE id=?', (message.text.strip(), data['category_id']))
        conn.commit()
        conn.close()
        clear_state(user.id)
        bot.send_message(message.chat.id, 'تم تعديل اسم القسم.')
        return

    if st == 'admin_add_product_form' and is_admin(user.id):
        try:
            name, price, auto_delivery, description = [x.strip() for x in message.text.split('|', 3)]
            add_product(data['category_id'], name, description, int(price), int(auto_delivery) == 1)
            clear_state(user.id)
            bot.send_message(message.chat.id, 'تمت إضافة المنتج.')
        except Exception:
            bot.send_message(message.chat.id, 'الصيغة غير صحيحة.')
        return

    if st == 'admin_edit_product_form' and is_admin(user.id):
        try:
            name, price, auto_delivery, description = [x.strip() for x in message.text.split('|', 3)]
            conn = db()
            conn.execute(
                'UPDATE products SET name=?, price_points=?, auto_delivery=?, description=? WHERE id=?',
                (name, int(price), 1 if int(auto_delivery) == 1 else 0, description, data['product_id'])
            )
            conn.commit()
            conn.close()
            clear_state(user.id)
            bot.send_message(message.chat.id, 'تم تعديل المنتج.')
        except Exception:
            bot.send_message(message.chat.id, 'الصيغة غير صحيحة.')
        return

    if st == 'admin_add_stock_codes' and is_admin(user.id):
        normalized_text = message.text.replace('\r\n', '\n')
        raw_entries = [block.strip() for block in normalized_text.split('\n\n') if block.strip()]
        if not raw_entries:
            bot.send_message(message.chat.id, 'لم يتم العثور على أكواد.')
            return
        conn = db()
        for code in raw_entries:
            conn.execute('INSERT INTO product_items (product_id, code_text, created_at) VALUES (?,?,?)', (data['product_id'], code, now_str()))
        conn.execute('UPDATE products SET stock_count = stock_count + ? WHERE id=?', (len(raw_entries), data['product_id']))
        conn.commit()
        conn.close()
        clear_state(user.id)
        bot.send_message(message.chat.id, f'تمت إضافة {len(raw_entries)} عنصر للمخزون التلقائي.')
        return


    if st == 'admin_delivery_message' and is_admin(user.id):
        conn = db()
        order = conn.execute('SELECT * FROM orders WHERE id=?', (data['order_id'],)).fetchone()
        if not order:
            conn.close()
            clear_state(user.id)
            bot.send_message(message.chat.id, 'الطلب غير موجود.')
            return
        target = conn.execute('SELECT * FROM users WHERE id=?', (order['user_id'],)).fetchone()
        conn.close()
        clear_state(user.id)
        if target:
            try:
                bot.send_message(target['tg_id'], f'📦 رسالة بخصوص تسليم طلبك #{data["order_id"]}:\n{escape(message.text)}')
            except Exception:
                pass
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton('✅ نعم تم التسليم', callback_data=f'confirmdelivery:{data["order_id"]}:yes'),
            types.InlineKeyboardButton('❌ لا لم يتم التسليم', callback_data=f'confirmdelivery:{data["order_id"]}:no')
        )
        bot.send_message(message.chat.id, f'تم إرسال الرسالة للمستخدم. هل تم التسليم بنجاح للطلب #{data["order_id"]}؟', reply_markup=kb)
        return

    if st == 'admin_reply_ticket' and is_admin(user.id):
        conn = db()
        ticket = conn.execute('SELECT * FROM tickets WHERE id=?', (data['ticket_id'],)).fetchone()
        if not ticket:
            conn.close()
            clear_state(user.id)
            bot.send_message(message.chat.id, 'الرسالة غير موجودة.')
            return
        target = conn.execute('SELECT * FROM users WHERE id=?', (ticket['user_id'],)).fetchone()
        conn.execute('UPDATE tickets SET admin_reply=?, status=? WHERE id=?', (message.text, 'answered', data['ticket_id']))
        conn.commit()
        conn.close()
        clear_state(user.id)
        if target:
            try:
                kind_label = {
                    'support': 'على رسالتك',
                    'recharge': 'على سند التحويل',
                    'order_note': 'على ملاحظتك',
                }.get(ticket['kind'], 'على رسالتك')
                bot.send_message(target['tg_id'], f'💬 رد الإدارة {kind_label}:\n{escape(message.text)}')
            except Exception:
                pass
        bot.send_message(message.chat.id, 'تم إرسال الرد للمستخدم.')
        return

    if st == 'admin_reply_order' and is_admin(user.id):
        conn = db()
        order = conn.execute('SELECT * FROM orders WHERE id=?', (data['order_id'],)).fetchone()
        if not order:
            conn.close()
            clear_state(user.id)
            bot.send_message(message.chat.id, 'الطلب غير موجود.')
            return
        conn.execute('UPDATE orders SET admin_note=?, updated_at=? WHERE id=?', (message.text, now_str(), data['order_id']))
        target = conn.execute('SELECT * FROM users WHERE id=?', (order['user_id'],)).fetchone()
        conn.commit()
        conn.close()
        clear_state(user.id)
        try:
            bot.send_message(target['tg_id'], f'💬 رد الإدارة على طلبك #{data["order_id"]}:\n{escape(message.text)}')
        except Exception:
            pass
        bot.send_message(message.chat.id, 'تم إرسال الرد.')
        return

    if st == 'user_note_order':
        conn = db()
        user_row = conn.execute('SELECT * FROM users WHERE tg_id=?', (user.id,)).fetchone()
        order = conn.execute('SELECT * FROM orders WHERE id=? AND user_id=?', (data['order_id'], user_row['id'])).fetchone()
        if not order:
            conn.close()
            clear_state(user.id)
            bot.send_message(message.chat.id, 'الطلب غير موجود.')
            return
        conn.execute('UPDATE orders SET user_note=?, updated_at=? WHERE id=?', (message.text, now_str(), data['order_id']))
        conn.execute('INSERT INTO tickets (user_id, kind, message, related_order_id, created_at) VALUES (?,?,?,?,?)', (user_row['id'], 'order_note', message.text, data['order_id'], now_str()))
        conn.commit()
        conn.close()
        clear_state(user.id)
        send_admin_alert(
            '📝 <b>ملاحظة جديدة على طلب</b>\n'
            f'رقم الطلب: <code>{data["order_id"]}</code>\n'
            f'من: {escape(user.full_name)} | <code>{user.id}</code>\n\n'
            f'{escape(message.text)}',
            reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton('فتح الطلب', callback_data=f'admin_order_open:{data["order_id"]}'))
        )
        bot.send_message(message.chat.id, 'تم إرسال ملاحظتك للإدارة.')
        return


def main():
    init_db()
    logger.info('Bot started...')
    bot.infinity_polling(skip_pending=True, timeout=60, long_polling_timeout=30)


if __name__ == '__main__':
    main()
