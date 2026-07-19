"""
GGWALL Keyword Monitor Bot v2.1
Clean design + persistent settings
"""
import os
import asyncio
import json
import time
import logging
from pathlib import Path
from aiohttp import web
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession

logging.basicConfig(
    format='%(asctime)s [%(levelname)s] %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============ CREDENTIALS ============
API_ID = int(os.getenv('API_ID', '0'))
API_HASH = os.getenv('API_HASH', '')
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
SESSION_STRING = os.getenv('SESSION_STRING', '')

if not API_ID or not API_HASH or not BOT_TOKEN:
    logger.error("Missing credentials!")
    exit(1)

# ============ FILES ============
SETTINGS_FILE = Path("settings.json")
ALERTS_FILE = Path("alerts.json")

# ============ DEFAULT SETTINGS ============
DEFAULT_SETTINGS = {
    "keywords": ["giving", "away"],
    "channels": ["atombuilderscommunity", "atomOGchat", "GGWALLMSG"],
    "auto_click": True,
    "buttons_only": True,
    "click_words": ["claim", "join"],
    "sound": True,
    "owner_id": None
}

# ============ CLIENTS ============
if SESSION_STRING:
    user_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    logger.info("Using StringSession")
else:
    user_client = TelegramClient('session_user', API_ID, API_HASH)
    logger.info("Using file session")
bot_client = TelegramClient('session_bot', API_ID, API_HASH)

# ============ GLOBALS ============
owner_id = None
my_channels = []
seen_messages = set()


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
        logger.error(f"Settings load error: {e}")
    return DEFAULT_SETTINGS.copy()


def save_settings(s):
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Settings save error: {e}")


settings = load_settings()

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


def save_alert(alert_data):
    try:
        alerts = load_alerts()
        alerts.insert(0, alert_data)
        alerts = alerts[:50]
        with open(ALERTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(alerts, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Alert save error: {e}")


# ============ FETCH CHANNELS ============
async def fetch_my_channels():
    global my_channels
    my_channels = []
    try:
        async for dialog in user_client.iter_dialogs():
            try:
                if not (dialog.is_channel or dialog.is_group):
                    continue
                title = getattr(dialog, 'title', '') or ''
                username = getattr(dialog.entity, 'username', '') or ''
                my_channels.append({
                    "title": title,
                    "username": username,
                    "id": dialog.id
                })
            except Exception:
                continue
        logger.info(f"Found {len(my_channels)} channels/groups")
    except Exception as e:
        logger.error(f"Fetch channels error: {e}")


# ============ MONITORING ============
@user_client.on(events.NewMessage())
@user_client.on(events.MessageEdited())
async def on_new_message(event):
    global settings
    try:
        if event.is_private:
            return
        if not settings.get("channels"):
            return
        if not owner_id:
            return

        has_keywords = bool(settings.get("keywords"))
        has_buttons = bool(event.message.buttons)
        if not has_keywords and not has_buttons:
            return

        msg_key = f"{event.chat_id}_{event.message.id}"
        # Για edited messages: αν το προηγούμενο δεν είχε button αλλά τώρα έχει, ξαναδές το
        has_button_now = bool(event.message.buttons)
        dedup_key = f"{msg_key}_btn" if has_button_now else msg_key
        if dedup_key in seen_messages:
            return

        try:
            chat = await event.get_chat()
        except Exception:
            return

        chat_username = (getattr(chat, 'username', '') or '').lower()
        chat_title = (getattr(chat, 'title', '') or '').lower()
        chat_id_str = str(event.chat_id)

        is_monitored = False
        for ch in settings["channels"]:
            cl = ch.lower()
            if cl == chat_username or cl == chat_title or ch == chat_id_str:
                is_monitored = True
                break
            if chat_username and cl in chat_username:
                is_monitored = True
                break
        if not is_monitored:
            return

        msg_text = event.message.text or ""
        msg_lower = msg_text.lower()

        found_keywords = []
        for kw in settings.get("keywords", []):
            if kw.lower() in msg_lower:
                found_keywords.append(kw)

        found_buttons = []
        if event.message.buttons:
            for row in event.message.buttons:
                for btn in row:
                    btn_text = (getattr(btn, 'text', '') or '').lower()
                    for cw in settings.get("click_words", []):
                        if cw.lower() in btn_text:
                            found_buttons.append(btn)
                            break

        if settings.get("buttons_only", True):
            if not found_buttons:
                return
        else:
            if not found_keywords and not found_buttons:
                return

        seen_messages.add(dedup_key)
        if len(seen_messages) > 500:
            seen_messages.clear()

        real_title = getattr(chat, 'title', 'Unknown')
        if chat_username:
            link = f"https://t.me/{chat_username}/{event.message.id}"
        else:
            clean_id = str(event.chat_id).replace("-100", "")
            link = f"https://t.me/c/{clean_id}/{event.message.id}"

        # Alert message (new design)
        has_btn = len(found_buttons) > 0
        
        alert_parts = ["🔔 **Νέο match**\n"]
        alert_parts.append(f"📡 Κανάλι  **{real_title}**")
        if found_keywords:
            alert_parts.append(f"🔑 Λέξη  **{', '.join(found_keywords)}**")
        if found_buttons:
            alert_parts.append(f"🖱️ Button  **{', '.join(b.text for b in found_buttons)}**")
        if msg_text:
            alert_parts.append(f"\n> 💬 {msg_text[:250]}")

        alert_msg = "\n".join(alert_parts)

        # Send with inline button (no raw link, no preview)
        btn_label = "👁️ Δες το μήνυμα" if has_btn else "🔗 Δες το μήνυμα"
        alert_buttons = [[Button.url(btn_label, link)]]
        
        try:
            await bot_client.send_message(owner_id, alert_msg, buttons=alert_buttons, link_preview=False)
            logger.info(f"🔔 Alert! KW:{found_keywords} BTN:{[b.text for b in found_buttons]}")
        except Exception as e:
            logger.error(f"Send alert error: {e}")
            return

        # Auto-click
        clicked = False
        click_time = ""
        if settings.get("auto_click", False) and found_buttons:
            for btn in found_buttons:
                try:
                    t1 = time.time()
                    await btn.click()
                    elapsed = round(time.time() - t1, 2)
                    click_time = f"{elapsed}s"
                    clicked = True
                    await bot_client.send_message(
                        owner_id,
                        f"✅ Auto-click: **{btn.text}**  `({click_time})`",
                        link_preview=False
                    )
                    logger.info(f"🖱️ Clicked: {btn.text} in {click_time}")
                except Exception as e:
                    await bot_client.send_message(
                        owner_id,
                        f"❌ Auto-click αποτυχία: **{btn.text}**",
                        link_preview=False
                    )
                    logger.error(f"Click error: {e}")

        save_alert({
            "id": int(time.time() * 1000),
            "channel": real_title,
            "keyword": ', '.join(found_keywords) if found_keywords else ', '.join(b.text for b in found_buttons),
            "message": msg_text[:200],
            "time": time.strftime("%H:%M"),
            "link": link,
            "autoClicked": clicked,
            "clickTime": click_time
        })

    except Exception as e:
        logger.error(f"Monitor error: {e}")


# ============ BOT UI ============
user_states = {}


def menu_buttons():
    ac = "🟢" if settings.get("auto_click") else "🔴"
    bo = "🟢" if settings.get("buttons_only") else "🔴"
    return [
        [Button.inline("📋 Λέξεις", b"keywords"), Button.inline("📡 Κανάλια", b"channels")],
        [Button.inline("🏷️ Λέξεις κουμπιών", b"clickwords"), Button.inline("📊 Κατάσταση", b"status")],
        [Button.inline(f"⚡ Auto-click {ac}", b"toggle_ac"), Button.inline(f"🎯 Μόνο κουμπιά {bo}", b"toggle_bo")],
        [Button.inline("🧪 Δοκιμή", b"test"), Button.inline("🔄 Ανανέωση", b"refresh")]
    ]


def menu_text():
    ac_status = "ON" if settings.get("auto_click") else "OFF"
    ch_count = len(settings.get("channels", []))
    return (
        f"⚙️ **GGWALL Monitor** `v2.0`\n"
        f"🟢 Ενεργό  ·  ⚡ Auto-click {ac_status}  ·  {ch_count} κανάλια\n"
        f"─────────────────────\n"
        f"Επίλεξε ρύθμιση:"
    )


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
        logger.error(f"Start error: {e}")


@bot_client.on(events.CallbackQuery())
async def on_callback(event):
    global settings
    try:
        data = event.data.decode('utf-8')

        if data == "keywords":
            btns = []
            for kw in settings.get("keywords", []):
                btns.append([Button.inline(f"🗑️ {kw}", f"rmkw_{kw}".encode())])
            btns.append([Button.inline("➕ Προσθήκη", b"add_kw")])
            btns.append([Button.inline("← Πίσω", b"back")])
            kw_list = settings.get("keywords", [])
            text = "📋 **Λέξεις-Κλειδιά**\n─────────────────────\n\n" + ("\n".join(f"• {k}" for k in kw_list) if kw_list else "_Κενό — πάτησε ➕_")
            await event.edit(text, buttons=btns)

        elif data == "channels":
            btns = []
            for ch in settings.get("channels", []):
                btns.append([Button.inline(f"🗑️ {ch}", f"rmch_{ch}".encode())])
            btns.append([Button.inline("➕ Προσθήκη", b"add_ch")])
            btns.append([Button.inline("← Πίσω", b"back")])
            ch_list = settings.get("channels", [])
            text = "📡 **Κανάλια**\n─────────────────────\n\n" + ("\n".join(f"• {c}" for c in ch_list) if ch_list else "_Κενό — πάτησε ➕_")
            await event.edit(text, buttons=btns)

        elif data == "clickwords":
            btns = []
            for cw in settings.get("click_words", []):
                btns.append([Button.inline(f"🗑️ {cw}", f"rmcw_{cw}".encode())])
            btns.append([Button.inline("➕ Προσθήκη", b"add_cw")])
            btns.append([Button.inline("← Πίσω", b"back")])
            cw_list = settings.get("click_words", [])
            text = "🏷️ **Λέξεις Κουμπιών**\n─────────────────────\n\n" + ("\n".join(f"• {c}" for c in cw_list) if cw_list else "_Κενό — πάτησε ➕_")
            await event.edit(text, buttons=btns)

        elif data == "status":
            cw = ", ".join(settings.get("click_words", [])) or "—"
            kw = ", ".join(settings.get("keywords", [])) or "—"
            ch = "\n".join(f"  • {c}" for c in settings.get("channels", [])) or "  —"
            text = (
                "📊 **Κατάσταση**\n"
                "─────────────────────\n\n"
                f"🟢 **Bot:**  Online\n\n"
                f"📡 **Κανάλια:**\n{ch}\n\n"
                f"🔑 **Λέξεις:**  {kw}\n\n"
                f"🏷️ **Button words:**  {cw}\n\n"
                f"⚡ **Auto-click:**  {'🟢 ON' if settings.get('auto_click') else '🔴 OFF'}\n"
                f"🎯 **Μόνο κουμπιά:**  {'🟢 ON' if settings.get('buttons_only') else '🔴 OFF'}"
            )
            await event.edit(text, buttons=[[Button.inline("← Πίσω", b"back")]])

        elif data == "toggle_ac":
            settings["auto_click"] = not settings.get("auto_click", False)
            save_settings(settings)
            await event.edit(menu_text(), buttons=menu_buttons())

        elif data == "toggle_bo":
            settings["buttons_only"] = not settings.get("buttons_only", True)
            save_settings(settings)
            await event.edit(menu_text(), buttons=menu_buttons())

        elif data == "test":
            await send_test_alert()
            await event.answer("🧪 Test στάλθηκε!")

        elif data == "test_claim":
            await event.answer("✅ Claimed! (Test)")
            logger.info("🧪 Test claim clicked")

        elif data == "refresh":
            await event.edit("🔄 Ανανέωση...")
            await fetch_my_channels()
            await event.edit(menu_text(), buttons=menu_buttons())

        elif data == "add_kw":
            user_states[event.sender_id] = "ADD_KW"
            await event.edit("📝 Γράψε τη λέξη:")

        elif data == "add_ch":
            btns = []
            for ch in my_channels:
                name = ch["username"] or ch["title"]
                if name not in settings.get("channels", []):
                    btns.append([Button.inline(f"📡 {ch['title'][:25]}", f"pick_{name}".encode())])
            btns.append([Button.inline("📝 Χειροκίνητα", b"add_ch_manual")])
            btns.append([Button.inline("← Πίσω", b"channels")])
            await event.edit("📡 **Διάλεξε κανάλι:**", buttons=btns)

        elif data == "add_ch_manual":
            user_states[event.sender_id] = "ADD_CH"
            await event.edit("📝 Γράψε το κανάλι:")

        elif data == "add_cw":
            user_states[event.sender_id] = "ADD_CW"
            await event.edit("📝 Γράψε τη λέξη κουμπιού:")

        elif data.startswith("pick_"):
            name = data[5:]
            if name not in settings.get("channels", []):
                settings.setdefault("channels", []).append(name)
                save_settings(settings)
            btns = [[Button.inline(f"🗑️ {c}", f"rmch_{c}".encode())] for c in settings["channels"]]
            btns.append([Button.inline("➕ Προσθήκη", b"add_ch")])
            btns.append([Button.inline("← Πίσω", b"back")])
            text = "📡 **Κανάλια**\n─────────────────────\n\n" + "\n".join(f"• {c}" for c in settings["channels"])
            await event.edit(text, buttons=btns)

        elif data.startswith("rmkw_"):
            kw = data[5:]
            if kw in settings.get("keywords", []): settings["keywords"].remove(kw)
            save_settings(settings)
            btns = [[Button.inline(f"🗑️ {k}", f"rmkw_{k}".encode())] for k in settings.get("keywords", [])]
            btns.append([Button.inline("➕ Προσθήκη", b"add_kw")])
            btns.append([Button.inline("← Πίσω", b"back")])
            kw_list = settings.get("keywords", [])
            text = "📋 **Λέξεις-Κλειδιά**\n─────────────────────\n\n" + ("\n".join(f"• {k}" for k in kw_list) if kw_list else "_Κενό_")
            await event.edit(text, buttons=btns)

        elif data.startswith("rmch_"):
            ch = data[5:]
            if ch in settings.get("channels", []): settings["channels"].remove(ch)
            save_settings(settings)
            btns = [[Button.inline(f"🗑️ {c}", f"rmch_{c}".encode())] for c in settings.get("channels", [])]
            btns.append([Button.inline("➕ Προσθήκη", b"add_ch")])
            btns.append([Button.inline("← Πίσω", b"back")])
            ch_list = settings.get("channels", [])
            text = "📡 **Κανάλια**\n─────────────────────\n\n" + ("\n".join(f"• {c}" for c in ch_list) if ch_list else "_Κενό_")
            await event.edit(text, buttons=btns)

        elif data.startswith("rmcw_"):
            cw = data[5:]
            if cw in settings.get("click_words", []): settings["click_words"].remove(cw)
            save_settings(settings)
            btns = [[Button.inline(f"🗑️ {c}", f"rmcw_{c}".encode())] for c in settings.get("click_words", [])]
            btns.append([Button.inline("➕ Προσθήκη", b"add_cw")])
            btns.append([Button.inline("← Πίσω", b"back")])
            cw_list = settings.get("click_words", [])
            text = "🏷️ **Λέξεις Κουμπιών**\n─────────────────────\n\n" + ("\n".join(f"• {c}" for c in cw_list) if cw_list else "_Κενό_")
            await event.edit(text, buttons=btns)

        elif data == "back":
            await event.edit(menu_text(), buttons=menu_buttons())

        await event.answer()

    except Exception as e:
        logger.error(f"Callback error: {e}")
        try:
            await event.answer("⚠️ Σφάλμα")
        except Exception:
            pass


@bot_client.on(events.NewMessage())
async def on_text(event):
    global settings
    try:
        sid = event.sender_id
        if sid not in user_states:
            return
        if not event.message.text or event.message.text.startswith("/"):
            return
        state = user_states.pop(sid)
        text = event.message.text.strip()

        if state == "ADD_KW":
            if text and text not in settings.get("keywords", []):
                settings.setdefault("keywords", []).append(text)
                save_settings(settings)
                await event.reply(f"✅ Λέξη: **{text}**")
            else:
                await event.reply("⚠️ Υπάρχει ή κενό")

        elif state == "ADD_CH":
            ch = text.replace("@", "").strip()
            if ch and ch not in settings.get("channels", []):
                settings.setdefault("channels", []).append(ch)
                save_settings(settings)
                await event.reply(f"✅ Κανάλι: **{ch}**")
            else:
                await event.reply("⚠️ Υπάρχει ή κενό")

        elif state == "ADD_CW":
            cw = text.lower().strip()
            if cw and cw not in settings.get("click_words", []):
                settings.setdefault("click_words", []).append(cw)
                save_settings(settings)
                await event.reply(f"✅ Button word: **{cw}**")
            else:
                await event.reply("⚠️ Υπάρχει ή κενό")

    except Exception as e:
        logger.error(f"Text handler error: {e}")


# ============ TEST ============
async def send_test_alert():
    if not owner_id:
        return
    try:
        alert = (
            "🔔 **Νέο match**\n\n"
            "📡 Κανάλι  **Test Channel**\n"
            "🖱️ Button  **Claim 0/5**\n\n"
            "> 💬 Test alert — αν βλέπεις αυτό, ΟΛΑ δουλεύουν!"
        )
        await bot_client.send_message(
            owner_id, alert,
            buttons=[[Button.url("👁️ Δες το μήνυμα", "https://t.me/test")]],
            link_preview=False
        )
        save_alert({
            "id": int(time.time() * 1000),
            "channel": "Test Channel",
            "keyword": "Claim 0/5",
            "message": "Test alert",
            "time": time.strftime("%H:%M"),
            "link": "https://t.me/test",
            "autoClicked": False,
            "clickTime": ""
        })
        logger.info("🧪 Test alert sent")
    except Exception as e:
        logger.error(f"Test alert error: {e}")


# ============ API SERVER ============
def cors():
    return {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, POST, OPTIONS", "Access-Control-Allow-Headers": "Content-Type"}

async def api_get_settings(r): return web.json_response(settings, headers=cors())
async def api_post_settings(r):
    global settings
    try:
        data = await r.json()
        settings.update(data)
        save_settings(settings)
        return web.json_response({"ok": True}, headers=cors())
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400, headers=cors())
async def api_channels(r): return web.json_response(my_channels, headers=cors())
async def api_alerts(r): return web.json_response(load_alerts(), headers=cors())
async def api_test(r):
    await send_test_alert()
    return web.json_response({"ok": True}, headers=cors())
async def api_test_button(r):
    try:
        test_ch = None
        for ch in my_channels:
            if 'test' in ch['title'].lower() or 'ggwallmsg' in (ch.get('username', '') or '').lower():
                test_ch = ch
                break
        if not test_ch:
            return web.json_response({"ok": False, "error": "No test channel"}, headers=cors())
        await bot_client.send_message(test_ch['id'], "🧪 **Test Button Message**\n\nThis is a test! Press the button below!", buttons=[Button.inline("Claim 0/5", b"test_claim")])
        return web.json_response({"ok": True, "channel": test_ch['title']}, headers=cors())
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, headers=cors())
async def api_refresh(r):
    await fetch_my_channels()
    return web.json_response(my_channels, headers=cors())
async def api_options(r): return web.Response(headers=cors())
async def api_health(r):
    return web.json_response({"status": "running", "owner": owner_id, "channels": len(settings.get("channels", []))}, headers=cors())

async def start_api():
    app = web.Application()
    app.router.add_get('/', api_health)
    app.router.add_get('/api/settings', api_get_settings)
    app.router.add_post('/api/settings', api_post_settings)
    app.router.add_options('/api/settings', api_options)
    app.router.add_get('/api/channels', api_channels)
    app.router.add_get('/api/alerts', api_alerts)
    app.router.add_get('/api/test', api_test)
    app.router.add_get('/api/test_button', api_test_button)
    app.router.add_get('/api/refresh', api_refresh)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', 8080).start()
    logger.info("🌐 API: http://0.0.0.0:8080")


# ============ MAIN ============
async def main():
    logger.info("🚀 Starting GGWALL Monitor v2.1...")
    logger.info("📱 Connecting user...")
    await user_client.start()
    logger.info("✅ User connected!")
    await fetch_my_channels()
    logger.info("🤖 Connecting bot...")
    await bot_client.start(bot_token=BOT_TOKEN)
    logger.info("✅ Bot connected!")
    await start_api()
    logger.info("✅ Ready! Send /start to your bot")
    logger.info(f"📡 Monitoring {len(settings.get('channels', []))} channels")
    logger.info(f"🔑 Keywords: {settings.get('keywords', [])}")
    logger.info(f"🏷️ Click words: {settings.get('click_words', [])}")
    logger.info(f"⚡ Auto-click: {settings.get('auto_click', False)}")
    logger.info(f"🎯 Buttons only: {settings.get('buttons_only', True)}")
    await asyncio.gather(
        user_client.run_until_disconnected(),
        bot_client.run_until_disconnected()
    )

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Stopped")
    except Exception as e:
        logger.error(f"Fatal: {e}")
