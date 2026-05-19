"""
Cosmetics Price Bot — Telegram bot for searching cosmetics and calculating
the final customer price including delivery + storage + profit margin.

Pricing formula:
  base_cost    = product price (already includes ¥50 wholesale markup)
  delivery     = $6 ≈ ¥43  (China → Russia shipping per item)
  storage      = 7 days × $1/day = $7 ≈ ¥50
  overhead     = ¥93
  total_cost   = base_cost + overhead

  Profit tiers (based on base_cost):
    base < ¥200  → +¥60
    ¥200–¥500    → +¥120
    base ≥ ¥500  → +30% of total_cost

  customer_price = total_cost + profit
  customer_rub   = customer_price × CNY_TO_RUB rate
"""

import json, os, math, re, logging, unicodedata, html as html_mod
from urllib.parse import quote_plus
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CONTACT   = "@WongnoW225"

DELIVERY_CNY = 43
STORAGE_CNY  = 50
OVERHEAD     = DELIVERY_CNY + STORAGE_CNY   # ¥93

CNY_TO_RUB   = 12.0
ITEMS_PER_PAGE = 8

# ── Load data ─────────────────────────────────────────────────────────────
_dir = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(_dir, "products.json"), "r") as f:
    PRODUCTS = json.load(f)

# Brand images (logo/product thumbnails)
_bi_path = os.path.join(_dir, "brand_images.json")
if os.path.exists(_bi_path):
    with open(_bi_path, "r") as f:
        BRAND_IMAGES = json.load(f)
else:
    BRAND_IMAGES = {}

with open(os.path.join(_dir, "translations.json"), "r") as f:
    _tr = json.load(f)
    TD_EN = _tr["td_en"]    # Chinese term → English
    TD_RU = _tr["td_ru"]    # Chinese term → Russian
    TB    = _tr["tb"]        # Chinese brand → English brand
    TB_RU = _tr["tb_ru"]    # Chinese brand → Russian brand (overrides)

# Pre-sort term keys by length desc (longest match first)
_TK_EN = sorted(TD_EN.keys(), key=len, reverse=True)
_TK_RU = sorted(TD_RU.keys(), key=len, reverse=True)

# Build brand list
_brand_count: dict[str, int] = {}
for p in PRODUCTS:
    _brand_count[p["b"]] = _brand_count.get(p["b"], 0) + 1
BRANDS = sorted(_brand_count.items(), key=lambda x: -x[1])

# ── Synonym / fuzzy search maps ──────────────────────────────────────────
# Russian → Chinese cosmetics terms (so user can search in Russian)
RU_SEARCH_MAP = {
    "крем": "面霜", "маска": "面膜", "сыворотка": "精华液", "эссенция": "精华",
    "тонер": "水", "лосьон": "乳液", "помада": "口红", "бальзам": "唇膏",
    "тени": "眼影", "тушь": "睫毛膏", "пудра": "粉", "румяна": "腮红",
    "консилер": "遮瑕", "хайлайтер": "高光", "шампунь": "洗发水",
    "кондиционер": "护发素", "духи": "香水", "парфюм": "香水",
    "солнцезащитный": "防晒", "санскрин": "防晒", "spf": "防晒",
    "очищение": "洁面", "умывание": "洁面", "гель": "洁面",
    "патчи": "眼膜", "кушон": "气垫", "праймер": "隔离",
    "карандаш": "笔", "блеск": "唇蜜", "тинт": "唇釉",
    "скраб": "磨砂", "пилинг": "去角质", "ампула": "安瓶",
    "масло": "精华油", "спрей": "喷雾", "мист": "喷雾",
    "ночной": "晚", "дневной": "日", "увлажняющий": "保湿",
    "отбеливающий": "美白", "антивозрастной": "抗老",
    "глаза": "眼", "губы": "唇", "лицо": "面", "тело": "身体",
}
# English → Chinese cosmetics terms
EN_SEARCH_MAP = {
    "cream": "面霜", "mask": "面膜", "serum": "精华", "essence": "精华",
    "toner": "水", "lotion": "乳液", "lipstick": "口红", "lip balm": "唇膏",
    "eyeshadow": "眼影", "eye shadow": "眼影", "mascara": "睫毛膏",
    "powder": "粉", "blush": "腮红", "concealer": "遮瑕",
    "highlighter": "高光", "shampoo": "洗发水", "conditioner": "护发素",
    "perfume": "香水", "fragrance": "香水", "sunscreen": "防晒",
    "cleanser": "洁面", "face wash": "洁面", "cushion": "气垫",
    "primer": "隔离", "foundation": "粉底", "lip gloss": "唇蜜",
    "lip tint": "唇釉", "scrub": "磨砂", "spray": "喷雾",
    "moisturizer": "保湿", "moisturizing": "保湿", "whitening": "美白",
    "anti-aging": "抗老", "eye cream": "眼霜", "eye": "眼",
    "lip": "唇", "body": "身体", "night": "晚", "day": "日",
    "oil": "油", "ampoule": "安瓶",
}


# ── Translation engine ────────────────────────────────────────────────────
# Extra brand translations for brands not in the website's translation table
_EXTRA_BRANDS = {
    "高姿": "Gaoze",
    "红之": "Hongzhi",
    "柳丝木": "Liumusilk",
    "科蕴兰": "Keyunlan",
    "施巴": "Sebamed",
    "三资堂": "Sanzitang",
    "朱栈": "Zhuzhan",
    "苏酷": "Suku",
}


def tr_brand(brand_zh: str, lang: str) -> str:
    if lang == "zh":
        return brand_zh
    if lang == "ru" and brand_zh in TB_RU:
        return TB_RU[brand_zh]
    if brand_zh in TB:
        return TB[brand_zh]
    if brand_zh in _EXTRA_BRANDS:
        return _EXTRA_BRANDS[brand_zh]
    return brand_zh


def tr_name(name: str, lang: str) -> str:
    if lang == "zh":
        return name

    r = name
    td = TD_RU if lang == "ru" else TD_EN
    keys = _TK_RU if lang == "ru" else _TK_EN

    # Mark already-translated spans to avoid double-translation
    # Use \x00...\x01 markers around translated text
    def _mark(text):
        return "\x00" + text + "\x01"

    for zh, en in TB.items():
        if zh in r:
            rep = TB_RU.get(zh, en) if lang == "ru" else en
            r = r.replace(zh, _mark(rep) + " ")

    for k in keys:
        if k in r:
            r = r.replace(k, " " + _mark(td[k]) + " ")

    # Remove markers
    r = r.replace("\x00", "").replace("\x01", "")

    r = re.sub(r"\s{2,}", " ", r).strip()
    r = re.sub(r"([a-zA-ZÀ-ɏ])(\d)", r"\1 \2", r)
    r = re.sub(r"(\d)([a-zA-ZÀ-ɏ])", r"\1 \2", r)
    r = re.sub(r"([一-鿿])([A-Za-zÀ-ɏЀ-ӿ])", r"\1 \2", r)
    r = re.sub(r"([A-Za-zÀ-ɏЀ-ӿ])([一-鿿])", r"\1 \2", r)

    # Strip remaining CJK
    r = re.sub(r"[一-鿿㐀-䶿　-〿぀-ゟ゠-ヿ]+", " ", r)

    # Deduplicate consecutive identical words (e.g. "Cream Cream" → "Cream")
    words = re.sub(r"\s{2,}", " ", r).strip().split()
    deduped = []
    for w in words:
        if not deduped or w.lower() != deduped[-1].lower():
            deduped.append(w)
    return " ".join(deduped)


# ── i18n ──────────────────────────────────────────────────────────────────
L = {
    "ru": {
        "welcome": (
            "✨ <b>Янь's Beauty Shop</b> ✨\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🌸 <i>Лучшая косметика из Китая — прямо к вашей двери</i>\n\n"
            "🔹 6600+ товаров от 350 брендов\n"
            "🔹 Цена с доставкой до России\n"
            "🔹 Chanel, Dior, Lancôme, La Mer и др.\n\n"
            "Просто введите название — я найду товар "
            "и покажу финальную цену! 💰\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📩 Заказать: {CONTACT}\n"
            "👇 Выберите действие:"
        ),
        "no_results": (
            "😕 По запросу «{q}» ничего не найдено.\n\n"
            "💡 <b>Подсказки:</b>\n"
            "• Попробуйте короче: <code>Dior крем</code>\n"
            "• Бренд на английском: <code>Chanel</code>\n"
            "• Тип товара: <code>маска</code>, <code>крем</code>, <code>помада</code>\n"
            "• Или нажмите 🏷 Бренды"
        ),
        "found": "🔍 По запросу «<b>{q}</b>» найдено <b>{n}</b> товаров:",
        "price_line": "   💰 <b>¥{cny}</b>  (~<b>{rub} ₽</b>)",
        "page": "📄 Стр. {cur} из {total}",
        "ask_price": "   📞 Цена по запросу",
        "brands_title": "🏷 <b>Популярные бренды</b>\nНажмите, чтобы найти товары:",
        "lang_switched": "✅ Язык: Русский 🇷🇺",
        "prev": "⬅️ Назад",
        "next": "➡️ Далее",
        "pricing_note": (
            "\n<i>💡 Цена = товар + доставка + хранение</i>\n"
            f"📩 <b>Заказать:</b> {CONTACT}"
        ),
        "help": (
            "📖 <b>Как пользоваться:</b>\n\n"
            "🔍 Просто напишите что ищете:\n"
            "   • <code>Dior помада</code>\n"
            "   • <code>Chanel крем</code>\n"
            "   • <code>маска для лица</code>\n"
            "   • <code>lancome сыворотка</code>\n\n"
            "Я понимаю русский, английский и китайский!\n\n"
            "🏷 /brands — популярные бренды\n"
            "📊 /pricing — как формируется цена\n"
            "🔤 /lang — сменить язык\n\n"
            f"📩 <b>Заказать:</b> {CONTACT}"
        ),
        "pricing_info": (
            "📊 <b>Как формируется цена:</b>\n\n"
            "1️⃣ Оптовая цена товара\n"
            "2️⃣ + Доставка из Китая (~$6)\n"
            "3️⃣ + Хранение на складе (7 дней)\n"
            "4️⃣ + Комиссия сервиса\n\n"
            "= <b>Итоговая цена</b> в ¥ и ₽\n\n"
            "💱 Курс: 1 ¥ ≈ {rate} ₽\n\n"
            f"📩 <b>Заказать:</b> {CONTACT}"
        ),
        "menu_search": "🔍 Поиск товара",
        "menu_brands": "🏷 Бренды",
        "menu_pricing": "📊 О ценах",
        "menu_lang": "🔤 Язык / Language",
        "menu_help": "❓ Помощь",
        "menu_buy": "🛒 Заказать",
        "search_prompt": "🔍 Введите название товара или бренда.\n\nПримеры: <code>Dior крем</code>, <code>маска</code>, <code>Chanel</code>",
        "buy_msg": (
            "🛒 <b>Хотите заказать?</b>\n\n"
            "Напишите мне напрямую — я помогу с заказом!\n\n"
            f"📩 Telegram: <b>{CONTACT}</b>\n\n"
            "Отправьте название товара и я подготовлю заказ 💫"
        ),
    },
    "en": {
        "welcome": (
            "✨ <b>Yan's Beauty Shop</b> ✨\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🌸 <i>Premium cosmetics from China — delivered to your door</i>\n\n"
            "🔹 6600+ products from 350 brands\n"
            "🔹 Price includes delivery to Russia\n"
            "🔹 Chanel, Dior, Lancôme, La Mer & more\n\n"
            "Just type a product name — I'll find it\n"
            "and show you the final price! 💰\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📩 Order: {CONTACT}\n"
            "👇 Choose an action:"
        ),
        "no_results": (
            "😕 Nothing found for \"{q}\".\n\n"
            "💡 <b>Tips:</b>\n"
            "• Try shorter keywords: <code>Dior cream</code>\n"
            "• Brand in English: <code>Chanel</code>\n"
            "• Product type: <code>mask</code>, <code>serum</code>, <code>lipstick</code>\n"
            "• Or tap 🏷 Brands"
        ),
        "found": "🔍 Found <b>{n}</b> products for \"<b>{q}</b>\":",
        "price_line": "   💰 <b>¥{cny}</b>  (~<b>{rub} ₽</b>)",
        "page": "📄 Page {cur} of {total}",
        "ask_price": "   📞 Price on request",
        "brands_title": "🏷 <b>Popular Brands</b>\nTap to browse products:",
        "lang_switched": "✅ Language: English 🇬🇧",
        "prev": "⬅️ Prev",
        "next": "➡️ Next",
        "pricing_note": (
            "\n<i>💡 Price = product + delivery + storage</i>\n"
            f"📩 <b>Order:</b> {CONTACT}"
        ),
        "help": (
            "📖 <b>How to use:</b>\n\n"
            "🔍 Just type what you're looking for:\n"
            "   • <code>Dior lipstick</code>\n"
            "   • <code>Chanel cream</code>\n"
            "   • <code>face mask</code>\n"
            "   • <code>lancome serum</code>\n\n"
            "I understand English, Russian and Chinese!\n\n"
            "🏷 /brands — popular brands\n"
            "📊 /pricing — how prices work\n"
            "🔤 /lang — change language\n\n"
            f"📩 <b>Order:</b> {CONTACT}"
        ),
        "pricing_info": (
            "📊 <b>How pricing works:</b>\n\n"
            "1️⃣ Wholesale product price\n"
            "2️⃣ + Shipping from China (~$6)\n"
            "3️⃣ + Warehouse storage (7 days)\n"
            "4️⃣ + Service fee\n\n"
            "= <b>Final price</b> in ¥ and ₽\n\n"
            "💱 Rate: 1 ¥ ≈ {rate} ₽\n\n"
            f"📩 <b>Order:</b> {CONTACT}"
        ),
        "menu_search": "🔍 Search",
        "menu_brands": "🏷 Brands",
        "menu_pricing": "📊 Pricing",
        "menu_lang": "🔤 Language",
        "menu_help": "❓ Help",
        "menu_buy": "🛒 Order",
        "search_prompt": "🔍 Type a product name or brand.\n\nExamples: <code>Dior cream</code>, <code>mask</code>, <code>Chanel</code>",
        "buy_msg": (
            "🛒 <b>Want to order?</b>\n\n"
            "Message me directly — I'll help with your order!\n\n"
            f"📩 Telegram: <b>{CONTACT}</b>\n\n"
            "Send the product name and I'll arrange it 💫"
        ),
    },
    "zh": {
        "welcome": (
            "✨ <b>Янь的美妆小店</b> ✨\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🌸 <i>正品化妆品，中国直邮俄罗斯</i>\n\n"
            "🔹 6600+ 商品，350+ 品牌\n"
            "🔹 价格含运费和仓储\n"
            "🔹 香奈儿、迪奥、兰蔻、海蓝之谜等\n\n"
            "输入商品名称，即刻查询到手价！💰\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📩 下单联系：{CONTACT}\n"
            "👇 选择功能："
        ),
        "no_results": (
            "😕 未找到「{q}」相关商品。\n\n"
            "💡 <b>搜索技巧：</b>\n"
            "• 试试更短的关键词：<code>兰蔻 精华</code>\n"
            "• 品牌名：<code>香奈儿</code>、<code>Dior</code>\n"
            "• 产品类型：<code>面膜</code>、<code>口红</code>、<code>眼霜</code>\n"
            "• 或点击 🏷 品牌列表 浏览"
        ),
        "found": "🔍 搜索「<b>{q}</b>」找到 <b>{n}</b> 件商品：",
        "price_line": "   💰 <b>¥{cny}</b>  (~<b>{rub} ₽</b>)",
        "page": "📄 第 {cur}/{total} 页",
        "ask_price": "   📞 需询价",
        "brands_title": "🏷 <b>热门品牌</b>\n点击查看该品牌商品：",
        "lang_switched": "✅ 语言：中文 🇨🇳",
        "prev": "⬅️ 上一页",
        "next": "➡️ 下一页",
        "pricing_note": (
            "\n<i>💡 价格 = 商品 + 中俄运费 + 仓储</i>\n"
            f"📩 <b>下单：</b>{CONTACT}"
        ),
        "help": (
            "📖 <b>使用说明：</b>\n\n"
            "🔍 直接输入您要找的商品：\n"
            "   • <code>兰蔻 精华</code>\n"
            "   • <code>Dior 口红</code>\n"
            "   • <code>面膜</code>\n"
            "   • <code>玻色因</code>\n\n"
            "支持中文、英文搜索！\n\n"
            "🏷 /brands — 热门品牌\n"
            "📊 /pricing — 价格说明\n"
            "🔤 /lang — 切换语言\n\n"
            f"📩 <b>下单联系：</b>{CONTACT}"
        ),
        "pricing_info": (
            "📊 <b>价格构成说明：</b>\n\n"
            "1️⃣ 商品批发价\n"
            "2️⃣ + 中国到俄罗斯运费（约$6）\n"
            "3️⃣ + 仓储费（7天）\n"
            "4️⃣ + 服务佣金\n\n"
            "= <b>最终到手价</b>（¥ 和 ₽）\n\n"
            "💱 汇率：1 ¥ ≈ {rate} ₽\n\n"
            f"📩 <b>下单联系：</b>{CONTACT}"
        ),
        "menu_search": "🔍 搜索商品",
        "menu_brands": "🏷 品牌列表",
        "menu_pricing": "📊 价格说明",
        "menu_lang": "🔤 语言 / Язык",
        "menu_help": "❓ 帮助",
        "menu_buy": "🛒 下单",
        "search_prompt": "🔍 请输入商品名称或品牌名。\n\n例如：<code>兰蔻 精华</code>、<code>面膜</code>、<code>Dior</code>",
        "buy_msg": (
            "🛒 <b>想要下单？</b>\n\n"
            "请直接联系我，我会帮您处理订单！\n\n"
            f"📩 Telegram：<b>{CONTACT}</b>\n\n"
            "发送商品名称，我来为您安排 💫"
        ),
    },
}


def t(ctx: ContextTypes.DEFAULT_TYPE, key: str, **kwargs) -> str:
    lang = ctx.user_data.get("lang", "ru")
    template = L.get(lang, L["ru"]).get(key, key)
    return template.format(**kwargs) if kwargs else template


def get_lang(ctx: ContextTypes.DEFAULT_TYPE) -> str:
    return ctx.user_data.get("lang", "ru")


def get_menu_keyboard(ctx: ContextTypes.DEFAULT_TYPE) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(t(ctx, "menu_search")), KeyboardButton(t(ctx, "menu_brands"))],
            [KeyboardButton(t(ctx, "menu_buy")),    KeyboardButton(t(ctx, "menu_pricing"))],
            [KeyboardButton(t(ctx, "menu_lang")),   KeyboardButton(t(ctx, "menu_help"))],
        ],
        resize_keyboard=True,
    )


# ── Pricing ───────────────────────────────────────────────────────────────
def calc_customer_price(base_price: float) -> tuple[float, float]:
    total_cost = base_price + OVERHEAD
    if base_price < 200:
        profit = 60
    elif base_price < 500:
        profit = 120
    else:
        profit = total_cost * 0.30
    customer_cny = math.ceil(total_cost + profit)
    customer_rub = round(customer_cny * CNY_TO_RUB)
    return customer_cny, customer_rub


# ── Smart Search ──────────────────────────────────────────────────────────
def _norm(s: str) -> str:
    s = s.lower()
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c) and c not in "''")

# Build reverse brand maps for search
_EN_TO_ZH: dict[str, str] = {}
for _zh, _en in TB.items():
    _EN_TO_ZH[_norm(_en)] = _zh
for _zh, _ru in TB_RU.items():
    _EN_TO_ZH[_norm(_ru)] = _zh
for _zh, _en in _EXTRA_BRANDS.items():
    _EN_TO_ZH[_norm(_en)] = _zh


def _expand_word(w: str) -> dict:
    """
    Expand one word into a search hint.
    Returns {"type": "brand"|"term"|"raw", "zh": "...", "original": "..."}
    """
    w_low = w.lower()

    # Check Russian cosmetics terms
    for ru_term, zh_term in RU_SEARCH_MAP.items():
        if w_low == ru_term or (len(w_low) >= 4 and ru_term.startswith(w_low)):
            return {"type": "term", "zh": zh_term, "original": w}

    # Check English cosmetics terms
    for en_term, zh_term in EN_SEARCH_MAP.items():
        if w_low == en_term or w_low in en_term.split():
            return {"type": "term", "zh": zh_term, "original": w}

    # Check brand names
    w_norm = _norm(w)
    for en_norm, zh in _EN_TO_ZH.items():
        if w_norm in en_norm or en_norm.startswith(w_norm):
            return {"type": "brand", "zh": zh, "original": w}

    return {"type": "raw", "zh": w, "original": w}


def search_products(query: str) -> list[dict]:
    """
    Smart search: handles partial names, mixed languages, multi-word queries.
    E.g. "dior крем" → finds Dior brand + 面霜 in name
    E.g. "lancome mask" → finds 兰蔻 brand + 面膜 in name
    E.g. "маска" → finds all products with 面膜
    """
    raw = query.strip()
    if not raw:
        return []

    q_norm = _norm(raw)

    # ── Pass 1: Direct substring match ────────────────────────────────
    direct = []
    matched_zh_brands = set()
    for name_norm, zh in _EN_TO_ZH.items():
        if q_norm in name_norm:
            matched_zh_brands.add(zh)

    for p in PRODUCTS:
        if q_norm in _norm(p["n"]) or q_norm in _norm(p["b"]):
            direct.append(p)
        elif any(p["b"] == zh or zh in p["b"] for zh in matched_zh_brands):
            direct.append(p)

    if direct:
        return direct

    # ── Pass 2: Smart multi-word expansion ────────────────────────────
    words = raw.split()
    expanded = [_expand_word(w) for w in words]

    brand_filters = [e["zh"] for e in expanded if e["type"] == "brand"]
    term_filters  = [e["zh"] for e in expanded if e["type"] == "term"]
    raw_filters   = [e["zh"].lower() for e in expanded if e["type"] == "raw"]

    if brand_filters or term_filters:
        results = []
        for p in PRODUCTS:
            # Brand must match (if any brand words given)
            if brand_filters:
                brand_ok = any(p["b"] == bf or bf in p["b"] for bf in brand_filters)
            else:
                brand_ok = True

            # Term must appear in product name (if any term words given)
            if term_filters:
                term_ok = all(tf in p["n"].lower() for tf in term_filters)
            else:
                term_ok = True

            # Raw words must appear somewhere
            if raw_filters:
                combined = (p["n"] + " " + p["b"]).lower()
                raw_ok = all(rf in combined for rf in raw_filters)
            else:
                raw_ok = True

            if brand_ok and term_ok and raw_ok:
                results.append(p)

        if results:
            return results

        # Relax: try any brand + any term
        if brand_filters and term_filters:
            results = []
            for p in PRODUCTS:
                brand_ok = any(p["b"] == bf or bf in p["b"] for bf in brand_filters)
                term_ok = any(tf in p["n"].lower() for tf in term_filters)
                if brand_ok and term_ok:
                    results.append(p)
            if results:
                return results

        # Even more relaxed: just brand OR term
        results = []
        all_zh = [e["zh"].lower() for e in expanded]
        for p in PRODUCTS:
            combined = (p["n"] + " " + p["b"]).lower()
            if any(z in combined for z in all_zh):
                results.append(p)
        if results:
            return results

    # ── Pass 3: Raw word matching on normalized text ──────────────────
    norm_words = [_norm(w) for w in words if len(w) >= 2]
    if norm_words:
        results = []
        for p in PRODUCTS:
            combined = _norm(p["n"] + " " + p["b"])
            if all(w in combined for w in norm_words):
                results.append(p)
        if results:
            return results

        results = []
        for p in PRODUCTS:
            combined = _norm(p["n"] + " " + p["b"])
            if any(w in combined for w in norm_words):
                results.append(p)
        if results:
            return results

    return []


def get_brand_image(brand_zh: str):
    """Get brand logo image URL if available."""
    if brand_zh in BRAND_IMAGES:
        return BRAND_IMAGES[brand_zh]
    # Try partial match for mixed-name brands like "Dior迪奥"
    for key, url in BRAND_IMAGES.items():
        if brand_zh in key or key in brand_zh:
            return url
    return None


def get_product_image_url(product_name: str, brand_zh: str) -> str:
    """Generate a Google Images search URL for a product."""
    brand_en = TB.get(brand_zh, brand_zh)
    query = f"{brand_en} {product_name}"
    return f"https://www.google.com/search?tbm=isch&q={quote_plus(query)}"


def format_product(p: dict, idx: int, ctx: ContextTypes.DEFAULT_TYPE) -> str:
    lang = get_lang(ctx)
    name = html_mod.escape(tr_name(p["n"], lang))
    brand = html_mod.escape(tr_brand(p["b"], lang))

    if p["p"] < 0:
        price_str = t(ctx, "ask_price")
    else:
        cny, rub = calc_customer_price(p["p"])
        price_str = t(ctx, "price_line", cny=cny, rub=rub)

    return (
        f"<b>{idx}.</b> {name}\n"
        f"   🏷 {brand}\n"
        f"{price_str}"
    )


def format_product_detail(p: dict, ctx: ContextTypes.DEFAULT_TYPE) -> str:
    """Detailed product card with full info."""
    lang = get_lang(ctx)
    name = html_mod.escape(tr_name(p["n"], lang))
    brand = html_mod.escape(tr_brand(p["b"], lang))
    name_zh = html_mod.escape(p["n"])

    if p["p"] < 0:
        price_cny = t(ctx, "ask_price")
        price_rub = ""
    else:
        cny, rub = calc_customer_price(p["p"])
        price_cny = f"¥{cny}"
        price_rub = f"~{rub} ₽"

    labels = {
        "ru": {"name": "Товар", "brand": "Бренд", "orig": "Оригинал",
               "price": "Цена", "order": "Заказать"},
        "en": {"name": "Product", "brand": "Brand", "orig": "Original",
               "price": "Price", "order": "Order"},
        "zh": {"name": "商品", "brand": "品牌", "orig": "原名",
               "price": "价格", "order": "下单"},
    }
    lb = labels.get(lang, labels["ru"])

    lines = [
        f"🛍 <b>{name}</b>\n",
        f"🏷 <b>{lb['brand']}:</b> {brand}",
        f"📝 <b>{lb['orig']}:</b> {name_zh}",
    ]
    if p["p"] >= 0:
        lines.append(f"💰 <b>{lb['price']}:</b> {price_cny}  ({price_rub})")
    else:
        lines.append(f"💰 <b>{lb['price']}:</b> {price_cny}")
    lines.append(f"\n📩 <b>{lb['order']}:</b> {CONTACT}")

    return "\n".join(lines)


# ── Handlers ──────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if "lang" not in ctx.user_data:
        ctx.user_data["lang"] = "ru"
    await update.message.reply_text(
        t(ctx, "welcome"),
        parse_mode="HTML",
        reply_markup=get_menu_keyboard(ctx),
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        t(ctx, "help"), parse_mode="HTML",
        reply_markup=get_menu_keyboard(ctx),
    )


async def cmd_pricing(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        t(ctx, "pricing_info", rate=CNY_TO_RUB), parse_mode="HTML",
        reply_markup=get_menu_keyboard(ctx),
    )


async def cmd_lang(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    buttons = [
        [
            InlineKeyboardButton("🇷🇺 Русский", callback_data="lang:ru"),
            InlineKeyboardButton("🇬🇧 English", callback_data="lang:en"),
            InlineKeyboardButton("🇨🇳 中文", callback_data="lang:zh"),
        ]
    ]
    await update.message.reply_text(
        "🔤 Выберите язык / Language / 选择语言:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cmd_brands(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(ctx)
    top = BRANDS[:30]
    rows = []
    row = []
    for name_zh, count in top:
        label = tr_brand(name_zh, lang)
        short = label[:15] if len(label) > 15 else label
        row.append(InlineKeyboardButton(
            f"{short} ({count})", callback_data=f"brand:{name_zh}"
        ))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    await update.message.reply_text(
        t(ctx, "brands_title"),
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="HTML",
    )


async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("lang:"):
        lang = data.split(":")[1]
        ctx.user_data["lang"] = lang
        await query.edit_message_text(t(ctx, "lang_switched"), parse_mode="HTML")
        await query.message.reply_text(
            t(ctx, "welcome"),
            parse_mode="HTML",
            reply_markup=get_menu_keyboard(ctx),
        )

    elif data.startswith("brand:"):
        brand_zh = data.split(":", 1)[1]
        lang = get_lang(ctx)
        display_q = tr_brand(brand_zh, lang)
        ctx.user_data["last_query"] = display_q
        ctx.user_data["page"] = 0
        results = [p for p in PRODUCTS if p["b"] == brand_zh]
        ctx.user_data["results"] = results
        await send_results_page(query.message, ctx, display_q, edit=True)

    elif data.startswith("page:"):
        page = int(data.split(":")[1])
        ctx.user_data["page"] = page
        q = ctx.user_data.get("last_query", "")
        await send_results_page(query.message, ctx, q, edit=True)

    elif data.startswith("photo:"):
        # Show product detail with photo
        idx = int(data.split(":")[1])
        results = ctx.user_data.get("results", [])
        if 0 <= idx < len(results):
            p = results[idx]
            detail_text = format_product_detail(p, ctx)
            brand_img = get_brand_image(p["b"])
            lang = get_lang(ctx)

            # "Search photo" button
            search_url = get_product_image_url(p["n"], p["b"])
            search_label = {"ru": "🔍 Фото в Google",
                            "en": "🔍 Photo on Google",
                            "zh": "🔍 搜索图片"}.get(lang, "🔍 Photo")
            buy_label = {"ru": f"🛒 Заказать ({CONTACT})",
                         "en": f"🛒 Order ({CONTACT})",
                         "zh": f"🛒 下单 ({CONTACT})"}.get(lang, "🛒 Order")
            back_label = {"ru": "⬅️ Назад к списку",
                          "en": "⬅️ Back to list",
                          "zh": "⬅️ 返回列表"}.get(lang, "⬅️ Back")

            buttons = [
                [InlineKeyboardButton(search_label, url=search_url)],
                [InlineKeyboardButton(buy_label, url=f"https://t.me/{CONTACT[1:]}")],
                [InlineKeyboardButton(back_label, callback_data=f"page:{ctx.user_data.get('page', 0)}")],
            ]
            markup = InlineKeyboardMarkup(buttons)

            if brand_img:
                # Send brand image with product details as caption
                try:
                    await query.message.reply_photo(
                        photo=brand_img,
                        caption=detail_text,
                        parse_mode="HTML",
                        reply_markup=markup,
                    )
                except Exception:
                    # Fallback to text if image fails
                    await query.message.reply_text(
                        detail_text, parse_mode="HTML", reply_markup=markup,
                    )
            else:
                await query.message.reply_text(
                    detail_text, parse_mode="HTML", reply_markup=markup,
                )


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text or len(text) > 100:
        return

    # Check menu buttons across all languages (keyboard may lag behind lang switch)
    _menu_keys = ("menu_search", "menu_brands", "menu_pricing", "menu_lang",
                  "menu_help", "menu_buy")
    matched_key = None
    for _l in L:
        for key in _menu_keys:
            if text == L[_l][key]:
                matched_key = key
                break
        if matched_key:
            break

    if matched_key:
        if matched_key == "menu_search":
            await update.message.reply_text(
                t(ctx, "search_prompt"), parse_mode="HTML",
                reply_markup=get_menu_keyboard(ctx),
            )
        elif matched_key == "menu_brands":
            await cmd_brands(update, ctx)
        elif matched_key == "menu_pricing":
            await cmd_pricing(update, ctx)
        elif matched_key == "menu_lang":
            await cmd_lang(update, ctx)
        elif matched_key == "menu_help":
            await cmd_help(update, ctx)
        elif matched_key == "menu_buy":
            await update.message.reply_text(
                t(ctx, "buy_msg"), parse_mode="HTML",
                reply_markup=get_menu_keyboard(ctx),
            )
        return

    # Otherwise treat as search
    results = search_products(text)
    ctx.user_data["last_query"] = text
    ctx.user_data["results"] = results
    ctx.user_data["page"] = 0

    await send_results_page(update.message, ctx, text)


async def send_results_page(
    message, ctx: ContextTypes.DEFAULT_TYPE, query: str, edit: bool = False
):
    results = ctx.user_data.get("results", [])
    page = ctx.user_data.get("page", 0)
    total = len(results)

    if not results:
        text = t(ctx, "no_results", q=html_mod.escape(query))
        if edit:
            await message.edit_text(text, parse_mode="HTML")
        else:
            await message.reply_text(text, parse_mode="HTML",
                                     reply_markup=get_menu_keyboard(ctx))
        return

    total_pages = math.ceil(total / ITEMS_PER_PAGE)
    start = page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    page_items = results[start:end]

    lang = get_lang(ctx)
    photo_label = {"ru": "📷", "en": "📷", "zh": "📷"}.get(lang, "📷")

    lines = [t(ctx, "found", q=html_mod.escape(query), n=total), ""]
    for i, p in enumerate(page_items, start=start + 1):
        lines.append(format_product(p, i, ctx))
        lines.append("")

    lines.append(t(ctx, "page", cur=page + 1, total=total_pages))
    lines.append(t(ctx, "pricing_note"))

    # Product photo buttons (2 per row)
    keyboard_rows = []
    photo_row = []
    for i, p in enumerate(page_items, start=start):
        short_name = tr_name(p["n"], lang)[:20]
        photo_row.append(InlineKeyboardButton(
            f"{photo_label} {i+1}. {short_name}",
            callback_data=f"photo:{i}"
        ))
        if len(photo_row) == 2:
            keyboard_rows.append(photo_row)
            photo_row = []
    if photo_row:
        keyboard_rows.append(photo_row)

    # Navigation buttons
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(
            t(ctx, "prev"), callback_data=f"page:{page - 1}"
        ))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(
            t(ctx, "next"), callback_data=f"page:{page + 1}"
        ))
    if nav_buttons:
        keyboard_rows.append(nav_buttons)

    markup = InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None
    text = "\n".join(lines)

    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await message.reply_text(text, parse_mode="HTML", reply_markup=markup)


# ── Main ──────────────────────────────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        print("❌ Set BOT_TOKEN environment variable!")
        print("   export BOT_TOKEN='your-telegram-bot-token'")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("pricing", cmd_pricing))
    app.add_handler(CommandHandler("lang", cmd_lang))
    app.add_handler(CommandHandler("brands", cmd_brands))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🤖 Bot started! %d products, %d brands.", len(PRODUCTS), len(BRANDS))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
