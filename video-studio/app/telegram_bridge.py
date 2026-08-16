"""Two-way bridge between the studio chat and the sg-hermes Telegram group.

Connects as the FOUNDER'S OWN Telegram account (Telethon / MTProto), not as a
bot: Telegram bots cannot see other bots' messages, so a bot bridge would never
receive Hermes's replies. As the founder's account the studio chat IS the
group thread — messages typed in the studio are sent from the founder (Hermes's
bot sees them like any Telegram message), and every group message (Hermes's
replies included) streams back into the studio chat.

Setup (one time):
  1. Get api_id + api_hash at https://my.telegram.org → "API development tools".
  2. Put them in video-studio/config.json:
       "telegram_api_id": 123456, "telegram_api_hash": "…", "telegram_group": "sg-hermes"
  3. Log in once (sends a code to your Telegram):
       <autoVSL venv python> video-studio/app/telegram_bridge.py --login
  4. Restart the studio — the bridge connects automatically.

The session file lives at video-studio/jobs/telegram.session (never commit it).
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
VS_ROOT = APP_DIR.parent
SESSION = VS_ROOT / "jobs" / "telegram"      # telethon appends .session

state = {"configured": False, "connected": False, "authorized": False,
         "group": None, "error": None, "started": None}

_loop: asyncio.AbstractEventLoop | None = None
_queue: asyncio.Queue | None = None
_recent_sent: list[str] = []                 # to skip the echo of our own sends
_recent_lock = threading.Lock()


def _cfg() -> dict:
    return json.loads((VS_ROOT / "config.json").read_text(encoding="utf-8"))


async def _resolve_group(client, want: str):
    """Find the group by exact title (case-insensitive) or @username."""
    want_l = want.lstrip("@").lower()
    async for d in client.iter_dialogs():
        if not (d.is_group or d.is_channel):
            continue
        title = (d.title or "").lower()
        uname = (getattr(d.entity, "username", "") or "").lower()
        if title == want_l or uname == want_l:
            return d.entity
    return None


async def _run(api_id: int, api_hash: str, group: str, on_message) -> None:
    global _queue
    from telethon import TelegramClient, events

    _queue = asyncio.Queue()
    client = TelegramClient(str(SESSION), api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        state["error"] = ("not logged in — run telegram_bridge.py --login once "
                          "(sends a code to your Telegram)")
        await client.disconnect()
        return
    state["authorized"] = True
    entity = await _resolve_group(client, group)
    if entity is None:
        state["error"] = f"group “{group}” not found in your Telegram dialogs"
        await client.disconnect()
        return
    me = await client.get_me()
    state["connected"] = True
    state["group"] = getattr(entity, "title", group)
    state["error"] = None

    @client.on(events.NewMessage(chats=entity))
    async def _on_msg(ev):  # noqa: ANN001
        text = (ev.message.message or "").strip()
        if not text:
            return
        if ev.message.out:
            # our own account — either the bridge's send echoing back (skip;
            # it's already in the store) or the founder typing from their phone
            with _recent_lock:
                if text in _recent_sent:
                    _recent_sent.remove(text)
                    return
            author, name = "founder", "you (from Telegram)"
        else:
            sender = await ev.get_sender()
            name = (getattr(sender, "first_name", None) or
                    getattr(sender, "title", None) or "?")
            author = "hermes" if "hermes" in name.lower() else "member"
        try:
            on_message(author, text, source="telegram", name=name)
        except Exception:  # noqa: BLE001 — never kill the bridge on a store error
            pass

    async def _sender():
        while True:
            text = await _queue.get()
            try:
                with _recent_lock:
                    _recent_sent.append(text)
                    del _recent_sent[:-20]
                await client.send_message(entity, text)
            except Exception as e:  # noqa: BLE001
                state["error"] = f"send failed: {str(e)[:120]}"

    asyncio.create_task(_sender())
    await client.run_until_disconnected()
    state["connected"] = False


def start(on_message) -> None:
    """Start the bridge thread if config.json has the Telegram keys."""
    cfg = _cfg()
    api_id, api_hash = cfg.get("telegram_api_id"), cfg.get("telegram_api_hash")
    group = cfg.get("telegram_group") or "sg-hermes"
    if not (api_id and api_hash):
        state["error"] = ("telegram_api_id / telegram_api_hash missing in "
                          "config.json — get them at my.telegram.org")
        return
    state["configured"] = True
    state["started"] = time.time()

    def _thread():
        global _loop
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
        try:
            _loop.run_until_complete(_run(int(api_id), str(api_hash), group, on_message))
        except Exception as e:  # noqa: BLE001
            state["connected"] = False
            state["error"] = str(e)[:200]

    threading.Thread(target=_thread, daemon=True, name="telegram-bridge").start()


def send(text: str) -> bool:
    """Queue a message to the group (thread-safe). False when not connected."""
    if not (state["connected"] and _loop and _queue):
        return False
    _loop.call_soon_threadsafe(_queue.put_nowait, text)
    return True


def _login() -> None:
    """One-time interactive login — run from a real terminal."""
    cfg = _cfg()
    api_id, api_hash = cfg.get("telegram_api_id"), cfg.get("telegram_api_hash")
    if not (api_id and api_hash):
        raise SystemExit("Add telegram_api_id + telegram_api_hash to "
                         "video-studio/config.json first (from my.telegram.org)")
    from telethon.sync import TelegramClient
    SESSION.parent.mkdir(parents=True, exist_ok=True)
    with TelegramClient(str(SESSION), int(api_id), str(api_hash)) as client:
        me = client.get_me()
        print(f"✓ logged in as {me.first_name} (+{me.phone}) — session saved.")
        want = (cfg.get("telegram_group") or "sg-hermes").lower()
        found = [d.title for d in client.iter_dialogs()
                 if (d.is_group or d.is_channel) and (d.title or "").lower() == want]
        print(f"✓ group “{want}”: {'found' if found else 'NOT FOUND — check the name in config.json'}")
    print("Restart the studio server and the chat connects automatically.")


if __name__ == "__main__":
    import sys
    if "--login" in sys.argv:
        _login()
    else:
        print(__doc__)
