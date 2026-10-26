"""
Pocket Option trading handler using pocketoptionapi_async.

Credentials resolved in this priority order:
  1. Environment variable: POCKET_OPTION_SSID
  2. config.json -> pocket_option.ssid (set via Settings dashboard)
"""

import asyncio
import os
import json
import logging
import time
from datetime import datetime
from typing import Optional, Any, List, Dict
from .config import Config

try:
    from pocketoptionapi_async import AsyncPocketOptionClient, OrderDirection
except ImportError:
    AsyncPocketOptionClient = None
    OrderDirection = None


class PocketHandler:
    def __init__(self, config: Config):
        self.config = config
        self.logger = logging.getLogger(__name__)
        self.client: Optional[Any] = None
        self.is_connected = False
        self.last_trade_time = None
        self.daily_trades = 0
        self.daily_loss = 0.0          # realized drawdown today (opening balance - current)
        self.wins = 0                  # settled winning trades today
        self.losses = 0                # settled losing trades today
        self.daily_pnl = 0.0           # realized profit/loss today
        self.active_trades = 0
        self._trade_date = None
        self._day_start_balance: Optional[float] = None   # balance snapshot at 00:00
        self.balance: Optional[float] = None              # last known account balance (dashboard)
        self.last_trade: Optional[dict] = None            # last placed trade + result (dashboard)
        self.last_place_latency: float = 0.0

        # Market assets cache and broker error tracking
        self.assets_info: dict = {}
        self._last_order_errors: dict = {}

        # Martingale progression that PERSISTS across signals/cycles:
        self._mg_amount: Optional[float] = None   # carried amount for next cycle's first trade
        self._mg_losses: int = 0                  # consecutive losses in current progression
        self.driver = None  # kept for interface compatibility with SignalExecutor

    # =========================================================================
    #  CONNECTION
    # =========================================================================

    async def connect(self, **kwargs) -> bool:
        """Connects to Pocket Option WebSocket API using SSID."""
        if AsyncPocketOptionClient is None:
            self.logger.error("pocketoptionapi_async is not installed.")
            return False

        ssid = os.getenv("POCKET_OPTION_SSID") or self.config.pocket_option.ssid
        if not ssid:
            self.logger.error(
                "Pocket Option SSID missing. "
                "Enter it in Settings -> Pocket Option Account, or set POCKET_OPTION_SSID env var."
            )
            return False

        is_demo = (self.config.trading.account_type.lower() == "demo") or self.config.pocket_option.is_demo

        try:
            self.logger.info(f"Connecting to Pocket Option (demo={is_demo})...")
            self.client = AsyncPocketOptionClient(ssid=ssid, is_demo=is_demo)
            if hasattr(self.client, '_websocket'):
                self.client._websocket.add_event_handler("json_data", self._on_raw_json_data)
            await self.client.connect()

            # Wait briefly to confirm handshake settled
            for _ in range(10):
                if getattr(self.client, 'is_connected', False):
                    break
                await asyncio.sleep(0.5)

            if not getattr(self.client, 'is_connected', False):
                self.logger.error("Pocket Option WebSocket connection failed (check SSID validity).")
                self.is_connected = False
                return False

            self.is_connected = True
            self.logger.info("Successfully connected to Pocket Option.")

            try:
                balance = await self.get_balance()
                self.logger.info(f"Connected. Balance: ${balance:.2f}")
            except Exception as e:
                self.logger.warning(f"Could not read balance immediately: {e}")

            await self._refresh_balance_stats()
            return True

        except Exception as e:
            self.logger.error(f"Error connecting to Pocket Option: {e}", exc_info=True)
            self.is_connected = False
            return False

    def _has_credentials(self) -> bool:
        """Return True if SSID is available."""
        ssid = os.getenv("POCKET_OPTION_SSID") or self.config.pocket_option.ssid
        return bool(ssid and ssid.strip())

    def _driver_alive(self) -> bool:
        """Return True if connected."""
        return self.is_connected and self.client is not None and getattr(self.client, 'is_connected', False)

    async def disconnect(self):
        """Disconnects from the Pocket Option API."""
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
        self.is_connected = False
        self.logger.info("Pocket Option disconnected.")

    async def get_balance(self) -> float:
        """Fetch current account balance as a float."""
        if not self.client:
            return self.balance or 0.0
        try:
            bal_obj = await self.client.get_balance()
            val = getattr(bal_obj, 'balance', None)
            if val is not None:
                self.balance = float(val)
            else:
                self.balance = float(bal_obj or 0.0)
            return self.balance
        except Exception as e:
            self.logger.warning(f"Error fetching balance: {e}")
            return self.balance or 0.0

    # =========================================================================
    #  ASSET SELECTION & PRE-CONFIGURE
    # =========================================================================

    async def _on_raw_json_data(self, data):
        """Intercepts raw broker messages: assets catalog and rejection notices."""
        try:
            if isinstance(data, list) and len(data) > 10 and isinstance(data[0], list):
                count = 0
                for item in data:
                    if len(item) > 14:
                        sym = str(item[1])
                        info = {
                            "id": item[0],
                            "symbol": sym,
                            "name": str(item[2]),
                            "payout": item[5],
                            "is_open": bool(item[14]),
                        }
                        self.assets_info[sym] = info
                        clean = sym.replace("#", "")
                        self.assets_info[clean] = info
                        self.assets_info[clean.lower()] = info
                        self.assets_info[sym.lower()] = info
                        count += 1
                open_cnt = sum(1 for a in self.assets_info.values() if a.get("is_open"))
                self.logger.info(f"Loaded {count} Pocket Option assets ({open_cnt} open pairs).")
            elif isinstance(data, dict) and "error" in data:
                err = data.get("error")
                asset = data.get("asset")
                if asset:
                    self._last_order_errors[str(asset)] = str(err)
                    self._last_order_errors[str(asset).lower()] = str(err)
                    self.logger.warning(f"Pocket Option broker notice for {asset}: {err}")
        except Exception as e:
            self.logger.debug(f"Error handling broker JSON: {e}")

    def is_asset_open(self, asset: str) -> Optional[bool]:
        """Returns True if open, False if closed, None if unknown/not yet loaded."""
        if not self.assets_info:
            return None
        formatted = self._format_asset(asset)
        clean = formatted.replace("#", "")
        info = (
            self.assets_info.get(formatted)
            or self.assets_info.get(clean)
            or self.assets_info.get(clean.lower())
            or self.assets_info.get(formatted.lower())
        )
        if info:
            return info.get("is_open", False)
        return None

    async def select_asset(self, _driver, asset: str):
        """Pre-warm asset info and verify market status."""
        formatted = self._format_asset(asset)
        is_open = self.is_asset_open(formatted)
        if is_open is False:
            self.logger.warning(
                f"Asset selected: {formatted} - NOTE: Market is currently CLOSED on Pocket Option."
            )
        else:
            payout_str = ""
            info = (
                self.assets_info.get(formatted)
                or self.assets_info.get(formatted.lower())
                or self.assets_info.get(formatted.replace('#', ''))
                or self.assets_info.get(formatted.replace('#', '').lower())
            ) if self.assets_info else None
            if info and "payout" in info:
                payout_str = f" (payout: {info['payout']}%)"
            self.logger.info(f"Asset selected: {formatted}{payout_str}")

    async def pre_configure_trade(self, expiry: Optional[str] = None) -> float:
        """Called immediately after asset selection for timed signals."""
        if self.config.trading.martingale_enabled and self._mg_amount:
            amount = self._mg_amount
        else:
            amount = await self._calc_amount()
        self.logger.info(
            f"Pre-configured: ${amount:.2f} | expiry: {expiry or '00:01:00'} - ready, waiting for entry time."
        )
        return amount

    # =========================================================================
    #  HELPERS & SIZING
    # =========================================================================

    async def _calc_amount(self) -> float:
        """Calculate BASE trade amount: percent of balance, or fixed dollars."""
        if self.config.trading.risk_mode == "percent":
            try:
                balance = await self.get_balance()
                if balance > 0:
                    amount = round(balance * (self.config.trading.risk_amount / 100), 2)
                    amount = max(1.0, amount)
                    self.logger.info(
                        f"Balance: ${balance:.2f} | {self.config.trading.risk_amount}% = ${amount:.2f}"
                    )
                    return amount
            except Exception:
                pass
            self.logger.warning("Could not read balance - using fixed risk_amount.")

        return max(1.0, float(self.config.trading.risk_amount))

    @staticmethod
    def _format_asset(asset: str) -> str:
        """
        Convert asset name to Pocket Option format:
          EURUSD -> EURUSD
          EUR/USD -> EURUSD
          GBPAUD-OTC -> GBPAUD_otc
          EURUSD_OTC -> EURUSD_otc
        """
        clean = asset.replace('/', '').replace(' ', '').upper()
        if clean.endswith("-OTC"):
            return clean[:-4] + "_otc"
        if clean.endswith("_OTC"):
            return clean[:-4] + "_otc"
        return clean

    @staticmethod
    def _expiry_to_seconds(expiry: str) -> int:
        """Convert '00:01:00', 'M1', or '60' to seconds."""
        try:
            if not expiry:
                return 60
            exp = str(expiry).strip().upper()
            if exp.startswith("M") and exp[1:].isdigit():
                return int(exp[1:]) * 60
            if ":" in exp:
                parts = exp.split(":")
                if len(parts) == 3:
                    return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                if len(parts) == 2:
                    return int(parts[0]) * 60 + int(parts[1])
            return int(exp)
        except Exception:
            return 60

    def _reset_daily_counters_if_new_day(self):
        today = datetime.now().date()
        if self._trade_date != today:
            self.daily_trades       = 0
            self.daily_loss         = 0.0
            self.wins               = 0
            self.losses             = 0
            self.daily_pnl          = 0.0
            self._day_start_balance = None
            self._trade_date        = today

    async def _refresh_balance_stats(self):
        """Snapshot opening balance and update daily realized P&L and drawdown."""
        self._reset_daily_counters_if_new_day()
        try:
            balance = await self.get_balance()
        except Exception as e:
            self.logger.warning(f"Could not read balance for stats: {e}")
            return

        if balance is None:
            return

        if self._day_start_balance is None:
            self._day_start_balance = balance
            self.logger.info(f"Opening balance for {self._trade_date}: ${balance:.2f}")

        gross = balance - self._day_start_balance
        if self.config.trading.risk_mode == "percent" and self._day_start_balance:
            self.daily_pnl = round(gross / self._day_start_balance * 100, 2)
        else:
            self.daily_pnl = round(gross, 2)
        self.daily_loss = max(0.0, -self.daily_pnl)

    # =========================================================================
    #  TRADE EXECUTION & SETTLEMENT
    # =========================================================================

    async def _get_trade_result(self, order_id: Any, duration_secs: int):
        """
        Uses check_win() to wait for trade completion message.
        Returns (won, profit): won is True/False/None.
        """
        try:
            wait_time = max(30.0, float(duration_secs) + 20.0)
            res = await self.client.check_win(str(order_id), max_wait_time=wait_time)
            if not res:
                return None, 0.0

            result_str = res.get("result", "").lower()
            profit = float(res.get("profit") or 0.0)

            won = True if result_str == "win" else False if result_str in ["loss", "lose"] else None
            self.logger.info(f"Trade {order_id} result: {result_str} | profit=${profit:.2f}")
            return won, profit
        except Exception as e:
            self.logger.error(f"Could not read trade result: {e}")
            return None, 0.0

    def _record_result(self, won: Optional[bool], profit: float):
        self._reset_daily_counters_if_new_day()
        if won is True:
            self.wins += 1
        elif won is False:
            self.losses += 1
        if self.last_trade is not None:
            self.last_trade["result"] = "win" if won is True else "loss" if won is False else "unknown"
            self.last_trade["profit"] = round(profit, 2)
        self.logger.info(
            f"Stats: {self.wins}W / {self.losses}L | last trade profit: ${profit:.2f}"
        )

    async def _settle_and_record(self, order_id: Any, duration_secs: int):
        won, profit = await self._get_trade_result(order_id, duration_secs)
        self._record_result(won, profit)
        await self._refresh_balance_stats()

    def _mg_reset(self):
        self._mg_amount = None
        self._mg_losses = 0

    async def _place_one(
        self,
        asset_name: str,
        direction: str,
        period_secs: int,
        amount: float,
        expiry: Optional[str],
        wait_result: bool,
        step_label: str = "Base Trade",
    ):
        """Places a single order on Pocket Option."""
        self.logger.info(
            f"Placing trade ({step_label}): {asset_name} {direction.upper()} ${amount:.2f} duration: {period_secs}s"
        )

        # Pre-check market open status
        is_open = self.is_asset_open(asset_name)
        if is_open is False:
            self.logger.warning(
                f"Trade SKIPPED: {asset_name} market is currently CLOSED on Pocket Option (IncorrectOpenTime)."
            )
            return False, None

        dir_enum = OrderDirection.CALL if direction.lower() == 'call' else OrderDirection.PUT
        start_time = time.time()

        # Clear any prior rejection error for this asset
        self._last_order_errors.pop(str(asset_name), None)
        self._last_order_errors.pop(str(asset_name).lower(), None)

        try:
            place_task = asyncio.create_task(
                self.client.place_order(
                    asset=asset_name,
                    amount=amount,
                    direction=dir_enum,
                    duration=period_secs,
                )
            )

            order = None
            max_wait = 25.0
            elapsed = 0.0
            while elapsed < max_wait:
                if place_task.done():
                    order = place_task.result()
                    break

                err = self._last_order_errors.get(str(asset_name)) or self._last_order_errors.get(str(asset_name).lower())
                if err:
                    self.logger.error(f"Trade rejected by Pocket Option for {asset_name}: {err}")
                    place_task.cancel()
                    return False, None

                await asyncio.sleep(0.2)
                elapsed += 0.2

            if not place_task.done():
                place_task.cancel()
                self.logger.error(f"Order placement timed out for {asset_name} within {max_wait:.0f}s.")
                return False, None

        except Exception as e:
            self.logger.error(f"Error placing order for {asset_name}: {e}")
            return False, None

        if not order:
            self.logger.error(f"Trade rejected by Pocket Option for {asset_name}.")
            return False, None

        latency = time.time() - start_time
        order_id = getattr(order, 'order_id', None)
        self.last_trade_time = datetime.now()
        self.daily_trades += 1

        self.last_trade = {
            "asset":     asset_name,
            "direction": direction,
            "amount":    amount,
            "duration":  period_secs,
            "latency":   round(latency, 2),
            "at":        datetime.now().strftime("%H:%M:%S"),
            "result":    None,
            "profit":    None,
            "step":      step_label,
        }

        self.logger.info(
            f"Trade #{self.daily_trades} placed ({step_label}) - {asset_name} {direction.upper()} "
            f"${amount:.2f} | order_id={order_id} | latency={latency:.2f}s | Daily: {self.daily_trades}/{self.config.trading.max_daily_trades}"
        )

        trade_log_msg = {
            'asset': asset_name,
            'direction': direction,
            'amount': amount,
            'expiry': expiry,
            'order_id': str(order_id),
            'step': step_label,
        }
        logging.getLogger('trades').info(f"POCKET_OPTION_TRADE_EXECUTED | {json.dumps(trade_log_msg)}")

        if not order_id:
            return True, None

        if wait_result:
            won, profit = await self._get_trade_result(order_id, period_secs)
            self._record_result(won, profit)
            await self._refresh_balance_stats()
            return True, won

        asyncio.ensure_future(self._settle_and_record(order_id, period_secs))
        return True, None

    # =========================================================================
    #  MAIN TRADE FLOW
    # =========================================================================

    async def perform_trade(
        self,
        symbol: str,
        direction: str,
        expiry: Optional[str] = None,
        pre_configured_amount: Optional[float] = None,
        signal_martingale_steps: Optional[int] = None,
        martingale_times: Optional[List[str]] = None,
        timezone: Optional[str] = None,
    ) -> bool:
        if not self.is_connected or not self.client:
            self.logger.error("PocketHandler not connected.")
            return False

        await self._refresh_balance_stats()
        loss_unit = "%" if self.config.trading.risk_mode == "percent" else "$"

        if self.daily_trades >= self.config.trading.max_daily_trades:
            self.logger.warning(
                f"Daily trade limit reached ({self.config.trading.max_daily_trades}). Skipping."
            )
            return False

        if (self.config.trading.max_daily_loss_enabled and
                self.daily_loss >= self.config.trading.max_daily_loss):
            self.logger.warning(
                f"Daily loss limit reached ({self.daily_loss:.2f}{loss_unit} >= {self.config.trading.max_daily_loss:.2f}{loss_unit}). Skipping."
            )
            return False

        if self.active_trades >= self.config.trading.max_concurrent_trades:
            self.logger.warning(
                f"Max concurrent trades reached ({self.config.trading.max_concurrent_trades}). Skipping."
            )
            return False

        asset_name = self._format_asset(symbol)
        self.active_trades += 1

        if self.last_trade_time is not None:
            elapsed = (datetime.now() - self.last_trade_time).total_seconds()
            cooldown = self.config.pocket_option.wait_between_trades - elapsed
            if cooldown > 0:
                self.logger.info(f"Cooldown: waiting {cooldown:.1f}s...")
                await asyncio.sleep(cooldown)

        try:
            base_amount = (
                pre_configured_amount
                if pre_configured_amount is not None
                else await self._calc_amount()
            )
            period_secs = self._expiry_to_seconds(expiry or "00:01:00")

            # ── Check if martingale should run (from config OR signal) ──
            has_signal_mg = (signal_martingale_steps is not None and signal_martingale_steps > 0)
            use_martingale = self.config.trading.martingale_enabled or has_signal_mg
            if not use_martingale:
                placed, _ = await self._place_one(
                    asset_name, direction, period_secs, base_amount, expiry,
                    wait_result=False, step_label="Base Trade",
                )
                return placed

            # ── Auto-Martingale: sequence confined to THIS pair & signal ──
            # Determine recovery steps from signal or configuration
            if signal_martingale_steps is not None and signal_martingale_steps > 0:
                recovery_steps = signal_martingale_steps
            else:
                recovery_steps = max(0, int(self.config.trading.martingale_steps))

            mult = float(self.config.trading.martingale_multiplier)

            # Step 0: Place Base Trade
            placed, won = await self._place_one(
                asset_name, direction, period_secs, base_amount, expiry,
                wait_result=True, step_label="Base Trade",
            )
            if not placed:
                return False

            if won is True:
                self.logger.info(
                    f"Base trade WIN at ${base_amount:.2f} — profit secured, no martingale needed."
                )
                return True

            if won is None:
                self.logger.warning(
                    "Base trade result unknown — ending sequence to protect capital."
                )
                return True

            # Step 1..N: Martingale recoveries (e.g. 1️⃣ at 13:10, 2️⃣ at 13:15, 3️⃣ at 13:20)
            current_amount = base_amount
            for step in range(1, recovery_steps + 1):
                if self.daily_trades >= self.config.trading.max_daily_trades:
                    self.logger.warning(
                        f"Martingale: daily trade limit reached ({self.daily_trades}) — recovery stopped."
                    )
                    return True

                if (self.config.trading.max_daily_loss_enabled and
                        self.daily_loss >= self.config.trading.max_daily_loss):
                    self.logger.warning(
                        f"Martingale: daily loss limit reached ({self.daily_loss:.2f}) — recovery stopped."
                    )
                    return True

                current_amount = round(current_amount * mult, 2)
                step_name = f"Martingale {step}/{recovery_steps}"
                target_time_str = (
                    f" (scheduled {martingale_times[step - 1]})"
                    if martingale_times and len(martingale_times) >= step
                    else ""
                )

                # If scheduled martingale time is provided, wait until target time minus early_entry_seconds
                if martingale_times and len(martingale_times) >= step:
                    target_time = martingale_times[step - 1]
                    try:
                        from bot.telegram_handler import SignalExecutor
                        tz = SignalExecutor._resolve_tz(timezone or getattr(self.config.pocket_option, 'timezone', 'UTC-4'))
                        t_target = datetime.strptime(target_time, "%H:%M:%S").time()
                        now_tz = datetime.now(tz)
                        target_dt = now_tz.replace(hour=t_target.hour, minute=t_target.minute, second=t_target.second, microsecond=0)
                        delta = (target_dt - now_tz).total_seconds()
                        if delta < -43200:
                            delta += 86400
                        early = max(0.0, float(getattr(self.config.pocket_option, 'early_entry_seconds', 1.0)))
                        sleep_time = max(0.0, delta - early)
                        if sleep_time > 0:
                            self.logger.info(
                                f"{step_name}: waiting {sleep_time:.1f}s until scheduled time {target_time} (entering {early:.1f}s early)..."
                            )
                            await asyncio.sleep(sleep_time)
                    except Exception as e:
                        self.logger.debug(f"Scheduled martingale wait error: {e}")

                self.logger.info(
                    f"Martingale Step {step}/{recovery_steps}{target_time_str}: LOSS on previous trade — "
                    f"re-entering {asset_name} {direction.upper()} at ${current_amount:.2f}..."
                )

                placed, won = await self._place_one(
                    asset_name, direction, period_secs, current_amount, expiry,
                    wait_result=True, step_label=step_name,
                )
                if not placed:
                    self.logger.warning(f"Could not place {step_name} order. Sequence stopped.")
                    return True

                if won is True:
                    self.logger.info(
                        f"{step_name} WIN at ${current_amount:.2f} — loss recovered! Sequence complete."
                    )
                    return True

                if won is None:
                    self.logger.warning(
                        f"{step_name} result unknown — ending sequence."
                    )
                    return True

            self.logger.warning(
                f"All {recovery_steps} Martingale recovery step(s) completed on {asset_name}. "
                f"Sequence finished. Next signal starts fresh at base ${base_amount:.2f}."
            )
            return True

        finally:
            self.active_trades = max(0, self.active_trades - 1)
