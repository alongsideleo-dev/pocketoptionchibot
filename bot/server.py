"""
Flask + SocketIO Dashboard Server for Pocket Option Telegram Trading Bot.

Endpoints:
  GET  /                        - Main dashboard HTML
  GET  /api/status              - Bot live metrics, balance, queue and connection state
  GET  /api/settings            - Current config.json
  POST /api/settings            - Save settings to config.json
  POST /api/bot/start           - Start the trading bot thread
  POST /api/bot/stop            - Stop the trading bot thread
  GET  /api/logs/download       - Download the full log file
  POST /api/telegram/connect    - Send Telegram SMS verification code
  POST /api/telegram/verify     - Submit Telegram verification code
  POST /api/telegram/password   - Submit Telegram 2FA password
  POST /api/telegram/disconnect - Clear saved Telegram session
  POST /api/pocket_option/connect    - Save SSID and test Pocket Option connection
  POST /api/pocket_option/disconnect - Clear Pocket Option credentials

SocketIO Events (outgoing):
  state_update      - Full state payload
  connection_update - Live connectivity status
  bot_status        - Bot running/stopped status
  log               - Streamed activity log entries
"""

import asyncio
import re
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, render_template, request, send_file
from flask_socketio import SocketIO, emit

BASE_DIR    = Path(__file__).parent.parent          # Project root
CONFIG_FILE = BASE_DIR / "config.json"
_ASSETS_DIR = BASE_DIR / "dashboard"                # HTML/CSS/JS live here
sys.path.insert(0, str(BASE_DIR))

app = Flask(
    __name__,
    template_folder=str(_ASSETS_DIR / "templates"),
    static_folder=str(_ASSETS_DIR / "static"),
)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY") or os.urandom(32).hex()
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ─── Shared async runtime ────────────────────────────────────
_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_lock = threading.Lock()
_loop_ready = threading.Event()


def _runtime() -> asyncio.AbstractEventLoop:
    """Return the shared event loop, starting its thread on first use."""
    global _loop
    with _loop_lock:
        if _loop is not None and not _loop.is_closed():
            return _loop
        _loop_ready.clear()

        def _runner():
            global _loop
            if sys.platform == "win32":
                asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            _loop = loop
            _loop_ready.set()
            loop.run_forever()

        threading.Thread(target=_runner, daemon=True, name="async-runtime").start()
        if not _loop_ready.wait(timeout=10):
            raise RuntimeError("async runtime failed to start")
        return _loop


def submit(coro):
    """Schedule a coroutine on the shared loop; returns a concurrent Future."""
    return asyncio.run_coroutine_threadsafe(coro, _runtime())


def run_sync(coro, timeout: float = 30):
    """Run a coroutine on the shared loop and wait for its result."""
    return submit(coro).result(timeout=timeout)


# ─── Shared State ────────────────────────────────────────────
_tg_client_store: dict = {}

# Live reference to the TradingBot
_bot_instance = None
_bot_thread: Optional[threading.Thread] = None
_bot_starting: bool = False
_bot_stopping: bool = False

# The Pocket Option session opened on startup or from Settings. Kept ALIVE and handed to the bot.
_shared_pocket = None

# Fallback store when bot is not running
_server_state: dict = {"alert": None, "pocket_option_connected": False, "balance": None}


# ─── Helpers ─────────────────────────────────────────────────

def _read_json(path: Path, default=None):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default if default is not None else {}


def _write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _patch_state(diff: dict):
    _server_state.update(diff)


def _pending_signals(th) -> list:
    """Pending (scheduled) signals with a live countdown for the dashboard."""
    out = []
    if not (th and th.executor):
        return out
    for sig in list(th.executor.pending_signals.values()):
        item = {
            "asset":        sig.get("asset"),
            "direction":    sig.get("direction"),
            "expiry":       sig.get("expiry"),
            "entry_time":   sig.get("entry_time"),
            "seconds_left": None,
        }
        if sig.get("entry_time"):
            try:
                left = th.executor._calc_wait(sig["entry_time"], sig.get("timezone"))
                item["seconds_left"] = round(left, 1) if left is not None else None
            except Exception:
                pass
        out.append(item)
    out.sort(key=lambda i: (i["seconds_left"] is None, i["seconds_left"]))
    return out


def _pocket_is_connected() -> bool:
    """True when a live Pocket Option session exists — the bot's or the shared one."""
    if _bot_instance is not None and _bot_instance.pocket_handler is not None:
        return bool(_bot_instance.pocket_handler.is_connected)
    if _shared_pocket is not None:
        return bool(_shared_pocket.is_connected)
    return bool(_server_state.get("pocket_option_connected", False))


def get_bot_state() -> dict:
    """
    Live bot state. Figures come from whichever PocketHandler is alive —
    the bot's while it runs, or the shared session opened from Settings —
    so the dashboard keeps showing the balance even with the bot stopped.
    """
    bot = _bot_instance
    cfg = _read_json(CONFIG_FILE)
    trading = cfg.get("trading", {})

    ph = (bot.pocket_handler if bot is not None else None) or _shared_pocket
    th = bot.telegram_handler if bot is not None else None
    pending = _pending_signals(th)

    running = bool(bot is not None and bot.running)
    if _bot_starting:
        status = "starting"
    elif _bot_stopping:
        status = "stopping"
    elif running:
        status = "running"
    else:
        status = "stopped"

    balance = ph.balance if (ph and ph.balance is not None) else _server_state.get("balance")

    return {
        "status":                  status,
        "daily_trades":            ph.daily_trades if ph else 0,
        "wins":                    ph.wins if ph else 0,
        "losses":                  ph.losses if ph else 0,
        "daily_pnl":               ph.daily_pnl if ph else 0.0,
        "daily_loss":              ph.daily_loss if ph else 0.0,
        "balance":                 balance,
        "day_open_balance":        ph._day_start_balance if ph else None,
        "account_type":            (ph.config.trading.account_type if ph
                                    else trading.get("account_type", "demo")),
        "risk_mode":               (ph.config.trading.risk_mode if ph
                                    else (trading.get("risk_mode") or "fixed")),
        "max_daily_trades":        trading.get("max_daily_trades", 10),
        "max_daily_loss":          trading.get("max_daily_loss", 50.0),
        "max_daily_loss_enabled":  trading.get("max_daily_loss_enabled", True),
        "active_trades":           ph.active_trades if ph else 0,
        "max_concurrent_trades":   trading.get("max_concurrent_trades", 1),
        "last_trade":              ph.last_trade if ph else None,
        "pending":                 pending,
        "active_signals":          len(pending),
        "alert":                   bot.alert if bot is not None else _server_state.get("alert"),
        "pocket_option_connected": _pocket_is_connected(),
        "bot_running":             running,
        "is_starting":             _bot_starting,
        "is_stopping":             _bot_stopping,
    }


def get_connection_status() -> dict:
    """Only report connected when a live Telegram client is authorized.

    A saved StringSession is a credential, not proof of a current connection.
    """
    handler = getattr(_bot_instance, "telegram_handler", None) if _bot_instance else None
    client = getattr(handler, "client", None)
    telegram_ok = False
    if client is not None and client.is_connected():
        try:
            telegram_ok = bool(run_sync(client.is_user_authorized(), timeout=5))
        except Exception as exc:
            logging.getLogger(__name__).warning("Telegram status check failed: %s", type(exc).__name__)
    return {"telegram": telegram_ok, "pocket_option": _pocket_is_connected()}


def _telegram_session_file() -> Path:
    config = _read_json(CONFIG_FILE, {}) or {}
    name = config.get("telegram", {}).get("session_name") or "pocket_bot_session"
    return BASE_DIR / f"{name}.json"


def _save_telegram_session(client, phone):
    """Save locally for this instance; Render Free local storage is ephemeral."""
    session_string = client.session.save()
    target = _telegram_session_file()
    target.write_text(json.dumps({
        "session_string": session_string,
        "phone_number": phone,
        "saved_at": datetime.now().isoformat(),
    }, indent=2), encoding="utf-8")
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    logging.getLogger(__name__).info(
        "Telegram authorized and session saved locally. On Render Free, "
        "configure TELEGRAM_SESSION_STRING securely for persistence across restarts."
    )


def _telegram_auth_error(exc):
    """Avoid returning secret-bearing raw exception strings to the browser."""
    name = type(exc).__name__
    guidance = {
        "PhoneNumberInvalidError": "Phone number is invalid. Use international format such as +14035550123.",
        "ApiIdInvalidError": "Telegram API ID or API hash is invalid.",
        "PhoneCodeInvalidError": "Incorrect Telegram login code.",
        "PhoneCodeExpiredError": "Telegram login code expired. Request a new one.",
        "PhoneCodeEmptyError": "Enter the Telegram login code.",
        "FloodWaitError": "Telegram rate-limited login attempts. Wait before retrying.",
        "PasswordHashInvalidError": "Incorrect Telegram two-step verification password.",
        "PhoneNumberBannedError": "Telegram has restricted this phone number.",
    }
    return guidance.get(name, f"Telegram authentication failed ({name}). Check Render logs.")


async def _close_pending_telegram_client():
    previous = _tg_client_store.pop("client", None)
    _tg_client_store.clear()
    if previous is not None:
        try:
            await previous.disconnect()
        except Exception:
            pass


# ─── Routes ──────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    state = get_bot_state()
    state["connections"] = get_connection_status()
    return jsonify(state)


@app.route("/api/settings", methods=["GET"])
def get_settings():
    cfg_data = _read_json(CONFIG_FILE, {})
    resp = jsonify(cfg_data)
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.route("/api/settings", methods=["POST"])
def save_settings():
    try:
        data = request.get_json(force=True)
        existing = _read_json(CONFIG_FILE, {})
        merged = _deep_merge(existing, data)
        _write_json(CONFIG_FILE, merged)

        # Notify active handlers of updated config
        for holder in (_bot_instance, _shared_pocket):
            c = getattr(holder, "config", None)
            if c is not None:
                try:
                    c.refresh_if_changed()
                except Exception:
                    pass

        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/logs/download")
def download_logs():
    log_file = BASE_DIR / "logs" / "pocket_bot.log"
    for h in logging.getLogger().handlers:
        try:
            h.flush()
        except Exception:
            pass
    if not log_file.exists():
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | INFO | Log initialized\n", encoding="utf-8")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        str(log_file),
        as_attachment=True,
        download_name=f"pocket_bot_logs_{timestamp}.txt",
        mimetype="text/plain",
    )


@app.route("/api/logs", methods=["GET"])
def get_recent_logs():
    """Return all historical logs from the log file to populate the console view."""
    log_file = BASE_DIR / "logs" / "pocket_bot.log"
    if not log_file.exists():
        return jsonify({"logs": []})
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        parsed = []
        for line in lines[-1000:]:
            entry = _parse_log_line(line.strip())
            if entry:
                parsed.append(entry)
        return jsonify({"logs": parsed})
    except Exception as e:
        return jsonify({"logs": [], "error": str(e)})


# ─── Bot Start / Stop ────────────────────────────────────────

def _run_trading_bot():
    global _bot_instance, _bot_starting, _bot_stopping
    from main import TradingBot

    bot = TradingBot(pocket_handler=_shared_pocket)
    _bot_instance = bot
    _bot_starting = True
    socketio.emit("bot_status", {"status": "starting", "running": False})

    try:
        run_sync(bot.start(), timeout=None)
    except Exception as e:
        logging.getLogger(__name__).error(f"TradingBot error: {e}", exc_info=True)
    finally:
        _bot_instance = None
        _bot_starting = False
        _bot_stopping = False
        _server_state["alert"] = None
        socketio.emit("bot_status", {"status": "stopped", "running": False})


@app.route("/api/bot/start", methods=["POST"])
def bot_start():
    global _bot_thread, _bot_starting
    if (_bot_thread is not None and _bot_thread.is_alive()) or _bot_starting:
        return jsonify({"success": False, "message": "Bot is already starting or running"}), 400

    _server_state["alert"] = None
    _bot_starting = True
    socketio.emit("bot_status", {"status": "starting", "running": False})

    _bot_thread = threading.Thread(target=_run_trading_bot, daemon=True, name="TradingBotThread")
    _bot_thread.start()

    return jsonify({"success": True, "status": "starting", "running": False})


@app.route("/api/bot/stop", methods=["POST"])
def bot_stop():
    global _bot_thread, _bot_instance, _bot_stopping, _bot_starting
    if _bot_stopping:
        return jsonify({"success": True, "message": "Bot is already stopping"})

    _bot_stopping = True
    _bot_starting = False
    socketio.emit("bot_status", {"status": "stopping", "running": False})

    if _bot_instance is not None:
        try:
            run_sync(_bot_instance.shutdown(), timeout=10)
        except Exception:
            pass
    if _bot_thread is not None:
        _bot_thread.join(timeout=5)
        _bot_thread = None
    _bot_instance = None
    _bot_stopping = False
    socketio.emit("bot_status", {"status": "stopped", "running": False})
    return jsonify({"success": True})


# ─── Telegram Auth ───────────────────────────────────────────

@app.route("/api/telegram/connect", methods=["POST"])
def telegram_connect():
    data = request.get_json(silent=True) or {}
    phone = re.sub(r"[\s()\-]", "", str(data.get("phone") or ""))
    if not re.fullmatch(r"\+[1-9]\d{7,14}", phone):
        return jsonify({"success": False, "message": "Enter the number in international format, e.g. +14035550123."}), 400

    config = _read_json(CONFIG_FILE, {}) or {}
    api_id = config.get("telegram", {}).get("api_id")
    api_hash = config.get("telegram", {}).get("api_hash", "")
    if not api_id or not api_hash:
        return jsonify({"success": False, "message": "Save Telegram API ID and Hash in Settings first."}), 400

    async def _send():
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        await _close_pending_telegram_client()
        client = TelegramClient(StringSession(), int(api_id), str(api_hash))
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
            _tg_client_store.update(client=client, phone=phone, phone_code_hash=sent.phone_code_hash)
            delivery = type(getattr(sent, "type", None)).__name__
            logging.getLogger(__name__).info("Telegram accepted code request; delivery method: %s", delivery)
            return delivery
        except Exception:
            await client.disconnect()
            raise

    try:
        delivery = run_sync(_send(), timeout=45)
        return jsonify({
            "success": True,
            "message": "Telegram accepted the request. Check the official Telegram chat on your signed-in devices; delivery is not guaranteed.",
            "delivery_type": delivery,
        })
    except Exception as exc:
        logging.getLogger(__name__).warning("Telegram send-code failed: %s", type(exc).__name__)
        return jsonify({"success": False, "message": _telegram_auth_error(exc)}), 400


@app.route("/api/telegram/verify", methods=["POST"])
def telegram_verify():
    data = request.get_json(silent=True) or {}
    code = re.sub(r"\s", "", str(data.get("code") or ""))
    client = _tg_client_store.get("client")
    phone = _tg_client_store.get("phone")
    phone_code_hash = _tg_client_store.get("phone_code_hash")
    if not client or not phone or not phone_code_hash:
        return jsonify({"success": False, "message": "Login attempt expired. Request a new code."}), 400
    if not code.isdigit():
        return jsonify({"success": False, "message": "Enter the numeric code from Telegram."}), 400

    async def _verify():
        from telethon.errors import SessionPasswordNeededError
        try:
            await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
            if not await client.is_user_authorized():
                return {"success": False, "message": "Telegram did not authorize this session."}
            _save_telegram_session(client, phone)
            await _close_pending_telegram_client()
            return {"success": True, "message": "Telegram authorized. Start the bot to establish its live connection."}
        except SessionPasswordNeededError:
            return {"success": False, "two_factor_required": True, "message": "Enter your Telegram two-step verification password."}

    try:
        result = run_sync(_verify(), timeout=45)
    except Exception as exc:
        logging.getLogger(__name__).warning("Telegram verify failed: %s", type(exc).__name__)
        result = {"success": False, "message": _telegram_auth_error(exc)}
    if result.get("success"):
        socketio.emit("connection_update", get_connection_status())
    return jsonify(result), (200 if result.get("success") or result.get("two_factor_required") else 400)


@app.route("/api/telegram/password", methods=["POST"])
def telegram_password():
    data = request.get_json(silent=True) or {}
    password = str(data.get("password") or "")
    client = _tg_client_store.get("client")
    phone = _tg_client_store.get("phone")
    if not client:
        return jsonify({"success": False, "message": "Login attempt expired. Start again."}), 400
    if not password:
        return jsonify({"success": False, "message": "Enter the Telegram two-step verification password."}), 400

    async def _pw():
        await client.sign_in(password=password)
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session not authorized")
        _save_telegram_session(client, phone)
        await _close_pending_telegram_client()

    try:
        run_sync(_pw(), timeout=45)
        socketio.emit("connection_update", get_connection_status())
        return jsonify({"success": True, "message": "Telegram authorized. Start the bot to connect."})
    except Exception as exc:
        logging.getLogger(__name__).warning("Telegram password step failed: %s", type(exc).__name__)
        return jsonify({"success": False, "message": _telegram_auth_error(exc)}), 400


@app.route("/api/telegram/disconnect", methods=["POST"])
def telegram_disconnect():
    try:
        run_sync(_close_pending_telegram_client(), timeout=10)
    except Exception:
        pass
    session_file = _telegram_session_file()
    if session_file.exists():
        session_file.unlink()
    # An environment session cannot be removed from inside the app.
    # Do not falsely report disconnection while the bot is still connected.
    handler = getattr(_bot_instance, "telegram_handler", None) if _bot_instance else None
    if handler is not None:
        try:
            run_sync(handler.disconnect(), timeout=10)
        except Exception:
            pass
    env_active = bool(os.getenv("TELEGRAM_SESSION_STRING") or os.getenv("SESSION_STRING"))
    socketio.emit("connection_update", get_connection_status())
    return jsonify({"success": True, "message": (
        "Local session cleared. Remove TELEGRAM_SESSION_STRING from Render to prevent reconnection."
        if env_active else "Local Telegram session cleared."
    )})


# ─── Pocket Option Connection ────────────────────────────────

@app.route("/api/pocket_option/connect", methods=["POST"])
def pocket_option_connect():
    """Save Pocket Option SSID and test WebSocket connection."""
    data = request.get_json(force=True)
    ssid = (data.get("ssid") or "").strip()
    is_demo = bool(data.get("is_demo", False))

    if not ssid:
        return jsonify({"success": False, "message": "Pocket Option SSID / auth string is required."}), 400

    cfg = _read_json(CONFIG_FILE)
    cfg.setdefault("pocket_option", {})["ssid"] = ssid
    cfg.setdefault("pocket_option", {})["is_demo"] = is_demo
    cfg.setdefault("trading", {})["account_type"] = "demo" if is_demo else "live"
    _write_json(CONFIG_FILE, cfg)

    global _shared_pocket

    async def _connect():
        global _shared_pocket
        from bot.config import Config
        from bot.pocket_handler import PocketHandler

        if _shared_pocket is not None:
            try:
                await _shared_pocket.disconnect()
            except Exception:
                pass
            _shared_pocket = None

        config = Config(str(CONFIG_FILE))
        handler = PocketHandler(config)
        ok = await handler.connect()
        if ok:
            _shared_pocket = handler
            bal = await handler.get_balance()
            _patch_state({"pocket_option_connected": True, "alert": None, "balance": bal})
            socketio.emit("connection_update", {"pocket_option": True})
            return True, bal, ""
        _patch_state({"pocket_option_connected": False})
        return False, None, "Connection failed. Please check that your SSID is fresh and valid."

    try:
        ok, bal, msg = run_sync(_connect(), timeout=30)
        if ok:
            return jsonify({"success": True, "balance": bal})
        return jsonify({"success": False, "message": msg})
    except Exception as e:
        _patch_state({"pocket_option_connected": False})
        return jsonify({"success": False, "message": f"Connection error: {e}"})


@app.route("/api/pocket_option/disconnect", methods=["POST"])
def pocket_option_disconnect():
    global _shared_pocket
    if _shared_pocket is not None:
        try:
            run_sync(_shared_pocket.disconnect(), timeout=15)
        except Exception:
            pass
        _shared_pocket = None

    cfg = _read_json(CONFIG_FILE)
    cfg.setdefault("pocket_option", {})["ssid"] = ""
    _write_json(CONFIG_FILE, cfg)

    _patch_state({"pocket_option_connected": False, "balance": None})
    socketio.emit("connection_update", {"pocket_option": False})
    return jsonify({"success": True})


# ─── SocketIO Events & Loops ─────────────────────────────────

@socketio.on("connect")
def on_connect():
    state = get_bot_state()
    state["connections"] = get_connection_status()
    emit("state_update", state)


def _tail_log_file():
    """Tails logs/pocket_bot.log and broadcasts new lines to dashboard clients."""
    log_path = BASE_DIR / "logs" / "pocket_bot.log"
    while not log_path.exists():
        time.sleep(1)

    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(0, 2)  # Seek to end
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.2)
                continue
            entry = _parse_log_line(line.strip())
            if entry:
                socketio.emit("log", entry)


def _parse_log_line(line: str) -> Optional[dict]:
    if not line or not line.strip():
        return None
    parts = line.split(" | ")
    if len(parts) >= 4:
        return {
            "time": parts[0].strip(),
            "level": parts[2].strip().upper(),
            "message": " | ".join(parts[3:]).strip(),
        }
    elif len(parts) == 3:
        return {
            "time": parts[0].strip(),
            "level": parts[1].strip().upper(),
            "message": parts[2].strip(),
        }
    return {"time": "", "level": "INFO", "message": line.strip()}


def _broadcast_state():
    """Periodically emits state updates and refreshes the balance every 10 seconds."""
    last_balance_poll = 0
    while True:
        time.sleep(2)
        try:
            now = time.monotonic()
            if (_bot_instance is None and _shared_pocket is not None
                    and _shared_pocket.is_connected and (now - last_balance_poll > 10)):
                last_balance_poll = now
                submit(_shared_pocket.get_balance())

            state = get_bot_state()
            state["connections"] = get_connection_status()
            socketio.emit("state_update", state)
        except Exception:
            pass


def _auto_connect_pocket():
    """Automatically connect Pocket Option in background on startup if SSID is present."""
    cfg = _read_json(CONFIG_FILE)
    ssid = cfg.get("pocket_option", {}).get("ssid") or os.getenv("POCKET_OPTION_SSID")
    if ssid and ssid.strip():
        async def _do():
            global _shared_pocket
            from bot.config import Config
            from bot.pocket_handler import PocketHandler
            config = Config(str(CONFIG_FILE))
            handler = PocketHandler(config)
            if await handler.connect():
                _shared_pocket = handler
                bal = await handler.get_balance()
                _patch_state({"pocket_option_connected": True, "balance": bal})
                socketio.emit("connection_update", {"pocket_option": True})
        submit(_do())


def _ensure_logging_initialized():
    """Ensure logs directory and pocket_bot.log file handler are initialized on startup."""
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "pocket_bot.log"
    root = logging.getLogger()
    if not any(isinstance(h, logging.FileHandler) for h in root.handlers):
        from logging.handlers import RotatingFileHandler
        fh = RotatingFileHandler(str(log_file), maxBytes=10*1024*1024, backupCount=5, encoding="utf-8")
        fmt = logging.Formatter('%(asctime)s | %(name)s | %(levelname)s | %(message)s', '%Y-%m-%d %H:%M:%S')
        fh.setFormatter(fmt)
        fh.setLevel(logging.INFO)
        root.addHandler(fh)
        if not root.level or root.level > logging.INFO:
            root.setLevel(logging.INFO)


# ─── Entry Point ─────────────────────────────────────────────

def start_server(host="0.0.0.0", port=None):
    if port is None:
        port = int(os.environ.get("PORT", 5000))
    else:
        port = int(os.environ.get("PORT", port))
    _ensure_logging_initialized()
    _runtime()  # start shared async loop
    _auto_connect_pocket()
    threading.Thread(target=_tail_log_file,   daemon=True, name="LogTailThread").start()
    threading.Thread(target=_broadcast_state, daemon=True, name="StateBroadcastThread").start()
    print(f"\n  POCKET OPTION Dashboard -> http://0.0.0.0:{port}\n")
    socketio.run(app, host=host, port=port, debug=False, allow_unsafe_werkzeug=True)
