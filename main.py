"""
Pocket Option Telegram Trading Bot

Entry point: python main.py
  -> Starts the dashboard only (http://localhost:5000)
  -> Click START in the dashboard to launch the bot
  -> Click STOP to shut it down
"""

import asyncio
import sys
import logging
import os
from datetime import datetime
from bot.config import Config
from bot.telegram_handler import TelegramHandler
from bot.pocket_handler import PocketHandler

# Windows: fix event loop and console encoding
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass


class TradingBot:
    """Trading bot - holds live state for Pocket Option trading."""

    def __init__(self, pocket_handler=None):
        self.config           = None
        self.logger           = None
        self.telegram_handler = None
        self.pocket_handler   = pocket_handler
        self._owns_pocket     = pocket_handler is None
        self._tasks: list     = []
        self.running          = False
        self.alert: str | None = None

    async def start(self) -> bool:
        try:
            self.config = Config()
            if not os.path.exists(self.config.config_file):
                print(f"{self.config.config_file} not found - open Settings in dashboard to configure.")
                return False
            self._setup_logging()
            self.logger = logging.getLogger(__name__)
            self.logger.info("=== Pocket Option Telegram Trading Bot Starting ===")

            if not self.config.validate():
                self.logger.error("Configuration validation failed - check telegram API credentials & channels.")
                return False

            if self.pocket_handler is None:
                self.pocket_handler = PocketHandler(self.config)
                self._owns_pocket   = True
            else:
                self.logger.info(
                    "Adopting the Pocket Option session already open in dashboard "
                    f"(connected={self.pocket_handler.is_connected})."
                )

            self.telegram_handler = TelegramHandler(self.config)

            # Register with dashboard
            try:
                from bot import server as _srv
                _srv._bot_instance = self
            except Exception:
                pass

            if not await self.telegram_handler.initialize(self.pocket_handler):
                self.logger.error("Failed to initialize Telegram client. Please authenticate in Dashboard Settings.")
                return False

            if not await self.telegram_handler.test_connection():
                self.logger.error("Telegram connection test failed.")
                return False

            # Pocket Option connection
            if self.pocket_handler.is_connected:
                self.logger.info("Pocket Option already connected - reusing open session.")
                self.alert = None
            elif self.pocket_handler._has_credentials():
                if await self.pocket_handler.connect():
                    self.logger.info("Pocket Option connected.")
                    self.alert = None
                else:
                    self.logger.error("Pocket Option connect failed. Check SSID in Settings.")
                    self.alert = "Pocket Option connect failed - check SSID in Settings."
            else:
                self.logger.warning("Pocket Option SSID not set. Open Settings -> Pocket Option Account and enter SSID.")
                self.alert = None

            self.running = True
            self.logger.info("Bot started - monitoring Telegram for trading signals.")

            # Notify server dashboard that bot is fully running
            try:
                from bot import server as _srv
                _srv._bot_starting = False
                _srv.socketio.emit("bot_status", {"status": "running", "running": True})
            except Exception:
                pass

            self._tasks.append(asyncio.create_task(self._health_monitor()))
            self._tasks.append(asyncio.create_task(self._balance_monitor()))
            await self.telegram_handler.start_monitoring(self._handle_signal)
            return True

        except KeyboardInterrupt:
            await self.shutdown()
            return True
        except Exception as e:
            msg = f"Critical error starting bot: {e}"
            self.alert = msg
            try:
                from bot import server as _srv
                _srv._bot_starting = False
                _srv._server_state["alert"] = msg
                _srv.socketio.emit("bot_status", {"status": "stopped", "running": False})
            except Exception:
                pass
            if self.logger:
                self.logger.error(msg, exc_info=True)
            else:
                print(msg)
            return False

    async def _handle_signal(self, signal):
        try:
            self.logger.info(f"Received signal: {signal['type']} for {signal.get('asset')}")
            if not self.pocket_handler or not self.pocket_handler.is_connected:
                self.logger.warning(
                    "Signal ignored - Pocket Option not connected. Connect via Settings -> Pocket Option Account."
                )
                return
            await self.telegram_handler.executor.schedule_signal(signal)
        except Exception as e:
            self.logger.error(f"Error handling signal: {e}", exc_info=True)

    async def _health_monitor(self):
        """Check Pocket Option connection periodically."""
        self.logger.info("Health monitor started")
        while self.running:
            interval = 60 if not self.pocket_handler.is_connected else 300
            await asyncio.sleep(interval)
            if not self.running:
                break
            try:
                if not self.pocket_handler.is_connected:
                    if self.pocket_handler._has_credentials():
                        self.logger.info("Health monitor: attempting Pocket Option reconnect...")
                        if await self.pocket_handler.connect():
                            self.logger.info("Health monitor: Pocket Option reconnected successfully")
                            self.alert = None
                        else:
                            self.logger.error("Health monitor: Pocket Option reconnect failed")
                else:
                    if not self.pocket_handler._driver_alive():
                        self.logger.warning("Health monitor: Pocket Option disconnected - reconnecting...")
                        self.alert = "Pocket Option disconnected - reconnecting..."
                        if await self.pocket_handler.connect():
                            self.logger.info("Health monitor: reconnected OK")
                            self.alert = None
                        else:
                            self.alert = "Pocket Option reconnect failed - check SSID in Settings"
                            self.logger.error(f"Health monitor: {self.alert}")
            except Exception as e:
                self.logger.error(f"Health monitor error: {e}")

    async def _balance_monitor(self):
        """Refresh balance periodically and pick up changes to config.json."""
        while self.running:
            await asyncio.sleep(10)
            if not self.running:
                break
            try:
                self.config.refresh_if_changed()
                if self.pocket_handler and self.pocket_handler.is_connected:
                    await self.pocket_handler.get_balance()
            except Exception as e:
                self.logger.debug(f"Balance monitor: {e}")

    async def shutdown(self):
        if self.running:
            self.running = False
            self.alert   = None
            for t in self._tasks:
                t.cancel()
            self._tasks.clear()

            if self.logger:
                self.logger.info("Shutting down trading bot...")

            if self.pocket_handler and self._owns_pocket:
                await self.pocket_handler.disconnect()

            if self.telegram_handler:
                await self.telegram_handler.disconnect()

            try:
                from bot import server as _srv
                _srv._bot_instance = None
            except Exception:
                pass

            if self.logger:
                self.logger.info("Bot shutdown complete.")

    def _setup_logging(self):
        from logging.handlers import RotatingFileHandler

        os.makedirs('logs', exist_ok=True)
        log_level = getattr(logging, self.config.logging.log_level.upper(), logging.INFO)
        log_file  = f"logs/{self.config.logging.log_file}"

        fmt_console = logging.Formatter('%(asctime)s | %(levelname)-8s | %(message)s', '%H:%M:%S')
        fmt_file    = logging.Formatter('%(asctime)s | %(name)s | %(levelname)s | %(message)s', '%Y-%m-%d %H:%M:%S')

        root = logging.getLogger()
        root.setLevel(log_level)
        root.handlers.clear()

        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt_console)
        console.setLevel(log_level)
        root.addHandler(console)

        fh = RotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
        fh.setFormatter(fmt_file)
        fh.setLevel(log_level)
        root.addHandler(fh)

        _silent_at_warning = [
            'websockets', 'websockets.client', 'websockets.server',
            'telethon.network', 'telethon.extensions', 'telethon.crypto',
            'werkzeug', 'asyncio', 'urllib3'
        ]
        for name in _silent_at_warning:
            logging.getLogger(name).setLevel(logging.WARNING)

        logging.getLogger('telethon').setLevel(max(log_level, logging.INFO))

        trade_log = logging.getLogger('trades')
        th = RotatingFileHandler(
            f"logs/trades_{datetime.now().strftime('%Y-%m-%d')}.log",
            maxBytes=5*1024*1024, backupCount=10, encoding='utf-8'
        )
        th.setFormatter(fmt_file)
        trade_log.addHandler(th)
        trade_log.setLevel(logging.INFO)

        sig_log = logging.getLogger('signals')
        sh = RotatingFileHandler(
            'logs/received_signals.log', maxBytes=5*1024*1024, backupCount=10, encoding='utf-8'
        )
        sh.setFormatter(fmt_file)
        sig_log.addHandler(sh)
        sig_log.setLevel(logging.INFO)

        logging.getLogger(__name__).info("Logging initialized - level: %s  file: %s", self.config.logging.log_level, log_file)


if __name__ == "__main__":
    from bot.server import start_server
    port = int(os.environ.get("PORT", 5000))
    start_server(host="0.0.0.0", port=port)
