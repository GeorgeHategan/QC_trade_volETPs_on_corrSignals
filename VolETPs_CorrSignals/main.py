# region imports
from AlgorithmImports import *
from datetime import datetime, timedelta
import os
# endregion

class CorrelationData(PythonData):
    """Custom data type for reading correlation data from CSV"""
    
    def get_source(self, config, date, is_live):
        # For cloud, we'll embed data directly - this won't be called in our implementation
        return SubscriptionDataSource("", SubscriptionTransportMedium.REST)
    
    def reader(self, config, line, date, is_live):
        return None


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
        
        # Load CSV data into memory
        self.cor1m_data = self._load_csv_data("data/custom/cor1m.csv")
        self.cor3m_data = self._load_csv_data("data/custom/cor3m.csv")
        
        self.debug(f"Loaded COR1M: {len(self.cor1m_data)} rows")
        self.debug(f"Loaded COR3M: {len(self.cor3m_data)} rows")
        
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
            if not os.path.exists(filepath):
                self.debug(f"WARNING: File not found: {filepath}")
                return data
            
            with open(filepath, 'r') as f:
                lines = f.readlines()
                for line in lines[1:]:  # Skip header
                    if not line.strip():
                        continue
                    parts = line.strip().split(',')
                    if len(parts) < 5:
                        continue
                    try:
                        time_str = parts[0].strip()
                        # Parse ISO8601 timestamp
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
                    except Exception as e:
                        continue
        except Exception as e:
            self.debug(f"Error loading {filepath}: {str(e)}")
        
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
            self.debug(f"{self.time.date()}: Missing correlation data")
            return
        
        # Calculate difference and update RSI
        diff = cor1m_value - cor3m_value
        self.rsi_diff.update(self.time, diff)
        
        if not self.rsi_diff.is_ready:
            self.debug(f"Warming up RSI... {self.time.date()}")
            return
        
        rsi_value = self.rsi_diff.current.value
        
        # Debug output
        self.debug(f"{self.time.date()}: COR1M={cor1m_value:.2f}, COR3M={cor3m_value:.2f}, Diff={diff:.4f}, RSI={rsi_value:.2f}, VXX={vxx_price:.2f}")
        
        # Entry: RSI oversold (< 50) - reversal signal to short vol
        if not self.is_short and rsi_value < self.rsi_threshold:
            self.set_holdings(self.vxx, -self.position_size)
            self.is_short = True
            self.entry_price = vxx_price
            self.trade_count += 1
            self.debug(f"TRADE #{self.trade_count} ENTRY: Short VXX at {vxx_price:.2f}, RSI={rsi_value:.2f}")
        
        # Exit: VXX rises above threshold or RSI recovers
        elif self.is_short and (vxx_price > self.vxx_close_threshold or rsi_value > 60):
            self.liquidate(self.vxx)
            self.is_short = False
            profit = self.entry_price - vxx_price
            profit_pct = (profit / self.entry_price) * 100
            self.debug(f"TRADE #{self.trade_count} EXIT: Close at {vxx_price:.2f}, P/L: {profit:.2f} ({profit_pct:.1f}%)")
    
    def on_end_of_algorithm(self):
        """Called at the end of the algorithm"""
        self.debug(f"Algorithm Complete. Total trades: {self.trade_count}, Final Portfolio: {self.portfolio.total_portfolio_value:.2f}")
