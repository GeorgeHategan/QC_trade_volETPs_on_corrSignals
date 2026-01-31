# region imports
from AlgorithmImports import *
from datetime import datetime, timedelta
# endregion

class CorrelationData(PythonData):
    """Custom data type for reading correlation data from CSV"""
    
    def get_source(self, config, date, is_live):
        # Read from local data folder by date
        if config.symbol.value == "COR1M":
            filename = "cor1m.csv"
        else:
            filename = "cor3m.csv"
        
        # Use file protocol for local files
        source = f"file://data/custom/{filename}"
        return SubscriptionDataSource(source, SubscriptionTransportMedium.REMOTE_FILE)
    
    def reader(self, config, line, date, is_live):
        # Skip header and empty lines
        if not line.strip():
            return None
        if line.startswith("time"):
            return None
        if not (line[0:1].isdigit() or line[0:1] == "2"):
            return None
        
        try:
            parts = line.strip().split(',')
            if len(parts) < 5:
                return None
            
            # Parse CSV: time,open,high,low,close
            time_str = parts[0].strip()
            data_time = datetime.fromisoformat(time_str.replace('Z', '+00:00'))
            
            corr = CorrelationData()
            corr.symbol = config.symbol
            corr.time = data_time
            corr.end_time = data_time + timedelta(hours=1)
            corr.value = float(parts[4])  # close price
            corr["open"] = float(parts[1])
            corr["high"] = float(parts[2])
            corr["low"] = float(parts[3])
            corr["close"] = float(parts[4])
            
            return corr
        except Exception as e:
            return None


class VolETPsCorrSignals(QCAlgorithm):
    """
    VIX Short Strategy based on COR1M vs COR3M correlation
    
    Strategy:
    - Calculates RSI of (COR1M - COR3M) difference
    - Shorts VXX when RSI crosses under 69 (overbought)
    - Closes short when VXX closes below 20
    """

    def initialize(self):
        self.set_start_date(2022, 3, 18)
        self.set_end_date(2022, 6, 30)
        self.set_cash(100000)
        
        # Parameters - relaxed for testing
        self.rsi_threshold = 50  # Lower threshold for more entries
        self.vxx_close_threshold = 25
        self.position_size = 0.50  # 50% for safety
        
        # Add VXX as trading symbol
        self.vxx = self.add_equity("VXX", Resolution.DAILY).symbol
        
        # Add custom correlation data
        self.cor1m = self.add_data(CorrelationData, "COR1M", Resolution.DAILY).symbol
        self.cor3m = self.add_data(CorrelationData, "COR3M", Resolution.DAILY).symbol
        
        # RSI indicator
        self.rsi_diff = RelativeStrengthIndex(14)
        
        # Track state
        self.is_short = False
        self.entry_price = 0
        self.data_count = 0
        self.trade_count = 0

    def on_data(self, data: Slice):
        """Main algorithm logic"""
        
        # Check if we have both correlation data points
        if not (data.contains_key(self.cor1m) and data.contains_key(self.cor3m) and data.contains_key(self.vxx)):
            return
        
        self.data_count += 1
        
        # Get current values
        cor1m_value = data[self.cor1m].close
        cor3m_value = data[self.cor3m].close
        vxx_price = data[self.vxx].close
        diff = cor1m_value - cor3m_value
        
        # Calculate RSI on the difference
        self.rsi_diff.update(self.time, diff)
        
        if not self.rsi_diff.is_ready:
            self.debug(f"Warming up... Data point {self.data_count}")
            return
        
        rsi_value = self.rsi_diff.current.value
        
        # Log for debugging
        self.debug(f"Time: {self.time}, COR1M: {cor1m_value:.2f}, COR3M: {cor3m_value:.2f}, Diff: {diff:.4f}, RSI: {rsi_value:.2f}, VXX: {vxx_price:.2f}")
        
        # Entry condition: RSI below threshold
        if not self.is_short and rsi_value < self.rsi_threshold:
            self.set_holdings(self.vxx, -self.position_size)
            self.is_short = True
            self.entry_price = vxx_price
            self.trade_count += 1
            self.debug(f"TRADE #{self.trade_count} SHORT ENTRY: VXX at {vxx_price:.2f}, RSI: {rsi_value:.2f}")
        
        # Exit condition: VXX closes above threshold
        elif self.is_short and vxx_price > self.vxx_close_threshold:
            self.liquidate(self.vxx)
            self.is_short = False
            profit = self.entry_price - vxx_price
            self.debug(f"TRADE #{self.trade_count} CLOSE: VXX at {vxx_price:.2f}, Profit: {profit:.2f}")
    
    def on_end_of_algorithm(self):
        """Called at the end of the algorithm"""
        self.debug(f"Algorithm Complete. Total trades: {self.trade_count}, Final Portfolio Value: {self.portfolio.total_portfolio_value:.2f}")
