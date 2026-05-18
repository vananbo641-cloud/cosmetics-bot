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

import json, os, math, re, logging, unicodedata
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

DELIVERY_CNY = 43
STORAGE_CNY  = 50
OVERHEAD     = DELIVERY_CNY + STORAGE_CNY   # ¥93

CNY_TO_RUB   = 12.0
ITEMS_PER_PAGE = 8

# ── Load data ─────────────────────────────────────────────────────────────
_dir = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(_dir, "products.json"), "r") as f:
    PRODUCTS = json.load(f)

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


# ── Translation engine (same logic as the website) ────────────────────────
def tr_brand(brand_zh: str, lang: str) -> str:
    """Translate a Chinese brand name to the target language."""
    if lang == "zh":
        return brand_zh
    if lang == "ru" and brand_zh in TB_RU:
        return TB_RU[brand_zh]
    if brand_zh in TB:
        return TB[brand_zh]
    return brand_zh


def tr_name(name: str, lang: str) -> str:
    """Translate a Chinese product name to the target language."""
    if lang == "zh":
        return name

    r = name
    td = TD_RU if lang == "ru" else TD_EN
    keys = _TK_RU if lang == "ru" else _TK_EN

    # Step 1: Translate brand names in the text
    for zh, en in TB.items():
        if zh in r:
            rep = TB_RU.get(zh, en) if lang == "ru" else en
            r = r.replace(zh, rep + " ")

    # Step 2: Translate cosmetics terms (longest match first)
    for k in keys:
        if k in r:
            r = r.replace(k, " " + td[k] + " ")

    # Step 3: Clean up spacing
    r = re.sub(r"\s{2,}", " ", r).strip()
    # Add space between Latin/CJK boundaries
    r = re.sub(r"([a-zA-ZÀ-ɏ])(\d)", r"\1 \2", r)
    r = re.sub(r"(\d)([a-zA-ZÀ-ɏ])", r"\1 \2", r)
    r = re.sub(r"([一-鿿])([A-Za-zÀ-ɏЀ-ӿ])", r"\1 \2", r)
    r = re.sub(r"([A-Za-zÀ-ɏЀ-ӿ])([一-鿿])", r"\1 \2", r)

    # Step 4: Strip any remaining CJK characters
    r = re.sub(r"[一-鿿㐀-䶿　-〿぀-ゟ゠-ヿ]+", " ", r)
    r = re.sub(r"\s{2,}", " ", r).strip()

    return r


# ── i18n ──────────────────────────────────────────────────────────────────
L = {
    "ru": {
        "welcome": (
            "🎀 <b>Каталог косметики — Янь</b>\n\n"
            "Я помогу найти любую косметику и рассчитаю цену с доставкой в Россию!\n\n"
            "📦 В цену включено:\n"
            "• Товар\n"
            "• Доставка из Китая\n"
            "• Хранение на складе\n\n"
            "👇 <b>Выберите действие или введите название товара:</b>"
        ),
        "no_results": "😕 По запросу «{q}» ничего не найдено.\nПопробуйте другое название.",
        "found": "🔍 По запросу «<b>{q}</b>» найдено <b>{n}</b> товаров:",
        "price_line": "   💰 <b>¥{cny}</b>  (~<b>{rub} ₽</b>)",
        "page": "📄 Стр. {cur} из {total}",
        "ask_price": "   📞 Цена по запросу",
        "brands_title": "🏷 <b>Популярные бренды</b>\nНажмите, чтобы найти товары:",
        "lang_switched": "✅ Язык: Русский 🇷🇺",
        "prev": "⬅️ Назад",
        "next": "➡️ Далее",
        "pricing_note": "\n<i>💡 Цена = товар + доставка Китай→Россия + хранение</i>",
        "help": (
            "🔍 Введите название товара или бренда\n"
            "🏷 /brands — популярные бренды\n"
            "🔤 /lang — сменить язык\n"
            "📊 /pricing — как формируется цена"
        ),
        "pricing_info": (
            "📊 <b>Как формируется цена:</b>\n\n"
            "1️⃣ Оптовая цена товара\n"
            "2️⃣ + Доставка из Китая (~$6)\n"
            "3️⃣ + Хранение на складе (7 дней)\n"
            "4️⃣ + Комиссия сервиса\n\n"
            "= <b>Итоговая цена</b> в ¥ и ₽\n\n"
            "Курс: 1 ¥ ≈ {rate} ₽"
        ),
        # Menu buttons
        "menu_search": "🔍 Поиск товара",
        "menu_brands": "🏷 Бренды",
        "menu_pricing": "📊 О ценах",
        "menu_lang": "🔤 Язык / Language",
        "menu_help": "❓ Помощь",
        "search_prompt": "🔍 Введите название товара или бренда:",
    },
    "zh": {
        "welcome": (
            "🎀 <b>化妆品报价查询 — Янь的店</b>\n\n"
            "帮您查找化妆品并计算含运费的最终到手价！\n\n"
            "📦 价格已包含：\n"
            "• 商品费\n"
            "• 中国到俄罗斯运费\n"
            "• 仓储费\n\n"
            "👇 <b>选择功能或直接输入商品名称：</b>"
        ),
        "no_results": "😕 未找到「{q}」相关商品。\n请换个关键词试试。",
        "found": "🔍 搜索「<b>{q}</b>」找到 <b>{n}</b> 件商品：",
        "price_line": "   💰 <b>¥{cny}</b>  (~<b>{rub} ₽</b>)",
        "page": "📄 第 {cur}/{total} 页",
        "ask_price": "   📞 需询价",
        "brands_title": "🏷 <b>热门品牌</b>\n点击查看该品牌商品：",
        "lang_switched": "✅ 语言：中文 🇨🇳",
        "prev": "⬅️ 上一页",
        "next": "➡️ 下一页",
        "pricing_note": "\n<i>💡 价格 = 商品 + 中俄运费 + 仓储费</i>",
        "help": (
            "🔍 直接发送商品名或品牌名搜索\n"
            "🏷 /brands — 查看热门品牌\n"
            "🔤 /lang — 切换语言\n"
            "📊 /pricing — 价格说明"
        ),
        "pricing_info": (
            "📊 <b>价格构成说明：</b>\n\n"
            "1️⃣ 商品批发价\n"
            "2️⃣ + 中国到俄罗斯运费（约$6）\n"
            "3️⃣ + 仓储费（7天）\n"
            "4️⃣ + 服务佣金\n\n"
            "= <b>最终到手价</b>（¥ 和 ₽）\n\n"
            "汇率：1 ¥ ≈ {rate} ₽"
        ),
        # Menu buttons
        "menu_search": "🔍 搜索商品",
        "menu_brands": "🏷 品牌列表",
        "menu_pricing": "📊 价格说明",
        "menu_lang": "🔤 语言 / Язык",
        "menu_help": "❓ 帮助",
        "search_prompt": "🔍 请输入商品名称或品牌名：",
    },
}


def t(ctx: ContextTypes.DEFAULT_TYPE, key: str, **kwargs) -> str:
    lang = ctx.user_data.get("lang", "ru")
    template = L.get(lang, L["ru"]).get(key, key)
    return template.format(**kwargs) if kwargs else template


def get_lang(ctx: ContextTypes.DEFAULT_TYPE) -> str:
    return ctx.user_data.get("lang", "ru")


def get_menu_keyboard(ctx: ContextTypes.DEFAULT_TYPE) -> ReplyKeyboardMarkup:
    """Build the persistent bottom menu keyboard."""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(t(ctx, "menu_search")), KeyboardButton(t(ctx, "menu_brands"))],
            [KeyboardButton(t(ctx, "menu_pricing")), KeyboardButton(t(ctx, "menu_lang"))],
            [KeyboardButton(t(ctx, "menu_help"))],
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


# ── Search ────────────────────────────────────────────────────────────────
def _norm(s: str) -> str:
    s = s.lower()
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c) and c not in "''")

# Build reverse English→Chinese brand map
_EN_TO_ZH: dict[str, str] = {}
for _zh, _en in TB.items():
    _EN_TO_ZH[_norm(_en)] = _zh

# Also index Russian brand names
for _zh, _ru in TB_RU.items():
    _EN_TO_ZH[_norm(_ru)] = _zh


def search_products(query: str) -> list[dict]:
    q = _norm(query.strip())
    if not q:
        return []

    matched_zh_brands = set()
    for name_norm, zh in _EN_TO_ZH.items():
        if q in name_norm:
            matched_zh_brands.add(zh)

    results = []
    for p in PRODUCTS:
        if q in _norm(p["n"]) or q in _norm(p["b"]):
            results.append(p)
        elif any(p["b"] == zh or zh in p["b"] for zh in matched_zh_brands):
            results.append(p)

    return results


def format_product(p: dict, idx: int, ctx: ContextTypes.DEFAULT_TYPE) -> str:
    """Format a product — fully translated based on user language."""
    lang = get_lang(ctx)
    name = tr_name(p["n"], lang)
    brand = tr_brand(p["b"], lang)

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
            InlineKeyboardButton("🇨🇳 中文", callback_data="lang:zh"),
        ]
    ]
    await update.message.reply_text(
        "🔤 Выберите язык / 选择语言:",
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
        # Send language confirmation + refresh menu
        await query.edit_message_text(t(ctx, "lang_switched"), parse_mode="HTML")
        # Send a new message with updated menu
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


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle text messages — either menu buttons or search queries."""
    text = update.message.text.strip()
    if not text or len(text) > 100:
        return

    # Check if it's a menu button press
    lang = get_lang(ctx)
    menu_search = L[lang]["menu_search"]
    menu_brands = L[lang]["menu_brands"]
    menu_pricing = L[lang]["menu_pricing"]
    menu_lang = L[lang]["menu_lang"]
    menu_help = L[lang]["menu_help"]

    if text == menu_search:
        await update.message.reply_text(
            t(ctx, "search_prompt"), parse_mode="HTML",
            reply_markup=get_menu_keyboard(ctx),
        )
        return
    elif text == menu_brands:
        await cmd_brands(update, ctx)
        return
    elif text == menu_pricing:
        await cmd_pricing(update, ctx)
        return
    elif text == menu_lang:
        await cmd_lang(update, ctx)
        return
    elif text == menu_help:
        await cmd_help(update, ctx)
        return

    # Otherwise treat as search
    results = search_products(text)
    lang = get_lang(ctx)
    # If user typed an English/Russian brand, show that as query display
    display_q = text
    ctx.user_data["last_query"] = display_q
    ctx.user_data["results"] = results
    ctx.user_data["page"] = 0

    await send_results_page(update.message, ctx, display_q)


async def send_results_page(
    message, ctx: ContextTypes.DEFAULT_TYPE, query: str, edit: bool = False
):
    results = ctx.user_data.get("results", [])
    page = ctx.user_data.get("page", 0)
    total = len(results)

    if not results:
        text = t(ctx, "no_results", q=query)
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

    lines = [t(ctx, "found", q=query, n=total), ""]
    for i, p in enumerate(page_items, start=start + 1):
        lines.append(format_product(p, i, ctx))
        lines.append("")

    lines.append(t(ctx, "page", cur=page + 1, total=total_pages))
    lines.append(t(ctx, "pricing_note"))

    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton(
            t(ctx, "prev"), callback_data=f"page:{page - 1}"
        ))
    if page < total_pages - 1:
        buttons.append(InlineKeyboardButton(
            t(ctx, "next"), callback_data=f"page:{page + 1}"
        ))

    markup = InlineKeyboardMarkup([buttons]) if buttons else None
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

    logger.info("🤖 Bot started! %d products, %d brands, %d EN terms, %d RU terms.",
                len(PRODUCTS), len(BRANDS), len(TD_EN), len(TD_RU))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
