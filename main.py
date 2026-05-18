from keep_alive import keep_alive
keep_alive()
import ccxt
import asyncio
import pandas as pd
import mplfinance as mpf
import matplotlib.pyplot as plt
from telegram import Bot
from dotenv import load_dotenv
from scanner import scan_all
import os
import io
from telegram.request import HTTPXRequest

# Збільшуємо пул з'єднань
request = HTTPXRequest(
    connection_pool_size=20,
    read_timeout=30,
    write_timeout=30,
    connect_timeout=30,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

exchange = ccxt.binance()

def get_candles(symbol, timeframe, limit=None):
    if limit is None:
        limits = {
            '5m': 60,
            '15m': 50,
            '1h': 40,
            '4h': 30,
            '1d': 20,
        }
        limit = limits.get(timeframe, 30)

    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)
    return df

def get_price(symbol):
    ticker = exchange.fetch_ticker(symbol)
    return ticker['last']

def generate_chart(symbol, timeframe, direction, entry, dobar_low, dobar_high, tps, hit_tps=[]):
    ccxt_symbol = symbol[:-4] + '/USDT' if symbol.endswith('USDT') else symbol
    df = get_candles(ccxt_symbol, timeframe, limit=40)

    bg_color = '#f28b82' if direction == 'SHORT' else '#90c97a'
    up_color = '#2d7a2d'
    down_color = '#c0392b'

    mc = mpf.make_marketcolors(
        up=up_color, down=down_color,
        edge={'up': up_color, 'down': down_color},
        wick={'up': up_color, 'down': down_color},
        volume='inherit'
    )
    style = mpf.make_mpf_style(
        marketcolors=mc,
        facecolor=bg_color, edgecolor=bg_color,
        gridcolor=bg_color, figcolor=bg_color,
        rc={'axes.labelcolor': bg_color, 'xtick.color': bg_color, 'ytick.color': bg_color}
    )

    hlines = [entry] + [tp[0] for tp in tps]
    hline_colors = ['#1a6dcc'] + ['#1a1a1a'] * len(tps)

    fig, ax = mpf.plot(
        df, type='candle', style=style,
        hlines=dict(hlines=hlines, colors=hline_colors, linestyle='--', linewidths=0.8),
        returnfig=True, figsize=(14, 4), axisoff=True,
    )

    ax = fig.axes[0]
    xlim = ax.get_xlim()
    x_right = xlim[1]
    x_left = xlim[0]
    x_range = x_right - x_left

    # Зона ДОБОРУ
    dobar_color = '#c0392b' if direction == 'SHORT' else '#2d7a2d'
    dobar_rect = plt.Rectangle(
        (x_left, min(dobar_low, dobar_high)),
        x_range,
        abs(dobar_high - dobar_low),
        color=dobar_color,
        alpha=0.25,
        zorder=0
    )
    ax.add_patch(dobar_rect)

    ax.text(
        x_right - x_range * 0.02,
        (dobar_low + dobar_high) / 2,
        'ДОБОР',
        fontsize=8, va='center', ha='right',
        color='white',
        bbox=dict(boxstyle='round,pad=0.2', facecolor=dobar_color, edgecolor='none', alpha=0.8)
    )
    # Підписи рівнів добору
    ax.text(
        1.01, dobar_low,
        f'{dobar_low}',
        transform=ax.get_yaxis_transform(),
        fontsize=8, va='center', color='white',
        bbox=dict(boxstyle='round,pad=0.2', facecolor=dobar_color, edgecolor='none', alpha=0.8)
    )
    ax.text(
        1.01, dobar_high,
        f'{dobar_high}',
        transform=ax.get_yaxis_transform(),
        fontsize=8, va='center', color='white',
        bbox=dict(boxstyle='round,pad=0.2', facecolor=dobar_color, edgecolor='none', alpha=0.8)
    )

    # Мітка SHORT/LONG
    label_color = '#c0392b' if direction == 'SHORT' else '#27ae60'
    x_label = x_right - x_range * 0.35
    y_label = entry

    ax.text(
        x_label, y_label,
        f' {direction} ',
        fontsize=13, va='center', ha='center',
        color='white', fontweight='bold',
        bbox=dict(boxstyle='round,pad=0.4', facecolor=label_color, edgecolor='white', linewidth=1.5)
    )

    # Entry підпис
    ax.text(
        1.01, entry, f'Entry  {entry}',
        transform=ax.get_yaxis_transform(),
        fontsize=9, va='center', color='white',
        bbox=dict(boxstyle='round,pad=0.3', facecolor='#1a6dcc', edgecolor='none')
    )

    # TP підписи
    tp_labels = ['TP1', 'TP2', 'TP3', 'TP4']
    for i, (tp_price, prob, pct) in enumerate(tps):
        is_hit = i in hit_tps
        checkmark = '✓ ' if is_hit else ''
        label = f'{checkmark}{tp_labels[i]}  {tp_price}  (-{pct}%)'
        bg = '#2d7a2d' if is_hit else '#1a1a1a'
        ax.text(
            1.01, tp_price, label,
            transform=ax.get_yaxis_transform(),
            fontsize=9, va='center', color='white',
            bbox=dict(boxstyle='round,pad=0.3', facecolor=bg, edgecolor='none')
        )

    # Кола на досягнутих TP
    for i in hit_tps:
        tp_price = tps[i][0]
        ax.plot(
            x_right - x_range * 0.05,
            tp_price,
            'o', markersize=10,
            markerfacecolor='white',
            markeredgecolor='#1a1a1a',
            markeredgewidth=1.5,
            zorder=5
        )
        ax.text(
            x_right - x_range * 0.05,
            tp_price,
            '✓',
            fontsize=7, va='center', ha='center',
            color='#1a1a1a', zorder=6
        )

    # Заголовок
    dir_text = 'SHORT' if direction == 'SHORT' else 'LONG'
    ax.set_title(
        f'{symbol} · {timeframe} · {dir_text}',
        loc='right', fontsize=11, color='#1a1a1a', pad=10
    )

    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight',
                facecolor=bg_color, dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf

def format_signal(symbol, timeframe, direction, entry, dobar_low, dobar_high, tps, stats, hit_tps=[]):
    tier = "🟢"
    dir_emoji = "📈" if direction == "LONG" else "📉"
    lines = []
    lines.append(f"#{symbol} {timeframe} {tier}")
    lines.append(f"💎 СТАТУС : {direction} {dir_emoji}")
    lines.append(f"")
    lines.append(f"👉 ENTRY : {entry}")
    lines.append(f"👉 ДОБОР : {dobar_low} — {dobar_high}")
    lines.append(f"")

    tp_labels = ['TP1', 'TP2', 'TP3', 'TP4']
    for i, (tp_price, prob, pct) in enumerate(tps):
        check = "✅ " if i in hit_tps else ""
        lines.append(f"🎯 {check}{tp_labels[i]} : {tp_price} (🔥{prob}%) | (💰{pct}%)")

    lines.append(f"")
    if stats['count'] > 0:
        lines.append(f"📊 {stats['count']} сигналів")
        lines.append(f"📉 Середнє відхилення: {stats['avg_dev']}%")
        for dev, cnt in stats['deviations'].items():
            lines.append(f"📉 Відхилення ≥ {dev}%: {cnt}")

    return "\n".join(lines)

async def monitor_signal(bot, signal):
    symbol = signal['symbol']
    direction = signal['direction']
    tps = signal['tps']
    hit_tps = set()
    chart_message_id = signal['chart_message_id']

    ccxt_symbol = symbol[:-4] + '/USDT' if symbol.endswith('USDT') else symbol
    print(f"Моніторинг {symbol} {signal['timeframe']} {direction}...")

    while len(hit_tps) < len(tps):
        await asyncio.sleep(30)

        try:
            price = get_price(ccxt_symbol)
        except Exception as e:
            print(f"Помилка отримання ціни {symbol}: {e}")
            continue

        new_hits = set()
        for i, (tp_price, prob, pct) in enumerate(tps):
            if direction == 'SHORT' and price <= tp_price:
                new_hits.add(i)
            elif direction == 'LONG' and price >= tp_price:
                new_hits.add(i)

        if new_hits - hit_tps:
            hit_tps = hit_tps | new_hits
            print(f"✅ {symbol} досягнуто TP: {hit_tps}")

            new_text = format_signal(
                symbol, signal['timeframe'], direction,
                signal['entry'], signal['dobar_low'], signal['dobar_high'],
                tps, signal['stats'], hit_tps
            )

            new_chart = generate_chart(
                symbol, signal['timeframe'], direction,
                signal['entry'], signal['dobar_low'], signal['dobar_high'],
                tps, list(hit_tps)
            )

            try:
                await bot.send_photo(
                    chat_id=CHAT_ID,
                    photo=new_chart,
                    caption=new_text,
                    reply_to_message_id=chart_message_id,
                    read_timeout=30,
                    write_timeout=30,
                    connect_timeout=30,
                )
            except Exception as e:
                print(f"Помилка відправки оновлення {symbol}: {e}")

            if len(hit_tps) == len(tps):
                try:
                    await bot.send_message(
                        chat_id=CHAT_ID,
                        text=f"🏁 #{symbol} {signal['timeframe']} — Сигнал закрито! Всі цілі досягнуті ✅",
                        reply_to_message_id=chart_message_id
                    )
                except Exception as e:
                    print(f"Помилка відправки закриття {symbol}: {e}")
                break

async def scan_and_send(bot, active_signals, timeframes):
    all_signals = scan_all(timeframes)
    new_count = 0

    for signal in all_signals:
        if new_count >= 3:
            break

        key = f"{signal['symbol']}_{signal['timeframe']}"
        existing_keys = [f"{s['symbol']}_{s['timeframe']}" for s in active_signals]

        if key not in existing_keys:
            try:
                signal_text = format_signal(
                    signal['symbol'], signal['timeframe'],
                    signal['direction'], signal['entry'],
                    signal['dobar_low'], signal['dobar_high'],
                    signal['tps'], signal['stats']
                )

                chart = generate_chart(
                    signal['symbol'], signal['timeframe'],
                    signal['direction'], signal['entry'],
                    signal['dobar_low'], signal['dobar_high'],
                    signal['tps']
                )

                sent = await bot.send_photo(
                    chat_id=CHAT_ID,
                    photo=chart,
                    caption=signal_text,
                    read_timeout=30,
                    write_timeout=30,
                    connect_timeout=30,
                )

                signal['chart_message_id'] = sent.message_id
                active_signals.append(signal)
                new_count += 1

                asyncio.create_task(monitor_signal(bot, signal))
                await asyncio.sleep(5)

            except Exception as e:
                print(f"Помилка відправки {signal['symbol']}: {e}")
                await asyncio.sleep(10)

    if new_count > 0:
        print(f"Відправлено {new_count} нових сигналів.")

async def main():
    bot = Bot(token=BOT_TOKEN, request=request)
    active_signals = []

    async def loop_5m():
        while True:
            print("Сканування 5m...")
            await scan_and_send(bot, active_signals, ['5m'])
            await asyncio.sleep(5 * 60)

    async def loop_15m():
        while True:
            print("Сканування 15m...")
            await scan_and_send(bot, active_signals, ['15m'])
            await asyncio.sleep(15 * 60)

    async def loop_1h():
        while True:
            print("Сканування 1h...")
            await scan_and_send(bot, active_signals, ['1h'])
            await asyncio.sleep(30 * 60)

    async def loop_4h():
        while True:
            print("Сканування 4h...")
            await scan_and_send(bot, active_signals, ['4h'])
            await asyncio.sleep(60 * 60)

    async def loop_1d():
        while True:
            print("Сканування 1d...")
            await scan_and_send(bot, active_signals, ['1d'])
            await asyncio.sleep(4 * 60 * 60)

    await asyncio.gather(
        loop_5m(),
        loop_15m(),
        loop_1h(),
        loop_4h(),
        loop_1d(),
    )

asyncio.run(main())