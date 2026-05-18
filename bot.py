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

import json, os, math, logging, unicodedata
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# Cost parameters (easy to adjust)
DELIVERY_CNY = 43       # $6 shipping per item
STORAGE_CNY  = 50       # 7 days × $1/day
OVERHEAD     = DELIVERY_CNY + STORAGE_CNY   # ¥93 total

CNY_TO_RUB   = 12.0     # 1 CNY ≈ 12 RUB (adjust as needed)

ITEMS_PER_PAGE = 8       # results per page in search

# ── Load product data ─────────────────────────────────────────────────────
with open(os.path.join(os.path.dirname(__file__), "products.json"), "r") as f:
    PRODUCTS = json.load(f)

# Build brand list (sorted by count)
_brand_count: dict[str, int] = {}
for p in PRODUCTS:
    _brand_count[p["b"]] = _brand_count.get(p["b"], 0) + 1
BRANDS = sorted(_brand_count.items(), key=lambda x: -x[1])

# ── Translation dict (brand zh→en for display) ───────────────────────────
TB = {
    "欧莱雅":"L'Oréal","珀莱雅":"Proya","香奈儿":"Chanel","兰蔻":"Lancôme",
    "雅诗兰黛":"Estée Lauder","海蓝之谜":"La Mer","毛戈平":"Mao Geping",
    "卡诗":"Kérastase","修丽可":"SkinCeuticals","橘朵":"Judydoll",
    "阿玛尼":"Armani","玉兰油OLAY":"OLAY","彩棠":"Timage","理肤泉":"La Roche-Posay",
    "古驰":"Gucci","TF汤姆福特":"Tom Ford","娇韵诗":"Clarins","Whoo后":"Whoo",
    "爱马仕":"Hermès","HR赫莲娜":"Helena Rubinstein","资生堂":"Shiseido",
    "科颜氏":"Kiehl's","宝格丽":"Bvlgari","娇兰":"Guerlain",
    "CPB肌肤之钥":"Clé de Peau","巴宝莉":"Burberry","莱珀妮":"La Prairie",
    "纪梵希":"Givenchy","韩束":"Kans","欧舒丹":"L'Occitane","植村秀":"Shu Uemura",
    "祖马龙":"Jo Malone","馥蕾诗":"Fresh","百瑞德":"Byredo","雅漾":"Avène",
    "芭比布朗":"Bobbi Brown","倩碧":"Clinique","玫珂菲":"Make Up For Ever",
    "奥尔滨":"Albion","雪花秀":"Sulwhasoo","薇姿":"Vichy","碧欧泉":"Biotherm",
    "希思黎":"Sisley","丝芙兰":"Sephora","美宝莲":"Maybelline","兰芝":"Laneige",
    "YSL圣罗兰":"YSL","Dior迪奥":"Dior","3CE":"3CE","MAC":"MAC","SK-II":"SK-II",
    "NARS":"NARS","HBN":"HBN","花知晓":"Flower Knows","FanBeauty范冰冰":"FanBeauty",
}

# ── i18n ──────────────────────────────────────────────────────────────────
L = {
    "ru": {
        "welcome": (
            "🎀 <b>Каталог косметики</b>\n\n"
            "Отправьте мне <b>название бренда или товара</b> — "
            "я найду его и покажу цену с доставкой в Россию.\n\n"
            "Примеры: <code>Chanel</code>, <code>兰蔻</code>, <code>Dior</code>, "
            "<code>маска</code>, <code>крем</code>\n\n"
            "📦 В цену уже включена доставка и хранение.\n\n"
            "🔤 /lang — сменить язык\n"
            "🏷 /brands — популярные бренды"
        ),
        "no_results": "😕 Ничего не найдено по запросу «{q}». Попробуйте другой запрос.",
        "found": "🔍 Найдено <b>{n}</b> товаров по запросу «<b>{q}</b>»:",
        "price_line": (
            "   💰 <b>¥{cny}</b>  (~<b>{rub} ₽</b>)"
        ),
        "page": "Стр. {cur}/{total}",
        "ask_price": "📞 Цена по запросу",
        "brands_title": "🏷 <b>Популярные бренды</b> (нажмите для поиска):",
        "lang_switched": "✅ Язык переключён на русский.",
        "prev": "⬅️ Назад",
        "next": "➡️ Далее",
        "pricing_note": (
            "\n\n<i>💡 Цена включает: товар + доставка Китай→Россия + хранение</i>"
        ),
        "help": (
            "🔍 Просто отправьте название товара или бренда.\n"
            "🏷 /brands — посмотреть популярные бренды\n"
            "🔤 /lang — сменить язык\n"
            "ℹ️ /pricing — как формируется цена"
        ),
        "pricing_info": (
            "📊 <b>Как формируется цена:</b>\n\n"
            "• Оптовая цена товара\n"
            "• + Доставка из Китая (~$6)\n"
            "• + Хранение на складе (7 дней)\n"
            "• + Комиссия сервиса\n\n"
            "= Итоговая цена для вас в ¥ и ₽\n\n"
            "Курс: 1 ¥ ≈ {rate} ₽"
        ),
    },
    "zh": {
        "welcome": (
            "🎀 <b>化妆品报价查询机器人</b>\n\n"
            "发送<b>品牌名称或商品名称</b>，"
            "我会帮你查找商品并计算含运费的最终价格。\n\n"
            "示例：<code>兰蔻</code>、<code>Dior</code>、<code>面膜</code>、"
            "<code>精华</code>\n\n"
            "📦 价格已包含中国到俄罗斯运费和仓储费。\n\n"
            "🔤 /lang — 切换语言\n"
            "🏷 /brands — 热门品牌"
        ),
        "no_results": "😕 未找到「{q}」相关商品，请换个关键词试试。",
        "found": "🔍 搜索「<b>{q}</b>」找到 <b>{n}</b> 件商品：",
        "price_line": (
            "   💰 <b>¥{cny}</b>  (~<b>{rub} ₽</b>)"
        ),
        "page": "第 {cur}/{total} 页",
        "ask_price": "📞 需询价",
        "brands_title": "🏷 <b>热门品牌</b>（点击搜索）：",
        "lang_switched": "✅ 已切换为中文。",
        "prev": "⬅️ 上一页",
        "next": "➡️ 下一页",
        "pricing_note": (
            "\n\n<i>💡 价格包含：商品 + 中俄运费 + 仓储费</i>"
        ),
        "help": (
            "🔍 直接发送商品名或品牌名即可搜索\n"
            "🏷 /brands — 查看热门品牌\n"
            "🔤 /lang — 切换语言\n"
            "ℹ️ /pricing — 价格说明"
        ),
        "pricing_info": (
            "📊 <b>价格构成说明：</b>\n\n"
            "• 商品批发价\n"
            "• + 中国到俄罗斯运费（约$6）\n"
            "• + 仓储费（7天）\n"
            "• + 服务佣金\n\n"
            "= 最终到手价（¥ 和 ₽）\n\n"
            "汇率：1 ¥ ≈ {rate} ₽"
        ),
    },
}


def t(ctx: ContextTypes.DEFAULT_TYPE, key: str, **kwargs) -> str:
    """Get translated string for user's language."""
    lang = ctx.user_data.get("lang", "ru")
    template = L.get(lang, L["ru"]).get(key, key)
    return template.format(**kwargs) if kwargs else template


# ── Pricing logic ─────────────────────────────────────────────────────────
def calc_customer_price(base_price: float) -> tuple[float, float]:
    """
    Returns (customer_cny, customer_rub).
    base_price is the product price from the database (already +¥50 markup).
    """
    total_cost = base_price + OVERHEAD  # product + delivery + storage

    if base_price < 200:
        profit = 60
    elif base_price < 500:
        profit = 120
    else:
        profit = total_cost * 0.30

    customer_cny = math.ceil(total_cost + profit)  # round up to whole yuan
    customer_rub = round(customer_cny * CNY_TO_RUB)
    return customer_cny, customer_rub


# ── Search logic ──────────────────────────────────────────────────────────
def _norm(s: str) -> str:
    """Normalize text: lowercase, strip accents (é→e, ô→o), for fuzzy matching."""
    s = s.lower()
    # Decompose unicode, strip combining marks (accents) and apostrophes
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c) and c not in "''")

# Pre-build reverse English→Chinese brand map (normalized)
_EN_TO_ZH: dict[str, str] = {}
for _zh, _en in TB.items():
    _EN_TO_ZH[_norm(_en)] = _zh

def search_products(query: str) -> list[dict]:
    """Search products by name or brand (case-insensitive, accent-insensitive)."""
    q = _norm(query.strip())
    if not q:
        return []

    # First check if query matches an English brand name → get Chinese brand
    matched_zh_brands = set()
    for en_norm, zh in _EN_TO_ZH.items():
        if q in en_norm:
            matched_zh_brands.add(zh)

    results = []
    for p in PRODUCTS:
        if q in _norm(p["n"]) or q in _norm(p["b"]):
            results.append(p)
        elif any(p["b"] == zh or zh in p["b"] for zh in matched_zh_brands):
            results.append(p)

    return results


def format_product(p: dict, idx: int, ctx: ContextTypes.DEFAULT_TYPE) -> str:
    """Format a single product as a text block."""
    brand = TB.get(p["b"], p["b"])

    if p["p"] < 0:
        price_str = t(ctx, "ask_price")
    else:
        cny, rub = calc_customer_price(p["p"])
        price_str = t(ctx, "price_line", cny=cny, rub=rub)

    return (
        f"<b>{idx}.</b> {p['n']}\n"
        f"   🏷 {brand}\n"
        f"{price_str}"
    )


# ── Handlers ──────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if "lang" not in ctx.user_data:
        ctx.user_data["lang"] = "ru"
    await update.message.reply_text(
        t(ctx, "welcome"), parse_mode="HTML"
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(t(ctx, "help"), parse_mode="HTML")


async def cmd_pricing(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        t(ctx, "pricing_info", rate=CNY_TO_RUB), parse_mode="HTML"
    )


async def cmd_lang(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    buttons = [
        [
            InlineKeyboardButton("🇷🇺 Русский", callback_data="lang:ru"),
            InlineKeyboardButton("🇨🇳 中文", callback_data="lang:zh"),
        ]
    ]
    await update.message.reply_text(
        "🔤 Choose language / 选择语言:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cmd_brands(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Show top 30 brands as inline buttons
    top = BRANDS[:30]
    rows = []
    row = []
    for name, count in top:
        label = TB.get(name, name)
        short = label[:15] if len(label) > 15 else label
        row.append(InlineKeyboardButton(
            f"{short} ({count})", callback_data=f"brand:{name}"
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

    elif data.startswith("brand:"):
        brand = data.split(":", 1)[1]
        ctx.user_data["last_query"] = brand
        ctx.user_data["page"] = 0
        results = [p for p in PRODUCTS if p["b"] == brand]
        ctx.user_data["results"] = results
        await send_results_page(query.message, ctx, brand, edit=True)

    elif data.startswith("page:"):
        page = int(data.split(":")[1])
        ctx.user_data["page"] = page
        q = ctx.user_data.get("last_query", "")
        await send_results_page(query.message, ctx, q, edit=True)


async def handle_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle any text message as a search query."""
    q = update.message.text.strip()
    if not q or len(q) > 100:
        return

    results = search_products(q)
    ctx.user_data["last_query"] = q
    ctx.user_data["results"] = results
    ctx.user_data["page"] = 0

    await send_results_page(update.message, ctx, q)


async def send_results_page(
    message, ctx: ContextTypes.DEFAULT_TYPE, query: str, edit: bool = False
):
    """Send a page of search results with pagination."""
    results = ctx.user_data.get("results", [])
    page = ctx.user_data.get("page", 0)
    total = len(results)

    if not results:
        text = t(ctx, "no_results", q=query)
        if edit:
            await message.edit_text(text, parse_mode="HTML")
        else:
            await message.reply_text(text, parse_mode="HTML")
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

    # Pagination buttons
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search))

    logger.info("🤖 Bot started! Loaded %d products, %d brands.", len(PRODUCTS), len(BRANDS))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
