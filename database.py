import os
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime
from dotenv import load_dotenv  # Додаємо імпорт для завантаження оточення

def get_connection():
    # Примусово завантажуємо .env перед зчитуванням змінних
    load_dotenv()
    database_url = os.getenv("DATABASE_URL")
    
    if not database_url:
        raise ValueError(
            "❌ КРИТИЧНА ПОМИЛКА: Не знайдено змінну DATABASE_URL!\n"
            "Переконайтеся, що ви:\n"
            "1. Створили безкоштовну базу на Neon.tech\n"
            "2. Створили файл .env у папці вашого бота\n"
            "3. Прописали туди рядок: DATABASE_URL=ваше_посилання_з_neon\n"
            "4. Також додали цю змінну у вкладку Environment на Render."
        )
        
    conn = psycopg2.connect(database_url, sslmode='require')
    return conn


def to_native_float(val):
    """Примусово конвертує будь-які типи (включаючи numpy.float64) у стандартний float Python"""
    if val is None:
        return None
    try:
        return float(val)
    except Exception:
        return val


def to_native_int(val):
    """Примусово конвертує any типи (включаючи numpy.int64) у стандартний int Python"""
    if val is None:
        return None
    try:
        return int(val)
    except Exception:
        return val


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
            created_at TEXT,
            closed_at TEXT,
            FOREIGN KEY (signal_id) REFERENCES signals(id) ON DELETE CASCADE
        )
    ''')

    # Синхронізація сиротинських записів у таблиці stats при запуску
    cursor.execute('''
        UPDATE stats 
        SET result = 'cleared', closed_at = %s 
        WHERE result = 'active' 
          AND id NOT IN (SELECT stat_id FROM signals WHERE status = 'active' AND stat_id IS NOT NULL)
    ''', (datetime.now().isoformat(),))

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
        # Видаляємо всі активні і записуємо заново
        cursor.execute("DELETE FROM signals WHERE status = 'active'")

        for s in signals:
            tps = s.get('tps', [])
            # Безпечне приведення числових значень масивів
            tp_prices = [to_native_float(tp[0]) for tp in tps] + [None] * 4
            tp_probs = [to_native_int(tp[1]) for tp in tps] + [None] * 4

            hit_tps_set = s.get('hit_tps', set())
            hit_tps_str = ",".join(map(str, sorted(list(hit_tps_set)))) if hit_tps_set else ""

            # Примусове приведення типів даних для сумісності з PostgreSQL
            db_id = to_native_int(s.get('db_id'))
            entry = to_native_float(s['entry'])
            stop_loss = to_native_float(s.get('stop_loss'))
            dobar_low = to_native_float(s.get('dobar_low'))
            dobar_high = to_native_float(s.get('dobar_high'))
            chart_message_id = to_native_int(s.get('chart_message_id'))
            stat_id = to_native_int(s.get('stat_id'))

            if db_id:
                # Якщо ID вже є, вставляємо з фіксованим ID
                cursor.execute('''
                    INSERT INTO signals (
                        id, symbol, timeframe, direction, entry,
                        stop_loss, dobar_low, dobar_high,
                        tp1, tp2, tp3, tp4,
                        tp1_prob, tp2_prob, tp3_prob, tp4_prob,
                        tier, chart_message_id, stat_id,
                        status, show_dobar, hit_tps, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s, %s, %s)
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
                    s.get('created_at', datetime.now().isoformat()),
                ))
            else:
                # Якщо це новий сигнал (точно 18 знаків %s перед 'active' для повної відповідності 21 параметру)
                cursor.execute('''
                    INSERT INTO signals (
                        symbol, timeframe, direction, entry,
                        stop_loss, dobar_low, dobar_high,
                        tp1, tp2, tp3, tp4,
                        tp1_prob, tp2_prob, tp3_prob, tp4_prob,
                        tier, chart_message_id, stat_id,
                        status, show_dobar, hit_tps, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s, %s, %s)
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
                    s.get('created_at', datetime.now().isoformat()),
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

            # Десеріалізуємо hit_tps назад у Python-сет
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
                'hit_tps': hit_tps_set,  # Передаємо відновлений сет досягнутих ТР
                'tier': row['tier'],
                'chart_message_id': row['chart_message_id'],
                'stat_id': row['stat_id'],
                'show_dobar': bool(row['show_dobar']),
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
        conn.execute("UPDATE signals SET status = 'cleared' WHERE status = 'active'")
        conn.execute("UPDATE stats SET result = 'cleared', closed_at = %s WHERE result = 'active'", (datetime.now().isoformat(),))
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
        ''', (symbol, timeframe, direction, tier, datetime.now().isoformat()))
        conn.commit()
        stat_id = cursor.fetchone()['id']
        cursor.close()
        return stat_id
    except Exception as e:
        print(f"Помилка додавання статистики: {e}")
        return None
    finally:
        conn.close()

def close_signal_stat(signal_id, result, pct):
    if signal_id is None:
        return
    conn = get_connection()
    try:
        cursor = conn.cursor()
        # Примусово кастуємо відсотки та ID до стандартних числових типів Python
        cursor.execute('''
            UPDATE stats SET result = %s, pct = %s, closed_at = %s
            WHERE id = %s
        ''', (result, to_native_float(pct), datetime.now().isoformat(), to_native_int(signal_id)))
        conn.commit()
        cursor.close()
    except Exception as e:
        print(f"Помилка закриття статистики: {e}")
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

        lines = [
            "📊 Статистика бота",
            f"",
            f"📈 Всього сигналів: {total}",
            f"🟢 Закрито в TP: {tp}",
            f"↩️ Закрито в БУ: {be}",
            f"🛑 Закрито в SL: {sl}",
            f"⏳ Активних: {active}",
            f"",
            f"🎯 Winrate (TP+БУ): {winrate}%",
            f"💰 Середній прибуток: +{avg_profit}%",
            f"💸 Середній збиток: {avg_loss}%",
        ]

        # Розбивка по Tier
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

        # Топ 5 пар за winrate (Повністю адаптовано під синтаксис PostgreSQL)
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