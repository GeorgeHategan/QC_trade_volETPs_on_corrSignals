# region imports
from AlgorithmImports import *
from datetime import datetime, timedelta
import os
# endregion


class VolETPsCorrSignals(QCAlgorithm):
    """
    VIX Short Strategy based on COR1M vs COR3M correlation
    
    Strategy:
    - Loads correlation data from CSV files
    - Calculates RSI of (COR1M - COR3M) difference
    - Shorts VXX when RSI < 50 (oversold, reversal signal)
    - Closes short when VXX > 25 or RSI recovers
    """

    def initialize(self):
        self.set_start_date(2022, 3, 18)
        self.set_end_date(2022, 6, 30)
        self.set_cash(100000)
        
        # Load CSV data into memory - try multiple paths for local vs cloud
        paths_to_try = [
            "data/custom/cor1m.csv",
            "../data/custom/cor1m.csv", 
            "../../data/custom/cor1m.csv"
        ]
        
        self.cor1m_data = None
        self.cor3m_data = None
        
        for path_prefix in paths_to_try:
            cor1m_path = path_prefix.replace("cor1m.csv", "cor1m.csv")
            cor3m_path = path_prefix.replace("cor1m.csv", "cor3m.csv")
            
            self.debug(f"[INIT] Trying to load from: {path_prefix}")
            self.cor1m_data = self._load_csv_data(cor1m_path)
            self.cor3m_data = self._load_csv_data(cor3m_path)
            
            if self.cor1m_data and len(self.cor1m_data) > 0:
                self.debug(f"[INIT] ✓ Successfully loaded from: {path_prefix}")
                break
        
        if not self.cor1m_data or len(self.cor1m_data) == 0:
            self.debug(f"[INIT] ✗ FAILED to load CSV data from any path!")
            self.cor1m_data = {}
            self.cor3m_data = {}
        
        self.debug(f"[INIT] COR1M data: {len(self.cor1m_data)} rows")
        self.debug(f"[INIT] COR3M data: {len(self.cor3m_data)} rows")
        
        # Add trading instrument
        self.vxx = self.add_equity("VXX", Resolution.DAILY).symbol
        
        # Indicators
        self.rsi_diff = RelativeStrengthIndex(14)
        
        # Parameters
        self.rsi_threshold = 50
        self.vxx_close_threshold = 25
        self.position_size = 0.50
        
        # State tracking
        self.is_short = False
        self.entry_price = 0
        self.trade_count = 0
        self.last_cor1m = None
        self.last_cor3m = None
    
    def _load_csv_data(self, filepath):
        """Load CSV data from file into a dictionary indexed by date"""
        data = {}
        try:
            self.debug(f"[DATA LOAD] Attempting to load: {filepath}")
            
            if not os.path.exists(filepath):
                self.debug(f"[ERROR] File not found: {filepath}")
                self.debug(f"[DEBUG] Working directory: {os.getcwd()}")
                return data
            
            self.debug(f"[DATA LOAD] File found! Reading...")
            
            with open(filepath, 'r') as f:
                lines = f.readlines()
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
                        dt = datetime.fromisoformat(time_str.replace('Z', '+00:00'))
                        date_key = dt.date()
                        
                        close_price = float(parts[4])
                        data[date_key] = {
                            'time': dt,
                            'open': float(parts[1]),
                            'high': float(parts[2]),
                            'low': float(parts[3]),
                            'close': close_price
                        }
                        valid_rows += 1
                        
                        if valid_rows % 500 == 0:
                            self.debug(f"[DATA LOAD] Processed {valid_rows} rows...")
                    except Exception as e:
                        continue
                
                self.debug(f"[DATA LOAD] ✓ SUCCESS: {valid_rows} rows loaded")
                if len(data) > 0:
                    dates = sorted(data.keys())
                    self.debug(f"[DATA LOAD] Date range: {dates[0]} → {dates[-1]}")
        except Exception as e:
            self.debug(f"[ERROR] Failed to load {filepath}: {str(e)}")
        
        return data

    def on_data(self, data: Slice):
        """Main algorithm logic"""
        
        # Check if VXX data is available
        if not data.contains_key(self.vxx):
            return
        
        vxx_price = data[self.vxx].close
        current_date = self.time.date()
        
        # Get correlation data for today
        cor1m_value = None
        cor3m_value = None
        
        if current_date in self.cor1m_data:
            cor1m_value = self.cor1m_data[current_date]['close']
        if current_date in self.cor3m_data:
            cor3m_value = self.cor3m_data[current_date]['close']
        
        # If we don't have today's data, try to use the most recent available
        if cor1m_value is None or cor3m_value is None:
            # Find closest date
            for offset in range(1, 10):
                check_date = datetime(current_date.year, current_date.month, current_date.day) - timedelta(days=offset)
                check_date = check_date.date()
                if cor1m_value is None and check_date in self.cor1m_data:
                    cor1m_value = self.cor1m_data[check_date]['close']
                if cor3m_value is None and check_date in self.cor3m_data:
                    cor3m_value = self.cor3m_data[check_date]['close']
                if cor1m_value and cor3m_value:
                    break
        
        if cor1m_value is None or cor3m_value is None:
            self.debug(f"[ON_DATA] {self.time.date()}: ✗ MISSING - COR1M:{cor1m_value} COR3M:{cor3m_value}")
            return
        
        self.debug(f"[ON_DATA] {self.time.date()}: ✓ DATA ACCESSED - COR1M={cor1m_value:.2f} | COR3M={cor3m_value:.2f}")
        
        # Calculate difference and update RSI
        diff = cor1m_value - cor3m_value
        self.rsi_diff.update(self.time, diff)
        
        if not self.rsi_diff.is_ready:
            self.debug(f"[RSI WARMUP] {self.time.date()}: RSI initializing... Diff={diff:.4f}")
            return
        
        rsi_value = self.rsi_diff.current.value
        
        # Debug output
        self.debug(f"[SIGNAL] {self.time.date()}: COR1M={cor1m_value:.2f} | COR3M={cor3m_value:.2f} | Diff={diff:.4f} | RSI={rsi_value:.2f} | VXX={vxx_price:.2f} | Pos={'SHORT' if self.is_short else 'FLAT'}")
        
        # Entry: RSI oversold (< 50) - reversal signal to short vol
        if not self.is_short and rsi_value < self.rsi_threshold:
            self.set_holdings(self.vxx, -self.position_size)
            self.is_short = True
            self.entry_price = vxx_price
            self.trade_count += 1
            self.debug(f"[TRADE {self.trade_count}] ▼ ENTRY @ {self.time.date()}: SHORT VXX @ {vxx_price:.2f} | RSI={rsi_value:.2f}<{self.rsi_threshold} | Diff={diff:.4f}")
        
        # Exit: VXX rises above threshold or RSI recovers
        elif self.is_short and (vxx_price > self.vxx_close_threshold or rsi_value > 60):
            exit_reason = f"VXX={vxx_price:.2f}>{self.vxx_close_threshold}" if vxx_price > self.vxx_close_threshold else f"RSI={rsi_value:.2f}>60"
            self.liquidate(self.vxx)
            self.is_short = False
            profit = self.entry_price - vxx_price
            profit_pct = (profit / self.entry_price) * 100
            self.debug(f"[TRADE {self.trade_count}] ▲ EXIT @ {self.time.date()}: CLOSE @ {vxx_price:.2f} | {exit_reason} | P/L: {profit:.2f} ({profit_pct:.1f}%)")
    
    def on_end_of_algorithm(self):
        """Called at the end of the algorithm"""
        self.debug(f"\n{'='*90}")
        self.debug(f"[FINAL SUMMARY]")
        self.debug(f"[FINAL] Algorithm ended: {self.time.date()}")
        self.debug(f"[FINAL] Total trades executed: {self.trade_count}")
        self.debug(f"[FINAL] COR1M data points: {len(self.cor1m_data)} rows")
        self.debug(f"[FINAL] COR3M data points: {len(self.cor3m_data)} rows")
        self.debug(f"[FINAL] Portfolio value: ${self.portfolio.total_portfolio_value:.2f}")
        self.debug(f"[FINAL] Cash remaining: ${self.portfolio.cash:.2f}")
        self.debug(f"{'='*90}\n")
