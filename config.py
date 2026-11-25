import os
from typing import Dict, List

BOT_CATALOG: List[Dict] = [
    {
        "id": "zenith",
        "name": "Zenith",
        "timeframe": "H12 / M15",
        "description": "H12 DEMA(60) trend flow + M15 Bollinger Band cross signals with adaptive trailing stop.",
        "lifetime_eur": 60,
        "monthly_eur": 15,
    },
    {
        "id": "twoshots",
        "name": "GOLDEN PULSE EA",
        "timeframe": "H4 Box / M5",
        "description": "00:00-04:00 H4 Range Box breakout & M5 retest with percentage SL/TP and 14-day drawdown guard.",
        "lifetime_eur": 30,
        "monthly_eur": 15,
    },
    {
        "id": "mox",
        "name": "MOX - EA",
        "timeframe": "M5",
        "description": "M5 support/resistance breakout with automatic x7 recovery trigger on stop-loss.",
        "lifetime_eur": 50,
        "monthly_eur": 15,
    },
    {
        "id": "momentum",
        "name": "Momentum EA",
        "timeframe": "M5",
        "description": "Billion Fortune Swing Stop breakout (M5 Swing High/Low, SL 750 / TP 900 pts) with trailing stop & 1% risk sizing.",
        "lifetime_eur": 20,
        "monthly_eur": 15,
    },
    {
        "id": "agnesa",
        "name": "Agnesa EA",
        "timeframe": "M5 / H1",
        "description": "ASQ Safe Scalping EA with M5 EMA(150/510), H1 MTF EMA(50/200), RSI(10) & ATR breakout management.",
        "lifetime_eur": 30,
        "monthly_eur": 15,
    },
    {
        "id": "multiversus",
        "name": "MULTI-VERSUS",
        "timeframe": "M5 (03:00)",
        "description": "High-precision 1 trade/day session strategy with built-in x15 recovery protection.",
        "lifetime_eur": 90,
        "monthly_eur": 15,
    },
    {
        "id": "albaniang",
        "name": "Albanian G",
        "timeframe": "M5",
        "description": "Billion Fortune Swing Stop breakout & dynamic trailing engine.",
        "lifetime_eur": 50,
        "monthly_eur": 15,
    },
]

BOT_BY_ID: Dict[str, Dict] = {b["id"]: b for b in BOT_CATALOG}
BOT_ORDER: List[str] = [b["id"] for b in BOT_CATALOG]

OFFICIAL_DOWNLOAD_URL = "https://drive.google.com/drive/folders/1sAqs1WWrm2C4nC31e8ylz6sRO2s8siVg?usp=sharing"
PLACEHOLDER_DOWNLOAD_URLS = {
    "",
    "https://aurumdesk.com/download-coming-soon",
    "https://aurumdesk.com",
    "https://drive.google.com/your-aurum-desk-zip-link",
}


def resolve_download_url(raw: str = "") -> str:
    cleaned = str(raw or "").strip()
    if cleaned in PLACEHOLDER_DOWNLOAD_URLS:
        return OFFICIAL_DOWNLOAD_URL
    return cleaned


BUNDLE_CATALOG: List[Dict] = [
    {
        "id": "starter_3",
        "name": "Starter 3-EA Pack",
        "tagline": "Random 3-EA mix on every click — best entry bundle",
        "pack_count": 3,
        "random_pick": True,
        "bot_ids": ["twoshots", "agnesa", "momentum"],
        "lifetime_eur": 65,
        "monthly_eur": 35,
    },
    {
        "id": "pro_4",
        "name": "Pro 4-EA Gold Pack",
        "tagline": "Random 4-EA power mix on every click — 16% bundle discount",
        "pack_count": 4,
        "random_pick": True,
        "bot_ids": ["zenith", "mox", "multiversus", "albaniang"],
        "lifetime_eur": 210,
        "monthly_eur": 49,
    },
    {
        "id": "full_7",
        "name": "Full 7-EA Aurum Suite",
        "tagline": "All 7 Gold EAs unlocked — maximum diversification",
        "pack_count": 7,
        "random_pick": False,
        "bot_ids": ["zenith", "twoshots", "mox", "momentum", "agnesa", "multiversus", "albaniang"],
        "lifetime_eur": 249,
        "monthly_eur": 75,
    },
]

BUNDLE_BY_ID: Dict[str, Dict] = {b["id"]: b for b in BUNDLE_CATALOG}

PLAN_MONTHLY_DAYS = 30


def calculate_order_price_eur(plan_type: str, bot_ids: List[str], bundle_id: str = "") -> int:
    plan = "monthly" if str(plan_type).lower() in ("monthly", "30_days", "30days") else "lifetime"
    clean_bots = [b for b in dict.fromkeys(bot_ids) if b in BOT_BY_ID]
    if not clean_bots and bundle_id in BUNDLE_BY_ID:
        clean_bots = list(BUNDLE_BY_ID[bundle_id]["bot_ids"])

    indiv_sum = sum(
        int(BOT_BY_ID[bid]["monthly_eur"] if plan == "monthly" else BOT_BY_ID[bid]["lifetime_eur"])
        for bid in clean_bots
    )

    for b in BUNDLE_CATALOG:
        pack_count = int(b.get("pack_count") or len(b["bot_ids"]))
        matches_bundle = (bundle_id == b["id"] and len(clean_bots) == pack_count)
        matches_default = (set(clean_bots) == set(b["bot_ids"]) and len(clean_bots) == len(b["bot_ids"]))
        if matches_bundle or matches_default:
            base_price = int(b["monthly_eur"] if plan == "monthly" else b["lifetime_eur"])
            if plan == "lifetime" and b["id"] == "pro_4" and indiv_sum < 245:
                return min(base_price, max(95, int(round(indiv_sum * 0.84))))
            return min(base_price, indiv_sum) if indiv_sum > 0 else base_price

    return indiv_sum


def get_env_config() -> Dict[str, str]:
    return {
        "PORT": os.environ.get("PORT", "8000"),
        "DATABASE_URL": os.environ.get("DATABASE_URL", "").strip(),
        "SQLITE_PATH": os.environ.get("SQLITE_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "aurum_server.db")),
        "ADMIN_PASSWORD": os.environ.get("ADMIN_PASSWORD", "AurumAdmin2026!").strip(),
        "SECRET_KEY": os.environ.get("SECRET_KEY", "dev-secret-change-in-production-aurum-2026").strip(),
        "PAYPAL_ME_URL": os.environ.get("PAYPAL_ME_URL", "https://paypal.me/AurmDesk").strip().rstrip("/"),
        "PUBLIC_CONTACT_EMAIL": os.environ.get("PUBLIC_CONTACT_EMAIL", "eliodemaj07@gmail.com").strip(),
        "SMTP_HOST": os.environ.get("SMTP_HOST", "smtp.gmail.com").strip(),
        "SMTP_PORT": os.environ.get("SMTP_PORT", "465").strip(),
        "SMTP_USER": os.environ.get("SMTP_USER", "eliodemaj07@gmail.com").strip(),
        "SMTP_PASS": os.environ.get("SMTP_PASS", "").strip(),
        "SMTP_FROM": os.environ.get("SMTP_FROM", "Aurum Desk <eliodemaj07@gmail.com>").strip(),
        "BREVO_API_KEY": os.environ.get("BREVO_API_KEY", "").strip(),
        "RESEND_API_KEY": os.environ.get("RESEND_API_KEY", "").strip(),
        "DEFAULT_DOWNLOAD_URL": resolve_download_url(os.environ.get("DEFAULT_DOWNLOAD_URL", "")),
        "APP_BASE_URL": os.environ.get("APP_BASE_URL", "").strip().rstrip("/"),
    }
