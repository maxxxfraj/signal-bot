import json
import os
from locks import stats_lock

STATS_FILE = 'bot_stats.json'

async def load_stats():
    async with stats_lock:
        if os.path.exists(STATS_FILE):
            with open(STATS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {
            'total_signals': 0,
            'closed_tp': 0,
            'closed_sl': 0,
            'closed_be': 0,
            'active': 0,
            'signals': []
        }

async def save_stats(stats):
    async with stats_lock:
        with open(STATS_FILE, 'w', encoding='utf-8') as f:
            json.dump(stats, f, indent=2, ensure_ascii=False)

async def add_signal(symbol, timeframe, direction, entry, tier):
    stats = await load_stats()
    stats['total_signals'] += 1
    stats['active'] += 1
    stats['signals'].append({
        'id': stats['total_signals'],
        'symbol': symbol,
        'timeframe': timeframe,
        'direction': direction,
        'entry': entry,
        'tier': tier,
        'status': 'active',
        'result': None,
        'pct': None,
    })
    await save_stats(stats)
    return stats['total_signals']

async def close_signal(signal_id, result, pct):
    if signal_id is None:
        print(f"⚠️ close_signal: signal_id is None, result={result}")
        return
    stats = await load_stats()
    # ... решта коду
    for s in stats['signals']:
        if s['id'] == signal_id:
            s['status'] = 'closed'
            s['result'] = result
            s['pct'] = pct
            break

    stats['active'] = max(0, stats['active'] - 1)
    if result == 'tp':
        stats['closed_tp'] += 1
    elif result == 'sl':
        stats['closed_sl'] += 1
    elif result == 'be':
        stats['closed_be'] += 1

    await save_stats(stats)

async def clear_stats():
    stats = {
        'total_signals': 0,
        'closed_tp': 0,
        'closed_sl': 0,
        'closed_be': 0,
        'active': 0,
        'signals': []
    }
    await save_stats(stats)

async def get_summary():
    stats = await load_stats()
    total = stats['total_signals']
    if total == 0:
        return "📊 Статистика бота\nСигналів ще не було"

    tp = stats['closed_tp']
    sl = stats['closed_sl']
    be = stats['closed_be']
    active = stats['active']
    closed = tp + sl + be

    positive = tp + be
    winrate = round(positive / closed * 100) if closed > 0 else 0

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
    ]

    tier_stats = {}
    for s in stats['signals']:
        tier = s.get('tier', '🟢')
        if tier not in tier_stats:
            tier_stats[tier] = {'tp': 0, 'sl': 0, 'be': 0}
        if s['result'] == 'tp':
            tier_stats[tier]['tp'] += 1
        elif s['result'] == 'sl':
            tier_stats[tier]['sl'] += 1
        elif s['result'] == 'be':
            tier_stats[tier]['be'] += 1

    if tier_stats:
        lines.append("")
        lines.append("Розбивка по Tier:")
        for tier, data in sorted(tier_stats.items()):
            total_tier = data['tp'] + data['sl'] + data['be']
            wr = round((data['tp'] + data['be']) / total_tier * 100) if total_tier > 0 else 0
            lines.append(f"{tier} TP:{data['tp']} БУ:{data['be']} SL:{data['sl']} | {wr}%")

    return "\n".join(lines)