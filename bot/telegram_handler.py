"""
Telegram integration and signal parsing for Pocket Option Trading Bot.

Supports:
  - Multi-line emoji signals (e.g. Pocket Option standard):
      POCKET OPTION [M1]
      🔥 GBPAUD-OTC
      👑 M1
      🫡 15:23:07
      ❤️ call
  - Single-line / Multi-line standard & premium signals:
      EUR/USD-OTC UP (1 MIN)
      15:23:00 ENTRY TIME
      UTC +00:00
  - Multiple channels monitored simultaneously via telegram.channels in config.json.
"""

import asyncio
import logging
import re
import os
from pathlib import Path
import json
import unicodedata
from datetime import datetime, timedelta
import pytz
from typing import Optional, Dict, Any, List
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.utils import get_peer_id
from telethon.tl.types import Channel, Chat
from .pocket_handler import PocketHandler
from .config import Config, ChannelConfig


class SignalParser:
    """Parses incoming trade signals from Telegram messages."""

    def __init__(self, config: Optional[Config] = None):
        self.logger = logging.getLogger(__name__)
        self.config = config

    @staticmethod
    def _normalize(text: str) -> str:
        """Convert stylised Unicode letters/digits and strip exotic characters, flags & markdown markup."""
        norm = unicodedata.normalize('NFKC', text)
        # Strip regional indicator flag emojis [\U0001F1E6-\U0001F1FF]
        cleaned = re.sub(r'[\U0001F1E6-\U0001F1FF]', '', norm)
        # Strip markdown formatting characters that can split currency pairs (e.g. EUR**/GBP -> EUR/GBP)
        cleaned = re.sub(r'[*_~`#|]', '', cleaned)
        return cleaned

    def parse_signal(self, text: str) -> Optional[Dict[str, Any]]:
        """
        Parse a trade signal message.
        Supports:
          - Channel Signal format with 3 Martingale levels (e.g. from Lorenzo Free / VIP):
              🇪🇺 EUR/CHF 🇨🇭 OTC
              🕒 Expiration 5M
              ⚪ Entry at 13:05
              🟥 SELL

              🔽 Martingale levels
              1️⃣ level at 13:10
              2️⃣ level at 13:15
              3️⃣ level at 13:20
          - Emoji format with Martingale:
              🪙 AUD/NZD OTC
              ⏳ Expiration 5 minutes
              ✅ Entry at 01:30
              🔴 SELL
              Use martingale if necessary 👇
              1️⃣ MARTINGALE AT 01:35
              2️⃣ MARTINGALE AT 01:40
          - Multi-line standard emoji format (🔥 GBPAUD-OTC, 👑 M1, 🫡 15:23:07, ❤️ call)
          - Single-line / Multi-line standard & premium signals (EUR/USD-OTC UP (1 MIN), 15:23:00 ENTRY TIME)
        """
        if not text or not text.strip():
            return None

        norm = self._normalize(text)
        asset, direction, expiry, entry_time, timezone_str = None, None, '00:01:00', None, None
        martingale_steps: List[Dict[str, Any]] = []
        in_mg_block = False

        lines = [line.strip() for line in norm.split('\n') if line.strip()]

        for line in lines:
            clean_line = re.sub(r'^[^\w\s/]+', '', line).strip()
            if not clean_line:
                continue

            # Skip promotional links, win recap notices, and footer text
            if any(skip in clean_line.upper() for skip in [
                'CLICK HERE', 'HOW TO START', 'HOW TO TRADER', 'GET THIS SIGNAL',
                'OPEN THE BROKER', 'REGISTRATION', 'DIRECT VICTORY', 'DISCLAIMER', 'HTTP'
            ]):
                continue

            # Track Martingale section header (e.g. 🔽 Martingale levels)
            if 'MARTINGALE' in clean_line.upper():
                in_mg_block = True

            # Martingale step detection (e.g. 1️⃣ level at 13:10, 1 level at 07:00, 1️⃣ MARTINGALE AT 01:35)
            mg_m = re.search(
                r'(?:(\d+)\s*[-_.)]?)?\s*(?:st|nd|rd|th)?\s*(?:level|martingale|mg|step)\s*(?:(\d+))?\s*(?:at|:)?\s*(\d{1,2}:\d{2}(?::\d{2})?)',
                line,
                re.IGNORECASE,
            )
            if mg_m and mg_m.group(3):
                step = int(mg_m.group(1) or mg_m.group(2) or (len(martingale_steps) + 1))
                t = mg_m.group(3)
                t = t if t.count(':') == 2 else t + ':00'
                if not any(m['step'] == step for m in martingale_steps):
                    martingale_steps.append({'step': step, 'time': t})
                continue
            elif in_mg_block:
                fallback_mg = re.search(r'(\d+)\s*.*?(?:at|:)?\s*(\d{1,2}:\d{2}(?::\d{2})?)', line, re.IGNORECASE)
                if fallback_mg and fallback_mg.group(2):
                    step = int(fallback_mg.group(1) or (len(martingale_steps) + 1))
                    t = fallback_mg.group(2)
                    t = t if t.count(':') == 2 else t + ':00'
                    if not any(m['step'] == step for m in martingale_steps):
                        martingale_steps.append({'step': step, 'time': t})
                        continue

            # Asset check (e.g. EUR/CHF OTC, EUR/JPY OTC, EUR/JPY, AUD/NZD OTC, GBPAUD-OTC, EURUSD-OTC, GBP/USD, EURUSD)
            if not asset:
                pair_m = re.search(r'\b([A-Z]{3})\s*[/_\-\s]*([A-Z]{3})\b(?:\s*[-_/(]?\s*(OTC)\b)?', clean_line, re.IGNORECASE)
                if pair_m:
                    c1, c2 = pair_m.group(1).upper(), pair_m.group(2).upper()
                    if c1 not in ('POC', 'OPT', 'SIG', 'ENT', 'BRO', 'TEL', 'REG', 'USE', 'FOR', 'THE', 'GET', 'HOW', 'WIN') and c2 != 'OTC':
                        base_pair = c1 + c2
                        # Check strictly if OTC is specified on this pair line
                        is_otc = bool(pair_m.group(3) or re.search(r'\bOTC\b', clean_line, re.IGNORECASE))
                        if is_otc:
                            asset = base_pair + '-OTC'
                        else:
                            asset = base_pair

            # Duration / Expiry check (e.g. Expiration 5M, Expiration 5 minutes, M1, M5, 1 MIN, 5 MIN)
            if not expiry or expiry == '00:01:00':
                tf_m = re.search(r'(?:Expiration|Expiry|Timeframe)?\s*[:\-]?\s*(\d+)\s*(?:min|minute|minutes|m)\b', clean_line, re.IGNORECASE)
                if tf_m:
                    mins_total = int(tf_m.group(1))
                    h, m = divmod(mins_total, 60)
                    expiry = f"{h:02d}:{m:02d}:00"
                elif re.search(r'\bM(\d+)\b', clean_line, re.IGNORECASE):
                    tf_m2 = re.search(r'\bM(\d+)\b', clean_line, re.IGNORECASE)
                    mins_total = int(tf_m2.group(1))
                    h, m = divmod(mins_total, 60)
                    expiry = f"{h:02d}:{m:02d}:00"

            # Entry time check (e.g. ⚪ Entry at 13:05, Entry at 06:55, ✅ Entry at 01:30, 15:23:07, 15:23:00 ENTRY TIME)
            if not entry_time and not in_mg_block:
                ent_m = (
                    re.search(r'(?:Entry\s*(?:at|:)?\s*|Time\s*[:\-]?\s*)(\d{1,2}:\d{2}(?::\d{2})?)', clean_line, re.IGNORECASE)
                    or re.search(r'(\d{1,2}:\d{2}(?::\d{2})?)\s*ENTRY', clean_line, re.IGNORECASE)
                    or re.match(r'^(\d{1,2}:\d{2}(?::\d{2})?)$', clean_line)
                )
                if ent_m:
                    t = ent_m.group(1)
                    entry_time = t if t.count(':') == 2 else t + ':00'

            # Direction check (e.g. 🟥 SELL, 🟩 BUY, CALL, PUT)
            if not direction:
                if re.search(r'\b(CALL|UP|BUY)\b', clean_line, re.IGNORECASE) or any(e in line for e in ['🟢', '🟩', '⬆️']):
                    direction = 'call'
                elif re.search(r'\b(PUT|DOWN|SELL)\b', clean_line, re.IGNORECASE) or any(e in line for e in ['🔴', '🟥', '⬇️']):
                    direction = 'put'

            # Timezone check (e.g. UTC +5:30, UTC+0)
            if not timezone_str:
                tz_m = re.search(r'(UTC\s*[+-]\s*\d{1,2}:\d{2}|[+-]\d{1,2}:\d{2})', clean_line, re.IGNORECASE)
                if tz_m:
                    timezone_str = tz_m.group(1).replace(' ', '').upper()

        # Free-form fallback if not both found line-by-line
        if not (asset and direction):
            if not asset:
                pair_m = re.search(r'\b([A-Z]{3})\s*[/_\-\s]*([A-Z]{3})\b\s*(-?\s*OTC\b)?', norm)
                if pair_m:
                    c1, c2 = pair_m.group(1).upper(), pair_m.group(2).upper()
                    if c1 not in ('POC', 'OPT', 'SIG', 'ENT', 'BRO', 'TEL', 'REG'):
                        asset = c1 + c2
                        if pair_m.group(3):
                            asset += '-OTC'

            if not direction:
                if re.search(r'\b(UP|CALL|BUY)\b', norm, re.IGNORECASE) or any(e in norm for e in ['🟢', '🟩', '⬆️']):
                    direction = 'call'
                elif re.search(r'\b(DOWN|PUT|SELL)\b', norm, re.IGNORECASE) or any(e in norm for e in ['🔴', '🟥', '⬇️']):
                    direction = 'put'

        if asset and direction:
            default_tz = 'UTC-4'
            if self.config and hasattr(self.config, 'pocket_option'):
                default_tz = getattr(self.config.pocket_option, 'timezone', 'UTC-4')
            signal_tz = timezone_str or default_tz
            # Sort martingale steps chronologically
            martingale_steps.sort(key=lambda x: x['step'])
            signal = {
                'type':                 'trade',
                'asset':                asset,
                'direction':            direction,
                'expiry':               expiry,
                'entry_time':           entry_time,
                'martingale_steps':     martingale_steps,
                'max_martingale_steps': len(martingale_steps) if martingale_steps else None,
                'timezone':             signal_tz,
                'parsed_at':            datetime.now().isoformat(),
            }
            mg_summary = f"{len(martingale_steps)} levels ({', '.join(m['time'] for m in martingale_steps)})" if martingale_steps else "None"
            self.logger.info(
                f"[signal parsed] {asset} {direction.upper()} | entry: {entry_time} ({signal_tz}) | expiry: {expiry} | martingales: {mg_summary}"
            )
            return signal

        return None


class SignalExecutor:
    """Executes trade signals - time-scheduled or immediate."""

    def __init__(self, config: Config, pocket_handler: PocketHandler):
        self.config         = config
        self.pocket_handler = pocket_handler
        self.logger         = logging.getLogger(__name__)
        self.pending_signals: Dict[str, Any] = {}

    async def schedule_signal(self, signal: Dict[str, Any]) -> bool:
        try:
            if signal.get('type') == 'trade':
                sid = f"trade_{signal['asset']}_{signal['direction']}_{signal['parsed_at']}"
                self.pending_signals[sid] = {**signal, 'executed': False}
                asyncio.create_task(self._execute(sid))
                return True
            return False
        except Exception as e:
            self.logger.error(f"Error scheduling signal: {e}", exc_info=True)
            return False

    async def _execute(self, sid: str):
        try:
            signal = self.pending_signals.get(sid)
            if not signal or signal.get('executed'):
                return

            asset     = signal['asset']
            direction = signal['direction']
            expiry    = signal.get('expiry')

            # Pre-select / warm up
            try:
                await self.pocket_handler.select_asset(None, asset)
            except Exception as e:
                self.logger.error(f"Could not select asset {asset}: {e}")

            # Calculate wait time if entry time is specified
            wait_secs = None
            if signal.get('entry_time'):
                wait_secs = self._calc_wait(signal['entry_time'], signal.get('timezone'))
                grace = float(getattr(self.config.pocket_option, 'late_entry_grace_seconds', 5.0))
                if wait_secs is None:
                    self.logger.warning(
                        f"Could not calculate wait for {asset} at {signal['entry_time']}. Placing immediately."
                    )
                    wait_secs = 0.0
                elif wait_secs < -grace:
                    self.logger.warning(
                        f"Entry time {signal['entry_time']} already passed by {abs(wait_secs):.0f}s (grace: {grace:.0f}s) - skipping trade."
                    )
                    signal['executed'] = True
                    return

            # Pre-configure amount
            pre_configured_amount = None
            try:
                pre_configured_amount = await self.pocket_handler.pre_configure_trade(expiry)
            except Exception as e:
                self.logger.error(f"Pre-configure failed: {e}")

            # Wait until entry time minus early_entry_seconds
            if wait_secs is not None and wait_secs > 0:
                early = max(0, int(self.config.pocket_option.early_entry_seconds))
                sleep_time = max(0.0, wait_secs - early)
                if sleep_time > 0:
                    self.logger.info(
                        f"Pair {asset} queued. Waiting {sleep_time:.1f}s (entering {early}s before {signal['entry_time']})..."
                    )
                    await asyncio.sleep(sleep_time)

            # Execute trade
            signal['executed'] = True
            self.logger.info(f"Executing trade: {asset} {direction.upper()} expiry={expiry}")
            mg_times = [m['time'] for m in signal.get('martingale_steps', [])]
            await self.pocket_handler.perform_trade(
                symbol=asset,
                direction=direction,
                expiry=expiry,
                pre_configured_amount=pre_configured_amount,
                signal_martingale_steps=signal.get('max_martingale_steps'),
                martingale_times=mg_times if mg_times else None,
                timezone=signal.get('timezone'),
            )

        except Exception as e:
            self.logger.error(f"Error executing signal {sid}: {e}", exc_info=True)
        finally:
            self.pending_signals.pop(sid, None)

    @staticmethod
    def _resolve_tz(tz_str: Optional[str]):
        """Resolves timezone string (e.g. 'WAT', 'UTC+1', 'UTC +5:30', 'Africa/Lagos') to a pytz timezone."""
        if not tz_str or str(tz_str).strip().upper() in ('DEFAULT', 'LOCAL'):
            return pytz.FixedOffset(-240)
        clean = str(tz_str).strip().upper()
        if clean in ('WAT', 'WEST AFRICA TIME'):
            return pytz.timezone('Africa/Lagos')
        if clean in ('UTC', 'GMT', 'Z'):
            return pytz.UTC
        if clean.startswith('UTC'):
            clean = clean[3:].strip()
        if clean.startswith('GMT'):
            clean = clean[3:].strip()
        sign = -1 if clean.startswith('-') else 1
        clean = clean.lstrip('+-').strip()
        if ':' in clean:
            parts = clean.split(':')
            hours = int(parts[0])
            mins = int(parts[1]) if len(parts) > 1 else 0
        elif clean.isdigit():
            hours = int(clean)
            mins = 0
        else:
            try:
                return pytz.timezone(tz_str)
            except Exception:
                return pytz.timezone('Africa/Lagos')
        offset_mins = sign * (hours * 60 + mins)
        return pytz.FixedOffset(offset_mins)

    def _calc_wait(self, entry_time_str: str, tz_str: Optional[str]) -> Optional[float]:
        """Calculates seconds until the target entry time in WAT or signal timezone."""
        try:
            t = datetime.strptime(entry_time_str, "%H:%M:%S").time()
            target_tz = self._resolve_tz(tz_str or getattr(self.config.pocket_option, 'timezone', 'UTC-4'))
            now_tz = datetime.now(target_tz)
            entry = now_tz.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)
            delta = (entry - now_tz).total_seconds()

            if delta < -43200:
                delta += 86400

            return delta
        except Exception as e:
            self.logger.error(f"Could not calculate wait for '{entry_time_str}': {e}")
            return None


class TelegramHandler:
    """Complete Telegram integration - monitors multiple channels simultaneously."""

    def __init__(self, config: Config):
        self.config          = config
        self.logger          = logging.getLogger(__name__)
        self.client          = None
        self.parser          = SignalParser(self.config)
        self.executor        = None
        self.signal_callback = None
        self.session_string  = None
        self.api_id          = self.config.telegram.api_id
        self.api_hash        = self.config.telegram.api_hash
        self.phone_number    = None
        self.processed_message_ids = set()

    async def initialize(self, pocket_handler: PocketHandler) -> bool:
        try:
            self.logger.info("Initializing Telegram client...")
            self._load_session()

            if not self.session_string and not self.phone_number:
                self.logger.warning("No saved Telegram session found. Please connect via Settings in the Dashboard.")
                return False

            session = StringSession(self.session_string) if self.session_string else StringSession()
            self.client = TelegramClient(session, self.api_id, self.api_hash)

            if self.session_string:
                await self.client.connect()
                if not await self.client.is_user_authorized():
                    self.logger.warning("Telegram session expired or not authorized. Re-authentication required.")
                    return False
            else:
                await self.client.start(phone=self.phone_number)

            self.session_string = self.client.session.save()
            self._save_session()

            self.executor = SignalExecutor(self.config, pocket_handler)
            self.logger.info("Telegram client initialized successfully.")
            return True

        except Exception as e:
            self.logger.error(f"Failed to initialize Telegram client: {e}", exc_info=True)
            return False

    async def test_connection(self) -> bool:
        try:
            if not self.client or not await self.client.is_user_authorized():
                return False
            me = await self.client.get_me()
            self.logger.info(f"Telegram connected as: {getattr(me, 'first_name', 'User')} (@{getattr(me, 'username', '')})")
            await self.log_available_groups()
            return True
        except Exception as e:
            self.logger.error(f"Telegram test connection failed: {e}")
            return False

    async def log_available_groups(self):
        """Fetch and log all available channels, supergroups, and groups with their IDs to console."""
        try:
            self.logger.info("Fetching available Telegram channels and groups...")
            dialogs = await self.client.get_dialogs(limit=None)
            groups = []
            for d in dialogs:
                if d.is_channel or d.is_group:
                    entity = d.entity
                    title = getattr(entity, 'title', '') or getattr(entity, 'first_name', 'Unnamed')
                    peer_id = get_peer_id(entity)
                    if getattr(entity, 'broadcast', False):
                        gtype = "Channel"
                    elif getattr(entity, 'megagroup', False):
                        gtype = "Supergroup"
                    else:
                        gtype = "Group"
                    groups.append((gtype, title, peer_id))

            if groups:
                self.logger.info("=" * 65)
                self.logger.info("  AVAILABLE TELEGRAM GROUPS & CHANNELS:")
                for gtype, title, pid in groups:
                    self.logger.info(f"  • [{gtype.upper()}] \"{title}\"  -->  ID: {pid}")
                self.logger.info("=" * 65)
            else:
                self.logger.info("No Telegram channels or groups found in this account.")
        except Exception as e:
            self.logger.warning(f"Could not list Telegram groups/channels: {e}")

    def _load_session(self):
        try:
            env_session = os.getenv("TELEGRAM_SESSION_STRING") or os.getenv("SESSION_STRING")
            if env_session and env_session.strip():
                self.session_string = env_session.strip()
                self.logger.info("Telegram session loaded from environment variable.")
                return

            session_file_name = f"{self.config.telegram.session_name}.json"
            candidates = [
                Path(session_file_name),
                Path(__file__).resolve().parent.parent / session_file_name,
            ]
            for p in candidates:
                if p.exists():
                    with open(p, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    self.session_string = data.get('session_string')
                    self.phone_number   = data.get('phone_number')
                    self.logger.info(f"Telegram session loaded from {p.name}.")
                    return
        except Exception as e:
            self.logger.debug(f"Could not load session: {e}")

    def _save_session(self):
        try:
            session_file = Path(__file__).resolve().parent.parent / f"{self.config.telegram.session_name}.json"
            with open(session_file, 'w', encoding='utf-8') as f:
                json.dump({
                    'session_string': self.session_string,
                    'phone_number':   self.phone_number,
                    'saved_at':       datetime.now().isoformat(),
                }, f)
            self.logger.info("Telegram session saved.")
        except Exception as e:
            self.logger.error(f"Could not save session: {e}")

    async def _resolve_channel(self, identifier):
        """Resolve a channel identifier (name, @username, or numeric ID) to a Telethon entity."""
        is_plain = isinstance(identifier, str) and ' ' in identifier.strip()

        if not is_plain:
            try:
                return await self.client.get_entity(identifier)
            except Exception:
                pass
            if isinstance(identifier, str) and identifier.strip().lstrip('-').isdigit():
                try:
                    return await self.client.get_entity(int(identifier.strip()))
                except Exception:
                    pass

        dialogs = await self.client.get_dialogs(limit=None)
        id_str = str(identifier).strip()
        id_lower = id_str.lower()
        id_norm = unicodedata.normalize('NFKC', id_str).lower()
        id_clean = re.sub(r'[^\w\s]', '', id_norm).strip()

        # Pass 1: exact or Unicode normalized match
        for d in dialogs:
            title = getattr(d.entity, 'title', '') or getattr(d.entity, 'first_name', '') or ''
            t_str = title.strip()
            if t_str.lower() == id_lower or unicodedata.normalize('NFKC', t_str).lower() == id_norm:
                self.logger.info(f"Found '{identifier}' (ID: {get_peer_id(d.entity)})")
                return d.entity

        # Pass 2: clean alphanumeric match (ignoring emojis/decorations)
        if id_clean:
            for d in dialogs:
                title = getattr(d.entity, 'title', '') or getattr(d.entity, 'first_name', '') or ''
                t_clean = re.sub(r'[^\w\s]', '', unicodedata.normalize('NFKC', title)).strip().lower()
                if t_clean and (t_clean == id_clean or id_clean in t_clean or t_clean in id_clean):
                    self.logger.info(f"Found '{identifier}' matching '{title}' (ID: {get_peer_id(d.entity)})")
                    return d.entity

        raise ValueError(f"Could not find channel/group '{identifier}'.")

    async def start_monitoring(self, signal_callback):
        try:
            if not self.client:
                raise Exception("Telegram client not initialized")

            self.signal_callback = signal_callback

            enabled: List[ChannelConfig] = [
                c for c in self.config.telegram.channels if c.enabled and str(c.identifier).strip()
            ]
            if not enabled:
                self.logger.error("No channels enabled. Configure and enable channels in Settings.")
                return

            watch_ids: List[int] = []

            for ch in enabled:
                try:
                    entity = await self._resolve_channel(ch.identifier)
                    peer_id = get_peer_id(entity)
                    for pid in (entity.id, peer_id):
                        if pid not in watch_ids:
                            watch_ids.append(pid)
                    title = getattr(entity, 'title', ch.identifier)
                    self.logger.info(f"Watching channel/group: {title} (ID: {peer_id})")
                except Exception as e:
                    self.logger.error(f"Could not resolve channel '{ch.identifier}': {e}")

            if not watch_ids:
                self.logger.error("No channels could be resolved. Check telegram.channels in config.")
                return

            @self.client.on(events.NewMessage(chats=watch_ids))
            async def on_message(event):
                await self._handle_message(event)

            self.logger.info("Now monitoring Telegram channels for incoming trade signals...")
            await self.client.run_until_disconnected()

        except Exception as e:
            self.logger.error(f"Error monitoring channels: {e}", exc_info=True)

    async def _handle_message(self, event):
        msg = event.message
        if not msg or not msg.text:
            return

        if msg.id in self.processed_message_ids:
            self.logger.debug(f"Ignoring already processed message ID: {msg.id}")
            return
        self.processed_message_ids.add(msg.id)

        self.logger.info(f"New Telegram message (ID: {msg.id}):\n{msg.text.strip()}")
        signal = self.parser.parse_signal(msg.text)
        if signal and self.signal_callback:
            await self.signal_callback(signal)

    async def disconnect(self):
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
        self.logger.info("Telegram client disconnected.")
