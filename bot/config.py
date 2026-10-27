"""
Configuration management for the Pocket Option Telegram Trading Bot
"""

import json
import os
from pathlib import Path
from dataclasses import dataclass, field
import logging
from typing import Optional, Union, List


@dataclass
class ChannelConfig:
    """Configuration for a single monitored Telegram channel."""
    enabled: bool = True
    identifier: Union[str, int] = ""   # group name, @username, or numeric ID


@dataclass
class PocketOptionConfig:
    """Pocket Option platform configuration"""
    ssid: str = ""
    is_demo: bool = True
    wait_between_trades: int = 10
    early_entry_seconds: float = 1.0   # place the trade this many seconds before signal entry time
    late_entry_grace_seconds: float = 5.0  # drop trade if entry time missed by more than this seconds
    timezone: str = "UTC-4"               # Default timezone for signals (e.g. UTC-4)


@dataclass
class TelegramConfig:
    """Telegram configuration"""
    api_id: Optional[int] = None
    api_hash: str = ""
    session_name: str = "pocket_bot_session"
    channels: List[ChannelConfig] = field(default_factory=list)


@dataclass
class TradingConfig:
    """Trading configuration"""
    account_type: str = "demo"          # "demo" or "live"
    risk_mode: str = "fixed"            # "fixed" (dollar amount) or "percent" (% of balance)
    risk_amount: float = 1.0            # Dollar amount OR percentage value (e.g. 5 = 5%)
    max_daily_trades: int = 100
    max_daily_loss: float = 50.0
    max_daily_loss_enabled: bool = False  # when False, the daily-loss limit is not enforced
    max_concurrent_trades: int = 5
    martingale_enabled: bool = False
    martingale_multiplier: float = 2.0
    martingale_steps: int = 3           # trades per cycle on the SAME pair
    martingale_cycles: int = 3          # cycles the doubling continues across signals


@dataclass
class LoggingConfig:
    """Logging configuration"""
    log_level: str = "INFO"
    log_file: str = "pocket_bot.log"


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("BOT_DATA_DIR", str(BASE_DIR))).resolve()
DEFAULT_CONFIG_FILE = DATA_DIR / "config.json"


class Config:
    """Main configuration class"""

    def __init__(self, config_file: Optional[str] = None):
        requested = Path(config_file).resolve() if config_file else DEFAULT_CONFIG_FILE
        # Existing main.py may pass the repository's config.json explicitly.
        # Redirect that path to the persistent disk without changing main.py.
        if DATA_DIR != BASE_DIR and requested == (BASE_DIR / "config.json"):
            requested = DEFAULT_CONFIG_FILE
        self.config_file = str(requested)
        self.logger = logging.getLogger(__name__)
        self._load_config()

    def _load_config(self):
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    config_data = json.load(f)

                po_data = config_data.get('pocket_option', {})
                self.pocket_option = PocketOptionConfig(
                    ssid=os.getenv("POCKET_OPTION_SSID") or po_data.get('ssid', ''),
                    is_demo=po_data.get('is_demo', True),
                    early_entry_seconds=float(po_data.get('early_entry_seconds', 1.0)),
                    late_entry_grace_seconds=float(po_data.get('late_entry_grace_seconds', 5.0)),
                    timezone=str(os.getenv("TIMEZONE") or po_data.get('timezone', 'UTC-4')),
                )

                tg_data = config_data.get('telegram', {})
                channels = []
                for c in tg_data.get('channels', []):
                    if isinstance(c, dict):
                        ident = c.get('identifier', '')
                        if ident:
                            channels.append(ChannelConfig(
                                enabled=bool(c.get('enabled', True)),
                                identifier=ident,
                            ))
                    elif isinstance(c, (str, int)) and str(c).strip():
                        channels.append(ChannelConfig(
                            enabled=True,
                            identifier=c,
                        ))

                # Environment variable fallback (useful for Railway / cloud deployment)
                if not channels:
                    env_channels = os.getenv("TELEGRAM_CHANNELS") or os.getenv("TELEGRAM_CHANNEL")
                    if env_channels:
                        for ch in env_channels.split(","):
                            ch = ch.strip()
                            if ch:
                                channels.append(ChannelConfig(enabled=True, identifier=ch))

                api_id_env = os.getenv("TELEGRAM_API_ID")
                api_id_val = tg_data.get('api_id')
                if api_id_env and api_id_env.isdigit():
                    api_id_val = int(api_id_env)

                self.telegram = TelegramConfig(
                    api_id=api_id_val,
                    api_hash=os.getenv("TELEGRAM_API_HASH") or tg_data.get('api_hash', ''),
                    session_name=tg_data.get('session_name', 'pocket_bot_session'),
                    channels=channels,
                )

                tr_data = config_data.get('trading', {})
                self.trading = TradingConfig(
                    account_type=tr_data.get('account_type', 'demo'),
                    risk_mode=tr_data.get('risk_mode', 'fixed'),
                    risk_amount=float(tr_data.get('risk_amount', 1.0)),
                    max_daily_trades=int(tr_data.get('max_daily_trades', 100)),
                    max_daily_loss=float(tr_data.get('max_daily_loss', 50.0)),
                    max_daily_loss_enabled=bool(tr_data.get('max_daily_loss_enabled', False)),
                    max_concurrent_trades=int(tr_data.get('max_concurrent_trades', 5)),
                    martingale_enabled=bool(tr_data.get('martingale_enabled', False)),
                    martingale_multiplier=float(tr_data.get('martingale_multiplier', 2.0)),
                    martingale_steps=int(tr_data.get('martingale_steps', 3)),
                    martingale_cycles=int(tr_data.get('martingale_cycles', 3)),
                )

                self.logging = LoggingConfig(**config_data.get('logging', {}))
                self.logger.info(f"Configuration loaded from {self.config_file}")
            else:
                self.logger.warning(f"Config file not found: {self.config_file}. Initializing with empty settings.")
                self._init_empty_config()

        except Exception as e:
            self.logger.error(f"Error loading configuration from {self.config_file}: {e}")
            if not hasattr(self, 'telegram'):
                self._init_empty_config()

    def _init_empty_config(self):
        """Initializes empty configuration structures in memory without overwriting config.json."""
        self.pocket_option = PocketOptionConfig(
            ssid=os.getenv("POCKET_OPTION_SSID", ""),
            timezone=os.getenv("TIMEZONE", "UTC-4"),
        )

        api_id_env = os.getenv("TELEGRAM_API_ID")
        api_id_val = int(api_id_env) if api_id_env and api_id_env.isdigit() else None

        channels = []
        env_channels = os.getenv("TELEGRAM_CHANNELS") or os.getenv("TELEGRAM_CHANNEL")
        if env_channels:
            for ch in env_channels.split(","):
                ch = ch.strip()
                if ch:
                    channels.append(ChannelConfig(enabled=True, identifier=ch))

        self.telegram = TelegramConfig(
            api_id=api_id_val,
            api_hash=os.getenv("TELEGRAM_API_HASH", ""),
            session_name="pocket_bot_session",
            channels=channels,
        )
        self.trading = TradingConfig()
        self.logging = LoggingConfig()

    def save_config(self):
        try:
            config_data = {
                'pocket_option': {
                    'ssid':                self.pocket_option.ssid,
                    'is_demo':             self.pocket_option.is_demo,
                    'wait_between_trades': self.pocket_option.wait_between_trades,
                    'early_entry_seconds': self.pocket_option.early_entry_seconds,
                    'late_entry_grace_seconds': self.pocket_option.late_entry_grace_seconds,
                    'timezone':            getattr(self.pocket_option, 'timezone', 'UTC-4'),
                },
                'telegram': {
                    'api_id':          self.telegram.api_id,
                    'api_hash':        self.telegram.api_hash,
                    'session_name':    self.telegram.session_name,
                    'channels': [
                        {
                            'enabled':    c.enabled,
                            'identifier': c.identifier,
                        }
                        for c in self.telegram.channels
                    ],
                },
                'trading': {
                    'account_type':          self.trading.account_type,
                    'risk_mode':             self.trading.risk_mode,
                    'risk_amount':           self.trading.risk_amount,
                    'max_daily_trades':      self.trading.max_daily_trades,
                    'max_daily_loss':        self.trading.max_daily_loss,
                    'max_daily_loss_enabled': self.trading.max_daily_loss_enabled,
                    'max_concurrent_trades': self.trading.max_concurrent_trades,
                    'martingale_enabled':    self.trading.martingale_enabled,
                    'martingale_multiplier': self.trading.martingale_multiplier,
                    'martingale_steps':      self.trading.martingale_steps,
                    'martingale_cycles':     self.trading.martingale_cycles,
                },
                'logging': self.logging.__dict__,
            }
            import tempfile
            target = Path(self.config_file)
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, delete=False) as f:
                temporary = Path(f.name)
                json.dump(config_data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
            self.logger.info(f"Configuration saved to {self.config_file}")
        except Exception as e:
            self.logger.error(f"Error saving configuration: {e}")

    def validate(self) -> bool:
        errors = []
        if self.telegram.api_id is None:
            errors.append("Telegram API ID is required")
        if not self.telegram.api_hash:
            errors.append("Telegram API Hash is required")
        enabled = [c for c in self.telegram.channels if c.enabled]
        if not enabled:
            errors.append("At least one channel must be enabled in telegram.channels")
        for error in errors:
            self.logger.error(f"Configuration error: {error}")
        return len(errors) == 0

    def _stamp(self) -> Optional[float]:
        try:
            return os.path.getmtime(self.config_file)
        except OSError:
            return None

    def refresh_if_changed(self) -> bool:
        """Reload configuration if file modified on disk."""
        stamp = self._stamp()
        if stamp is None or stamp == getattr(self, "_stamp_seen", None):
            return False
        self._load_config()
        self._stamp_seen = stamp
        return True

