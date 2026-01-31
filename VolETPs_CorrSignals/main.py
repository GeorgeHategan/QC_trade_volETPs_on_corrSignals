# region imports
from AlgorithmImports import *
from datetime import datetime, timedelta
# endregion

class CorrelationData(PythonData):
    """Custom data type for reading correlation data from CSV"""
    
    def get_source(self, config, date, is_live):
        # Read from local data folder
        if config.symbol.value == "COR1M":
            filename = "cor1m.csv"
        else:
            filename = "cor3m.csv"
        
        source = f"file://data/custom/{filename}"
        return SubscriptionDataSource(source, SubscriptionTransportMedium.REMOTE_FILE)
    
    def reader(self, config, line, date, is_live):
        if not (line.strip() and line[0:1].isdigit()):
            return None
        
        try:
            parts = line.split(',')
            
            # Parse CSV: time,open,high,low,close
            data_time = datetime.fromisoformat(parts[0].replace('Z', '+00:00'))
            
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
        except:
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
        
        # Parameters
        self.rsi_threshold = 69
        self.vxx_close_threshold = 20
        self.position_size = 0.90  # 90% of equity
        
        # Add VXX as trading symbol
        self.vxx = self.add_equity("VXX", Resolution.DAILY).symbol
        
        # Add custom correlation data
        self.cor1m = self.add_data(CorrelationData, "COR1M", Resolution.DAILY).symbol
        self.cor3m = self.add_data(CorrelationData, "COR3M", Resolution.DAILY).symbol
        
        # Initialize rolling windows
        self.cor1m_window = RollingWindow[float](14)
        self.cor3m_window = RollingWindow[float](14)
        self.diff_window = RollingWindow[float](14)
        
        # RSI indicator
        self.rsi_diff = RelativeStrengthIndex(14)
        
        # Track state
        self.is_short = False
        self.entry_price = 0

    def on_data(self, data: Slice):
        """Main algorithm logic"""
        
        # Check if we have both correlation data points
        if not (data.contains_key(self.cor1m) and data.contains_key(self.cor3m)):
            return
        
        # Get current values
        cor1m_value = data[self.cor1m].close
        cor3m_value = data[self.cor3m].close
        diff = cor1m_value - cor3m_value
        
        # Add to windows
        self.cor1m_window.add(cor1m_value)
        self.cor3m_window.add(cor3m_value)
        self.diff_window.add(diff)
        
        # Calculate RSI on the difference
        self.rsi_diff.update(self.time, diff)
        
        if not self.rsi_diff.is_ready:
            return
        
        rsi_value = self.rsi_diff.current.value
        
        # Log for debugging
        self.debug(f"COR1M: {cor1m_value:.2f}, COR3M: {cor3m_value:.2f}, Diff: {diff:.4f}, RSI: {rsi_value:.2f}, VXX: {data[self.vxx].close:.2f}")
        
        # Get VXX price
        if data.contains_key(self.vxx):
            vxx_price = data[self.vxx].close
            
            # Entry condition: RSI crosses under threshold
            if not self.is_short and rsi_value < self.rsi_threshold:
                self.set_holdings(self.vxx, -self.position_size)
                self.is_short = True
                self.entry_price = vxx_price
                self.debug(f"SHORT ENTRY: VXX at {vxx_price:.2f}, RSI: {rsi_value:.2f}")
            
            # Exit condition: VXX closes below threshold
            elif self.is_short and vxx_price < self.vxx_close_threshold:
                self.liquidate(self.vxx)
                self.is_short = False
                profit = self.entry_price - vxx_price
                self.debug(f"SHORT CLOSE: VXX at {vxx_price:.2f}, Profit: {profit:.2f}")
    
    def on_end_of_algorithm(self):
        """Called at the end of the algorithm"""
        self.debug(f"Algorithm Complete. Final Portfolio Value: {self.portfolio.total_portfolio_value:.2f}")
