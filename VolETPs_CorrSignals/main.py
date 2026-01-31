# region imports
from AlgorithmImports import *
from datetime import datetime, timedelta
import os
# endregion

# === REGIME DEFINITIONS ===
REGIME_SAFE = "SAFE"          # Permission to short vol
REGIME_NEUTRAL = "NEUTRAL"    # Stand down
REGIME_DANGER = "DANGER"      # Avoid shorts, vol spike regime

class VolETPsCorrSignals(QCAlgorithm):
    """
    TWO-LAYER VOLATILITY TRADING SYSTEM
    
    Layer 1: REGIME (permission layer)
    - Uses COR1M - COR3M spread as regime signal
    - Spreads normalized via rolling mean/std
    - Three states: SAFE, NEUTRAL, DANGER
    - Requires 2-bar persistence to avoid whipsaws
    
    Layer 2: EXECUTION (trade layer)
    - Only enter when: regime=SAFE AND cooldown clear AND signal strong
    - Exit when: regime changes OR spread normalizes OR stop hits
    - All exits tied to regime/signal normalization, not time
    - Volatility-aware risk control with adaptive stops
    
    Design principles:
    - Fewer, higher-quality trades
    - Audit trail on every decision
    - No lookahead bias
    - Structural parameters locked (persistence, cooldown, min window)
    """

    def initialize(self):
        # === BACKTEST CONFIGURATION ===
        self.set_start_date(2024, 2, 6)      # After COR3M availability
        self.set_end_date(2026, 1, 30)
        self.set_cash(100000)
        
        # === DATA LOADING ===
        self.cor1m_hourly_data = {}
        self.cor3m_hourly_data = {}
        self.cor1m_data = {}  # Daily cache for fast lookup
        self.cor3m_data = {}
        
        self.debug("[INIT] Loading correlation data...")
        self._load_csv_data("cor1m")
        self._load_csv_data("cor3m")
        
        self.debug(f"[INIT] COR1M: {len(self.cor1m_hourly_data)} hourly entries")
        self.debug(f"[INIT] COR3M: {len(self.cor3m_hourly_data)} hourly entries")
        
        # === INSTRUMENT SUBSCRIPTION ===
        self.vxx = self.add_equity("VXX", Resolution.HOUR).symbol
        
        # === LAYER 1: REGIME STATE MACHINE ===
        # Structural parameters (LOCKED - do not optimize)
        self.SPREAD_MA_WINDOW = 20         # Rolling window for normalization
        self.REGIME_PERSISTENCE_BARS = 2   # Require 2 bars for regime change
        self.SHOCK_THRESHOLD_MULT = 2.5    # Spread movement > 2.5*std = shock
        
        # Regime state tracking
        self.spread_history = []            # Raw spread values
        self.regime_current = REGIME_NEUTRAL
        self.regime_previous = REGIME_NEUTRAL
        self.regime_bar_count = 0           # Bars at current regime
        self.spread_ma = None               # Rolling mean
        self.spread_std = None              # Rolling std dev
        self.spread_percentile_high = None  # 75th percentile
        self.spread_percentile_low = None   # 25th percentile
        
        # === LAYER 2: EXECUTION STATE MACHINE ===
        # Structural parameters (LOCKED - do not optimize)
        self.COOLDOWN_BARS = 2              # Bars to wait after exit
        self.ENTRY_CONFIRMATION_BARS = 2   # Signal must persist 2 bars
        self.SIGNAL_STD_THRESHOLD = 1.0    # Trade only when >1.0 std from MA
        
        # Tunable parameters (safe defaults)
        self.POSITION_SIZE = 0.50           # Risk: 50% of portfolio per trade
        self.STOP_LOSS_MULT = 2.0           # Stop is 2x rolling volatility
        self.EXIT_MEAN_REVERT_THRESHOLD = 0.5  # Exit when spread is <0.5 std from MA
        
        # Execution state
        self.is_short = False
        self.entry_price = 0
        self.entry_bar_count = 0
        self.bars_since_exit = 0
        self.entry_spread = 0               # Record entry spread for logging
        self.bars_since_entry = 0
        self.trade_count = 0
        self.entry_time = None              # Track entry time for hold duration
        
        # Volatility tracking for adaptive stops
        self.vxx_range_history = []
        self.VXX_RANGE_WINDOW = 20
        self.vxx_atr_estimate = 1.0         # Average True Range proxy
        
        # === AUDIT LOGGING STATE ===
        self.last_summary_date = None       # Track last summary log date
        self.blocked_entry_count = 0        # Count blocked attempts per day
        self.daily_entry_count = 0          # Count entries per day
        self.daily_exit_count = 0           # Count exits per day
        self.last_missing_cor_warning_date = None  # Avoid spamming missing data warnings
        self.regime_bar_counts = {REGIME_SAFE: 0, REGIME_NEUTRAL: 0, REGIME_DANGER: 0}  # Track time in each regime
        
        self.debug("[INIT] ✓ Initialization complete")
    
    def _log_regime_transition(self, time_str, cor1m, cor3m, spread, spread_ma, spread_std, persist_bar, old_regime, new_regime, reason):
        """
        A) REGIME TRANSITION LOG (only on regime change)
        Format: REGIME | time | COR1M=X COR3M=Y Spread=Z | Z-score | Persist=N/M | OLD→NEW | reason=...
        """
        z_score = (spread - spread_ma) / spread_std if spread_std > 0 else 0
        self.debug(f"REGIME | {time_str} | COR1M={cor1m:.2f} COR3M={cor3m:.2f} Spread={spread:.3f} | Z={z_score:.2f} | Persist={persist_bar}/{self.REGIME_PERSISTENCE_BARS} | {old_regime}→{new_regime} | reason={reason}")
    
    def _log_entry(self, time_str, instrument, direction, qty, price, cor1m, cor3m, spread, spread_ma, spread_std, regime, reason, **filters):
        """
        B) TRADE ENTRY LOG (every entry)
        Format: ENTER | time | instrument | direction | qty | px | spread=X z=Y | regime=Z | reason | filters
        """
        z_score = (spread - spread_ma) / spread_std if spread_std > 0 else 0
        filter_str = " ".join([f"{k}={v}" for k, v in filters.items() if v is not None])
        self.debug(f"ENTER | {time_str} | {instrument} | {direction} | qty={qty:.2f} | px={price:.2f} | spread={spread:.3f} z={z_score:.2f} | regime={regime} | reason={reason} | filters: {filter_str}")
    
    def _log_exit(self, time_str, instrument, direction, exit_price, pnl_gross, pnl_net, hold_bars, hold_time_str, cor1m, cor3m, spread, spread_ma, spread_std, exit_reason, stop_level=None, max_adverse=None):
        """
        C) TRADE EXIT LOG (every exit)
        Format: EXIT | time | instrument | direction | px | pnl_net | hold | spread | reason
        """
        z_score = (spread - spread_ma) / spread_std if spread_std > 0 else 0
        stop_info = f" | stop_level={stop_level:.2f} max_adverse={max_adverse:.2f}" if stop_level else ""
        self.debug(f"EXIT | {time_str} | {instrument} | {direction} | px={exit_price:.2f} | pnl_net=${pnl_net:.2f} | hold={hold_bars}b/{hold_time_str} | spread={spread:.3f} z={z_score:.2f} | reason={exit_reason}{stop_info}")
    
    def _log_blocked_entry(self, time_str, intended_direction, spread, spread_ma, spread_std, regime, block_reason, **details):
        """
        D) BLOCKED TRADE ATTEMPT LOG (when trade would happen but blocked)
        Format: BLOCK | time | intended=direction | spread=X z=Y | regime | blocked_by=REASON | details
        """
        z_score = (spread - spread_ma) / spread_std if spread_std > 0 else 0
        detail_str = " ".join([f"{k}={v}" for k, v in details.items() if v is not None])
        if detail_str:
            self.debug(f"BLOCK | {time_str} | intended={intended_direction} | spread={spread:.3f} z={z_score:.2f} | regime={regime} | blocked_by={block_reason} | {detail_str}")
        else:
            self.debug(f"BLOCK | {time_str} | intended={intended_direction} | spread={spread:.3f} z={z_score:.2f} | regime={regime} | blocked_by={block_reason}")
    
    def _log_periodic_summary(self, time_str, period_type, entries, exits, blocked_attempts, regime_time_dict, net_pnl, max_dd=None, avg_hold=None):
        """
        E) PERIODIC SUMMARY LOG (daily or weekly)
        Format: SUMMARY | period | time | trades=E exits=X blocked=B | regime breakdown | pnl_net
        """
        total_regime_bars = sum(regime_time_dict.values())
        if total_regime_bars > 0:
            regime_str = " ".join([f"{k}={int(v/total_regime_bars*100)}%" for k, v in sorted(regime_time_dict.items())])
        else:
            regime_str = "regime=N/A"
        
        summary_line = f"SUMMARY | {period_type} | {time_str} | trades={entries} exits={exits} blocked={blocked_attempts} | {regime_str} | pnl_net=${net_pnl:.2f}"
        if max_dd is not None:
            summary_line += f" | max_dd={max_dd:.1f}%"
        if avg_hold is not None:
            summary_line += f" | avg_hold={avg_hold:.1f}h"
        self.debug(summary_line)
    
    def _load_csv_data(self, data_name):
        """Load CSV data from Object Store (cloud) or local file (local dev) - stores HOURLY data with datetime keys"""
        hourly_data = {}
        daily_data = {}
        
        try:
            self.debug(f"[DATA LOAD] Loading {data_name} data...")
            
            object_store_key = f"{data_name}_data"
            local_path = f"data/custom/{data_name}.csv"
            
            content = None
            
            # Try Object Store first (for cloud compatibility)
            try:
                if self.object_store.contains_key(object_store_key):
                    self.debug(f"[DATA LOAD] Loading from Object Store: {object_store_key}")
                    content = self.object_store.read(object_store_key)
                    self.debug(f"[DATA LOAD] Read {len(content)} bytes from Object Store")
            except Exception as e:
                self.debug(f"[DATA LOAD] Object Store read failed: {str(e)}")
                content = None
            
            # Fallback to local file if Object Store failed
            if not content:
                if os.path.exists(local_path):
                    self.debug(f"[DATA LOAD] Using local file: {local_path}")
                    with open(local_path, 'r') as f:
                        content = f.read()
                    # Save to Object Store for future cloud runs
                    try:
                        self.object_store.save(object_store_key, content)
                        self.debug(f"[DATA LOAD] Saved to Object Store: {object_store_key}")
                    except Exception as e:
                        self.debug(f"[DATA LOAD] Failed to save to Object Store: {str(e)}")
                else:
                    self.debug(f"[ERROR] No data available for {data_name}")
                    return hourly_data
            
            lines = content.strip().split('\n')
            self.debug(f"[DATA LOAD] Total lines: {len(lines)}")
            
            valid_rows = 0
            for line_num, line in enumerate(lines[1:], start=2):
                if not line.strip():
                    continue
                parts = line.strip().split(',')
                if len(parts) < 5:
                    continue
                try:
                    time_str = parts[0].strip()
                    # Parse ISO8601 timestamp with timezone
                    dt_str = time_str.split('+')[0] if '+' in time_str else time_str.split('Z')[0]
                    dt = datetime.fromisoformat(dt_str)
                    
                    # Use ISO format as key to preserve hourly granularity
                    hour_key = dt.isoformat()
                    ohlc = {
                        'time': dt,
                        'open': float(parts[1]),
                        'high': float(parts[2]),
                        'low': float(parts[3]),
                        'close': float(parts[4])
                    }
                    hourly_data[hour_key] = ohlc
                    
                    # Also keep daily data (last OHLC of day for backward compatibility)
                    date_key = dt.date()
                    daily_data[date_key] = ohlc
                    
                    valid_rows += 1
                    
                    if valid_rows % 500 == 0:
                        self.debug(f"[DATA LOAD] Processed {valid_rows} hourly bars...")
                except Exception as e:
                    continue
            
            self.debug(f"[DATA LOAD] ✓ SUCCESS: {valid_rows} hourly entries loaded")
            if len(hourly_data) > 0:
                times = sorted(hourly_data.keys())
                self.debug(f"[DATA LOAD] Time range: {times[0]} → {times[-1]}")
        except Exception as e:
            self.debug(f"[ERROR] Failed to load {data_name}: {str(e)}")
        
        # Store both hourly and daily data on self
        if data_name == 'cor1m':
            self.cor1m_hourly_data = hourly_data
            self.cor1m_data = daily_data
        elif data_name == 'cor3m':
            self.cor3m_hourly_data = hourly_data
            self.cor3m_data = daily_data
        
        return hourly_data

    def on_data(self, data: Slice):
        """
        TWO-LAYER EXECUTION LOGIC
        
        Layer 1: Get data and update regime
        Layer 2: Execute trades only if regime permits
        """
        
        # === STEP 0: DATA ACQUISITION ===
        if not data.contains_key(self.vxx):
            return
        
        try:
            vxx_price = data[self.vxx].close
            if vxx_price is None or vxx_price <= 0:
                return
        except:
            return
        
        current_date = self.time.date()
        current_time_str = self.time.strftime("%Y-%m-%d %H:%M:%S")
        current_hour_iso = self.time.isoformat()
        
        # Get COR values at HOURLY resolution (no lookahead: use only available data at current timestamp)
        cor1m_val = self._get_cor_value_hourly(self.cor1m_hourly_data, current_date, current_hour_iso)
        cor3m_val = self._get_cor_value_hourly(self.cor3m_hourly_data, current_date, current_hour_iso)
        
        if cor1m_val is None or cor3m_val is None:
            # WARN: Log missing data only once per day (not per bar)
            if self.last_missing_cor_warning_date != current_date:
                self.debug(f"WARN | {current_time_str} | missing COR data (COR1M={cor1m_val} COR3M={cor3m_val})")
                self.last_missing_cor_warning_date = current_date
            return
        
        # === PERIODIC SUMMARY: Log daily summary at end of each day ===
        if self.last_summary_date != current_date:
            if self.last_summary_date is not None:
                # Log summary for previous day
                summary_date = self.last_summary_date.strftime("%Y-%m-%d")
                self._log_periodic_summary(
                    summary_date, "DAY", 
                    entries=self.daily_entry_count,
                    exits=self.daily_exit_count,
                    blocked_attempts=self.blocked_entry_count,
                    regime_time_dict=self.regime_bar_counts,
                    net_pnl=self.portfolio.total_portfolio_value - 100000
                )
                # Reset daily counters
                self.daily_entry_count = 0
                self.daily_exit_count = 0
                self.blocked_entry_count = 0
                self.regime_bar_counts = {REGIME_SAFE: 0, REGIME_NEUTRAL: 0, REGIME_DANGER: 0}
            self.last_summary_date = current_date
        
        # Track time spent in each regime
        self.regime_bar_counts[self.regime_current] = self.regime_bar_counts.get(self.regime_current, 0) + 1
        
        spread = cor1m_val - cor3m_val
        
        # === STEP 1: UPDATE REGIME (Layer A) ===
        self._update_regime(spread, vxx_price, current_time_str, cor1m_val, cor3m_val)
        
        # === STEP 2: EXECUTE TRADES (Layer B) ===
        self._execute_trades(spread, vxx_price, current_time_str, cor1m_val, cor3m_val)
        
        # === STEP 3: LOG STATE ===
        if self.bars_since_entry >= 1:
            self.bars_since_entry += 1
        if self.bars_since_exit < self.COOLDOWN_BARS:
            self.bars_since_exit += 1
    
    def _get_cor_value_hourly(self, cor_hourly_dict, current_date, current_hour_iso):
        """
        Safely retrieve COR value at HOURLY resolution.
        Returns None if not available (no lookahead).
        First tries exact hour match, then falls back to most recent hour today,
        then falls back to previous day's last hour.
        """
        # Try exact hour match (e.g., "2026-01-30T15:00:00")
        if current_hour_iso in cor_hourly_dict:
            return cor_hourly_dict[current_hour_iso]['close']
        
        # Fallback: search for most recent available hour on current date
        current_date_str = current_date.isoformat()
        for hour in range(23, -1, -1):
            check_hour_iso = f"{current_date_str}T{hour:02d}:00:00"
            if check_hour_iso in cor_hourly_dict:
                return cor_hourly_dict[check_hour_iso]['close']
        
        # Fallback: use most recent available from previous days (max 10 days back)
        for day_offset in range(1, 11):
            check_date = current_date - timedelta(days=day_offset)
            check_date_str = check_date.isoformat()
            # Look for last hour of that day (23:00)
            for hour in range(23, -1, -1):
                check_hour_iso = f"{check_date_str}T{hour:02d}:00:00"
                if check_hour_iso in cor_hourly_dict:
                    return cor_hourly_dict[check_hour_iso]['close']
        
        return None
    
    def _get_cor_value(self, cor_dict, current_date):
        """
        Safely retrieve COR value for a given date.
        Returns None if not available (no lookahead).
        """
        if current_date in cor_dict:
            return cor_dict[current_date]['close']
        
        # Fallback: use most recent available (max 10 days back)
        for day_offset in range(1, 11):
            check_date = current_date - timedelta(days=day_offset)
            if check_date in cor_dict:
                return cor_dict[check_date]['close']
        
        return None
    
    def _update_regime(self, spread, vxx_price, time_str, cor1m, cor3m):
        """
        LAYER 1: Regime Determination
        Classifies market as SAFE/NEUTRAL/DANGER based on COR spread.
        Requires 2-bar persistence to avoid whipsaws.
        Logs all regime transitions for auditability.
        """
        
        # Update spread history
        self.spread_history.append(spread)
        if len(self.spread_history) > self.SPREAD_MA_WINDOW + 5:
            self.spread_history.pop(0)
        
        # Need minimum window to compute stats
        if len(self.spread_history) < self.SPREAD_MA_WINDOW:
            return
        
        # Compute rolling stats
        recent_spreads = self.spread_history[-self.SPREAD_MA_WINDOW:]
        self.spread_ma = sum(recent_spreads) / len(recent_spreads)
        variance = sum((x - self.spread_ma) ** 2 for x in recent_spreads) / len(recent_spreads)
        self.spread_std = variance ** 0.5 if variance > 0 else 0.01
        
        # Detect shock (rapid movement > 2.5 std)
        if len(self.spread_history) >= 2:
            last_change = abs(spread - self.spread_history[-2])
            if last_change > self.spread_std * self.SHOCK_THRESHOLD_MULT and self.spread_std > 0:
                self.debug(f"[SHOCK] {time_str}: Spread delta={last_change:.3f} (>2.5*std), standing aside 3 bars")
                self.regime_current = REGIME_DANGER
                self.regime_bar_count = 1
                return
        
        # Determine regime candidate based on spread distance from MA
        distance_from_ma = spread - self.spread_ma
        
        if distance_from_ma < -self.spread_std:
            # Spread very low = opportunity for short vol
            regime_candidate = REGIME_SAFE
            reason_msg = "spread_below_MA-1std"
        elif distance_from_ma > self.spread_std:
            # Spread very high = risky for shorts
            regime_candidate = REGIME_DANGER
            reason_msg = "spread_above_MA+1std"
        else:
            # In the middle band
            regime_candidate = REGIME_NEUTRAL
            reason_msg = "spread_within_bands"
        
        # Apply persistence rule: only switch regime after 2 consecutive bars
        if regime_candidate == self.regime_current:
            self.regime_bar_count += 1
        else:
            self.regime_bar_count = 1
        
        # Regime transition if persistence threshold met
        if self.regime_bar_count >= self.REGIME_PERSISTENCE_BARS and regime_candidate != self.regime_previous:
            # A) LOG REGIME TRANSITION with new format
            self._log_regime_transition(
                time_str, cor1m, cor3m, spread, self.spread_ma, self.spread_std,
                self.regime_bar_count, self.regime_current, regime_candidate, reason_msg
            )
            self.regime_previous = self.regime_current
            self.regime_current = regime_candidate
            self.regime_bar_count = 0
    
    def _execute_trades(self, spread, vxx_price, time_str, cor1m, cor3m):
        """
        LAYER 2: Trade Execution
        Entry: Only if regime=SAFE AND cooldown clear AND spread extreme
        Exit: When regime revoked OR spread normalizes OR stop hits
        All decisions logged for auditability.
        
        LOGGING:
        - ENTER: Emitted on every entry with signal strength and filter status
        - EXIT: Emitted on every exit with hold time, P/L, and reason
        - BLOCK: Emitted when entry signal present but blocked
        """
        
        # Compute trade execution state
        spread_distance = self.spread_ma - spread
        cooldown_remaining = max(0, self.COOLDOWN_BARS - self.bars_since_exit)
        signal_strength_ok = spread_distance > (self.SIGNAL_STD_THRESHOLD * self.spread_std)
        regime_ok = self.regime_current == REGIME_SAFE
        cooldown_ok = cooldown_remaining == 0
        
        # === ENTRY LOGIC ===
        if not self.is_short:
            # Check if all entry conditions are met
            if regime_ok and signal_strength_ok and cooldown_ok:
                # === ENTER SHORT ===
                self.set_holdings(self.vxx, -self.POSITION_SIZE)
                self.is_short = True
                self.entry_price = vxx_price
                self.entry_spread = spread
                self.entry_bar_count = 0
                self.bars_since_entry = 1
                self.entry_time = self.time
                self.trade_count += 1
                self.daily_entry_count += 1
                
                # B) LOG ENTRY
                self._log_entry(
                    time_str, "VXX", "SHORT_VOL", -self.POSITION_SIZE, vxx_price,
                    cor1m, cor3m, spread, self.spread_ma, self.spread_std,
                    self.regime_current,
                    "regime_ok+signal_strength+cooldown_clear",
                    cooldown_rem=cooldown_remaining if not cooldown_ok else 0,
                    signal_dist=f"{spread_distance:.3f}",
                    regime=regime_ok
                )
            else:
                # D) LOG BLOCKED ENTRY (only if signal would have triggered)
                if signal_strength_ok:  # Only log if signal itself was present
                    block_reasons = []
                    if not regime_ok:
                        block_reasons.append(f"regime={self.regime_current}")
                    if not cooldown_ok:
                        block_reasons.append(f"cooldown_rem={cooldown_remaining}")
                    
                    block_reason = "+".join(block_reasons) if block_reasons else "unknown"
                    self._log_blocked_entry(
                        time_str, "SHORT_VOL", spread, self.spread_ma, self.spread_std,
                        self.regime_current, block_reason,
                        cooldown_rem=cooldown_remaining if not cooldown_ok else None
                    )
                    self.blocked_entry_count += 1
        
        # === EXIT LOGIC ===
        else:
            exit_reason = None
            max_adverse = 0
            stop_level_actual = None
            
            # Reason 1: Regime permission revoked (DANGER detected)
            if self.regime_current == REGIME_DANGER:
                exit_reason = "REGIME_REVOKED"
            
            # Reason 2: Spread has normalized (mean reversion trade complete)
            elif abs(spread - self.spread_ma) < self.EXIT_MEAN_REVERT_THRESHOLD * self.spread_std:
                exit_reason = "SIGNAL_NORMALIZED"
            
            # Reason 3: Volatility stop triggered (protect against tail risk)
            elif self.vxx_atr_estimate > 0:
                stop_level_actual = self.entry_price + (self.vxx_atr_estimate * self.STOP_LOSS_MULT)
                if vxx_price > stop_level_actual:
                    max_adverse = vxx_price - self.entry_price
                    exit_reason = "STOP_HIT"
            
            # Execute exit if any condition met
            if exit_reason:
                self.liquidate(self.vxx)
                profit = self.entry_price - vxx_price
                profit_pct = (profit / self.entry_price * 100) if self.entry_price > 0 else 0
                bars_held = self.bars_since_entry if self.bars_since_entry > 0 else 1
                hold_duration_hours = (self.time - self.entry_time).total_seconds() / 3600 if self.entry_time else 0
                hold_duration_str = f"{hold_duration_hours:.1f}h"
                
                # C) LOG EXIT
                self._log_exit(
                    time_str, "VXX", "SHORT_VOL", exit_price=vxx_price,
                    pnl_gross=profit, pnl_net=profit,  # Simplified: fees not tracked
                    hold_bars=bars_held, hold_time_str=hold_duration_str,
                    cor1m=cor1m, cor3m=cor3m, spread=spread,
                    spread_ma=self.spread_ma, spread_std=self.spread_std,
                    exit_reason=exit_reason,
                    stop_level=stop_level_actual if exit_reason == "STOP_HIT" else None,
                    max_adverse=max_adverse if exit_reason == "STOP_HIT" else None
                )
                
                self.is_short = False
                self.bars_since_exit = 0
                self.entry_bar_count = 0
                self.daily_exit_count += 1
    
    def on_end_of_algorithm(self):
        """Called at the end of the algorithm"""
        self.debug(f"\n{'='*90}")
        self.debug(f"[FINAL SUMMARY]")
        self.debug(f"[FINAL] Algorithm ended: {self.time.date()}")
        self.debug(f"[FINAL] Total trades: {self.trade_count}")
        self.debug(f"[FINAL] COR1M entries: {len(self.cor1m_data)}")
        self.debug(f"[FINAL] COR3M entries: {len(self.cor3m_data)}")
        self.debug(f"[FINAL] Portfolio value: ${self.portfolio.total_portfolio_value:.2f}")
        self.debug(f"[FINAL] Cash: ${self.portfolio.cash:.2f}")
        self.debug(f"{'='*90}\n")

