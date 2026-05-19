import json
import os

ACTIVE_FILE = 'active_signals.json'

def save_active(signals):
    data = []
    for s in signals:
        try:
            data.append({
                'symbol': s['symbol'],
                'timeframe': s['timeframe'],
                'direction': s['direction'],
                'entry': s['entry'],
                'dobar_low': s['dobar_low'],
                'dobar_high': s['dobar_high'],
                'tps': s['tps'],
                'stats': s['stats'],
                'tier': s.get('tier', '🟢'),
                'stop_loss': s.get('stop_loss'),
                'show_dobar': s.get('show_dobar', True),
                'chart_message_id': s.get('chart_message_id'),
                'stat_id': s.get('stat_id'),
            })
        except Exception as e:
            print(f"Помилка збереження сигналу: {e}")
    with open(ACTIVE_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_active():
    if not os.path.exists(ACTIVE_FILE):
        return []
    try:
        with open(ACTIVE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"Помилка завантаження активних сигналів: {e}")
        return []

def remove_active(symbol, timeframe):
    signals = load_active()
    signals = [s for s in signals if not (
        s['symbol'] == symbol and s['timeframe'] == timeframe
    )]
    save_active(signals)