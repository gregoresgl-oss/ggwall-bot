"""
GGWALL Keyword Monitor Bot v3.0
- Persistent data (Railway Volume /app/data)
- Analytics (claims, win rate, timing)
- Auto-detect new channels (sends link to you)
- Edit message detection
"""
import os
import asyncio
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

# ============ CREDENTIALS ============
API_ID = int(os.getenv('API_ID', '0'))
API_HASH = os.getenv('API_HASH', '')
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
SESSION_STRING = os.getenv('SESSION_STRING', '')

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
    "sound": True,
    "owner_id": None,
    "auto_detect": True  # ανίχνευση νέων καναλιών
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
                    s.setdefault(k, v)
                return s
    except Exception as e:
        logger.error(f"Settings load: {e}")
    return DEFAULT_SETTINGS.copy()

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
                    s.setdefault(k, v)
                return s
    except Exception:
        pass
    return DEFAULT_STATS.copy()

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
# Πιάνει "0.38 $ATOM each" ή "0.5 $ATOM" - το ποσό ΑΝΑ άτομο
TOKEN_EACH_RE = re.compile(r'([\d,]+\.?\d*)\s*\$?([A-Z][A-Z0-9]{1,15})\s*each', re.IGNORECASE)
TOKEN_ANY_RE = re.compile(r'([\d,]+\.?\d*)\s*\$([A-Z][A-Z0-9]{1,15})')

def extract_token(msg_text):
    """Βρίσκει πόσα tokens παίρνει ο κάθε νικητής (το 'each' amount)"""
    if not msg_text:
        return None, None
    # Προτίμησε το "X TOKEN each" (το ποσό ανά άτομο)
    m = TOKEN_EACH_RE.search(msg_text)
    if not m:
        # Αλλιώς πάρε το πρώτο $TOKEN που δεν είναι το σύνολο
        matches = TOKEN_ANY_RE.findall(msg_text)
        if len(matches) >= 2:
            # Το δεύτερο συνήθως είναι το "each"
            m2 = matches[1]
            try:
                return float(m2[0].replace(',', '')), m2[1].upper()
            except:
                return None, None
        elif len(matches) == 1:
            try:
                return float(matches[0][0].replace(',', '')), matches[0][1].upper()
            except:
                return None, None
        return None, None
    try:
        return float(m.group(1).replace(',', '')), m.group(2).upper()
    except:
        return None, None

async def check_new_channels(msg_text):
    """Ψάχνει links για νέα κανάλια στο μήνυμα"""
    if not settings.get("auto_detect", True):
        return
    if not owner_id:
        return
    try:
        matches = CHANNEL_LINK_RE.findall(msg_text or "")
        for m in matches:
            # Καθάρισε το link
            clean = m.strip('+')
            # Αγνόησε αν είναι ήδη γνωστό
            known = [c.lower() for c in settings.get("channels", [])]
            known += [c["username"].lower() for c in my_channels if c["username"]]
            already_detected = [c.lower() for c in stats.get("channels_detected", [])]
            if clean.lower() in known or clean.lower() in already_detected:
                continue
            # Νέο κανάλι! Στείλε ειδοποίηση
            stats.setdefault("channels_detected", []).append(clean)
            save_stats(stats)
            await bot_client.send_message(
                owner_id,
                f"🆕 **Νέο κανάλι εντοπίστηκε!**\n\n"
                f"📡 `{clean}`\n\n"
                f"Θέλεις να μπεις; Πάτα το link:",
                buttons=[[Button.url("🔗 Άνοιξε το κανάλι", f"https://t.me/{m}")]],
                link_preview=False
            )
            logger.info(f"🆕 New channel detected: {clean}")
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
        await check_new_channels(msg_text)

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
    rows = [
        [Button.inline("📋 Λέξεις", b"keywords"), Button.inline("📡 Κανάλια", b"channels")],
        [Button.inline("🏷️ Λέξεις κουμπιών", b"clickwords"), Button.inline("📊 Στατιστικά", b"stats")],
        [Button.inline(f"⚡ Auto-click {ac}", b"toggle_ac"), Button.inline(f"🎯 Μόνο κουμπιά {bo}", b"toggle_bo")],
        [Button.inline(f"🆕 Auto-detect {ad}", b"toggle_ad")],
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

@bot_client.on(events.NewMessage(pattern='/start'))
async def cmd_start(event):
    global owner_id
    try:
        owner_id = event.sender_id
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
        data = event.data.decode('utf-8')

        if data == "keywords":
            btns = [[Button.inline(f"🗑️ {k}", f"rmkw_{k}".encode())] for k in settings.get("keywords", [])]
            btns.append([Button.inline("➕ Προσθήκη", b"add_kw")]); btns.append([Button.inline("← Πίσω", b"back")])
            kl = settings.get("keywords", [])
            await event.edit("📋 **Λέξεις-Κλειδιά**\n─────────────────────\n\n" + ("\n".join(f"• {k}" for k in kl) if kl else "_Κενό_"), buttons=btns)

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
            stats = DEFAULT_STATS.copy()
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

        elif data == "test_claim":
            await event.answer("✅ Claimed! (Test)")
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
    except Exception as e:
        logger.error(f"Text: {e}")

# ============ TEST ============
async def send_test():
    if not owner_id: return
    try:
        await bot_client.send_message(owner_id,
            "🔔 **Νέο match**\n\n📡 Κανάλι  **Test Channel**\n🖱️ Button  **Claim 0/5**\n\n> 💬 Test — ΟΛΑ δουλεύουν!",
            buttons=[[Button.url("👁️ Δες το μήνυμα", "https://t.me/test")]], link_preview=False)
        logger.info("🧪 Test sent")
    except Exception as e:
        logger.error(f"Test: {e}")

# ============ API ============
def cors(): return {"Access-Control-Allow-Origin": "*"}
async def a_settings(r): return web.json_response(settings, headers=cors())
async def a_stats(r): return web.json_response(stats, headers=cors())
async def a_alerts(r): return web.json_response(load_alerts(), headers=cors())
async def a_test(r):
    await send_test(); return web.json_response({"ok": True}, headers=cors())
async def a_test_btn(r):
    try:
        tc = None
        for ch in my_channels:
            if 'test' in ch['title'].lower() or 'ggwallmsg' in (ch.get('username','') or '').lower():
                tc = ch; break
        if not tc: return web.json_response({"ok": False}, headers=cors())
        await bot_client.send_message(tc['id'], "🧪 **Test**\n\nPress button!", buttons=[Button.inline("Claim 0/5", b"test_claim")])
        return web.json_response({"ok": True}, headers=cors())
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, headers=cors())
async def a_health(r): return web.json_response({"status": "running", "owner": owner_id}, headers=cors())

async def a_dashboard(r):
    try:
        dpath = Path("dashboard.html")
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
    app.router.add_get('/api/test', a_test)
    app.router.add_get('/api/test_button', a_test_btn)
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', 8080).start()
    logger.info("🌐 API: 8080 · Dashboard: /")

# ============ MAIN ============
async def main():
    logger.info("🚀 GGWALL Monitor v3.0...")
    await user_client.start(); logger.info("✅ User!")
    await fetch_my_channels()
    await bot_client.start(bot_token=BOT_TOKEN); logger.info("✅ Bot!")
    await start_api()
    logger.info(f"✅ Ready! {len(settings.get('channels',[]))} channels, auto-detect:{settings.get('auto_detect')}")
    await asyncio.gather(user_client.run_until_disconnected(), bot_client.run_until_disconnected())

if __name__ == '__main__':
    try: asyncio.run(main())
    except KeyboardInterrupt: logger.info("🛑")
    except Exception as e: logger.error(f"Fatal: {e}")
