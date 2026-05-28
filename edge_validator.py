# edge_validator.py
import numpy as np
import pandas as pd

class QuantitativeEdgeValidator:
    def __init__(self, target_oos_ratio: float = 0.30, fee_rate: float = 0.0004, slippage_pct: float = 0.0005):
        self.oos_ratio = target_oos_ratio
        self.fee_rate = fee_rate
        self.slippage = slippage_pct

    def split_data(self, df: pd.DataFrame) -> tuple:
        """Розділяє історичні дані на In-Sample (навчання) та Out-of-Sample (тестування)"""
        split_idx = int(len(df) * (1.0 - self.oos_ratio))
        df_in_sample = df.iloc[:split_idx].copy()
        df_out_of_sample = df.iloc[split_idx:].copy()
        return df_in_sample, df_out_of_sample

    def evaluate_edge(self, trades: list, min_trades: int = 15) -> dict:
        """
        Аналізує вибірку угод за допомогою класичної квантової статистики.
        Розраховує математичне сподівання (Expectancy) та t-статистику з динамічним лімітом угод.
        """
        if len(trades) < min_trades:
            return {
                "is_valid_edge": False,
                "reason": f"Недостатній статистичний розмір вибірки (всього {len(trades)} угод, потрібно мін. {min_trades})",
                "expectancy_pct": 0.0,
                "t_stat": 0.0,
                "profit_factor": 0.0,
                "sortino_ratio": 0.0
            }

        pnl_array = np.array([t['net_pnl'] for t in trades])
        
        # Математичне сподівання (середній чистий прибуток на одну угоду в %)
        mean_pnl = np.mean(pnl_array)
        std_pnl = np.std(pnl_array)
        if std_pnl == 0:
            std_pnl = 1e-6

        # Розрахунок t-статистики (критерій Стьюдента для нульової гіпотези)
        n_trades = len(trades)
        t_stat = mean_pnl / (std_pnl / np.sqrt(n_trades))

        # Розрахунок Profit Factor
        wins = pnl_array[pnl_array > 0]
        losses = pnl_array[pnl_array < 0]
        
        sum_wins = np.sum(wins) if len(wins) > 0 else 0.0
        sum_losses = np.abs(np.sum(losses)) if len(losses) > 0 else 1e-6
        profit_factor = sum_wins / sum_losses

        # Розрахунок Sortino Ratio (відношення середнього прибутку до волатильності збитків)
        downside_returns = pnl_array[pnl_array < 0]
        downside_std = np.std(downside_returns) if len(downside_returns) > 1 else 1e-6
        sortino_ratio = (mean_pnl / downside_std) * np.sqrt(252) if downside_std > 0 else 0.0

        # Критерії підтвердження реальної статистичної переваги (Edge)
        is_valid_edge = (t_stat >= 1.96) and (mean_pnl > 0.15) and (profit_factor >= 1.35)

        return {
            "is_valid_edge": is_valid_edge,
            "reason": "Математична перевага підтверджена" if is_valid_edge else "Низьке математичне сподівання або висока випадковість",
            "expectancy_pct": round(float(mean_pnl), 3),
            "t_stat": round(float(t_stat), 2),
            "profit_factor": round(float(profit_factor), 2),
            "sortino_ratio": round(float(sortino_ratio), 2),
            "total_trades": n_trades
        }