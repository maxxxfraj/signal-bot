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
            # Об'єднуємо та вирівнюємо індекси (часові мітки)
            combined = pd.concat([target_returns, active_ret], axis=1).dropna()
            if len(combined) > 5:
                corr_val = combined.iloc[:, 0].corr(combined.iloc[:, 1])
                if not pd.isna(corr_val):
                    correlations.append(corr_val)
                    
        if not correlations:
            return 1.0, 0.0
            
        avg_corr = sum(correlations) / len(correlations)
        
        # Розрахунок штрафу за формулою безперервного згасання
        if avg_corr <= 0.4:
            cpf = 1.0
        else:
            # Лінійне згасання ризику від 100% до мінімум 15% за високої кореляції
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
        dobar_low: float = None,
        dobar_high: float = None
    ) -> dict:
        """
        Адаптує розмір позиції під волатильність і CPF-штраф.
        """
        actual_entry = entry
        is_averaged = False
        
        if use_dobar and dobar_low is not None and dobar_high is not None:
            dobar_price = (dobar_low + dobar_high) / 2.0
            actual_entry = (entry + dobar_price) / 2.0
            is_averaged = True

        stop_distance_pct = abs(actual_entry - stop_loss) / actual_entry
        if stop_distance_pct == 0:
            return {
                "risk_usd": 0.0,
                "pos_usd": 0.0,
                "pos_contracts": 0.0,
                "margin_required": 0.0,
                "is_averaged": is_averaged,
                "actual_entry": actual_entry
            }

        # Базовий розмір під загрозою (штрафується за допомогою CPF)
        risk_amount = portfolio_size * (risk_pct / 100.0) * cpf
        
        position_size_usd = risk_amount / stop_distance_pct
        position_size_contracts = position_size_usd / actual_entry
        margin_required = position_size_usd / leverage
        
        return {
            "risk_usd": round(risk_amount, 2),
            "pos_usd": round(position_size_usd, 2),
            "pos_contracts": round(position_size_contracts, 4),
            "margin_required": round(margin_required, 2),
            "is_averaged": is_averaged,
            "actual_entry": round(actual_entry, 6)
        }