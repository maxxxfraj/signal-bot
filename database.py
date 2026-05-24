# database.py
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timezone
from dotenv import load_dotenv

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

    # Безпечні міграції PostgreSQL
    try:
        cursor.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS pos_usd REAL")
        cursor.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS pos_contracts REAL")
        cursor.execute("ALTER TABLE stats ADD COLUMN IF NOT EXISTS pnl_usd REAL")
        cursor.execute("ALTER TABLE stats ADD COLUMN IF NOT EXISTS exit_price REAL")
        cursor.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS funding_rate REAL")
        cursor.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS open_interest REAL")
        cursor.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS mode VARCHAR(10) DEFAULT 'swing'")
        cursor.execute("ALTER TABLE stats ADD COLUMN IF NOT EXISTS mode VARCHAR(10) DEFAULT 'swing'")
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

            if db_id:
                cursor.execute('''
                    INSERT INTO signals (
                        id, symbol, timeframe, direction, entry,
                        stop_loss, dobar_low, dobar_high,
                        tp1, tp2, tp3, tp4,
                        tp1_prob, tp2_prob, tp3_prob, tp4_prob,
                        tier, chart_message_id, stat_id,
                        status, show_dobar, hit_tps, pos_usd, pos_contracts,
                        funding_rate, open_interest, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s, %s, %s, %s, %s, %s, %s)
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
                        funding_rate, open_interest, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s, %s, %s, %s, %s, %s, %s)
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
            pos_contracts = to_native_float(signal_row['pos_contracts']) or 0.0
            
            dobar_low = to_native_float(signal_row['dobar_low'])
            dobar_high = to_native_float(signal_row['dobar_high'])
            
            use_dobar = get_setting('use_dobar')
            if use_dobar is None:
                use_dobar = True
                
            actual_entry = entry
            if use_dobar and dobar_low is not None and dobar_high is not None:
                dobar_mid = (dobar_low + dobar_high) / 2.0
                actual_entry = (entry + dobar_mid) / 2.0
            
            if calculated_exit_price is None:
                if direction == 'LONG':
                    calculated_exit_price = actual_entry * (1 + pct / 100.0) if result == 'tp' else actual_entry * (1 - abs(pct) / 100.0)
                else:
                    calculated_exit_price = actual_entry * (1 - pct / 100.0) if result == 'tp' else actual_entry * (1 + abs(pct) / 100.0)
            
            maker_fee, taker_fee = get_fees_for_exchange()
            
            entry_fee_usd = actual_entry * pos_contracts * taker_fee
            exit_fee_usd = calculated_exit_price * pos_contracts * (maker_fee if result == 'tp' else taker_fee)
            total_fees = entry_fee_usd + exit_fee_usd
            
            if direction == 'LONG':
                gross_pnl_usd = (calculated_exit_price - actual_entry) * pos_contracts
            else:
                gross_pnl_usd = (actual_entry - calculated_exit_price) * pos_contracts
                
            pnl_usd = gross_pnl_usd - total_fees
            
        cursor.execute('''
            UPDATE stats 
            SET result = %s, pct = %s, pnl_usd = %s, exit_price = %s, closed_at = %s
            WHERE id = %s
        ''', (
            result, 
            to_native_float(pct), 
            to_native_float(pnl_usd), 
            to_native_float(calculated_exit_price), 
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