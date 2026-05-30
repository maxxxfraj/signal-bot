# database.py
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timezone
from dotenv import load_dotenv
import numpy as np
import html

# Імпортуємо налаштування та утиліти приведення типів з settings.py на самому початку
from settings import get_setting, to_native_float, to_native_int

DATABASE_URL = os.getenv("DATABASE_URL")

def get_connection():
    load_dotenv()
    database_url = os.getenv("DATABASE_URL")
    
    if not database_url:
        raise ValueError(
            "❌ КРИТИЧНА ПОМИЛКА: Не знайдено змінну DATABASE_URL!\n"
            "Переконайтеся, що ви прописали її у .env та Render."
        )
        
    conn = psycopg2.connect(database_url, sslmode='require')
    return conn


def get_fees_for_exchange():
    """Повертає точні комісії (Maker, Taker) для обраної біржі"""
    exchange_name = get_setting('exchange_name') or 'binance'
    if exchange_name == 'mexc':
        return 0.0001, 0.0004  # Maker 0.01%, Taker 0.04%
    else:
        return 0.0002, 0.0005  # Maker 0.02%, Taker 0.05%


def init_db():
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    # Створюємо таблицю сигналів у форматі PostgreSQL
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS signals (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR(20) NOT NULL,
            timeframe VARCHAR(10) NOT NULL,
            direction VARCHAR(10) NOT NULL,
            entry REAL NOT NULL,
            stop_loss REAL,
            dobar_low REAL,
            dobar_high REAL,
            tp1 REAL, tp2 REAL, tp3 REAL, tp4 REAL,
            tp1_prob INTEGER, tp2_prob INTEGER,
            tp3_prob INTEGER, tp4_prob INTEGER,
            tier VARCHAR(5),
            strategy_type VARCHAR(30),
            chart_message_id BIGINT,
            stat_id INTEGER,
            status VARCHAR(20) DEFAULT 'active',
            result VARCHAR(20),
            pct REAL,
            show_dobar INTEGER DEFAULT 1,
            hit_tps TEXT,
            pos_usd REAL,
            pos_contracts REAL,
            mode VARCHAR(10) DEFAULT 'swing',
            funding_rate REAL,
            open_interest REAL,
            stop_loss_id VARCHAR(50),
            dobar_order_id VARCHAR(50),
            tp_order_ids TEXT,
            dobar_filled_state INTEGER DEFAULT 0,
            created_at TEXT,
            closed_at TEXT
        )
    ''')

    # Створюємо таблицю статистики
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS stats (
            id SERIAL PRIMARY KEY,
            signal_id INTEGER,
            symbol VARCHAR(20),
            timeframe VARCHAR(10),
            direction VARCHAR(10),
            tier VARCHAR(5),
            result VARCHAR(20),
            pct REAL,
            mode VARCHAR(10) DEFAULT 'swing',
            pnl_usd REAL,
            exit_price REAL,
            created_at TEXT,
            closed_at TEXT,
            FOREIGN KEY (signal_id) REFERENCES signals(id) ON DELETE CASCADE
        )
    ''')

    # Створюємо таблицю конфігурації стратегій (Persistent Strategy Parameters)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS strategy_configs (
            symbol VARCHAR(20) NOT NULL,
            timeframe VARCHAR(10) NOT NULL,
            direction VARCHAR(10) NOT NULL,
            regime_group VARCHAR(20) NOT NULL, -- 'TREND' або 'REVERSION'
            strategy_type VARCHAR(30) NOT NULL,
            score REAL NOT NULL,
            
            -- Трендові параметри (TREND)
            ema_fast INTEGER,
            ema_slow INTEGER,
            rsi_min INTEGER,
            rsi_max INTEGER,
            
            -- Параметри Боллінджера (REVERSION - bb_bounce)
            bb_window INTEGER,
            bb_std REAL,
            
            -- Параметри WaveTrend (REVERSION - wavetrend_bounce)
            wt_channel_len INTEGER,
            wt_average_len INTEGER,
            wt_dot_level INTEGER,
            
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (symbol, timeframe, direction, regime_group)
        )
    ''')

   # Створюємо таблицю заблокованих сигналів для аналітики телеметрії фільтрів
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS rejected_signals (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR(20) NOT NULL,
            timeframe VARCHAR(10) NOT NULL,
            direction VARCHAR(10) NOT NULL,
            entry REAL,
            stop_loss REAL,
            reason TEXT NOT NULL,
            correlation REAL,
            funding_rate REAL,
            open_interest REAL,
            regime VARCHAR(30),
            er REAL,
            z_vol REAL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Створюємо таблицю аналітики виконання угод (Slippage & Latency Tracking)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS order_executions (
            id SERIAL PRIMARY KEY,
            signal_id INTEGER,
            symbol VARCHAR(20) NOT NULL,
            order_type VARCHAR(30) NOT NULL, -- 'entry_market', 'entry_dobar', 'sl', 'tp'
            side VARCHAR(10) NOT NULL,       -- 'BUY', 'SELL'
            requested_price REAL,            -- Теоретична ціна сигналу / тейку
            executed_price REAL,             -- Фактична ціна виконання біржею
            slippage_pct REAL,               -- % прослизання
            executed_qty REAL NOT NULL,
            fee_paid REAL,
            latency_ms INTEGER,              -- Мережева затримка у мілісекундах
            executed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (signal_id) REFERENCES stats(id) ON DELETE CASCADE
        )
    ''')

    # Безпечні міграції PostgreSQL (Додано нові поля збереження ID ордерів)
    try:
        cursor.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS pos_usd REAL")
        cursor.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS pos_contracts REAL")
        cursor.execute("ALTER TABLE stats ADD COLUMN IF NOT EXISTS pnl_usd REAL")
        cursor.execute("ALTER TABLE stats ADD COLUMN IF NOT EXISTS exit_price REAL")
        cursor.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS funding_rate REAL")
        cursor.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS open_interest REAL")
        cursor.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS mode VARCHAR(10) DEFAULT 'swing'")
        cursor.execute("ALTER TABLE stats ADD COLUMN IF NOT EXISTS mode VARCHAR(10) DEFAULT 'swing'")
        
        # НОВІ МІГРАЦІЇ ДЛЯ ФАЗИ Б (Збереження стану ордерів при рестарті)
        cursor.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS stop_loss_id VARCHAR(50)")
        cursor.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS dobar_order_id VARCHAR(50)")
        cursor.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS tp_order_ids TEXT")
        cursor.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS dobar_filled_state INTEGER DEFAULT 0")
    except Exception as e:
        print(f"Попередження міграції: {e}")

    # Синхронізація сиротинських записів у таблиці stats при запуску
    cursor.execute('''
        UPDATE stats 
        SET result = 'cleared', closed_at = %s 
        WHERE result = 'active' 
          AND id NOT IN (SELECT stat_id FROM signals WHERE status = 'active' AND stat_id IS NOT NULL)
    ''', (datetime.now(timezone.utc).isoformat(),))

    conn.commit()
    cursor.close()
    conn.close()
    print("✅ База даних PostgreSQL ініціалізована та мігрована")


# =====================
# Активні сигнали
# =====================

def save_active_signals(signals):
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    try:
        for s in signals:
            tps = s.get('tps', [])
            tp_prices = [to_native_float(tp[0]) for tp in tps] + [None] * 4
            tp_probs = [to_native_int(tp[1]) for tp in tps] + [None] * 4

            hit_tps_set = s.get('hit_tps', set())
            hit_tps_str = ",".join(map(str, sorted(list(hit_tps_set)))) if hit_tps_set else ""

            db_id = to_native_int(s.get('db_id'))
            entry = to_native_float(s['entry'])
            stop_loss = to_native_float(s.get('stop_loss'))
            dobar_low = to_native_float(s.get('dobar_low'))
            dobar_high = to_native_float(s.get('dobar_high'))
            chart_message_id = to_native_int(s.get('chart_message_id'))
            stat_id = to_native_int(s.get('stat_id'))
            
            pos_usd = to_native_float(s.get('pos_usd', 0.0))
            pos_contracts = to_native_float(s.get('pos_contracts', 0.0))
            funding_rate = to_native_float(s.get('funding_rate'))
            open_interest = to_native_float(s.get('open_interest'))

            # Збереження ID ордерів у форматі рядків
            stop_loss_id = s.get('stop_loss_id')
            dobar_order_id = s.get('dobar_order_id')
            tp_order_ids_str = ",".join(s.get('tp_order_ids', [])) if s.get('tp_order_ids') else ""
            dobar_filled_state = 1 if s.get('dobar_filled_state', False) else 0

            if db_id:
                cursor.execute('''
                    INSERT INTO signals (
                        id, symbol, timeframe, direction, entry,
                        stop_loss, dobar_low, dobar_high,
                        tp1, tp2, tp3, tp4,
                        tp1_prob, tp2_prob, tp3_prob, tp4_prob,
                        tier, chart_message_id, stat_id,
                        status, show_dobar, hit_tps, pos_usd, pos_contracts,
                        funding_rate, open_interest, stop_loss_id, dobar_order_id,
                        tp_order_ids, dobar_filled_state, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        symbol = EXCLUDED.symbol,
                        timeframe = EXCLUDED.timeframe,
                        direction = EXCLUDED.direction,
                        entry = EXCLUDED.entry,
                        stop_loss = EXCLUDED.stop_loss,
                        dobar_low = EXCLUDED.dobar_low,
                        dobar_high = EXCLUDED.dobar_high,
                        tp1 = EXCLUDED.tp1,
                        tp2 = EXCLUDED.tp2,
                        tp3 = EXCLUDED.tp3,
                        tp4 = EXCLUDED.tp4,
                        tp1_prob = EXCLUDED.tp1_prob,
                        tp2_prob = EXCLUDED.tp2_prob,
                        tp3_prob = EXCLUDED.tp3_prob,
                        tp4_prob = EXCLUDED.tp4_prob,
                        tier = EXCLUDED.tier,
                        chart_message_id = EXCLUDED.chart_message_id,
                        stat_id = EXCLUDED.stat_id,
                        status = EXCLUDED.status,
                        show_dobar = EXCLUDED.show_dobar,
                        hit_tps = EXCLUDED.hit_tps,
                        pos_usd = EXCLUDED.pos_usd,
                        pos_contracts = EXCLUDED.pos_contracts,
                        funding_rate = EXCLUDED.funding_rate,
                        open_interest = EXCLUDED.open_interest,
                        stop_loss_id = EXCLUDED.stop_loss_id,
                        dobar_order_id = EXCLUDED.dobar_order_id,
                        tp_order_ids = EXCLUDED.tp_order_ids,
                        dobar_filled_state = EXCLUDED.dobar_filled_state,
                        created_at = EXCLUDED.created_at
                ''', (
                    db_id,
                    s['symbol'], s['timeframe'], s['direction'], entry,
                    stop_loss, dobar_low, dobar_high,
                    tp_prices[0], tp_prices[1], tp_prices[2], tp_prices[3],
                    tp_probs[0], tp_probs[1], tp_probs[2], tp_probs[3],
                    s.get('tier', '🟢'),
                    chart_message_id,
                    stat_id,
                    1 if s.get('show_dobar', True) else 0,
                    hit_tps_str,
                    pos_usd,
                    pos_contracts,
                    funding_rate,
                    open_interest,
                    stop_loss_id,
                    dobar_order_id,
                    tp_order_ids_str,
                    dobar_filled_state,
                    s.get('created_at', datetime.now(timezone.utc).isoformat()),
                ))
            else:
                cursor.execute('''
                    INSERT INTO signals (
                        symbol, timeframe, direction, entry,
                        stop_loss, dobar_low, dobar_high,
                        tp1, tp2, tp3, tp4,
                        tp1_prob, tp2_prob, tp3_prob, tp4_prob,
                        tier, chart_message_id, stat_id,
                        status, show_dobar, hit_tps, pos_usd, pos_contracts,
                        funding_rate, open_interest, stop_loss_id, dobar_order_id,
                        tp_order_ids, dobar_filled_state, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                ''', (
                    s['symbol'], s['timeframe'], s['direction'], entry,
                    stop_loss, dobar_low, dobar_high,
                    tp_prices[0], tp_prices[1], tp_prices[2], tp_prices[3],
                    tp_probs[0], tp_probs[1], tp_probs[2], tp_probs[3],
                    s.get('tier', '🟢'),
                    chart_message_id,
                    stat_id,
                    1 if s.get('show_dobar', True) else 0,
                    hit_tps_str,
                    pos_usd,
                    pos_contracts,
                    funding_rate,
                    open_interest,
                    stop_loss_id,
                    dobar_order_id,
                    tp_order_ids_str,
                    dobar_filled_state,
                    s.get('created_at', datetime.now(timezone.utc).isoformat()),
                ))
                s['db_id'] = cursor.fetchone()['id']

        conn.commit()
    except Exception as e:
        print(f"Помилка збереження активних: {e}")
        conn.rollback()
    finally:
        cursor.close()
        conn.close()


def load_active_signals():
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    try:
        cursor.execute("SELECT * FROM signals WHERE status = 'active'")
        rows = cursor.fetchall()

        signals = []
        for row in rows:
            tps = []
            for i, (price_col, prob_col, pct) in enumerate([
                ('tp1', 'tp1_prob', None),
                ('tp2', 'tp2_prob', None),
                ('tp3', 'tp3_prob', None),
                ('tp4', 'tp4_prob', None),
            ]):
                price = row[price_col]
                prob = row[prob_col]
                if price is not None:
                    entry = row['entry']
                    pct_val = round(abs(price - entry) / entry * 100, 1)
                    tps.append((price, prob or 50, pct_val))

            hit_tps_str = row['hit_tps']
            hit_tps_set = set()
            if hit_tps_str:
                try:
                    hit_tps_set = set(map(int, hit_tps_str.split(',')))
                except ValueError:
                    pass

            tp_order_ids_str = row.get('tp_order_ids')
            tp_order_ids = tp_order_ids_str.split(',') if tp_order_ids_str else []

            signals.append({
                'db_id': row['id'],
                'symbol': row['symbol'],
                'timeframe': row['timeframe'],
                'direction': row['direction'],
                'entry': row['entry'],
                'stop_loss': row['stop_loss'],
                'dobar_low': row['dobar_low'],
                'dobar_high': row['dobar_high'],
                'tps': tps,
                'hit_tps': hit_tps_set,
                'tier': row['tier'],
                'chart_message_id': row['chart_message_id'],
                'stat_id': row['stat_id'],
                'show_dobar': bool(row['show_dobar']),
                'pos_usd': to_native_float(row.get('pos_usd', 0.0)),
                'pos_contracts': to_native_float(row.get('pos_contracts', 0.0)),
                'funding_rate': to_native_float(row.get('funding_rate')),
                'open_interest': to_native_float(row.get('open_interest')),
                'stop_loss_id': row.get('stop_loss_id'),
                'dobar_order_id': row.get('dobar_order_id'),
                'tp_order_ids': tp_order_ids,
                'dobar_filled_state': bool(row.get('dobar_filled_state', 0)),
                'created_at': row['created_at'],
                'stats': {
                    'count': 0, 'avg_dev': 0,
                    'deviations': {}, 'tp_probs': [50, 40, 30, 20],
                    'is_valid': True,
                },
            })

        return signals
    except Exception as e:
        print(f"Помилка завантаження активних: {e}")
        return []
    finally:
        cursor.close()
        conn.close()


def remove_active_signal(symbol, timeframe):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE signals SET status = 'closed' WHERE symbol = %s AND timeframe = %s AND status = 'active'",
            (symbol, timeframe)
        )
        conn.commit()
        cursor.close()
    except Exception as e:
        print(f"Помилка видалення сигналу: {e}")
    finally:
        conn.close()


def clear_active_signals():
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE signals SET status = 'cleared' WHERE status = 'active'")
        cursor.execute("UPDATE stats SET result = 'cleared', closed_at = %s WHERE result = 'active'", (datetime.now(timezone.utc).isoformat(),))
        conn.commit()
        cursor.close()
    except Exception as e:
        print(f"Помилка очищення: {e}")
    finally:
        conn.close()

# =====================
# Статистика
# =====================

def add_signal_stat(symbol, timeframe, direction, entry, tier):
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute('''
            INSERT INTO stats (symbol, timeframe, direction, tier, result, created_at)
            VALUES (%s, %s, %s, %s, 'active', %s)
            RETURNING id
        ''', (symbol, timeframe, direction, tier, datetime.now(timezone.utc).isoformat()))
        conn.commit()
        stat_id = cursor.fetchone()['id']
        cursor.close()
        return stat_id
    except Exception as e:
        print(f"Помилка додавання статистики: {e}")
        return None
    finally:
        conn.close()


def close_signal_stat(signal_id, result, pct, exit_price=None):
    if signal_id is None:
        return 0.0
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("SELECT * FROM signals WHERE stat_id = %s", (signal_id,))
        signal_row = cursor.fetchone()
        
        pnl_usd = 0.0
        calculated_exit_price = to_native_float(exit_price)
        
        if signal_row:
            entry = to_native_float(signal_row['entry'])
            direction = signal_row['direction']
            total_expected_contracts = to_native_float(signal_row['pos_contracts']) or 0.0
            
            dobar_low = to_native_float(signal_row['dobar_low'])
            dobar_high = to_native_float(signal_row['dobar_high'])
            dobar_filled_state = bool(signal_row.get('dobar_filled_state', 0))
            
            use_dobar = get_setting('use_dobar')
            if use_dobar is None:
                use_dobar = True
                
            actual_entry = entry
            if use_dobar and dobar_low is not None and dobar_high is not None:
                dobar_mid = (dobar_low + dobar_high) / 2.0
                avg_entry = (entry + dobar_mid) / 2.0
            
            # Обчислюємо максимальний об'єм, якого реально досягла позиція (50% чи 100%)
            filled_factor = 1.0 if (not use_dobar or dobar_filled_state) else 0.5
            filled_volume = total_expected_contracts * filled_factor
            
            # Розшифровуємо взяті Тейки з бази даних
            hit_tps_str = signal_row.get('hit_tps') or ""
            hit_tps = set()
            if hit_tps_str:
                try:
                    hit_tps = set(map(int, hit_tps_str.split(',')))
                except ValueError:
                    pass
            
            # Отримуємо ціни Тейків з бази
            tp_prices = [
                to_native_float(signal_row['tp1']),
                to_native_float(signal_row['tp2']),
                to_native_float(signal_row['tp3']),
                to_native_float(signal_row['tp4'])
            ]
            
            # Розрахунок прибутку від виконаних Тейк-Профітів
            original_percentages = [0.50, 0.20, 0.15, 0.15]
            total_gross_pnl_usd = 0.0
            closed_share_sum = 0.0
            
            for idx in sorted(list(hit_tps)):
                if idx < len(tp_prices) and tp_prices[idx] is not None:
                    tp_price = tp_prices[idx]
                    tp_contracts = filled_volume * original_percentages[idx]
                    closed_share_sum += original_percentages[idx]
                    
                    if direction == 'LONG':
                        tp_pnl = (tp_price - actual_entry) * tp_contracts
                    else:
                        tp_pnl = (actual_entry - tp_price) * tp_contracts
                    total_gross_pnl_usd += tp_pnl

            # Розрахунок результату для залишку позиції (який закрився по БУ або Стопу)
            remaining_share = 1.0 - closed_share_sum
            if remaining_share > 0:
                remaining_contracts = filled_volume * remaining_share
                
                if calculated_exit_price is None:
                    if direction == 'LONG':
                        calculated_exit_price = actual_entry * (1 + pct / 100.0) if result == 'tp' else actual_entry * (1 - abs(pct) / 100.0)
                    else:
                        calculated_exit_price = actual_entry * (1 - pct / 100.0) if result == 'tp' else actual_entry * (1 + abs(pct) / 100.0)
                
                if direction == 'LONG':
                    rem_pnl = (calculated_exit_price - actual_entry) * remaining_contracts
                else:
                    rem_pnl = (actual_entry - calculated_exit_price) * remaining_contracts
                total_gross_pnl_usd += rem_pnl
            
            # Розрахунок реальних комісій (Maker для ліміток ТП, Taker для входу та стопу)
            maker_fee, taker_fee = get_fees_for_exchange()
            entry_fee_usd = actual_entry * filled_volume * taker_fee
            exit_fee_usd = (calculated_exit_price or actual_entry) * filled_volume * (maker_fee if result == 'tp' else taker_fee)
            total_fees = entry_fee_usd + exit_fee_usd
            
            pnl_usd = total_gross_pnl_usd - total_fees
            
        cursor.execute('''
            UPDATE stats 
            SET result = %s, pct = %s, pnl_usd = %s, exit_price = %s, closed_at = %s
            WHERE id = %s
        ''', (
            result, 
            to_native_float(pct), 
            to_native_float(pnl_usd), 
            to_native_float(calculated_exit_price or actual_entry), 
            datetime.now(timezone.utc).isoformat(), 
            signal_id
        ))
        conn.commit()
        cursor.close()
        return round(pnl_usd, 2)
    except Exception as e:
        print(f"Помилка закриття статистики: {e}")
        return 0.0
    finally:
        conn.close()


def get_stats_summary():
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        cursor.execute("SELECT COUNT(*) as total FROM stats")
        total = cursor.fetchone()['total']

        if total == 0:
            cursor.close()
            return "📊 Статистика бота\nСигналів ще не було"

        cursor.execute("SELECT COUNT(*) as cnt FROM stats WHERE result = 'tp'")
        tp = cursor.fetchone()['cnt']

        cursor.execute("SELECT COUNT(*) as cnt FROM stats WHERE result = 'sl'")
        sl = cursor.fetchone()['cnt']

        cursor.execute("SELECT COUNT(*) as cnt FROM stats WHERE result = 'be'")
        be = cursor.fetchone()['cnt']

        cursor.execute("SELECT COUNT(*) as cnt FROM signals WHERE status = 'active'")
        active = cursor.fetchone()['cnt']

        closed = tp + sl + be
        positive = tp + be
        winrate = round(positive / closed * 100) if closed > 0 else 0

        cursor.execute("SELECT AVG(pct) as avg FROM stats WHERE result = 'tp' AND pct IS NOT NULL")
        avg_profit_row = cursor.fetchone()
        avg_profit = round(avg_profit_row['avg'], 1) if avg_profit_row['avg'] else 0

        cursor.execute("SELECT AVG(pct) as avg FROM stats WHERE result = 'sl' AND pct IS NOT NULL")
        avg_loss_row = cursor.fetchone()
        avg_loss = round(avg_loss_row['avg'], 1) if avg_loss_row['avg'] else 0

        cursor.execute("SELECT SUM(pnl_usd) as total_usd FROM stats WHERE pnl_usd IS NOT NULL")
        total_pnl_usd = cursor.fetchone()['total_usd'] or 0.0

        lines = [
            "📊 Статистика бота",
            f"",
            f"📈 Всього сигналів: {total}",
            f"🟢 Закрито в TP: {tp}",
            f"↩️ Закрито в БУ: {be}",
            f"🛑 Закрито в SL: {sl}",
            f"⏳ Активних: {active}",
            f"",
            f"💰 Фінансовий результат: <b>${total_pnl_usd:.2f}</b>",
            f"🎯 Winrate (TP+БУ): {winrate}%",
            f"💰 Середній прибуток: +{avg_profit}%",
            f"💸 Середній збиток: {avg_loss}%",
        ]

        cursor.execute('''
            SELECT tier,
                   SUM(CASE WHEN result='tp' THEN 1 ELSE 0 END) as tp_cnt,
                   SUM(CASE WHEN result='be' THEN 1 ELSE 0 END) as be_cnt,
                   SUM(CASE WHEN result='sl' THEN 1 ELSE 0 END) as sl_cnt
            FROM stats
            WHERE result IN ('tp','be','sl')
            GROUP BY tier
            ORDER BY tier
        ''')
        tier_rows = cursor.fetchall()

        if tier_rows:
            lines.append("")
            lines.append("Розбивка по Tier:")
            for row in tier_rows:
                t = row['tp_cnt'] + row['be_cnt']
                total_tier = t + row['sl_cnt']
                wr = round(t / total_tier * 100) if total_tier > 0 else 0
                lines.append(
                    f"{row['tier']} TP:{row['tp_cnt']} БУ:{row['be_cnt']} "
                    f"SL:{row['sl_cnt']} | {wr}%"
                )

        cursor.execute('''
            SELECT symbol,
                   COUNT(*) as total,
                   SUM(CASE WHEN result IN ('tp','be') THEN 1 ELSE 0 END) as wins
            FROM stats
            WHERE result IN ('tp','be','sl')
            GROUP BY symbol
            HAVING COUNT(*) >= 3
            ORDER BY CAST(SUM(CASE WHEN result IN ('tp','be') THEN 1 ELSE 0 END) AS REAL) / COUNT(*) DESC
            LIMIT 5
        ''')
        top_pairs = cursor.fetchall()

        if top_pairs:
            lines.append("")
            lines.append("🏆 Топ пари:")
            for row in top_pairs:
                wr = round(row['wins'] / row['total'] * 100)
                lines.append(f"  {row['symbol']}: {wr}% ({row['total']} сигналів)")

        cursor.close()
        return "\n".join(lines)

    except Exception as e:
        print(f"Помилка отримання статистики: {e}")
        return "❌ Помилка читання статистики"
    finally:
        conn.close()

def clear_stats():
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM stats")
        conn.commit()
        cursor.close()
    except Exception as e:
        print(f"Помилка очищення статистики: {e}")
    finally:
        conn.close()
        # database.py

def get_daily_pnl_usd():
    """Повертає чистий реалізований PnL у USD за останні 24 години"""
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute('''
            SELECT SUM(pnl_usd) as daily_pnl 
            FROM stats 
            WHERE closed_at IS NOT NULL 
              AND closed_at::timestamptz >= NOW() - INTERVAL '24 hours'
        ''')
        row = cursor.fetchone()
        cursor.close()
        return float(row['daily_pnl']) if row and row['daily_pnl'] is not None else 0.0
    except Exception as e:
        print(f"Помилка отримання денного PnL з БД: {e}")
        return 0.0
    finally:
        conn.close()


def get_consecutive_losses_count(limit=5):
    """Визначає кількість послідовних стоп-лоссів серед останніх N закритих угод"""
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute('''
            SELECT result 
            FROM stats 
            WHERE result IN ('tp', 'be', 'sl') 
            ORDER BY id DESC 
            LIMIT %s
        ''', (limit,))
        rows = cursor.fetchall()
        cursor.close()
        
        consecutive_losses = 0
        for row in rows:
            if row['result'] == 'sl':
                consecutive_losses += 1
            else:
                break
        return consecutive_losses
    except Exception as e:
        print(f"Помилка отримання серії стопів з БД: {e}")
        return 0
    finally:
        conn.close()


def get_last_trade_closed_at():
    """Повертає ISO 8601 дату закриття останньої завершеної угоди"""
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute('''
            SELECT closed_at 
            FROM stats 
            WHERE result IN ('tp', 'be', 'sl') 
            ORDER BY id DESC 
            LIMIT 1
        ''')
        row = cursor.fetchone()
        cursor.close()
        return row['closed_at'] if row else None
    except Exception as e:
        print(f"Помилка отримання дати останньої угоди: {e}")
        return None
    finally:
        conn.close()
    
def log_order_execution(signal_id, symbol, order_type, side, requested_price, executed_price, executed_qty, fee_paid, latency_ms):
    """
    Розраховує чисте прослизання в % та записує параметри виконання ордера у PostgreSQL.
    """
    # Обчислюємо чисте прослизання (Slippage %) залежно від напрямку угоди
    if requested_price > 0 and executed_price > 0:
        if side.upper() == 'BUY' or (side.upper() == 'SELL' and 'tp' in order_type):
            # Для покупок (або закриття шортів по ТР): гірша ціна — це ціна, що вища за очікувану
            slippage_pct = ((executed_price - requested_price) / requested_price) * 100.0
        else:
            # Для продажів (або закриття лонгів по ТР): гірша ціна — це ціна, що нижча за очікувану
            slippage_pct = ((requested_price - executed_price) / requested_price) * 100.0
    else:
        slippage_pct = 0.0

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO order_executions (
                signal_id, symbol, order_type, side, requested_price, 
                executed_price, slippage_pct, executed_qty, fee_paid, latency_ms
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ''', (
            to_native_int(signal_id), symbol, order_type, side, 
            to_native_float(requested_price), to_native_float(executed_price), 
            to_native_float(slippage_pct), to_native_float(executed_qty), 
            to_native_float(fee_paid), to_native_int(latency_ms)
        ))
        conn.commit()
        cursor.close()
        print(f"📊 [DB EXECUTION] Записано лог для {symbol} ({order_type}). Slippage: {slippage_pct:.4f}%, Latency: {latency_ms}ms")
    except Exception as e:
        print(f"Помилка запису аналітики виконання у БД: {e}")
    finally:
        conn.close()

def get_execution_analytics_summary():
    """
    Асинхронний звіт якості виконання ордерів (Slippage, Latency, Sortino Ratio).
    Виявляє ТОП-3 найгірших активів за прослизанням.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        # 1. Загальна статистика затримки та прослизання
        cursor.execute('''
            SELECT 
                AVG(latency_ms) as avg_lat,
                AVG(slippage_pct) as avg_slip,
                SUM(fee_paid) as total_fees,
                COUNT(*) as total_orders
            FROM order_executions
        ''')
        exec_stats = cursor.fetchone()
        
        if not exec_stats or exec_stats['total_orders'] == 0:
            cursor.close()
            return "📊 <b>Аналітика виконання (Execution Quality)</b>\n\nДані про реальні виконання ордерів ще відсутні."
            
        avg_lat = exec_stats['avg_lat'] or 0.0
        avg_slip = exec_stats['avg_slip'] or 0.0
        total_fees = exec_stats['total_fees'] or 0.0
        total_orders = exec_stats['total_orders']
        
        # 2. Метрики ефективності стратегії (Profit Factor, Sortino)
        cursor.execute("SELECT pnl_usd FROM stats WHERE result IN ('tp', 'be', 'sl')")
        trades = cursor.fetchall()
        
        profit_factor = 0.0
        sortino_ratio = 0.0
        if trades:
            pnl_array = np.array([float(t['pnl_usd'] or 0.0) for t in trades])
            wins = pnl_array[pnl_array > 0]
            losses = pnl_array[pnl_array < 0]
            
            sum_wins = np.sum(wins) if len(wins) > 0 else 0.0
            sum_losses = np.abs(np.sum(losses)) if len(losses) > 0 else 1e-6
            profit_factor = sum_wins / sum_losses
            
            downside_std = np.std(losses) if len(losses) > 1 else 1e-6
            mean_pnl = np.mean(pnl_array)
            sortino_ratio = (mean_pnl / downside_std) * np.sqrt(252) if downside_std > 0 else 0.0
            
        # 3. Аналіз прослизання по парах (ТОП-3 найгірших)
        cursor.execute('''
            SELECT symbol, AVG(slippage_pct) as pair_slip, COUNT(*) as pair_count
            FROM order_executions
            WHERE order_type = 'entry_market'
            GROUP BY symbol
            ORDER BY pair_slip DESC
            LIMIT 3
        ''')
        worst_pairs = cursor.fetchall()
        
        lines = [
            "📊 <b>ЗВІТ ЯКОСТІ ВИКОНАННЯ (EXECUTION QUALITY)</b>",
            f"",
            f"⏱ Середня затримка (Latency): <b>{int(avg_lat)} ms</b>",
            f"📉 Середнє прослизання (Slippage): <b>{avg_slip:.4f}%</b>",
            f"💸 Сумарна комісія (Maker/Taker): <b>${total_fees:.4f}</b>",
            f"📦 Оброблено ордерів: <b>{total_orders}</b>",
            f"",
            f"📈 Співвідношення прибутку (Profit Factor): <b>{profit_factor:.2f}</b>",
            f"🛡️ Коефіцієнт Сортіно (Sortino Ratio): <b>{sortino_ratio:.2f}</b>",
            f""
        ]
        
        if worst_pairs:
            lines.append("🚩 <b>ТОП-3 пари з найбільшим прослизанням (Slippage):</b>")
            for i, row in enumerate(worst_pairs):
                lines.append(f"  {i+1}. #{row['symbol']}: <b>{row['pair_slip']:.4f}%</b> ({row['pair_count']} ордерів)")
            lines.append("")
            lines.append("💡 <i>Рекомендація: Розгляньте видалення цих пар з watchlist через високі транзакційні втрати.</i>")
            
        cursor.close()
        return "\n".join(lines)
    except Exception as e:
        print(f"Помилка розрахунку аналітики виконання: {e}")
        return "❌ Помилка розрахунку аналітики"
    finally:
        conn.close()

def save_strategy_config_to_db(symbol, timeframe, direction, regime_group, strategy_type, score,
                               ema_fast=None, ema_slow=None, rsi_min=None, rsi_max=None,
                               bb_window=None, bb_std=None,
                               wt_channel_len=None, wt_average_len=None, wt_dot_level=None):
    """Зберігає параметри стратегії у відповідні явні колонки БД"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO strategy_configs (
                symbol, timeframe, direction, regime_group, strategy_type, score,
                ema_fast, ema_slow, rsi_min, rsi_max,
                bb_window, bb_std,
                wt_channel_len, wt_average_len, wt_dot_level, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (symbol, timeframe, direction, regime_group) DO UPDATE SET
                strategy_type = EXCLUDED.strategy_type,
                score = EXCLUDED.score,
                ema_fast = EXCLUDED.ema_fast,
                ema_slow = EXCLUDED.ema_slow,
                rsi_min = EXCLUDED.rsi_min,
                rsi_max = EXCLUDED.rsi_max,
                bb_window = EXCLUDED.bb_window,
                bb_std = EXCLUDED.bb_std,
                wt_channel_len = EXCLUDED.wt_channel_len,
                wt_average_len = EXCLUDED.wt_average_len,
                wt_dot_level = EXCLUDED.wt_dot_level,
                updated_at = NOW()
        ''', (
            symbol, timeframe, direction, regime_group, strategy_type, score,
            to_native_int(ema_fast), to_native_int(ema_slow), to_native_int(rsi_min), to_native_int(rsi_max),
            to_native_int(bb_window), to_native_float(bb_std),
            to_native_int(wt_channel_len), to_native_int(wt_average_len), to_native_int(wt_dot_level)
        ))
        conn.commit()
        cursor.close()
        print(f"💾 [DB CONFIG] Збережено явні параметри для {symbol} ({timeframe} {regime_group})")
    except Exception as e:
        print(f"Помилка збереження конфігурації в БД: {e}")
    finally:
        conn.close()

def load_strategy_config_from_db(symbol, timeframe, direction, regime_group):
    """Завантажує весь рядок налаштувань стратегії з БД"""
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute('''
            SELECT * FROM strategy_configs 
            WHERE symbol = %s AND timeframe = %s AND direction = %s AND regime_group = %s
        ''', (symbol, timeframe, direction, regime_group))
        row = cursor.fetchone()
        cursor.close()
        return row
    except Exception as e:
        print(f"Помилка завантаження конфігурації з БД: {e}")
        return None
    finally:
        conn.close()

def log_rejected_signal(symbol, timeframe, direction, entry, stop_loss, reason, 
                        correlation=None, funding_rate=None, open_interest=None, 
                        regime=None, er=None, z_vol=None):
    """Записує відхилений фільтрами сигнал у PostgreSQL для подальшого аналізу"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO rejected_signals (
                symbol, timeframe, direction, entry, stop_loss, reason, 
                correlation, funding_rate, open_interest, regime, er, z_vol
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ''', (
            symbol, timeframe, direction, to_native_float(entry), to_native_float(stop_loss), reason,
            to_native_float(correlation), to_native_float(funding_rate), to_native_float(open_interest),
            regime, to_native_float(er), to_native_float(z_vol)
        ))
        conn.commit()
        cursor.close()
        print(f"📊 [TELEMETRY] Зафіксовано відхилення сигналу {symbol} {timeframe}. Причина: {reason}")
    except Exception as e:
        print(f"Помилка логування відхиленого сигналу: {e}")
    finally:
        conn.close()


# 3. Додайте функцію вивантаження аналітики в Telegram у кінець файлу database.py
def get_rejected_stats_summary():
    """Повертає статистику заблокованих сигналів по фільтрах за останні 30 днів"""
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute('''
            SELECT reason, COUNT(*) as count 
            FROM rejected_signals 
            WHERE created_at >= NOW() - INTERVAL '30 days'
            GROUP BY reason 
            ORDER BY count DESC
        ''')
        rows = cursor.fetchall()
        cursor.close()
        
        if not rows:
            return "📋 <b>Телеметрія відхилених сигналів</b>\n\nНемає зафіксованих записів за останні 30 днів."
            
        total = sum(row['count'] for row in rows)
        lines = [
            "📋 <b>ТЕЛЕМЕТРІЯ ФІЛЬТРІВ (За 30 днів)</b>",
            f"Усього заблоковано сигналів: <b>{total}</b>",
            ""
        ]
        for row in rows:
            pct = (row['count'] / total) * 100.0
            escaped_reason = html.escape(row['reason'])
            lines.append(f"• <code>{escaped_reason}</code>: <b>{row['count']}</b> ({pct:.1f}%)")
            
        return "\n".join(lines)
    except Exception as e:
        print(f"Помилка отримання телеметрії відхилень: {e}")
        return "❌ Помилка розрахунку телеметрії відхилень"
    finally:
        conn.close()
