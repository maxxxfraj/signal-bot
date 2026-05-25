# risk_engine.py
import pandas as pd
import numpy as np

class PortfolioRiskEngine:
    @staticmethod
    def calculate_cpf(target_returns: pd.Series, active_positions_returns: list) -> tuple:
        """
        Розраховує Correlation Penalty Factor (CPF).
        Повертає (CPF, average_correlation).
        """
        if not active_positions_returns:
            return 1.0, 0.0
            
        correlations = []
        for active_ret in active_positions_returns:
            combined = pd.concat([target_returns, active_ret], axis=1).dropna()
            if len(combined) > 5:
                corr_val = combined.iloc[:, 0].corr(combined.iloc[:, 1])
                if not pd.isna(corr_val):
                    correlations.append(corr_val)
                    
        if not correlations:
            return 1.0, 0.0
            
        avg_corr = sum(correlations) / len(correlations)
        
        if avg_corr <= 0.4:
            cpf = 1.0
        else:
            cpf = max(0.15, 1.0 - (avg_corr - 0.4) / 0.5)
            
        return float(cpf), float(avg_corr)

    @staticmethod
    def calculate_position_size_v3(
        portfolio_size: float,
        risk_pct: float,
        leverage: int,
        entry: float,
        stop_loss: float,
        cpf: float,
        use_dobar: bool,
        er: float,
        strategy_type: str,
        dobar_low: float = None,
        dobar_high: float = None
    ) -> dict:
        """
        Обчислює об'єм позиції з урахуванням:
        1. Кореляційного штрафу (CPF)
        2. Динамічного режиму ринку (Kaufman ER) та типу стратегії (RMF)
        """
        # Визначаємо приналежність стратегії
        trend_strategies = ['ema_rsi', 'macd_cross', 'breakout', 'vol_spike']
        mean_reversion_strategies = ['bb_bounce', 'mean_reversion', 'wavetrend_bounce']
        
        # Розраховуємо коефіцієнт масштабування ризику за фазою ринку (Regime Multiplier Factor - RMF)
        rmf = 1.0  # за замовчуванням
        
        if strategy_type in trend_strategies:
            # Трендова стратегія: повний ризик при високій ефективності, половинний при середній
            if er >= 0.55:
                rmf = 1.0
            elif 0.45 <= er < 0.55:
                rmf = 0.5
                print(f"⚖️ [RISK SCALING] Середній тренд (ER={er:.2f}). Ризик трендової стратегії зменшено на 50% (RMF=0.5)")
            else:
                rmf = 0.2  # Мінімальний фолбек-ризик для безпеки
        elif strategy_type in mean_reversion_strategies:
            # Контртрендова стратегія: повний ризик у чистому боковику, половинний при наявності помірного тренду
            if er <= 0.35:
                rmf = 1.0
            elif 0.35 < er <= 0.45:
                rmf = 0.5
                print(f"⚖️ [RISK SCALING] Помірний тренд (ER={er:.2f}). Ризик контртрендової стратегії зменшено на 50% (RMF=0.5)")
            else:
                rmf = 0.0  # Блокуємо вхід проти сильного тренду
                
        actual_entry = entry
        is_averaged = False
        
        if use_dobar and dobar_low is not None and dobar_high is not None:
            dobar_price = (dobar_low + dobar_high) / 2.0
            actual_entry = (entry + dobar_price) / 2.0
            is_averaged = True

        stop_distance_pct = abs(actual_entry - stop_loss) / actual_entry
        if stop_distance_pct == 0 or rmf == 0:
            return {
                "risk_usd": 0.0,
                "pos_usd": 0.0,
                "pos_contracts": 0.0,
                "margin_required": 0.0,
                "is_averaged": is_averaged,
                "actual_entry": actual_entry,
                "rmf": rmf
            }

        # Фінальний ризик у USD з урахуванням CPF-штрафу та RMF-масштабування за волатильністю!
        risk_amount = portfolio_size * (risk_pct / 100.0) * cpf * rmf
        
        position_size_usd = risk_amount / stop_distance_pct
        position_size_contracts = position_size_usd / actual_entry
        margin_required = position_size_usd / leverage
        
        return {
            "risk_usd": round(risk_amount, 2),
            "pos_usd": round(position_size_usd, 2),
            "pos_contracts": round(position_size_contracts, 4),
            "margin_required": round(margin_required, 2),
            "is_averaged": is_averaged,
            "actual_entry": round(actual_entry, 6),
            "rmf": rmf
        }