"""
GGWALL.NET — Giveaway Auto-Claim Bot
- Persistent data (Railway Volume /app/data)
- Analytics (claims, win rate, timing)
- Auto-detect new channels (sends link to you)
- Edit message detection
"""
import os
import asyncio
import copy
import json
import time
import re
import random
import logging
from pathlib import Path
from aiohttp import web
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession

logging.basicConfig(format='%(asctime)s [%(levelname)s] %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)
# Σβήσε τα noisy logs (γεμίζουν το Railway)
logging.getLogger('aiohttp.access').setLevel(logging.WARNING)
logging.getLogger('telethon').setLevel(logging.WARNING)

# ============ CREDENTIALS ============
API_ID = int(os.getenv('API_ID', '0'))
API_HASH = os.getenv('API_HASH', '')
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
SESSION_STRING = os.getenv('SESSION_STRING', '')
# Owner lock: μόνο αυτό το user ID μπορεί να χρησιμοποιήσει το bot
# Αν είναι κενό, ο πρώτος που κάνει /start γίνεται owner (μία φορά)
AUTHORIZED_USER_IDS = [int(x.strip()) for x in os.getenv('AUTHORIZED_USER_ID', '0').split(',') if x.strip().isdigit()]

if not API_ID or not API_HASH or not BOT_TOKEN:
    logger.error("Missing credentials!")
    exit(1)

# ============ DATA DIRECTORY (Railway Volume) ============
DATA_DIR = Path("/app/data")
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logger.info(f"Using persistent volume: {DATA_DIR}")
except Exception:
    DATA_DIR = Path(".")
    logger.info("Volume not available, using local dir")

SETTINGS_FILE = DATA_DIR / "settings.json"
ALERTS_FILE = DATA_DIR / "alerts.json"
STATS_FILE = DATA_DIR / "stats.json"

# ============ DEFAULTS ============
DEFAULT_SETTINGS = {
    "keywords": ["giving", "away"],
    "channels": ["atombuilderscommunity", "atomOGchat", "GGWALLMSG"],
    "auto_click": True,
    "buttons_only": True,
    "click_words": ["claim", "join"],
    "owner_id": None,
    "smart_delay": True,       # human jitter + capacity safety
    "jitter_min": 8.0,         # ελάχιστο random delay πριν το claim
    "jitter_max": 15.0,        # μέγιστο random delay
    "capacity_threshold": 70,  # % πληρότητας → πάτα ΤΩΡΑ (ασφάλεια)
    "sleep_enabled": False,    # ώρες ύπνου on/off
    "sleep_start": 3,          # ώρα έναρξης ύπνου (0-23)
    "sleep_end": 7             # ώρα λήξης ύπνου (0-23)
}

DEFAULT_STATS = {
    "total_alerts": 0,
    "total_clicks": 0,
    "successful_clicks": 0,
    "failed_clicks": 0,
    "fastest_click": None,
    "tokens": {},          # tokens βάσει giveaway message (estimate)
    "confirmed_tokens": {},# tokens από giveaways (επιβεβαιωμένα)
    "game_tokens": {},     # tokens από minigames
    "tip_tokens": {},      # tokens από tips άλλων χρηστών
    "real_claims": 0,      # επιβεβαιωμένες επιτυχίες giveaway
    "real_failed": 0,      # επιβεβαιωμένες αποτυχίες giveaway
    "game_wins": 0,        # πόσα games κέρδισα
    "tips_received": 0,    # πόσα tips έλαβα
    "withdrawals": {},     # tokens που έκανα withdraw {"ATOM": 50.0}
    "daily": {}  # {"2026-07-27": {"alerts": 5, "claims": 4, "tokens": {"ATOM": 1.2}}}
}

# ============ CLIENTS ============
if SESSION_STRING:
    user_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    logger.info("Using StringSession")
else:
    user_client = TelegramClient('session_user', API_ID, API_HASH)

bot_client = TelegramClient('session_bot', API_ID, API_HASH)

owner_id = None
my_channels = []
seen_messages = set()
START_TIME = time.time()

# ============ SETTINGS ============
def load_settings():
    try:
        if SETTINGS_FILE.exists():
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                s = json.load(f)
                for k, v in DEFAULT_SETTINGS.items():
                    s.setdefault(k, copy.deepcopy(v))
                return s
    except Exception as e:
        logger.error(f"Settings load: {e}")
    return copy.deepcopy(DEFAULT_SETTINGS)

def save_settings(s):
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Settings save: {e}")

# ============ STATS ============
def load_stats():
    try:
        if STATS_FILE.exists():
            with open(STATS_FILE, 'r', encoding='utf-8') as f:
                s = json.load(f)
                for k, v in DEFAULT_STATS.items():
                    s.setdefault(k, copy.deepcopy(v))
                return s
    except Exception:
        pass
    return copy.deepcopy(DEFAULT_STATS)

def save_stats(s):
    try:
        with open(STATS_FILE, 'w', encoding='utf-8') as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Stats save: {e}")

def bump_daily(kind, tok_sym=None, tok_amt=None):
    """Ενημερώνει τον daily counter για σήμερα.
    kind: 'alert' | 'claim' | 'confirmed' | 'rejected'
    """
    today = time.strftime("%Y-%m-%d")
    stats.setdefault("daily", {})
    day = stats["daily"].setdefault(today, {"alerts": 0, "claims": 0, "confirmed": 0, "rejected": 0, "tokens": {}})
    if kind == "alert":
        day["alerts"] = day.get("alerts", 0) + 1
    elif kind == "claim":
        day["claims"] = day.get("claims", 0) + 1
        if tok_sym and tok_amt:
            day.setdefault("tokens", {})
            day["tokens"][tok_sym] = round(day["tokens"].get(tok_sym, 0) + tok_amt, 4)
    elif kind == "confirmed":
        day["confirmed"] = day.get("confirmed", 0) + 1
    elif kind == "rejected":
        day["rejected"] = day.get("rejected", 0) + 1
    # Κράτα μόνο τις τελευταίες 60 μέρες (καθάρισμα)
    if len(stats["daily"]) > 60:
        for old_key in sorted(stats["daily"].keys())[:-60]:
            del stats["daily"][old_key]

settings = load_settings()
stats = load_stats()

if settings.get("owner_id"):
    owner_id = settings["owner_id"]
    logger.info(f"Loaded owner: {owner_id}")

# ============ ALERTS ============
def load_alerts():
    try:
        if ALERTS_FILE.exists():
            with open(ALERTS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return []

def save_alert(a):
    try:
        alerts = load_alerts()
        alerts.insert(0, a)
        alerts = alerts[:100]
        with open(ALERTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(alerts, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Alert save: {e}")

def save_alerts(alerts_list):
    """Αντικατάσταση όλης της λίστας alerts (χρησιμοποιείται από purge)."""
    try:
        with open(ALERTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(alerts_list[:100], f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Alerts save: {e}")

# ============ FETCH CHANNELS ============
async def fetch_my_channels():
    global my_channels
    my_channels = []
    try:
        async for d in user_client.iter_dialogs():
            try:
                if not (d.is_channel or d.is_group):
                    continue
                title = getattr(d, 'title', '') or ''
                username = getattr(d.entity, 'username', '') or ''
                my_channels.append({"title": title, "username": username, "id": d.id})
            except Exception:
                continue
        logger.info(f"Found {len(my_channels)} channels/groups")
    except Exception as e:
        logger.error(f"Fetch: {e}")

# ============ TOKEN PARSER ============
# Έτοιμα patterns για τα βασικά νομίσματα (πιο αξιόπιστα)
# Πιάνει: "0.38 $ATOM each", "2.17 $ATOM", "1,666.67 $ATOM1KLFG each"
TOKEN_PATTERNS = [
    # "X $TOKEN each" - το ποσό ανά νικητή (προτεραιότητα)
    re.compile(r'([\d,]+\.?\d*)\s*\$?(ATOM1KLFG)\s*each', re.IGNORECASE),
    re.compile(r'([\d,]+\.?\d*)\s*\$?(ATOM)\s*each', re.IGNORECASE),
    # Generic "X $TOKEN each" — απαιτεί $ για να μη ματσάρει "40 people each"
    re.compile(r'([\d,]+\.?\d*)\s*\$([A-Z][A-Z0-9]{1,15})\s*each', re.IGNORECASE),
]
TOKEN_FALLBACK_RE = re.compile(r'([\d,]+\.?\d*)\s*\$([A-Z][A-Z0-9]{1,15})')

def extract_token(msg_text):
    """
    Επιστρέφει (amount_per_winner, symbol) ή (None, None)
    Ψάχνει πρώτα ATOM1KLFG, μετά ATOM, μετά generic 'each', μετά fallback
    """
    if not msg_text:
        return None, None
    # Δοκίμασε τα έτοιμα patterns με σειρά προτεραιότητας
    for pat in TOKEN_PATTERNS:
        m = pat.search(msg_text)
        if m:
            try:
                return float(m.group(1).replace(',', '')), m.group(2).upper()
            except:
                continue
    # Fallback: αν υπάρχουν 2+ $TOKEN, το 2ο είναι συνήθως το 'each'
    matches = TOKEN_FALLBACK_RE.findall(msg_text)
    if len(matches) >= 2:
        try:
            return float(matches[1][0].replace(',', '')), matches[1][1].upper()
        except:
            pass
    elif len(matches) == 1:
        try:
            return float(matches[0][0].replace(',', '')), matches[0][1].upper()
        except:
            pass
    return None, None


# Πιάνει counter σε button: "Claim 8/50", "Join 12/100"
COUNTER_RE = re.compile(r'(\d+)\s*/\s*(\d+)')


def parse_counter(button_text):
    """Από 'Claim 8/50' επιστρέφει (8, 50). Αλλιώς (None, None)."""
    if not button_text:
        return None, None
    m = COUNTER_RE.search(button_text)
    if m:
        try:
            return int(m.group(1)), int(m.group(2))
        except:
            return None, None
    return None, None


def is_sleeping():
    """Ελέγχει αν ο bot 'κοιμάται' (εντός ωρών ύπνου)."""
    if not settings.get("sleep_enabled", False):
        return False
    import datetime
    now_h = datetime.datetime.now().hour
    start = settings.get("sleep_start", 3)
    end = settings.get("sleep_end", 7)
    if start < end:
        return start <= now_h < end        # π.χ. 3-7
    else:
        return now_h >= start or now_h < end  # π.χ. 23-6 (overnight)


def pick_jitter_delay():
    """Επιστρέφει ένα τυχαίο delay μέσα στο ρυθμισμένο range (human-like)."""
    lo = settings.get("jitter_min", 8.0)
    hi = settings.get("jitter_max", 15.0)
    if hi < lo:
        lo, hi = hi, lo
    return round(random.uniform(lo, hi), 2)


async def smart_wait_and_check(chat_id, msg_id, target_delay, button_index):
    """
    Περιμένει μέχρι target_delay ΑΛΛΑ ελέγχει τον counter κάθε ~0.5s.
    Αν η πληρότητα φτάσει το capacity_threshold %, επιστρέφει νωρίτερα.
    Επιστρέφει: (πραγματικός_χρόνος_αναμονής, reason)
      reason: 'timer' (πέρασε ο χρόνος) | 'capacity' (danger zone) | 'error'
    """
    threshold = settings.get("capacity_threshold", 70)
    waited = 0.0
    step = 0.5    # check κάθε 0.5 δευτερόλεπτα (γρήγορη αντίδραση)
    while waited < target_delay:
        sleep_now = min(step, target_delay - waited)
        await asyncio.sleep(sleep_now)
        waited += sleep_now
        # Re-fetch το μήνυμα για να δούμε τον νέο counter
        try:
            fresh = await user_client.get_messages(chat_id, ids=msg_id)
            if fresh and fresh.buttons:
                cur = tot = None
                flat = [b for row in fresh.buttons for b in row]
                if 0 <= button_index < len(flat):
                    cur, tot = parse_counter(flat[button_index].text)
                if cur is None:
                    for b in flat:
                        cur, tot = parse_counter(b.text)
                        if cur is not None:
                            break
                if cur is not None and tot and tot > 0:
                    pct = cur / tot * 100
                    if pct >= threshold:
                        return round(waited, 2), "capacity"
        except Exception as e:
            logger.debug(f"Counter check: {e}")
    return round(waited, 2), "timer"

# ============ COSMOBOT CONFIRMATION TRACKING ============
# Regex για parse του confirmed amount: "claimed a giveaway of 0.2 $ATOM"
CONFIRM_AMOUNT_RE = re.compile(r'giveaway of\s+([\d,]+\.?\d*)\s*\$?([A-Z][A-Z0-9]{1,15})', re.IGNORECASE)
# Regex για requirements: "Required: 22,500 $ATOM"
REQUIRED_RE = re.compile(r'Required:\s*([\d,]+\.?\d*)\s*\$?([A-Z][A-Z0-9]{1,15})', re.IGNORECASE)
# Regex για aggregate: "Your aggregate: 1,237.74 $ATOM"
AGGREGATE_RE = re.compile(r'aggregate:\s*([\d,]+\.?\d*)\s*\$?([A-Z][A-Z0-9]{1,15})', re.IGNORECASE)
# Regex για minigame: "won 0.289583 $ATOM" + "placed 2nd"
GAME_WON_RE = re.compile(r'won\s+([\d,]+\.?\d*)\s*\$?([A-Z][A-Z0-9]{1,15})', re.IGNORECASE)
GAME_PLACE_RE = re.compile(r'placed\s+(\d+)(?:st|nd|rd|th)', re.IGNORECASE)
# Regex για tip: "just sent you 0.5 $ATOM"
TIP_RE = re.compile(r'sent you\s+([\d,]+\.?\d*)\s*\$?([A-Z][A-Z0-9]{1,15})', re.IGNORECASE)
# Regex για withdraw: "Withdrew 16 $ATOM to cosmos13..."
WITHDRAW_RE = re.compile(r'Withdrew\s+([\d,]+\.?\d*)\s*\$?([A-Z][A-Z0-9]{1,15})', re.IGNORECASE)

@user_client.on(events.NewMessage(from_users='ibc_cosmobot'))
async def on_cosmobot_dm(event):
    """Ακούει τα confirmation DMs από το Cosmobot για πραγματικά αποτελέσματα."""
    global stats
    try:
        if not owner_id:
            return
        text = event.message.text or ""
        tl = text.lower()

        # ✅ ΕΠΙΤΥΧΙΑ
        if "successfully claimed" in tl:
            m = CONFIRM_AMOUNT_RE.search(text)
            amt, sym = None, None
            if m:
                try:
                    amt = float(m.group(1).replace(",", ""))
                    sym = m.group(2).upper()
                except Exception:
                    pass
            stats["real_claims"] = stats.get("real_claims", 0) + 1
            if amt and sym:
                stats.setdefault("confirmed_tokens", {})
                stats["confirmed_tokens"][sym] = round(stats["confirmed_tokens"].get(sym, 0) + amt, 4)
            bump_daily("confirmed")
            save_stats(stats)
            if amt and sym:
                await bot_client.send_message(owner_id,
                    f"✅ **Επιβεβαιωμένο!**\n\nΠήρες **{amt:g} ${sym}** 🎉", link_preview=False)
            logger.info(f"✅ Confirmed claim: {amt} {sym}")

        # 🎮 MINIGAME reward: "You placed 2nd... won 0.28 $ATOM"
        elif "won" in tl and ("placed" in tl or "packet" in tl or "ibc-0" in tl):
            m = GAME_WON_RE.search(text)
            place_m = GAME_PLACE_RE.search(text)
            amt, sym = None, None
            if m:
                try:
                    amt = float(m.group(1).replace(",", ""))
                    sym = m.group(2).upper()
                except Exception:
                    pass
            place = place_m.group(1) if place_m else None
            stats["game_wins"] = stats.get("game_wins", 0) + 1
            if amt and sym:
                stats.setdefault("game_tokens", {})
                stats["game_tokens"][sym] = round(stats["game_tokens"].get(sym, 0) + amt, 4)
            save_stats(stats)
            if amt and sym:
                place_txt = f" ({place}η θέση)" if place else ""
                await bot_client.send_message(owner_id,
                    f"🎮 **Minigame!**{place_txt}\n\nΚέρδισες **{amt:g} ${sym}** 🕹️", link_preview=False)
            logger.info(f"🎮 Game win: {amt} {sym} (place {place})")

        # 🎁 TIP received: "Shalaxi just sent you 0.5 $ATOM"
        elif "sent you" in tl:
            m = TIP_RE.search(text)
            amt, sym = None, None
            if m:
                try:
                    amt = float(m.group(1).replace(",", ""))
                    sym = m.group(2).upper()
                except Exception:
                    pass
            stats["tips_received"] = stats.get("tips_received", 0) + 1
            if amt and sym:
                stats.setdefault("tip_tokens", {})
                stats["tip_tokens"][sym] = round(stats["tip_tokens"].get(sym, 0) + amt, 4)
            save_stats(stats)
            if amt and sym:
                await bot_client.send_message(owner_id,
                    f"🎁 **Tip!**\n\nΚάποιος σου έστειλε **{amt:g} ${sym}** 💝", link_preview=False)
            logger.info(f"🎁 Tip: {amt} {sym}")

        # 💸 WITHDRAW: "Withdrew 16 $ATOM to cosmos13..."
        elif "withdrew" in tl and "successful" in tl:
            m = WITHDRAW_RE.search(text)
            amt, sym = None, None
            if m:
                try:
                    amt = float(m.group(1).replace(",", ""))
                    sym = m.group(2).upper()
                except Exception:
                    pass
            if amt and sym:
                stats.setdefault("withdrawals", {})
                stats["withdrawals"][sym] = round(stats["withdrawals"].get(sym, 0) + amt, 4)
                save_stats(stats)
                await bot_client.send_message(owner_id,
                    f"💸 **Withdraw!**\n\nΈκανες withdraw **{amt:g} ${sym}** 💰", link_preview=False)
            logger.info(f"💸 Withdraw: {amt} {sym}")

        # ❌ ΑΠΟΤΥΧΙΑ — Requirements
        elif "don't meet the requirements" in tl or "do not meet the requirements" in tl:
            req = REQUIRED_RE.search(text)
            agg = AGGREGATE_RE.search(text)
            stats["real_failed"] = stats.get("real_failed", 0) + 1
            bump_daily("rejected")
            save_stats(stats)
            req_txt = ""
            if req and agg:
                try:
                    req_amt = float(req.group(1).replace(",", ""))
                    req_sym = req.group(2).upper()
                    agg_amt = float(agg.group(1).replace(",", ""))
                    req_txt = f"\n\n📊 Χρειάζεται: **{req_amt:,.0f} ${req_sym}** staked\n💼 Έχεις: **{agg_amt:,.2f} ${req_sym}**"
                except Exception:
                    pass
            await bot_client.send_message(owner_id,
                f"❌ **Έχασες giveaway** (elite){req_txt}\n\n_Δεν πληροίς τα wallet requirements._",
                link_preview=False)
            logger.info(f"❌ Failed claim (requirements)")

        # ⚠️ Ήδη claimed
        elif "already claimed" in tl:
            stats["real_failed"] = stats.get("real_failed", 0) + 1
            bump_daily("rejected")
            save_stats(stats)
            logger.info("⚠️ Already claimed")

        # ⏰ Πολύ αργά / γεμάτο
        elif "already ended" in tl or "giveaway has ended" in tl or "fully claimed" in tl or "no longer available" in tl:
            stats["real_failed"] = stats.get("real_failed", 0) + 1
            bump_daily("rejected")
            save_stats(stats)
            await bot_client.send_message(owner_id,
                "⏰ **Άργησες** — το giveaway τελείωσε ή γέμισε.", link_preview=False)
            logger.info("⏰ Giveaway ended/full")

    except Exception as e:
        logger.error(f"Cosmobot DM: {e}")


# ============ MONITORING ============
_chat_cache = {}  # {chat_id: {"user": username, "title": title, "ts": timestamp}}

@user_client.on(events.NewMessage())
@user_client.on(events.MessageEdited())
async def on_msg(event):
    global settings, stats
    try:
        if event.is_private:
            return
        if not owner_id:
            return

        msg_text = event.message.text or ""

        if not settings.get("channels"):
            return

        has_kw = bool(settings.get("keywords"))
        has_btn = bool(event.message.buttons)
        if not has_kw and not has_btn:
            return

        msg_key = f"{event.chat_id}_{event.message.id}"
        dedup = f"{msg_key}_btn" if has_btn else msg_key
        if dedup in seen_messages:
            return

        # Fast chat lookup (cached)
        cid = event.chat_id
        cached = _chat_cache.get(cid)
        if cached and (time.time() - cached["ts"]) < 3600:  # cache 1h
            c_user = cached["user"]
            c_title = cached["title"]
        else:
            try:
                chat = await event.get_chat()
            except Exception:
                return
            c_user = (getattr(chat, 'username', '') or '').lower()
            c_title = (getattr(chat, 'title', '') or '').lower()
            _chat_cache[cid] = {"user": c_user, "title": c_title, "ts": time.time()}

        c_id = str(cid)

        monitored = False
        for ch in settings["channels"]:
            cl = ch.lower()
            if cl == c_user or cl == c_title or ch == c_id:
                monitored = True; break
            if c_user and cl in c_user:
                monitored = True; break
        if not monitored:
            return

        msg_lower = msg_text.lower()
        found_kw = [k for k in settings.get("keywords", []) if k.lower() in msg_lower]
        found_btn = []
        if event.message.buttons:
            for row in event.message.buttons:
                for b in row:
                    bt = (getattr(b, 'text', '') or '').lower()
                    for cw in settings.get("click_words", []):
                        if cw.lower() in bt:
                            found_btn.append(b); break

        if settings.get("buttons_only", True):
            if not found_btn:
                return
        else:
            if not found_kw and not found_btn:
                return

        seen_messages.add(dedup)
        if len(seen_messages) > 500:
            seen_messages.clear()

        real_title = getattr(chat, 'title', 'Unknown')
        if c_user:
            link = f"https://t.me/{c_user}/{event.message.id}"
        else:
            link = f"https://t.me/c/{c_id.replace('-100','')}/{event.message.id}"

        # Stats
        stats["total_alerts"] = stats.get("total_alerts", 0) + 1
        bump_daily("alert")
        save_stats(stats)

        # Alert
        parts = ["🔔 **Νέο match**\n"]
        parts.append(f"📡 Κανάλι  **{real_title}**")
        if found_kw:
            parts.append(f"🔑 Λέξη  **{', '.join(found_kw)}**")
        if found_btn:
            parts.append(f"🖱️ Button  **{', '.join(b.text for b in found_btn)}**")
        if msg_text:
            parts.append(f"\n> 💬 {msg_text[:250]}")

        btn_label = "👁️ Δες το μήνυμα" if found_btn else "🔗 Δες το μήνυμα"
        try:
            await bot_client.send_message(owner_id, "\n".join(parts),
                buttons=[[Button.url(btn_label, link)]], link_preview=False)
            logger.info(f"🔔 Alert! KW:{found_kw} BTN:{[b.text for b in found_btn]}")
        except Exception as e:
            logger.error(f"Alert send: {e}")
            return

        # Auto-click
        clicked = False; ctime = ""
        if settings.get("auto_click", False) and found_btn:
            # Sleep mode check
            if is_sleeping():
                logger.debug("😴 Sleeping — skipped claim")
                return
            # Human jitter + capacity safety check
            wait_reason = "instant"
            waited_time = 0.0
            if settings.get("smart_delay", True):
                target = pick_jitter_delay()
                # Βρες το index του button (για re-check)
                btn_idx = 0
                try:
                    flat_orig = [bb for row in event.message.buttons for bb in row]
                    btn_idx = flat_orig.index(found_btn[0])
                except Exception:
                    btn_idx = 0
                logger.info(f"💤 Jitter target {target}s (capacity check @ {settings.get('capacity_threshold',70)}%)")
                waited_time, wait_reason = await smart_wait_and_check(
                    event.chat_id, event.message.id, target, btn_idx
                )
                if wait_reason == "capacity":
                    logger.info(f"⚡ Capacity danger! Πάτησα στα {waited_time}s (γέμιζε)")
                else:
                    logger.info(f"⏱️ Timer πέρασε στα {waited_time}s")

            for b in found_btn:
                try:
                    t1 = time.time()
                    try:
                        await asyncio.wait_for(b.click(), timeout=2.0)
                    except asyncio.TimeoutError:
                        pass
                    el = round(time.time() - t1, 2)
                    ctime = f"{el}s"
                    clicked = True
                    stats["total_clicks"] = stats.get("total_clicks", 0) + 1
                    stats["successful_clicks"] = stats.get("successful_clicks", 0) + 1
                    if stats.get("fastest_click") is None or el < stats["fastest_click"]:
                        stats["fastest_click"] = el
                    tok_amt, tok_sym = extract_token(msg_text)
                    if tok_amt and tok_sym:
                        stats.setdefault("tokens", {})
                        stats["tokens"][tok_sym] = round(stats["tokens"].get(tok_sym, 0) + tok_amt, 4)
                    bump_daily("claim", tok_sym, tok_amt)
                    save_stats(stats)
                    # Notification
                    if waited_time > 0:
                        tag = "⚡ γέμιζε!" if wait_reason == "capacity" else "🎲 jitter"
                        notif = f"✅ Auto-click: **{b.text}**  `({tag} {waited_time:g}s + click {ctime})`"
                    else:
                        notif = f"✅ Auto-click: **{b.text}**  `(instant + click {ctime})`"
                    await bot_client.send_message(owner_id, notif, link_preview=False)
                    logger.info(f"🖱️ Clicked: {b.text} · waited={waited_time}s ({wait_reason}) · click={ctime}")
                except Exception as e:
                    stats["total_clicks"] = stats.get("total_clicks", 0) + 1
                    stats["failed_clicks"] = stats.get("failed_clicks", 0) + 1
                    save_stats(stats)
                    await bot_client.send_message(owner_id,
                        f"❌ Auto-click απέτυχε: **{b.text}**", link_preview=False)
                    logger.error(f"Click: {e}")

        save_alert({
            "id": int(time.time()*1000), "channel": real_title,
            "keyword": ', '.join(found_kw) if found_kw else ', '.join(b.text for b in found_btn),
            "message": msg_text[:200], "time": time.strftime("%H:%M"),
            "link": link, "autoClicked": clicked, "clickTime": ctime
        })
    except Exception as e:
        logger.error(f"Monitor: {e}")

# ============ BOT UI ============
user_states = {}

def menu_buttons():
    ac = "ON" if settings.get("auto_click") else "OFF"
    bo = "ON" if settings.get("buttons_only") else "OFF"
    if settings.get("auto_click"):
        sd = "ON" if settings.get("smart_delay") else "OFF"
    else:
        sd = "—"
    if settings.get("buttons_only"):
        kw_label = "📋 Λέξεις · —"
    else:
        kw_label = "📋 Λέξεις"

    sl = "ON" if settings.get("sleep_enabled") else "OFF"
    sl_h = f"{settings.get('sleep_start',3):02d}:00-{settings.get('sleep_end',7):02d}:00"

    rows = [
        # ── Toggles (3 ανά σειρά) ──
        [Button.inline(f"⚡ Auto · {ac}", b"toggle_ac"), Button.inline(f"🎲 Human · {sd}", b"toggle_sd"), Button.inline(f"😴 Sleep · {sl}", b"toggle_sleep")],
        # ── Settings ──
        [Button.inline(f"🎯 Buttons · {bo}", b"toggle_bo"), Button.inline("⚙️ Jitter", b"delays"), Button.inline(f"🕐 {sl_h}", b"sleep_cfg")],
        # ── Filters ──
        [Button.inline(kw_label, b"keywords"), Button.inline("🏷️ Click words", b"clickwords"), Button.inline("📡 Κανάλια", b"channels")],
        # ── Actions ──
        [Button.inline("📊 Stats", b"stats"), Button.inline("❓ Help", b"help")],
    ]
    dash = os.getenv('RAILWAY_PUBLIC_DOMAIN', '')
    dk = os.getenv('DASHBOARD_KEY', '')
    dash_qs = f"?key={dk}" if dk else ""
    if dash:
        rows.append([Button.url("📈 Dashboard", f"https://{dash}{dash_qs}"), Button.inline("🔄 Refresh", b"refresh")])
    else:
        rows.append([Button.inline("🔄 Refresh", b"refresh")])
    return rows

def menu_text():
    up = int(time.time() - START_TIME)
    if up < 60:
        upt = f"{up}s"
    elif up < 3600:
        upt = f"{up//60}m"
    else:
        upt = f"{up//3600}h {(up%3600)//60}m"
    return (f"🌐 **GGWALL\u200b.NET**\n"
            f"🟢 `Online` · {upt}")


def build_submenu(kind):
    """Επιστρέφει (text, buttons) για ένα από τα sub-menus.
    Χρησιμοποιείται και από τον callback handler και μετά από save στο on_text."""
    if kind == "delays":
        jmin = settings.get("jitter_min", 8.0)
        jmax = settings.get("jitter_max", 15.0)
        cap = settings.get("capacity_threshold", 70)
        text = (
            "🎲 **Human Mode — Καθυστέρηση**\n"
            "─────────────────────\n\n"
            "Το bot περιμένει έναν **τυχαίο** χρόνο\n"
            "πριν το claim, για να μη φαίνεται bot:\n\n"
            f"🎲 **Jitter range:**  `{jmin:g}s – {jmax:g}s`\n"
            f"⚡ **Safety check:**  αν γεμίσει **{cap}%**,\n"
            "     πατάει ΑΜΕΣΩΣ (να μη χάσει το claim)\n\n"
            "_Πάτησε για αλλαγή:_"
        )
        btns = [
            [Button.inline(f"🎲 Min: {jmin:g}s", b"set_jmin"),
             Button.inline(f"🎲 Max: {jmax:g}s", b"set_jmax")],
            [Button.inline(f"⚡ Safety: {cap}%", b"set_cap")],
            [Button.inline("← Πίσω", b"back")]
        ]
        return text, btns

    elif kind == "keywords":
        btns = [[Button.inline(f"🗑️ {k}", f"rmkw_{k}".encode())] for k in settings.get("keywords", [])]
        btns.append([Button.inline("➕ Προσθήκη", b"add_kw")])
        btns.append([Button.inline("← Πίσω", b"back")])
        kl = settings.get("keywords", [])
        lock_note = ""
        if settings.get("buttons_only"):
            lock_note = "\n\n🔒 _Ανενεργές — το 'Μόνο κουμπιά' είναι ON.\nΓια να δουλέψουν, σβήσε το 'Μόνο κουμπιά'._"
        text = "📋 **Λέξεις-Κλειδιά**\n─────────────────────\n\n" + ("\n".join(f"• {k}" for k in kl) if kl else "_Κενό_") + lock_note
        return text, btns

    elif kind == "channels":
        btns = [[Button.inline(f"🗑️ {c}", f"rmch_{c}".encode())] for c in settings.get("channels", [])]
        btns.append([Button.inline("➕ Προσθήκη", b"add_ch")])
        btns.append([Button.inline("← Πίσω", b"back")])
        cl = settings.get("channels", [])
        text = "📡 **Κανάλια**\n─────────────────────\n\n" + ("\n".join(f"• {c}" for c in cl) if cl else "_Κενό_")
        return text, btns

    elif kind == "clickwords":
        btns = [[Button.inline(f"🗑️ {c}", f"rmcw_{c}".encode())] for c in settings.get("click_words", [])]
        btns.append([Button.inline("➕ Προσθήκη", b"add_cw")])
        btns.append([Button.inline("← Πίσω", b"back")])
        cl = settings.get("click_words", [])
        text = "🏷️ **Λέξεις Κουμπιών**\n─────────────────────\n\n" + ("\n".join(f"• {c}" for c in cl) if cl else "_Κενό_")
        return text, btns

    elif kind == "sleep":
        s = settings.get("sleep_start", 3)
        e = settings.get("sleep_end", 7)
        sl = "🟢 ON" if settings.get("sleep_enabled") else "🔴 OFF"
        sleeping = "😴 **Κοιμάται τώρα!**\n\n" if is_sleeping() else ""
        text = (
            f"😴 **Ώρες Ύπνου**\n"
            f"─────────────────────\n\n"
            f"{sleeping}"
            f"Το bot **δεν** κάνει claim μεταξύ:\n\n"
            f"🕐 **{s:02d}:00** → **{e:02d}:00**\n\n"
            f"Status: {sl}\n\n"
            f"_Πάτησε για αλλαγή:_"
        )
        btns = [
            [Button.inline(f"🕐 Αρχή: {s:02d}:00", b"set_sleep_s"),
             Button.inline(f"🕐 Τέλος: {e:02d}:00", b"set_sleep_e")],
            [Button.inline("← Πίσω", b"back")]
        ]
        return text, btns

    return None, None


# Mapping από text-input state → submenu που πρέπει να ξαναεμφανιστεί μετά το save
STATE_TO_SUBMENU = {
    "SET_JMIN": "delays",
    "SET_JMAX": "delays",
    "SET_CAP": "delays",
    "SET_SLEEP_S": "sleep",
    "SET_SLEEP_E": "sleep",
    "ADD_KW": "keywords",
    "ADD_CH": "channels",
    "ADD_CW": "clickwords",
}


def is_authorized(user_id):
    """Ελέγχει αν ο χρήστης έχει δικαίωμα να χρησιμοποιήσει το bot"""
    # Αν έχει οριστεί AUTHORIZED_USER_ID, μόνο αυτοί επιτρέπονται
    if AUTHORIZED_USER_IDS and AUTHORIZED_USER_IDS != [0]:
        return user_id in AUTHORIZED_USER_IDS
    # Αλλιώς, μόνο ο αποθηκευμένος owner (πρώτος που έκανε /start)
    saved = settings.get("owner_id")
    if saved:
        return user_id == saved
    # Κανένας owner ακόμα → επίτρεψε (θα γίνει owner)
    return True


@bot_client.on(events.NewMessage(pattern='/whoami'))
async def cmd_whoami(event):
    try:
        uid = event.sender_id
        await event.respond(
            f"🆔 Το User ID σου είναι:\n\n`{uid}`\n\n"
            f"Βάλ' το στο Railway ως `AUTHORIZED_USER_ID` "
            f"για να κλειδώσεις το bot μόνο για σένα."
        )
    except Exception as e:
        logger.error(f"Whoami: {e}")


@bot_client.on(events.NewMessage(pattern='/start'))
async def cmd_start(event):
    global owner_id
    try:
        uid = event.sender_id
        if not is_authorized(uid):
            await event.respond("🔒 Δεν έχεις πρόσβαση σε αυτό το bot.")
            logger.warning(f"⛔ Unauthorized /start from {uid}")
            return
        owner_id = uid
        settings["owner_id"] = owner_id
        save_settings(settings)
        logger.info(f"Owner: {owner_id}")
        await event.respond(menu_text(), buttons=menu_buttons(), link_preview=False)
    except Exception as e:
        logger.error(f"Start: {e}")

@bot_client.on(events.CallbackQuery())
async def on_cb(event):
    global settings, stats
    try:
        if not is_authorized(event.sender_id):
            await event.answer("🔒 Δεν έχεις πρόσβαση", alert=True)
            return
        data = event.data.decode('utf-8')

        if data == "keywords":
            text, btns = build_submenu("keywords")
            await event.edit(text, buttons=btns)

        elif data == "channels":
            text, btns = build_submenu("channels")
            await event.edit(text, buttons=btns)

        elif data == "clickwords":
            text, btns = build_submenu("clickwords")
            await event.edit(text, buttons=btns)

        elif data == "stats":
            rc = stats.get("real_claims", 0)
            rf = stats.get("real_failed", 0)
            has_confirmed = rc > 0 or rf > 0

            # Hit rate: confirmed αν υπάρχει, αλλιώς clicks
            if has_confirmed:
                wr = round(rc / (rc + rf) * 100) if (rc + rf) > 0 else 0
            else:
                wr = round(stats.get("successful_clicks", 0) / stats["total_clicks"] * 100) if stats.get("total_clicks", 0) > 0 else 0

            # Header stats
            if has_confirmed:
                header = (
                    f"🔔 **Alerts:**  {stats.get('total_alerts', 0)}\n"
                    f"🖱️ **Attempts:**  {stats.get('total_clicks', 0)}\n"
                    f"✅ **Claims:**  {rc}\n"
                    f"❌ **Rejected:**  {rf}\n"
                    f"📈 **Success rate:**  {wr}%"
                )
            else:
                header = (
                    f"🔔 **Alerts:**  {stats.get('total_alerts', 0)}\n"
                    f"🖱️ **Clicks:**  {stats.get('total_clicks', 0)}\n"
                    f"✅ **Επιτυχή:**  {stats.get('successful_clicks', 0)}\n"
                    f"❌ **Αποτυχία:**  {stats.get('failed_clicks', 0)}\n"
                    f"📈 **Win rate:**  {wr}%\n\n"
                    f"_⏳ Αναμονή confirmed data από Cosmobot..._"
                )

            # Tokens: μόνο confirmed sources
            token_sections = ""

            # Giveaway tokens
            ctoks = stats.get("confirmed_tokens", {})
            if ctoks:
                sorted_c = sorted(ctoks.items(), key=lambda x: -x[1])
                token_sections += "\n\n🎯 **Giveaways:**\n" + "\n".join(
                    f"  • {v:g} ${k}" for k, v in sorted_c[:6]
                )

            # Game tokens
            gtoks = stats.get("game_tokens", {})
            if gtoks:
                gw = stats.get("game_wins", 0)
                sorted_g = sorted(gtoks.items(), key=lambda x: -x[1])
                token_sections += f"\n\n🎮 **Games** ({gw}):\n" + "\n".join(
                    f"  • {v:g} ${k}" for k, v in sorted_g[:6]
                )

            # Tip tokens
            ttoks = stats.get("tip_tokens", {})
            if ttoks:
                tr = stats.get("tips_received", 0)
                sorted_t = sorted(ttoks.items(), key=lambda x: -x[1])
                token_sections += f"\n\n🎁 **Tips** ({tr}):\n" + "\n".join(
                    f"  • {v:g} ${k}" for k, v in sorted_t[:6]
                )

            # Withdrawals
            wtoks = stats.get("withdrawals", {})
            if wtoks:
                sorted_w = sorted(wtoks.items(), key=lambda x: -x[1])
                token_sections += "\n\n💸 **Withdrawals:**\n" + "\n".join(
                    f"  • {v:g} ${k}" for k, v in sorted_w[:6]
                )

            if not token_sections and not has_confirmed:
                token_sections = "\n\n_Δεν υπάρχουν ακόμα confirmed data._"

            text = (
                "📊 **Στατιστικά**\n─────────────────────\n\n"
                f"{header}"
                f"{token_sections}"
            )
            dash_url = os.getenv('RAILWAY_PUBLIC_DOMAIN', '')
            dk = os.getenv('DASHBOARD_KEY', '')
            dash_qs = f"?key={dk}" if dk else ""
            buttons = []
            if dash_url:
                buttons.append([Button.url("📊 Άνοιξε Dashboard", f"https://{dash_url}{dash_qs}")])
            buttons.append([Button.inline("📊 Ημερήσια", b"sum_daily"), Button.inline("📈 Εβδομαδιαία", b"sum_weekly")])
            buttons.append([Button.inline("🔄 Reset", b"reset_stats")])
            buttons.append([Button.inline("← Πίσω", b"back")])
            await event.edit(text, buttons=buttons)

        elif data == "sum_daily":
            await event.answer("📊 Φτιάχνω αναφορά…")
            await bot_client.send_message(event.sender_id, build_summary("daily"), link_preview=False)

        elif data == "sum_weekly":
            await event.answer("📈 Φτιάχνω αναφορά…")
            await bot_client.send_message(event.sender_id, build_summary("weekly"), link_preview=False)

        elif data == "reset_stats":
            stats = copy.deepcopy(DEFAULT_STATS)
            save_stats(stats)
            await event.answer("✅ Reset!")
            await event.edit(menu_text(), buttons=menu_buttons())

        elif data == "toggle_ac":
            settings["auto_click"] = not settings.get("auto_click", False)
            save_settings(settings); await event.edit(menu_text(), buttons=menu_buttons())
        elif data == "toggle_bo":
            settings["buttons_only"] = not settings.get("buttons_only", True)
            save_settings(settings); await event.edit(menu_text(), buttons=menu_buttons())
        elif data == "toggle_sd":
            if not settings.get("auto_click"):
                await event.answer("🔒 Άναψε πρώτα το Auto-click!", alert=True)
            else:
                settings["smart_delay"] = not settings.get("smart_delay", True)
                save_settings(settings)
                await event.edit(menu_text(), buttons=menu_buttons())

        elif data == "toggle_sleep":
            settings["sleep_enabled"] = not settings.get("sleep_enabled", False)
            save_settings(settings)
            st = "ON 😴" if settings["sleep_enabled"] else "OFF ⚡"
            await event.answer(f"Sleep mode: {st}", alert=False)
            await event.edit(menu_text(), buttons=menu_buttons())

        elif data == "sleep_cfg":
            s = settings.get("sleep_start", 3)
            e = settings.get("sleep_end", 7)
            sl = "🟢 ON" if settings.get("sleep_enabled") else "🔴 OFF"
            sleeping = "😴 **Κοιμάται τώρα!**\n\n" if is_sleeping() else ""
            text = (
                f"😴 **Ώρες Ύπνου**\n"
                f"─────────────────────\n\n"
                f"{sleeping}"
                f"Το bot **δεν** κάνει claim μεταξύ:\n\n"
                f"🕐 **{s:02d}:00** → **{e:02d}:00**\n\n"
                f"Status: {sl}\n\n"
                f"_Πάτησε για αλλαγή:_"
            )
            btns = [
                [Button.inline(f"🕐 Αρχή: {s:02d}:00", b"set_sleep_s"),
                 Button.inline(f"🕐 Τέλος: {e:02d}:00", b"set_sleep_e")],
                [Button.inline("← Πίσω", b"back")]
            ]
            await event.edit(text, buttons=btns)

        elif data == "set_sleep_s":
            user_states[event.sender_id] = "SET_SLEEP_S"
            await event.edit("🕐 Γράψε την **ώρα έναρξης** ύπνου (0-23):\n\n_π.χ. 3 (= 03:00)_")
        elif data == "set_sleep_e":
            user_states[event.sender_id] = "SET_SLEEP_E"
            await event.edit("🕐 Γράψε την **ώρα λήξης** ύπνου (0-23):\n\n_π.χ. 7 (= 07:00)_")

        elif data == "delays":
            text, btns = build_submenu("delays")
            await event.edit(text, buttons=btns)

        elif data == "set_jmin":
            user_states[event.sender_id] = "SET_JMIN"
            await event.edit("🎲 Γράψε το **ελάχιστο** delay (δευτ/πτα):\n\n_π.χ. 8_")
        elif data == "set_jmax":
            user_states[event.sender_id] = "SET_JMAX"
            await event.edit("🎲 Γράψε το **μέγιστο** delay (δευτ/πτα):\n\n_π.χ. 15_")
        elif data == "set_cap":
            user_states[event.sender_id] = "SET_CAP"
            await event.edit("⚡ Γράψε το **safety threshold** (%):\n\n_Αν γεμίσει τόσο %, πατάει αμέσως._\n_π.χ. 70_")

        elif data == "refresh":
            await event.edit("🔄 Ανανέωση...")
            await fetch_my_channels()
            await event.edit(menu_text(), buttons=menu_buttons())

        elif data == "add_kw":
            user_states[event.sender_id] = "ADD_KW"; await event.edit("📝 Γράψε τη λέξη:")
        elif data == "add_ch":
            btns = []
            for ch in my_channels:
                nm = ch["username"] or ch["title"]
                if nm not in settings.get("channels", []):
                    btns.append([Button.inline(f"📡 {ch['title'][:25]}", f"pick_{nm}".encode())])
            btns.append([Button.inline("📝 Χειροκίνητα", b"add_ch_m")]); btns.append([Button.inline("← Πίσω", b"channels")])
            await event.edit("📡 **Διάλεξε κανάλι:**", buttons=btns)
        elif data == "add_ch_m":
            user_states[event.sender_id] = "ADD_CH"; await event.edit("📝 Γράψε το κανάλι:")
        elif data == "add_cw":
            user_states[event.sender_id] = "ADD_CW"; await event.edit("📝 Γράψε τη λέξη κουμπιού:")

        elif data.startswith("pick_"):
            nm = data[5:]
            if nm not in settings.get("channels", []):
                settings.setdefault("channels", []).append(nm); save_settings(settings)
            btns = [[Button.inline(f"🗑️ {c}", f"rmch_{c}".encode())] for c in settings["channels"]]
            btns.append([Button.inline("➕ Προσθήκη", b"add_ch")]); btns.append([Button.inline("← Πίσω", b"back")])
            await event.edit("📡 **Κανάλια**\n─────────────────────\n\n" + "\n".join(f"• {c}" for c in settings["channels"]), buttons=btns)

        elif data.startswith("rmkw_"):
            k = data[5:]
            if k in settings.get("keywords", []): settings["keywords"].remove(k); save_settings(settings)
            text, btns = build_submenu("keywords")
            await event.edit(text, buttons=btns)
        elif data.startswith("rmch_"):
            c = data[5:]
            if c in settings.get("channels", []): settings["channels"].remove(c); save_settings(settings)
            text, btns = build_submenu("channels")
            await event.edit(text, buttons=btns)
        elif data.startswith("rmcw_"):
            c = data[5:]
            if c in settings.get("click_words", []): settings["click_words"].remove(c); save_settings(settings)
            text, btns = build_submenu("clickwords")
            await event.edit(text, buttons=btns)

        elif data == "help":
            help_text = (
                "❓ **GGWALL\u200b.NET — Οδηγός**\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"

                "**⚡ Auto-click**\n"
                "Πατάει αυτόματα τα claim buttons\n"
                "στα giveaways που ταιριάζουν.\n\n"

                "**🎲 Human Mode**\n"
                "Προσθέτει τυχαία καθυστέρηση\n"
                "πριν το claim (jitter), ώστε να μη\n"
                "φαίνεται bot. Ρυθμιζόμενο range.\n\n"

                "**😴 Sleep Mode**\n"
                "Ώρες που το bot δεν κάνει claim.\n"
                "Κανένας άνθρωπος δεν πατάει\n"
                "giveaway στις 4 τα ξημερώματα.\n\n"

                "**🎯 Buttons Only**\n"
                "Ψάχνει μόνο inline buttons\n"
                "(Claim, Join) — αγνοεί text matches.\n"
                "Πιο ακριβές, λιγότερα false alerts.\n\n"

                "**⚙️ Jitter**\n"
                "Ρυθμίσεις Human Mode:\n"
                "• Min/Max delay σε δευτερόλεπτα\n"
                "• Safety %: αν γεμίσει τόσο,\n"
                "  πατάει αμέσως (να μη χάσει)\n\n"

                "**🕐 Sleep Hours**\n"
                "Ρυθμίσεις ωρών ύπνου.\n"
                "Υποστηρίζει overnight (π.χ. 23-06).\n\n"

                "**📋 Λέξεις**\n"
                "Keywords που ψάχνει στα μηνύματα.\n"
                "Αν βρει αυτές τις λέξεις → alert.\n"
                "Κλειδώνεται αν Buttons Only = ON.\n\n"

                "**🏷️ Click Words**\n"
                "Λέξεις που ψάχνει στα buttons\n"
                "(π.χ. claim, join). Αν ταιριάξει → click.\n\n"

                "**📡 Κανάλια**\n"
                "Τα κανάλια που παρακολουθεί.\n"
                "Πρόσθεσε/αφαίρεσε από τη λίστα.\n\n"

                "**📊 Stats**\n"
                "Αναλυτικά στατιστικά:\n"
                "• Confirmed claims από Cosmobot\n"
                "• Tokens: giveaways, games, tips\n"
                "• Daily & weekly αναφορές\n\n"

                "**📈 Dashboard**\n"
                "Web dashboard με charts, tokens,\n"
                "τιμές σε €, και ρυθμίσεις.\n"
                "Προστατεύεται με password.\n\n"

                "━━━━━━━━━━━━━━━━━━━━\n"
                "_🌐 GGWALL\u200b.NET_"
            )
            await event.edit(help_text, buttons=[[Button.inline("← Πίσω", b"back")]])

        elif data == "back":
            await event.edit(menu_text(), buttons=menu_buttons())

        await event.answer()
    except Exception as e:
        logger.error(f"CB: {e}")
        try: await event.answer("⚠️")
        except: pass

@bot_client.on(events.NewMessage())
async def on_text(event):
    global settings
    try:
        sid = event.sender_id
        if not is_authorized(sid): return
        if sid not in user_states: return
        if not event.message.text or event.message.text.startswith("/"): return
        st = user_states.pop(sid); t = event.message.text.strip()
        confirm_msg = None  # Το μήνυμα επιβεβαίωσης
        changed = False   # True αν έγινε αλλαγή

        if st == "ADD_KW":
            if t and t not in settings.get("keywords", []):
                settings.setdefault("keywords", []).append(t); save_settings(settings)
                confirm_msg = f"✅ Λέξη: **{t}**"; changed = True
            else:
                confirm_msg = "⚠️ Υπάρχει"
        elif st == "ADD_CH":
            c = t.replace("@","").strip()
            if c and c not in settings.get("channels", []):
                settings.setdefault("channels", []).append(c); save_settings(settings)
                confirm_msg = f"✅ Κανάλι: **{c}**"; changed = True
            else:
                confirm_msg = "⚠️ Υπάρχει"
        elif st == "ADD_CW":
            c = t.lower().strip()
            if c and c not in settings.get("click_words", []):
                settings.setdefault("click_words", []).append(c); save_settings(settings)
                confirm_msg = f"✅ Button word: **{c}**"; changed = True
            else:
                confirm_msg = "⚠️ Υπάρχει"
        elif st in ("SET_JMIN", "SET_JMAX"):
            try:
                val = float(t.replace(",", ".").strip())
                if val < 0 or val > 120:
                    confirm_msg = "⚠️ Βάλε αριθμό 0-120"
                else:
                    key = "jitter_min" if st == "SET_JMIN" else "jitter_max"
                    settings[key] = val
                    # Auto-fix: αν min > max, αντάλλαξέ τα
                    jmin = settings.get("jitter_min", 8.0)
                    jmax = settings.get("jitter_max", 15.0)
                    if jmin > jmax:
                        settings["jitter_min"], settings["jitter_max"] = jmax, jmin
                        confirm_msg = f"✅ Ρυθμίστηκε: **{val:g}s** _(διόρθωσα min/max σειρά)_"
                    else:
                        confirm_msg = f"✅ Ρυθμίστηκε: **{val:g}s**"
                    save_settings(settings)
                    changed = True
            except ValueError:
                confirm_msg = "⚠️ Βάλε έγκυρο αριθμό (π.χ. 10)"
        elif st == "SET_CAP":
            try:
                val = int(float(t.replace(",", ".").strip()))
                if val < 10 or val > 100:
                    confirm_msg = "⚠️ Βάλε ποσοστό 10-100"
                else:
                    settings["capacity_threshold"] = val
                    save_settings(settings)
                    confirm_msg = f"✅ Safety threshold: **{val}%**"
                    changed = True
            except ValueError:
                confirm_msg = "⚠️ Βάλε έγκυρο αριθμό (π.χ. 70)"
        elif st in ("SET_SLEEP_S", "SET_SLEEP_E"):
            try:
                val = int(float(t.strip()))
                if val < 0 or val > 23:
                    confirm_msg = "⚠️ Βάλε ώρα 0-23"
                else:
                    key = "sleep_start" if st == "SET_SLEEP_S" else "sleep_end"
                    settings[key] = val
                    save_settings(settings)
                    confirm_msg = f"✅ {'Αρχή' if st == 'SET_SLEEP_S' else 'Τέλος'} ύπνου: **{val:02d}:00**"
                    changed = True
            except ValueError:
                confirm_msg = "⚠️ Βάλε έγκυρο αριθμό (π.χ. 3)"

        # Στείλε επιβεβαίωση και ΞΑΝΑ το menu (για να μη χρειάζεται /start)
        menu_kind = STATE_TO_SUBMENU.get(st)
        if menu_kind and confirm_msg:
            menu_text_str, menu_btns = build_submenu(menu_kind)
            if menu_text_str:
                # Συνδύασε επιβεβαίωση + menu σε ένα μήνυμα
                await event.reply(f"{confirm_msg}\n\n{menu_text_str}", buttons=menu_btns)
            else:
                await event.reply(confirm_msg)
        elif confirm_msg:
            await event.reply(confirm_msg)
    except Exception as e:
        logger.error(f"Text: {e}")

# ============ API ============
DASHBOARD_KEY = os.getenv('DASHBOARD_KEY', '')

def cors(): return {"Access-Control-Allow-Origin": "*"}

def check_key(r):
    """Ελέγχει αν το request έχει σωστό key. Επιστρέφει True αν OK."""
    if not DASHBOARD_KEY:
        return True  # Αν δεν έχει οριστεί key, επίτρεψε (backward compatible)
    return r.query.get('key', '') == DASHBOARD_KEY

def denied():
    return web.Response(text="403 Forbidden", status=403)

async def a_settings(r):
    if not check_key(r): return denied()
    return web.json_response(settings, headers=cors())
async def a_stats(r):
    if not check_key(r): return denied()
    return web.json_response(stats, headers=cors())
async def a_alerts(r):
    if not check_key(r): return denied()
    return web.json_response(load_alerts(), headers=cors())
async def a_health(r): return web.json_response({"status": "running"}, headers=cors())

async def a_purge_alerts(r):
    """Διαγράφει όλα τα alerts από συγκεκριμένο κανάλι και προσαρμόζει τα stats."""
    if not check_key(r): return denied()
    global stats
    try:
        channel = (r.query.get('channel', '') or '').strip()
        if not channel:
            return web.json_response({"ok": False, "error": "no channel"}, headers=cors())

        alerts = load_alerts()
        matching = [a for a in alerts if (a.get('channel', '') or '').lower() == channel.lower()]
        remaining = [a for a in alerts if (a.get('channel', '') or '').lower() != channel.lower()]
        removed = len(matching)

        if removed == 0:
            return web.json_response({"ok": True, "removed": 0}, headers=cors())

        # Προσαρμογή stats — best effort
        successful_removed = sum(1 for a in matching if a.get('autoClicked'))
        stats['total_alerts'] = max(0, stats.get('total_alerts', 0) - removed)
        stats['successful_clicks'] = max(0, stats.get('successful_clicks', 0) - successful_removed)
        stats['total_clicks'] = max(0, stats.get('total_clicks', 0) - successful_removed)

        save_alerts(remaining)
        save_stats(stats)

        logger.info(f"🗑️  Purged {removed} alerts from channel '{channel}' (successful: {successful_removed})")
        return web.json_response({"ok": True, "removed": removed, "successful_removed": successful_removed}, headers=cors())
    except Exception as e:
        logger.error(f"Purge error: {e}")
        return web.json_response({"ok": False, "error": str(e)}, headers=cors())

async def a_dashboard(r):
    if not check_key(r): return denied()
    try:
        dpath = Path(__file__).parent / "dashboard.html"
        if dpath.exists():
            html = dpath.read_text(encoding='utf-8')
            # Inject key στο frontend ώστε τα API calls να το περιλαμβάνουν
            key = r.query.get('key', '')
            if key:
                html = html.replace('const LOCAL=window.location.origin;',
                    f'const LOCAL=window.location.origin;const API_KEY="{key}";', 1)
            else:
                html = html.replace('const LOCAL=window.location.origin;',
                    'const LOCAL=window.location.origin;const API_KEY="";', 1)
            return web.Response(text=html, content_type='text/html')
        return web.Response(text="Dashboard not found", status=404)
    except Exception as e:
        return web.Response(text=str(e), status=500)

# ============ DAILY / WEEKLY SUMMARY ============
def _fmt_tok(v):
    if v >= 1000:
        return f"{v:,.0f}"
    return f"{v:g}" if v == int(v) else f"{v:.2f}"

def build_summary(period="daily"):
    """Φτιάχνει όμορφο summary. period: 'daily' | 'weekly'."""
    import datetime
    daily = stats.get("daily", {})

    if period == "daily":
        # Στα μεσάνυχτα (00:00-00:05) δείξε τη ΧΘΕΣΙΝΗ μέρα
        import datetime
        now = datetime.datetime.now()
        if now.hour == 0:
            yesterday = (now - datetime.timedelta(days=1)).date()
            target = yesterday.strftime("%Y-%m-%d")
            date_label = yesterday.strftime("%d/%m/%Y")
        else:
            target = time.strftime("%Y-%m-%d")
            date_label = time.strftime("%d/%m/%Y")
        days = [target]
        title = "📊 **ΗΜΕΡΗΣΙΑ ΑΝΑΦΟΡΑ**"
    else:
        # τελευταίες 7 μέρες — στα μεσάνυχτα ξεκίνα από χθες
        import datetime as _dt2
        now2 = _dt2.datetime.now()
        if now2.hour == 0:
            base = now2.date() - _dt2.timedelta(days=1)
        else:
            base = now2.date()
        days = [(base - _dt2.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
        title = "📈 **ΕΒΔΟΜΑΔΙΑΙΑ ΑΝΑΦΟΡΑ**"
        d_from = (base - _dt2.timedelta(days=6)).strftime("%d/%m")
        d_to = base.strftime("%d/%m/%Y")
        date_label = f"{d_from} — {d_to}"

    # Άθροισμα
    total_alerts = 0
    total_claims = 0
    tok_totals = {}
    active_days = 0
    best_day = None
    best_day_claims = -1

    for d in days:
        rec = daily.get(d)
        if not rec:
            continue
        a = rec.get("alerts", 0)
        c = rec.get("claims", 0)
        if c > 0 or a > 0:
            active_days += 1
        total_alerts += a
        total_claims += c
        if c > best_day_claims:
            best_day_claims = c
            best_day = d
        for sym, amt in rec.get("tokens", {}).items():
            tok_totals[sym] = round(tok_totals.get(sym, 0) + amt, 4)

    hit = round(total_claims / total_alerts * 100) if total_alerts > 0 else 0

    # Χτίσιμο μηνύματος
    lines = [title, f"`{date_label}`", "━━━━━━━━━━━━━━━━━━━━", ""]

    if total_alerts == 0 and total_claims == 0:
        lines.append("😴 Καμία δραστηριότητα.")
        lines.append("")
        lines.append("_Τα bots παρακολουθούν, απλά δεν εμφανίστηκε giveaway._")
        return "\n".join(lines)

    # Highlights
    lines.append(f"🔔 Signals:  **{total_alerts}**")
    lines.append(f"✅ Claims:  **{total_claims}**")
    lines.append(f"🎯 Hit rate:  **{hit}%**")
    lines.append("")

    # Tokens
    if tok_totals:
        lines.append("💰 **Tokens που μάζεψες:**")
        for sym in sorted(tok_totals, key=lambda k: -tok_totals[k]):
            lines.append(f"   ▸ `{_fmt_tok(tok_totals[sym])}` **${sym}**")
        lines.append("")

    # Weekly extras
    if period == "weekly":
        import datetime as _dt
        lines.append(f"📅 Ενεργές μέρες:  **{active_days}/7**")
        if best_day and best_day_claims > 0:
            try:
                bd = _dt.datetime.strptime(best_day, "%Y-%m-%d").strftime("%A %d/%m")
            except Exception:
                bd = best_day
            lines.append(f"🏆 Καλύτερη μέρα:  **{best_day_claims} claims** ({bd})")
        avg = round(total_claims / 7, 1)
        lines.append(f"📉 Μ.Ο. ημέρας:  **{avg}** claims")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("_🌐 GGWALL\u200b.NET_")

    return "\n".join(lines)


async def summary_scheduler():
    """Στέλνει daily στις 22:00, weekly την Κυριακή 22:00."""
    import datetime
    await asyncio.sleep(10)  # Λίγη ώρα μετά το startup
    last_sent_date = None
    while True:
        try:
            now = datetime.datetime.now()
            # Στις 22:00 (ελέγχει στο παράθυρο 22:00-22:04)
            if now.hour == 0 and now.minute < 5:
                today_str = now.strftime("%Y-%m-%d")
                if last_sent_date != today_str and owner_id:
                    # Daily πάντα
                    await bot_client.send_message(owner_id, build_summary("daily"), link_preview=False)
                    # Weekly μόνο Κυριακή (weekday 6)
                    if now.weekday() == 6:
                        await asyncio.sleep(1)
                        await bot_client.send_message(owner_id, build_summary("weekly"), link_preview=False)
                    last_sent_date = today_str
                    logger.info(f"📊 Summary sent for {today_str}")
            await asyncio.sleep(60)  # Έλεγχος κάθε λεπτό
        except Exception as e:
            logger.error(f"Summary scheduler: {e}")
            await asyncio.sleep(60)


async def start_api():
    app = web.Application()
    app.router.add_get('/', a_dashboard)
    app.router.add_get('/health', a_health)
    app.router.add_get('/api/settings', a_settings)
    app.router.add_get('/api/stats', a_stats)
    app.router.add_get('/api/alerts', a_alerts)
    app.router.add_get('/api/alerts/purge', a_purge_alerts)
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', 8080).start()
    logger.info("🌐 API: 8080 · Dashboard: /")

# ============ MAIN ============
async def main():
    logger.info("🚀 GGWALL.NET starting...")
    # Backfill tokens από παλιά alerts (αν δεν έχουν καταγραφεί)
    try:
        if not stats.get("tokens"):
            old_alerts = load_alerts()
            backfilled = {}
            for a in old_alerts:
                if a.get("autoClicked"):
                    amt, sym = extract_token(a.get("message", ""))
                    if amt and sym:
                        backfilled[sym] = round(backfilled.get(sym, 0) + amt, 4)
            if backfilled:
                stats["tokens"] = backfilled
                save_stats(stats)
                logger.info(f"💰 Backfilled tokens: {backfilled}")
    except Exception as e:
        logger.error(f"Backfill: {e}")
    await user_client.start(); logger.info("✅ User!")
    await fetch_my_channels()
    await bot_client.start(bot_token=BOT_TOKEN); logger.info("✅ Bot!")
    await start_api()
    asyncio.create_task(summary_scheduler())
    logger.info(f"✅ Ready! {len(settings.get('channels',[]))} channels, smart-delay:{settings.get('smart_delay')}")
    await asyncio.gather(user_client.run_until_disconnected(), bot_client.run_until_disconnected())

if __name__ == '__main__':
    try: asyncio.run(main())
    except KeyboardInterrupt: logger.info("🛑")
    except Exception as e: logger.error(f"Fatal: {e}")
