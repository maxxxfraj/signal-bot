# regime_classifier.py
import numpy as np
import pandas as pd

class MarketRegimeClassifier:
    def __init__(self, er_period: int = 14, vol_period: int = 14, lookback_period: int = 200):
        self.er_period = er_period
        self.vol_period = vol_period
        self.lookback_period = lookback_period

    def classify(self, df: pd.DataFrame) -> dict:
        """
        Аналізує мікроструктуру та волатильність.
        Повертає словник з ідентифікованим режимом ринку та метриками.
        """
        if len(df) < self.lookback_period:
            return {"regime": "UNKNOWN", "er": 0.5, "z_vol": 0.0}
        
        # Перетворюємо в чисті numpy-масиви для максимальної швидкодії
        close = df['close'].astype(float).values
        high = df['high'].astype(float).values
        low = df['low'].astype(float).values
        
        # 1. Kaufman Efficiency Ratio (ER)
        change = np.abs(close - np.roll(close, self.er_period))
        diffs = np.abs(close - np.roll(close, 1))
        
        # Розрахунок ковзної суми шумів за допомогою pandas Series
        noise = pd.Series(diffs).rolling(window=self.er_period).sum().values
        noise[noise == 0] = 1e-6 # Захист від ділення на нуль
        
        er = change / noise
        er_current = er[-1]
        
        # 2. Normalized Volatility Ratio (NVR = ATR / Close)
        # Швидкий розрахунок True Range (TR)
        tr = np.maximum(
            high - low,
            np.maximum(
                np.abs(high - np.roll(close, 1)),
                np.abs(low - np.roll(close, 1))
            )
        )
        atr = pd.Series(tr).rolling(window=self.vol_period).mean().values
        ema_close = pd.Series(close).ewm(span=self.vol_period, adjust=False).mean().values
        
        nvr = atr / ema_close
        
        # 3. Z-Score на історичному вікні
        rolling_window = nvr[-self.lookback_period:]
        mean_vol = np.mean(rolling_window)
        std_vol = np.std(rolling_window)
        if std_vol == 0:
            std_vol = 1e-6
            
        z_vol = (nvr[-1] - mean_vol) / std_vol
        
        # 4. Дворівнева матриця класифікації режимів
        if z_vol < -1.0 and er_current < 0.3:
            regime = "LOW_VOL_FLAT"       # Накопичення, торгівля заборонена
        elif z_vol <= 1.0 and er_current < 0.45:
            regime = "MEAN_REVERSION"     # Боковик, підходить для контртренду
        elif z_vol > 0.5 and er_current >= 0.45 and z_vol <= 2.5:
            regime = "STABLE_TREND"       # Чистий тренд, підходить для EMA/MACD
        elif z_vol > 2.5:
            regime = "HIGH_VOL_CHAOS"     # Новинний шум / Паніка. Стоп-торгівля!
        else:
            regime = "TRANSITION"         # Перехідний стан
            
        return {
            "regime": regime,
            "er": float(er_current),
            "z_vol": float(z_vol)
        }