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
        "name": "TwoShotsInRow",
        "timeframe": "H4 / M5",
        "description": "H4 EMA(200) bias + M5 ATR(14) Silver Bullet entries with break-even & partial TP.",
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
        "description": "Tick volume & price ignition detector with 1:4 R:R ATR targeting.",
        "lifetime_eur": 20,
        "monthly_eur": 15,
    },
    {
        "id": "agnesa",
        "name": "Agnesa EA",
        "timeframe": "H4 / M30",
        "description": "Smart Money Concepts liquidity sweep + H4 EMA(50/100) trend alignment.",
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

BUNDLE_CATALOG: List[Dict] = [
    {
        "id": "starter_3",
        "name": "Starter 3-EA Pack",
        "tagline": "Best entry bundle for daily M5/M30 scalping",
        "bot_ids": ["twoshots", "agnesa", "momentum"],
        "lifetime_eur": 65,
        "monthly_eur": 35,
    },
    {
        "id": "pro_4",
        "name": "Pro 4-EA Gold Pack",
        "tagline": "Flagship breakout & recovery powerhouses",
        "bot_ids": ["zenith", "mox", "multiversus", "albaniang"],
        "lifetime_eur": 210,
        "monthly_eur": 49,
    },
    {
        "id": "full_7",
        "name": "Full 7-EA Aurum Suite",
        "tagline": "All 7 Gold EAs unlocked — maximum diversification",
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

    # Check exact bundle match first
    for b in BUNDLE_CATALOG:
        if (bundle_id == b["id"] or set(clean_bots) == set(b["bot_ids"])) and len(clean_bots) == len(b["bot_ids"]):
            return int(b["monthly_eur"] if plan == "monthly" else b["lifetime_eur"])

    total = 0
    for bid in clean_bots:
        item = BOT_BY_ID[bid]
        total += int(item["monthly_eur"] if plan == "monthly" else item["lifetime_eur"])
    return total


def get_env_config() -> Dict[str, str]:
    return {
        "PORT": os.environ.get("PORT", "8000"),
        "DATABASE_URL": os.environ.get("DATABASE_URL", "").strip(),
        "SQLITE_PATH": os.environ.get("SQLITE_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "aurum_server.db")),
        "ADMIN_PASSWORD": os.environ.get("ADMIN_PASSWORD", "AurumAdmin2026!").strip(),
        "SECRET_KEY": os.environ.get("SECRET_KEY", "dev-secret-change-in-production-aurum-2026").strip(),
        "PAYPAL_ME_URL": os.environ.get("PAYPAL_ME_URL", "https://paypal.me/AurumDesk").strip().rstrip("/"),
        "PUBLIC_CONTACT_EMAIL": os.environ.get("PUBLIC_CONTACT_EMAIL", "eliodemaj07@gmail.com").strip(),
        "SMTP_HOST": os.environ.get("SMTP_HOST", "smtp.gmail.com").strip(),
        "SMTP_PORT": os.environ.get("SMTP_PORT", "465").strip(),
        "SMTP_USER": os.environ.get("SMTP_USER", "eliodemaj07@gmail.com").strip(),
        "SMTP_PASS": os.environ.get("SMTP_PASS", "").strip(),
        "SMTP_FROM": os.environ.get("SMTP_FROM", "Aurum Desk <eliodemaj07@gmail.com>").strip(),
        "BREVO_API_KEY": os.environ.get("BREVO_API_KEY", "").strip(),
        "RESEND_API_KEY": os.environ.get("RESEND_API_KEY", "").strip(),
        "DEFAULT_DOWNLOAD_URL": os.environ.get("DEFAULT_DOWNLOAD_URL", "https://aurumdesk.com/download-coming-soon").strip(),
        "APP_BASE_URL": os.environ.get("APP_BASE_URL", "").strip().rstrip("/"),
    }
