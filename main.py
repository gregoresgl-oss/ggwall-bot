"""
GGWALL Keyword Monitor Bot v3.0
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
AUTHORIZED_USER_ID = int(os.getenv('AUTHORIZED_USER_ID', '0'))

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
    "detect_keywords": ["giveaway", "claim", "airdrop", "prize", "winner", "reward", "free", "drop", "distribution", "raffle", "contest"],
    "owner_id": None,
    "auto_detect": True,  # ανίχνευση νέων καναλιών
    "smart_delay": True,  # έξυπνη καθυστέρηση βάσει ποσού
    "delay_tiny": 10.0,   # < 0.5 token/χρήστη → 10s
    "delay_small": 5.0,   # 0.5-1 token/χρήστη → 5s
    "delay_good": 0.0,    # ≥ 1 token/χρήστη → 0s (αμέσως by default)
    "delay_many_people": 2.0  # +2s αν 20+ χρήστες
}

DEFAULT_STATS = {
    "total_alerts": 0,
    "total_clicks": 0,
    "successful_clicks": 0,
    "failed_clicks": 0,
    "fastest_click": None,
    "channels_detected": [],
    "tokens": {}
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

# ============ AUTO-DETECT LINKS ============
CHANNEL_LINK_RE = re.compile(r't\.me/(?:joinchat/)?([a-zA-Z0-9_+]+)')

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


# Πιάνει "to 40 people", "to 2 people"
PEOPLE_RE = re.compile(r'to\s+(\d+)\s+people', re.IGNORECASE)

def extract_people(msg_text):
    """Βρίσκει πόσοι χρήστες μπορούν να κάνουν claim"""
    if not msg_text:
        return None
    m = PEOPLE_RE.search(msg_text)
    if m:
        try:
            return int(m.group(1))
        except:
            return None
    return None


def calc_delay(amount, people):
    """
    Υπολογίζει πόσα δευτερόλεπτα να περιμένει πριν το claim.
    Λογική:
    - Μικρό ποσό ανά χρήστη → περίμενε περισσότερο
    - Πολλοί χρήστες → περίμενε λίγο ακόμα (γεμίζει πιο αργά)
    """
    delay = 0
    # Βάσει ποσού
    if amount is not None:
        if amount < 0.5:
            delay = settings.get("delay_tiny", 10.0)    # πολύ μικρό
        elif amount < 1.0:
            delay = settings.get("delay_small", 5.0)    # μικρό
        else:
            delay = settings.get("delay_good", 0.0)     # καλό (default 0s = αμέσως)
    # Extra delay αν πολλοί χρήστες (γεμίζει αργά, έχεις χρόνο)
    if people is not None and people >= 20 and delay > 0:
        delay += settings.get("delay_many_people", 2.0)
    return delay

async def check_new_channels(msg_text, chat_title=None, chat_username=None):
    """Ψάχνει links για νέα κανάλια στο μήνυμα (μόνο με giveaway context)."""
    if not settings.get("auto_detect", True):
        return
    if not owner_id:
        return
    try:
        matches = CHANNEL_LINK_RE.findall(msg_text or "")
        if not matches:
            return

        # ─── Filter A+B: πρέπει να ισχύει ένα από τα δύο ───
        # A) Το μήνυμα περιέχει giveaway keyword
        msg_lower = (msg_text or "").lower()
        detect_kws = settings.get("detect_keywords", [])
        matched_kw = None
        for kw in detect_kws:
            if kw.lower() in msg_lower:
                matched_kw = kw
                break

        # B) Το μήνυμα προέρχεται από monitored channel
        monitored = [c.lower() for c in settings.get("channels", [])]
        from_monitored = chat_username and chat_username.lower() in monitored

        # Αν κανένα από τα δύο δεν ισχύει, αγνόησε
        if not matched_kw and not from_monitored:
            return

        # Ετοιμασία reason για το notification
        if matched_kw and from_monitored:
            reason = f'keyword _"{matched_kw}"_ + monitored channel'
        elif matched_kw:
            reason = f'keyword _"{matched_kw}"_'
        else:
            reason = "από monitored channel"

        for m in matches:
            clean = m.strip('+')
            known = [c.lower() for c in settings.get("channels", [])]
            known += [c["username"].lower() for c in my_channels if c["username"]]
            already_detected = [c.lower() for c in stats.get("channels_detected", [])]
            if clean.lower() in known or clean.lower() in already_detected:
                continue

            # Νέο κανάλι! Στείλε rich ειδοποίηση
            stats.setdefault("channels_detected", []).append(clean)
            save_stats(stats)

            source = f"\n📍 Από: **{chat_title}**" if chat_title else ""
            msg = (
                f"🆕 **Νέο κανάλι εντοπίστηκε!**\n"
                f"─────────────────────\n\n"
                f"📡 `{clean}`\n"
                f"🎯 Match: {reason}"
                f"{source}\n\n"
                f"_Θέλεις να το προσθέσεις;_"
            )
            buttons = [
                [Button.url("🔗 Άνοιξε το κανάλι", f"https://t.me/{m}")],
                [Button.inline("✅ Προσθήκη στη λίστα", f"ac_{clean}".encode()),
                 Button.inline("❌ Αγνόησε", f"ic_{clean}".encode())]
            ]
            await bot_client.send_message(owner_id, msg, buttons=buttons, link_preview=False)
            logger.info(f"🆕 New channel: {clean} (reason: {reason})")
    except Exception as e:
        logger.error(f"Detect error: {e}")

# ============ MONITORING ============
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

        # Auto-detect νέων καναλιών (τρέχει πάντα)
        chat_title = None
        chat_username = None
        try:
            chat = await event.get_chat()
            chat_title = getattr(chat, "title", None) or getattr(chat, "first_name", None)
            chat_username = getattr(chat, "username", None)
        except Exception:
            pass
        await check_new_channels(msg_text, chat_title, chat_username)

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

        try:
            chat = await event.get_chat()
        except Exception:
            return

        c_user = (getattr(chat, 'username', '') or '').lower()
        c_title = (getattr(chat, 'title', '') or '').lower()
        c_id = str(event.chat_id)

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
            # Smart delay: κλιμακωτή καθυστέρηση βάσει ποσού + αριθμού χρηστών
            delay_applied = 0
            if settings.get("smart_delay", True):
                tok_amt, tok_sym = extract_token(msg_text)
                ppl = extract_people(msg_text)
                delay_applied = calc_delay(tok_amt, ppl)
                if delay_applied > 0:
                    logger.info(f"💤 Delay {delay_applied}s (amount:{tok_amt} {tok_sym}, people:{ppl})")
                    await asyncio.sleep(delay_applied)
            for b in found_btn:
                try:
                    t1 = time.time()
                    await b.click()
                    el = round(time.time() - t1, 2)
                    ctime = f"{el}s"
                    clicked = True
                    stats["total_clicks"] = stats.get("total_clicks", 0) + 1
                    stats["successful_clicks"] = stats.get("successful_clicks", 0) + 1
                    if stats.get("fastest_click") is None or el < stats["fastest_click"]:
                        stats["fastest_click"] = el
                    # Track tokens claimed
                    tok_amt, tok_sym = extract_token(msg_text)
                    if tok_amt and tok_sym:
                        stats.setdefault("tokens", {})
                        stats["tokens"][tok_sym] = round(stats["tokens"].get(tok_sym, 0) + tok_amt, 4)
                    save_stats(stats)
                    await bot_client.send_message(owner_id,
                        f"✅ Auto-click: **{b.text}**  `({ctime})`", link_preview=False)
                    logger.info(f"🖱️ Clicked: {b.text} in {ctime}")
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
    ac = "🟢" if settings.get("auto_click") else "🔴"
    bo = "🟢" if settings.get("buttons_only") else "🔴"
    ad = "🟢" if settings.get("auto_detect") else "🔴"
    # Smart-delay: κλειδωμένο αν Auto-click OFF (δεν χρησιμοποιείται)
    if settings.get("auto_click"):
        sd = "🟢" if settings.get("smart_delay") else "🔴"
        sd_label = f"💤 Smart-delay {sd}"
    else:
        sd_label = "💤 Smart-delay 🔒"
    # Λέξεις (text): κλειδωμένο αν Buttons Only ON (αγνοούνται)
    if settings.get("buttons_only"):
        kw_label = "📋 Λέξεις 🔒"
    else:
        kw_label = "📋 Λέξεις"

    rows = [
        [Button.inline(kw_label, b"keywords"), Button.inline("📡 Κανάλια", b"channels")],
        [Button.inline("🏷️ Λέξεις κουμπιών", b"clickwords"), Button.inline("🔍 Λέξεις ανίχνευσης", b"detectwords")],
        [Button.inline("📊 Στατιστικά", b"stats")],
        [Button.inline(f"⚡ Auto-click {ac}", b"toggle_ac"), Button.inline(f"🎯 Μόνο κουμπιά {bo}", b"toggle_bo")],
        [Button.inline(f"🆕 Auto-detect {ad}", b"toggle_ad"), Button.inline(sd_label, b"toggle_sd")],
        [Button.inline("⏱️ Ρυθμίσεις καθυστέρησης", b"delays")],
    ]
    dash = os.getenv('RAILWAY_PUBLIC_DOMAIN', '')
    if dash:
        rows.append([Button.url("📈 Dashboard", f"https://{dash}"), Button.inline("🔄 Ανανέωση", b"refresh")])
    else:
        rows.append([Button.inline("🔄 Ανανέωση", b"refresh")])
    return rows

def menu_text():
    up = int(time.time() - START_TIME)
    if up < 60:
        upt = f"{up}s"
    elif up < 3600:
        upt = f"{up//60}m"
    else:
        upt = f"{up//3600}h {(up%3600)//60}m"
    return (f"⚙️ **GGWALL Monitor** `v3.0`\n"
            f"🟢 Online · uptime {upt}")


def is_authorized(user_id):
    """Ελέγχει αν ο χρήστης έχει δικαίωμα να χρησιμοποιήσει το bot"""
    # Αν έχει οριστεί AUTHORIZED_USER_ID, μόνο αυτός επιτρέπεται
    if AUTHORIZED_USER_ID:
        return user_id == AUTHORIZED_USER_ID
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
            btns = [[Button.inline(f"🗑️ {k}", f"rmkw_{k}".encode())] for k in settings.get("keywords", [])]
            btns.append([Button.inline("➕ Προσθήκη", b"add_kw")]); btns.append([Button.inline("← Πίσω", b"back")])
            kl = settings.get("keywords", [])
            lock_note = ""
            if settings.get("buttons_only"):
                lock_note = "\n\n🔒 _Ανενεργές — το 'Μόνο κουμπιά' είναι ON.\nΓια να δουλέψουν, σβήσε το 'Μόνο κουμπιά'._"
            await event.edit("📋 **Λέξεις-Κλειδιά**\n─────────────────────\n\n" + ("\n".join(f"• {k}" for k in kl) if kl else "_Κενό_") + lock_note, buttons=btns)

        elif data == "channels":
            btns = [[Button.inline(f"🗑️ {c}", f"rmch_{c}".encode())] for c in settings.get("channels", [])]
            btns.append([Button.inline("➕ Προσθήκη", b"add_ch")]); btns.append([Button.inline("← Πίσω", b"back")])
            cl = settings.get("channels", [])
            await event.edit("📡 **Κανάλια**\n─────────────────────\n\n" + ("\n".join(f"• {c}" for c in cl) if cl else "_Κενό_"), buttons=btns)

        elif data == "clickwords":
            btns = [[Button.inline(f"🗑️ {c}", f"rmcw_{c}".encode())] for c in settings.get("click_words", [])]
            btns.append([Button.inline("➕ Προσθήκη", b"add_cw")]); btns.append([Button.inline("← Πίσω", b"back")])
            cl = settings.get("click_words", [])
            await event.edit("🏷️ **Λέξεις Κουμπιών**\n─────────────────────\n\n" + ("\n".join(f"• {c}" for c in cl) if cl else "_Κενό_"), buttons=btns)

        elif data == "detectwords":
            dkw = settings.get("detect_keywords", [])
            btns = [[Button.inline(f"🗑️ {c}", f"rmdk_{c}".encode())] for c in dkw]
            btns.append([Button.inline("➕ Προσθήκη", b"add_dk")])
            btns.append([Button.inline("← Πίσω", b"back")])
            ad_status = "🟢 ON" if settings.get("auto_detect") else "🔴 OFF"
            text = (
                "🔍 **Λέξεις Ανίχνευσης**\n─────────────────────\n\n"
                "Το bot προτείνει νέα κανάλια όταν το μήνυμα:\n"
                "• Περιέχει κάποια από αυτές τις λέξεις, **ή**\n"
                "• Προέρχεται από monitored κανάλι\n\n"
                f"Auto-detect: {ad_status}\n\n"
                "**Λέξεις:**\n"
                + ("\n".join(f"• {c}" for c in dkw) if dkw else "_Κενό_")
            )
            await event.edit(text, buttons=btns)

        elif data == "stats":
            wr = 0
            if stats.get("total_clicks", 0) > 0:
                wr = round(stats.get("successful_clicks", 0) / stats["total_clicks"] * 100)
            fc = stats.get("fastest_click")
            fc_txt = f"{fc}s" if fc else "—"
            # Tokens summary
            toks = stats.get("tokens", {})
            tok_lines = ""
            if toks:
                sorted_toks = sorted(toks.items(), key=lambda x: -x[1])
                tok_lines = "\n\n💰 **Tokens:**\n" + "\n".join(
                    f"  • {v:g} ${k}" for k, v in sorted_toks[:8]
                )
            text = (
                "📊 **Στατιστικά**\n─────────────────────\n\n"
                f"🔔 **Alerts:**  {stats.get('total_alerts', 0)}\n"
                f"🖱️ **Clicks:**  {stats.get('total_clicks', 0)}\n"
                f"✅ **Επιτυχή:**  {stats.get('successful_clicks', 0)}\n"
                f"❌ **Αποτυχία:**  {stats.get('failed_clicks', 0)}\n"
                f"📈 **Win rate:**  {wr}%\n"
                f"⚡ **Ταχύτερο:**  {fc_txt}"
                f"{tok_lines}"
            )
            dash_url = os.getenv('RAILWAY_PUBLIC_DOMAIN', '')
            buttons = []
            if dash_url:
                buttons.append([Button.url("📊 Άνοιξε Dashboard", f"https://{dash_url}")])
            buttons.append([Button.inline("🔄 Reset", b"reset_stats")])
            buttons.append([Button.inline("← Πίσω", b"back")])
            await event.edit(text, buttons=buttons)

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
        elif data == "toggle_ad":
            settings["auto_detect"] = not settings.get("auto_detect", True)
            save_settings(settings); await event.edit(menu_text(), buttons=menu_buttons())
        elif data == "toggle_sd":
            if not settings.get("auto_click"):
                await event.answer("🔒 Άναψε πρώτα το Auto-click!", alert=True)
            else:
                settings["smart_delay"] = not settings.get("smart_delay", True)
                save_settings(settings)
                await event.edit(menu_text(), buttons=menu_buttons())

        elif data == "delays":
            dt = settings.get("delay_tiny", 10.0)
            ds = settings.get("delay_small", 5.0)
            dg = settings.get("delay_good", 0.0)
            dp = settings.get("delay_many_people", 2.0)
            good_txt = f"**{dg:g}s**" if dg > 0 else "**0s** (αμέσως)"
            text = (
                "⏱️ **Ρυθμίσεις Καθυστέρησης**\n"
                "─────────────────────\n\n"
                "Πόσο περιμένει πριν το claim,\n"
                "βάσει ποσού ανά χρήστη:\n\n"
                f"🐌 **Πολύ μικρό** `< 0.5`  →  **{dt:g}s**\n"
                f"🚶 **Μικρό** `0.5–1`  →  **{ds:g}s**\n"
                f"⚡ **Καλό** `≥ 1`  →  {good_txt}\n\n"
                f"➕ **Bonus** αν 20+ άτομα  →  **+{dp:g}s**\n\n"
                "_Πάτησε για αλλαγή:_"
            )
            btns = [
                [Button.inline(f"🐌 Πολύ μικρό: {dt:g}s", b"set_tiny")],
                [Button.inline(f"🚶 Μικρό: {ds:g}s", b"set_small")],
                [Button.inline(f"⚡ Καλό: {dg:g}s", b"set_good")],
                [Button.inline(f"➕ Bonus πολλών: {dp:g}s", b"set_many")],
                [Button.inline("← Πίσω", b"back")]
            ]
            await event.edit(text, buttons=btns)

        elif data == "set_tiny":
            user_states[event.sender_id] = "SET_TINY"
            await event.edit("⏱️ Γράψε δευτερόλεπτα για **πολύ μικρά** ποσά (< 0.5):\n\n_π.χ. 15_")
        elif data == "set_small":
            user_states[event.sender_id] = "SET_SMALL"
            await event.edit("⏱️ Γράψε δευτερόλεπτα για **μικρά** ποσά (0.5–1):\n\n_π.χ. 5_")
        elif data == "set_good":
            user_states[event.sender_id] = "SET_GOOD"
            await event.edit("⏱️ Γράψε δευτερόλεπτα για **καλά** ποσά (≥ 1):\n\n_0 = αμέσως · π.χ. 3_")
        elif data == "set_many":
            user_states[event.sender_id] = "SET_MANY"
            await event.edit("⏱️ Γράψε extra δευτερόλεπτα για **20+ άτομα**:\n\n_π.χ. 2_")

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
        elif data == "add_dk":
            user_states[event.sender_id] = "ADD_DK"; await event.edit("📝 Γράψε λέξη-κλειδί ανίχνευσης\n\n_π.χ. atomdrop, cosmodrop, δώρο_")

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
            btns = [[Button.inline(f"🗑️ {x}", f"rmkw_{x}".encode())] for x in settings.get("keywords", [])]
            btns.append([Button.inline("➕ Προσθήκη", b"add_kw")]); btns.append([Button.inline("← Πίσω", b"back")])
            kl = settings.get("keywords", [])
            await event.edit("📋 **Λέξεις-Κλειδιά**\n─────────────────────\n\n" + ("\n".join(f"• {x}" for x in kl) if kl else "_Κενό_"), buttons=btns)
        elif data.startswith("rmch_"):
            c = data[5:]
            if c in settings.get("channels", []): settings["channels"].remove(c); save_settings(settings)
            btns = [[Button.inline(f"🗑️ {x}", f"rmch_{x}".encode())] for x in settings.get("channels", [])]
            btns.append([Button.inline("➕ Προσθήκη", b"add_ch")]); btns.append([Button.inline("← Πίσω", b"back")])
            cl = settings.get("channels", [])
            await event.edit("📡 **Κανάλια**\n─────────────────────\n\n" + ("\n".join(f"• {x}" for x in cl) if cl else "_Κενό_"), buttons=btns)
        elif data.startswith("rmcw_"):
            c = data[5:]
            if c in settings.get("click_words", []): settings["click_words"].remove(c); save_settings(settings)
            btns = [[Button.inline(f"🗑️ {x}", f"rmcw_{x}".encode())] for x in settings.get("click_words", [])]
            btns.append([Button.inline("➕ Προσθήκη", b"add_cw")]); btns.append([Button.inline("← Πίσω", b"back")])
            cl = settings.get("click_words", [])
            await event.edit("🏷️ **Λέξεις Κουμπιών**\n─────────────────────\n\n" + ("\n".join(f"• {x}" for x in cl) if cl else "_Κενό_"), buttons=btns)

        elif data.startswith("rmdk_"):
            k = data[5:]
            if k in settings.get("detect_keywords", []):
                settings["detect_keywords"].remove(k); save_settings(settings)
            dkw = settings.get("detect_keywords", [])
            btns = [[Button.inline(f"🗑️ {x}", f"rmdk_{x}".encode())] for x in dkw]
            btns.append([Button.inline("➕ Προσθήκη", b"add_dk")])
            btns.append([Button.inline("← Πίσω", b"back")])
            ad_status = "🟢 ON" if settings.get("auto_detect") else "🔴 OFF"
            text = (
                "🔍 **Λέξεις Ανίχνευσης**\n─────────────────────\n\n"
                "Το bot προτείνει νέα κανάλια όταν το μήνυμα:\n"
                "• Περιέχει κάποια από αυτές τις λέξεις, **ή**\n"
                "• Προέρχεται από monitored κανάλι\n\n"
                f"Auto-detect: {ad_status}\n\n"
                "**Λέξεις:**\n"
                + ("\n".join(f"• {x}" for x in dkw) if dkw else "_Κενό_")
            )
            await event.edit(text, buttons=btns)

        elif data.startswith("ac_"):
            # Quick-add από notification
            nm = data[3:]
            if nm not in settings.get("channels", []):
                settings.setdefault("channels", []).append(nm)
                save_settings(settings)
                await event.edit(f"✅ **Προστέθηκε στη λίστα!**\n\n📡 `{nm}`\n\n_Θυμήσου: πρέπει να μπεις στο κανάλι από τον λογαριασμό σου._")
            else:
                await event.edit(f"⚠️ Το `{nm}` υπάρχει ήδη στη λίστα.")

        elif data.startswith("ic_"):
            # Ignore από notification
            nm = data[3:]
            await event.edit(f"❌ **Αγνοήθηκε**\n\n📡 `{nm}`\n\n_Δεν θα σου ξαναπροταθεί._")

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
        if st == "ADD_KW":
            if t and t not in settings.get("keywords", []):
                settings.setdefault("keywords", []).append(t); save_settings(settings)
                await event.reply(f"✅ Λέξη: **{t}**")
            else: await event.reply("⚠️ Υπάρχει")
        elif st == "ADD_CH":
            c = t.replace("@","").strip()
            if c and c not in settings.get("channels", []):
                settings.setdefault("channels", []).append(c); save_settings(settings)
                await event.reply(f"✅ Κανάλι: **{c}**")
            else: await event.reply("⚠️ Υπάρχει")
        elif st == "ADD_CW":
            c = t.lower().strip()
            if c and c not in settings.get("click_words", []):
                settings.setdefault("click_words", []).append(c); save_settings(settings)
                await event.reply(f"✅ Button word: **{c}**")
            else: await event.reply("⚠️ Υπάρχει")
        elif st == "ADD_DK":
            c = t.lower().strip()
            if c and c not in settings.get("detect_keywords", []):
                settings.setdefault("detect_keywords", []).append(c); save_settings(settings)
                await event.reply(f"✅ Detect keyword: **{c}**")
            else: await event.reply("⚠️ Υπάρχει")
        elif st in ("SET_TINY", "SET_SMALL", "SET_GOOD", "SET_MANY"):
            try:
                val = float(t.replace(",", ".").strip())
                if val < 0 or val > 120:
                    await event.reply("⚠️ Βάλε αριθμό 0-120")
                else:
                    key = {
                        "SET_TINY": "delay_tiny",
                        "SET_SMALL": "delay_small",
                        "SET_GOOD": "delay_good",
                        "SET_MANY": "delay_many_people"
                    }[st]
                    settings[key] = val
                    save_settings(settings)
                    await event.reply(f"✅ Ρυθμίστηκε: **{val:g}s**")
            except ValueError:
                await event.reply("⚠️ Βάλε έγκυρο αριθμό (π.χ. 10)")
    except Exception as e:
        logger.error(f"Text: {e}")

# ============ API ============
def cors(): return {"Access-Control-Allow-Origin": "*"}
async def a_settings(r): return web.json_response(settings, headers=cors())
async def a_stats(r): return web.json_response(stats, headers=cors())
async def a_alerts(r): return web.json_response(load_alerts(), headers=cors())
async def a_health(r): return web.json_response({"status": "running", "owner": owner_id}, headers=cors())

async def a_dashboard(r):
    try:
        dpath = Path(__file__).parent / "dashboard.html"
        if dpath.exists():
            return web.Response(text=dpath.read_text(encoding='utf-8'), content_type='text/html')
        return web.Response(text="Dashboard not found", status=404)
    except Exception as e:
        return web.Response(text=str(e), status=500)

async def start_api():
    app = web.Application()
    app.router.add_get('/', a_dashboard)
    app.router.add_get('/health', a_health)
    app.router.add_get('/api/settings', a_settings)
    app.router.add_get('/api/stats', a_stats)
    app.router.add_get('/api/alerts', a_alerts)
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', 8080).start()
    logger.info("🌐 API: 8080 · Dashboard: /")

# ============ MAIN ============
async def main():
    logger.info("🚀 GGWALL Monitor v3.0...")
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
    logger.info(f"✅ Ready! {len(settings.get('channels',[]))} channels, auto-detect:{settings.get('auto_detect')}, smart-delay:{settings.get('smart_delay')}")
    await asyncio.gather(user_client.run_until_disconnected(), bot_client.run_until_disconnected())

if __name__ == '__main__':
    try: asyncio.run(main())
    except KeyboardInterrupt: logger.info("🛑")
    except Exception as e: logger.error(f"Fatal: {e}")
