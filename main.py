from settings import get_setting, set_setting
import matplotlib
matplotlib.use('Agg')  # вимикаємо GUI бекенд
import ccxt
import asyncio
import pandas as pd
import mplfinance as mpf
import matplotlib.pyplot as plt
from telegram import Bot
from telegram.request import HTTPXRequest
from dotenv import load_dotenv
from scanner import scan_all
from keep_alive import keep_alive
from stats import add_signal, close_signal, get_summary, clear_stats, load_stats, save_stats
from active_store import save_active, load_active, remove_active, clear_active
import os
import io
import sys

sys.stdout.reconfigure(line_buffering=True)

load_dotenv()
keep_alive()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
ADMIN_ID = os.getenv("ADMIN_ID")

request = HTTPXRequest(
    connection_pool_size=20,
    read_timeout=30,
    write_timeout=30,
    connect_timeout=30,
)

exchange = ccxt.binance({
    'enableRateLimit': True,
})

# Завантажуємо збережений таймфрейм
active_timeframe = get_setting('active_timeframe')

TIMEFRAME_OPTIONS = {
    'all': ['5m', '15m', '30m', '1h', '4h', '1d'],
    '5m': ['5m'],
    '15m': ['15m'],
    '30m': ['30m'],
    '1h': ['1h'],
    '4h': ['4h'],
    '1d': ['1d'],
}

def _get_candles_sync(symbol, timeframe, limit):
    limits = {'5m': 60, '15m': 50, '1h': 40, '4h': 30, '1d': 20}
    if limit is None:
        limit = limits.get(timeframe, 30)
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)
    return df

async def get_candles_main(symbol, timeframe, limit=None):
    return await asyncio.to_thread(_get_candles_sync, symbol, timeframe, limit)

def _get_price_sync(symbol):
    ticker = exchange.fetch_ticker(symbol)
    return ticker['last']

async def get_price(symbol):
    return await asyncio.to_thread(_get_price_sync, symbol)

def generate_chart(symbol, timeframe, direction, entry, dobar_low, dobar_high, tps, hit_tps=[], stop_loss=None, show_dobar=True, candles_df=None):
    df = candles_df

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

    if stop_loss:
        hlines.append(stop_loss)
        hline_colors.append('#e74c3c')

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

    if show_dobar:
        dobar_color = '#c0392b' if direction == 'SHORT' else '#2d7a2d'
        dobar_rect = plt.Rectangle(
            (x_left, min(dobar_low, dobar_high)),
            x_range, abs(dobar_high - dobar_low),
            color=dobar_color, alpha=0.25, zorder=0
        )
        ax.add_patch(dobar_rect)
        ax.text(
            x_right - x_range * 0.02,
            (dobar_low + dobar_high) / 2,
            'ДОБОР',
            fontsize=8, va='center', ha='right', color='white',
            bbox=dict(boxstyle='round,pad=0.2', facecolor=dobar_color, edgecolor='none', alpha=0.8)
        )
        ax.text(
            1.01, dobar_low, f'{dobar_low}',
            transform=ax.get_yaxis_transform(),
            fontsize=8, va='center', color='white',
            bbox=dict(boxstyle='round,pad=0.2', facecolor=dobar_color, edgecolor='none', alpha=0.8)
        )
        ax.text(
            1.01, dobar_high, f'{dobar_high}',
            transform=ax.get_yaxis_transform(),
            fontsize=8, va='center', color='white',
            bbox=dict(boxstyle='round,pad=0.2', facecolor=dobar_color, edgecolor='none', alpha=0.8)
        )

    label_color = '#c0392b' if direction == 'SHORT' else '#27ae60'
    x_label = x_right - x_range * 0.35
    ax.text(
        x_label, entry, f' {direction} ',
        fontsize=13, va='center', ha='center',
        color='white', fontweight='bold',
        bbox=dict(boxstyle='round,pad=0.4', facecolor=label_color, edgecolor='white', linewidth=1.5)
    )

    ax.text(
        1.01, entry, f'Entry  {entry}',
        transform=ax.get_yaxis_transform(),
        fontsize=9, va='center', color='white',
        bbox=dict(boxstyle='round,pad=0.3', facecolor='#1a6dcc', edgecolor='none')
    )

    if stop_loss:
        ax.text(
            1.01, stop_loss, f'SL  {stop_loss}',
            transform=ax.get_yaxis_transform(),
            fontsize=9, va='center', color='white',
            bbox=dict(boxstyle='round,pad=0.3', facecolor='#e74c3c', edgecolor='none')
        )

    tp_labels_list = ['TP1', 'TP2', 'TP3', 'TP4']
    for i, (tp_price, prob, pct) in enumerate(tps):
        is_hit = i in hit_tps
        checkmark = '✓ ' if is_hit else ''
        label = f'{checkmark}{tp_labels_list[i]}  {tp_price}  (-{pct}%)'
        bg = '#2d7a2d' if is_hit else '#1a1a1a'
        ax.text(
            1.01, tp_price, label,
            transform=ax.get_yaxis_transform(),
            fontsize=9, va='center', color='white',
            bbox=dict(boxstyle='round,pad=0.3', facecolor=bg, edgecolor='none')
        )

    for i in hit_tps:
        tp_price = tps[i][0]
        ax.plot(
            x_right - x_range * 0.05, tp_price,
            'o', markersize=10,
            markerfacecolor='white',
            markeredgecolor='#1a1a1a',
            markeredgewidth=1.5, zorder=5
        )
        ax.text(
            x_right - x_range * 0.05, tp_price, '✓',
            fontsize=7, va='center', ha='center',
            color='#1a1a1a', zorder=6
        )

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

def format_signal(symbol, timeframe, direction, entry, dobar_low, dobar_high, tps, stats, hit_tps=[], tier='🟢', stop_loss=None):
    dir_emoji = "📈" if direction == "LONG" else "📉"
    lines = []
    lines.append(f"#{symbol} {timeframe} {tier}")
    lines.append(f"💎 СТАТУС : {direction} {dir_emoji}")
    lines.append(f"")
    lines.append(f"👉 ENTRY : {entry}")
    lines.append(f"👉 ДОБОР : {dobar_low} — {dobar_high}")
    if stop_loss:
        lines.append(f"🛑 СТОП : {stop_loss}")
    lines.append(f"")

    tp_labels_list = ['TP1', 'TP2', 'TP3', 'TP4']
    for i, (tp_price, prob, pct) in enumerate(tps):
        check = "✅ " if i in hit_tps else ""
        if prob >= 70:
            fire = "🔥"
        elif prob >= 40:
            fire = "⚡"
        elif prob >= 20:
            fire = "🌡"
        else:
            fire = "❄️"
        lines.append(f"🎯 {check}{tp_labels_list[i]} : {tp_price} ({fire}{prob}%) | (💰{pct}%)")

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
    start_time = asyncio.get_event_loop().time()
    breakeven = False

    ccxt_symbol = symbol[:-4] + '/USDT' if symbol.endswith('USDT') else symbol
    print(f"Моніторинг {symbol} {signal['timeframe']} {direction}...")

    def elapsed_str():
        elapsed = int(asyncio.get_event_loop().time() - start_time)
        d = elapsed // 86400
        h = (elapsed % 86400) // 3600
        m = (elapsed % 3600) // 60
        return f"{d}d {h}h {m}m"

    while True:
        await asyncio.sleep(30)

        try:
            price = await get_price(ccxt_symbol)
        except Exception as e:
            print(f"Помилка отримання ціни {symbol}: {e}")
            continue

        stop_loss = signal.get('stop_loss')
        entry = signal['entry']

        # Перевірка стоп-лосс
        if stop_loss:
            sl_hit = (direction == 'SHORT' and price >= stop_loss) or \
                     (direction == 'LONG' and price <= stop_loss)

            if sl_hit:
                elapsed = elapsed_str()

                total_profit = 0.0
                tp_summary_lines = []
                for i in sorted(hit_tps):
                    tp_pct_i = tps[i][2]
                    total_profit += tp_pct_i
                    tp_summary_lines.append(f"✅ TP{i+1}: {tps[i][0]} | +{tp_pct_i}%")

                msg_lines = [
                    f"#{symbol} {signal['timeframe']} "
                    f"{'LONG 📈' if direction == 'LONG' else 'SHORT 📉'}",
                    f"",
                ]

                # Якщо стоп переведено в БУ і є досягнуті TP — позитивний результат
                if breakeven and hit_tps:
                    msg_lines.extend(tp_summary_lines)
                    msg_lines.append(f"")
                    msg_lines.append(f"↩️ Сигнал закрито в беззбиток ({elapsed})")
                    msg_lines.append(f"💰 Загальний прибуток: +{round(total_profit, 1)}%")

                    try:
                        await bot.send_message(
                            chat_id=CHAT_ID,
                            text="\n".join(msg_lines),
                            reply_to_message_id=chart_message_id
                        )
                    except Exception as e:
                        print(f"Помилка відправки БУ {symbol}: {e}")

                    await close_signal(signal.get('stat_id'), 'tp', round(total_profit, 1))
                    await remove_active(symbol, signal['timeframe'])
                    break

                else:
                    # Справжній стоп без жодного TP
                    sl_pct = round(abs(price - entry) / entry * 100, 1)
                    msg_lines.append(f"🛑 Stop-Loss спрацював ({elapsed}) | -{sl_pct}%")
                    msg_lines.append(f"💸 TP не було досягнуто")
                    msg_lines.append(f"❌ Сигнал закрито по стопу")

                    try:
                        await bot.send_message(
                            chat_id=CHAT_ID,
                            text="\n".join(msg_lines),
                            reply_to_message_id=chart_message_id
                        )
                    except Exception as e:
                        print(f"Помилка відправки СЛ {symbol}: {e}")

                    await close_signal(signal.get('stat_id'), 'sl', -sl_pct)
                    await remove_active(symbol, signal['timeframe'])
                    break

        # Перевірка БУ після TP1 — ціна повернулась до входу
        if breakeven and 0 in hit_tps:
            be_hit = (direction == 'SHORT' and price >= entry) or \
                     (direction == 'LONG' and price <= entry)

            if be_hit:
                elapsed = elapsed_str()

                total_profit = 0.0
                tp_summary_lines = []
                for i in sorted(hit_tps):
                    tp_pct_i = tps[i][2]
                    total_profit += tp_pct_i
                    tp_summary_lines.append(f"✅ TP{i+1}: {tps[i][0]} | +{tp_pct_i}%")

                msg_lines = [
                    f"#{symbol} {signal['timeframe']} "
                    f"{'LONG 📈' if direction == 'LONG' else 'SHORT 📉'}",
                    f"",
                ]
                msg_lines.extend(tp_summary_lines)
                msg_lines.append(f"")
                msg_lines.append(f"↩️ Сигнал закрито в беззбиток ({elapsed})")
                msg_lines.append(f"💰 Загальний прибуток: +{round(total_profit, 1)}%")

                try:
                    await bot.send_message(
                        chat_id=CHAT_ID,
                        text="\n".join(msg_lines),
                        reply_to_message_id=chart_message_id
                    )
                except Exception as e:
                    print(f"Помилка відправки БУ {symbol}: {e}")

                await close_signal(signal.get('stat_id'), 'tp', round(total_profit, 1))
                await remove_active(symbol, signal['timeframe'])
                break

        # Перевірка досягнення TP
        new_hits = set()
        for i, (tp_price, prob, pct) in enumerate(tps):
            if direction == 'SHORT' and price <= tp_price:
                new_hits.add(i)
            elif direction == 'LONG' and price >= tp_price:
                new_hits.add(i)

        if new_hits - hit_tps:
            hit_tps = hit_tps | new_hits
            elapsed = elapsed_str()
            print(f"✅ {symbol} досягнуто TP: {hit_tps}")

            if 0 in hit_tps and not breakeven:
                breakeven = True
                signal['stop_loss'] = entry
                signal['show_dobar'] = False
                print(f"🔄 {symbol} стоп переведено в БУ: {entry}")

            new_text = format_signal(
                symbol, signal['timeframe'], direction,
                signal['entry'], signal['dobar_low'], signal['dobar_high'],
                tps, signal['stats'], hit_tps,
                tier=signal.get('tier', '🟢'),
                stop_loss=signal.get('stop_loss')
            )

            try:
                candles_df = await get_candles_main(ccxt_symbol, signal['timeframe'])
                new_chart = await asyncio.to_thread(
                    generate_chart,
                    symbol, signal['timeframe'], direction,
                    signal['entry'], signal['dobar_low'], signal['dobar_high'],
                    tps, list(hit_tps),
                    signal.get('stop_loss'),
                    signal.get('show_dobar', True),
                    candles_df
                )
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

            # Всі TP досягнуті
            if len(hit_tps) == len(tps):
                last_tp = max(hit_tps)
                tp_price_final = tps[last_tp][0]
                tp_pct_final = tps[last_tp][2]
                total_profit = sum(tps[i][2] for i in hit_tps)

                tp_summary_lines = []
                for i in sorted(hit_tps):
                    tp_summary_lines.append(
                        f"✅ TP{i+1}: {tps[i][0]} | +{tps[i][2]}%"
                    )

                msg_lines = [
                    f"#{symbol} {signal['timeframe']} "
                    f"{'LONG 📈' if direction == 'LONG' else 'SHORT 📉'}",
                    f"",
                ]
                msg_lines.extend(tp_summary_lines)
                msg_lines.append(f"")
                msg_lines.append(f"🎯 TP{last_tp+1}: {tp_price_final} ✅ ({elapsed})")
                msg_lines.append(f"💰 Загальний прибуток: +{round(total_profit, 1)}%")
                msg_lines.append(f"🏁 Сигнал закрито")

                try:
                    await bot.send_message(
                        chat_id=CHAT_ID,
                        text="\n".join(msg_lines),
                        reply_to_message_id=chart_message_id
                    )
                except Exception as e:
                    print(f"Помилка відправки закриття {symbol}: {e}")

                await close_signal(signal.get('stat_id'), 'tp', round(total_profit, 1))
                await remove_active(symbol, signal['timeframe'])
                break

async def scan_and_send(bot, active_signals, timeframes):
    all_signals = await scan_all(timeframes)
    new_count = 0

    for signal in all_signals:
        if new_count >= 3:
            break

        # 1. Створюємо "чистий" символ без слеша (BTCUSDT)
        symbol_clean = signal['symbol'].replace('/', '')
        
        # 2. Перевіряємо, чи є вже такий самий сигнал (символ + ТФ) в активних
        # Ми перевіряємо і символ, і таймфрейм, щоб дозволити торгувати
        # одну монету на різних ТФ (якщо хочеш), або заборонити (за бажанням)
        
        # Якщо хочеш дозволити лише ОДИН сигнал на монету незалежно від ТФ:
        is_already_monitored = any(s['symbol'].replace('/', '') == symbol_clean for s in active_signals)
        
        if is_already_monitored:
            print(f"⚠️ {symbol_clean} вже моніториться — пропускаємо")
            continue

        try:
            # 3. Формуємо текст і графік
            signal_text = format_signal(
                symbol_clean, signal['timeframe'],
                signal['direction'], signal['entry'],
                signal['dobar_low'], signal['dobar_high'],
                signal['tps'], signal['stats'],
                tier=signal.get('tier', '🟢'),
                stop_loss=signal.get('stop_loss')
            )

            ccxt_symbol = symbol_clean[:-4] + '/USDT'
            candles_df = await get_candles_main(ccxt_symbol, signal['timeframe'])
            
            chart = await asyncio.to_thread(
                generate_chart,
                symbol_clean, signal['timeframe'],
                signal['direction'], signal['entry'],
                signal['dobar_low'], signal['dobar_high'],
                signal['tps'],[], signal.get('stop_loss'),
                True, candles_df
            )

            sent = await bot.send_photo(
                chat_id=CHAT_ID,
                photo=chart,
                caption=signal_text,
                read_timeout=30,
                write_timeout=30,
                connect_timeout=30,
            )

            # 4. Додаємо в активні та зберігаємо
            signal['chart_message_id'] = sent.message_id
            signal['symbol'] = symbol_clean # Зберігаємо "чистий" символ
            
            active_signals.append(signal)
            await save_active(active_signals)
            new_count += 1

            # 5. Статистика
            signal_id = await add_signal(
                symbol_clean,
                signal['timeframe'],
                signal['direction'],
                signal['entry'],
                signal.get('tier', '🟢')
            )
            signal['stat_id'] = signal_id

            asyncio.create_task(monitor_signal(bot, signal))
            await asyncio.sleep(5)

        except Exception as e:
            print(f"Помилка відправки {symbol_clean}: {e}")
            await asyncio.sleep(10)

    if new_count > 0:
        print(f"Відправлено {new_count} нових сигналів.")
        
def get_timeframe_keyboard(current):
    options = ['all', '5m', '15m', '30m', '1h', '4h', '1d']
    labels = {
        'all': '🌐 Всі', '5m': '5m', '15m': '15m',
        '30m': '30m', '1h': '1h', '4h': '4h', '1d': '1d'
    }
    keyboard = []
    row = []
    for opt in options:
        mark = '✅ ' if opt == current else ''
        row.append({'text': f'{mark}{labels[opt]}', 'callback_data': f'tf_{opt}'})
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([{'text': '🔙 Назад', 'callback_data': 'back'}])
    return keyboard

async def handle_updates(bot, active_signals):
    global active_timeframe
    offset = None

    while True:
        try:
            updates = await bot.get_updates(offset=offset, timeout=10)
            for update in updates:
                offset = update.update_id + 1

                if update.message and update.message.text:
                    text = update.message.text
                    chat_id = update.message.chat.id
                    user_id = update.message.from_user.id

                    if str(user_id) != str(ADMIN_ID):
                        await bot.send_message(
                            chat_id=chat_id,
                            text="⛔ Доступ заборонено"
                        )
                        continue

                    if text == '/start':
                        await bot.send_message(
                            chat_id=chat_id,
                            text="🤖 Сигнальний бот\nОберіть дію:",
                            reply_markup={
                                'inline_keyboard': [
                                    [{'text': '📊 Статистика', 'callback_data': 'stats'}],
                                    [{'text': '⏳ Активні сигнали', 'callback_data': 'active'}],
                                    [{'text': '⏱ Таймфрейм', 'callback_data': 'timeframe'}],
                                    [{'text': 'ℹ️ Про бота', 'callback_data': 'info'}],
                                    [{'text': '🗑 Очистити статистику', 'callback_data': 'clear'}],
                                    [{'text': '🔴 Закрити всі сигнали', 'callback_data': 'clear_active'}],
                                ]
                            }
                        )

                    elif text == '/stats':
                        summary = await get_summary()
                        await bot.send_message(chat_id=chat_id, text=summary)

                    elif text == '/active':
                        if not active_signals:
                            msg = "⏳ Активних сигналів немає"
                        else:
                            lines = ["⏳ Активні сигнали:\n"]
                            for s in active_signals:
                                lines.append(
                                    f"{s.get('tier','🟢')} #{s['symbol']} "
                                    f"{s['timeframe']} {s['direction']} | "
                                    f"Entry: {s['entry']}"
                                )
                            msg = "\n".join(lines)
                        await bot.send_message(chat_id=chat_id, text=msg)

                if update.callback_query:
                    query = update.callback_query
                    chat_id = query.message.chat.id
                    user_id = query.from_user.id

                    if str(user_id) != str(ADMIN_ID):
                        await bot.answer_callback_query(
                            query.id, text="⛔ Доступ заборонено"
                        )
                        continue

                    await bot.answer_callback_query(query.id)

                    if query.data == 'stats':
                        summary = await get_summary()
                        await bot.send_message(chat_id=chat_id, text=summary)

                    elif query.data == 'active':
                        if not active_signals:
                            msg = "⏳ Активних сигналів немає"
                        else:
                            lines = ["⏳ Активні сигнали:\n"]
                            for s in active_signals:
                                lines.append(
                                    f"{s.get('tier','🟢')} #{s['symbol']} "
                                    f"{s['timeframe']} {s['direction']} | "
                                    f"Entry: {s['entry']}"
                                )
                            msg = "\n".join(lines)
                        await bot.send_message(chat_id=chat_id, text=msg)

                    elif query.data == 'timeframe':
                        await bot.send_message(
                            chat_id=chat_id,
                            text=f"⏱ Поточний таймфрейм: {active_timeframe}\nОберіть таймфрейм:",
                            reply_markup={
                                'inline_keyboard': get_timeframe_keyboard(active_timeframe)
                            }
                        )

                    elif query.data.startswith('tf_'):
                        tf = query.data.replace('tf_', '')
                        active_timeframe = tf
                        set_setting('active_timeframe', tf)
                        print(f"⏱ Таймфрейм змінено на: {tf}")

                        await bot.send_message(
                            chat_id=chat_id,
                            text=f"✅ Таймфрейм змінено на: {tf}\n"
                                 f"Сканування: {TIMEFRAME_OPTIONS.get(tf, [tf])}\n"
                                 f"⏳ Запускаю сканування...",
                            reply_markup={
                                'inline_keyboard': get_timeframe_keyboard(active_timeframe)
                            }
                        )

                        # Одразу запускаємо сканування на новому таймфреймі
                        try:
                            timeframes_to_scan = TIMEFRAME_OPTIONS.get(tf, [tf])
                            await scan_and_send(bot, active_signals, timeframes_to_scan)
                            await bot.send_message(
                                chat_id=chat_id,
                                text="✅ Сканування завершено!"
                            )
                        except Exception as e:
                            print(f"Помилка сканування після зміни таймфрейму: {e}")
                            await bot.send_message(
                                chat_id=chat_id,
                                text=f"❌ Помилка сканування: {e}"
                            )

                    elif query.data == 'info':
                        msg = (
                            "🤖 Сигнальний бот\n\n"
                            "📊 Біржа: Binance Futures\n"
                            "⏱ Таймфрейми: 5m · 15m · 30m · 1h · 4h · 1d\n"
                            "🔍 Індикатори: EMA20 · EMA50 · RSI · ATR\n"
                            "✅ Фільтр: TP1 ≥ 60% · мін. 15 угод\n"
                            "🛑 Стоп-лосс: ATR × 2.0\n"
                            "↩️ БУ: після досягнення TP1\n"
                        )
                        await bot.send_message(chat_id=chat_id, text=msg)

                    elif query.data == 'clear':
                        await bot.send_message(
                            chat_id=chat_id,
                            text="⚠️ Ви впевнені що хочете очистити статистику?",
                            reply_markup={
                                'inline_keyboard': [
                                    [
                                        {'text': '✅ Так, очистити', 'callback_data': 'clear_confirm'},
                                        {'text': '❌ Скасувати', 'callback_data': 'clear_cancel'},
                                    ]
                                ]
                            }
                        )

                    elif query.data == 'clear_confirm':
                        await clear_stats()
                        await bot.send_message(
                            chat_id=chat_id,
                            text="✅ Статистику очищено!"
                        )

                    elif query.data == 'clear_cancel':
                        await bot.send_message(
                            chat_id=chat_id,
                            text="❌ Очищення скасовано"
                        )

                    elif query.data == 'clear_active':
                        await bot.send_message(
                            chat_id=chat_id,
                            text="⚠️ Закрити всі активні сигнали?",
                            reply_markup={
                                'inline_keyboard': [
                                    [
                                        {'text': '✅ Так', 'callback_data': 'clear_active_confirm'},
                                        {'text': '❌ Ні', 'callback_data': 'clear_active_cancel'},
                                    ]
                                ]
                            }
                        )

                    elif query.data == 'clear_active_confirm':
                        await clear_active()
                        active_signals.clear()
                        await bot.send_message(
                            chat_id=chat_id,
                            text="✅ Всі активні сигнали закрито!"
                        )

                    elif query.data == 'clear_active_cancel':
                        await bot.send_message(
                            chat_id=chat_id,
                            text="❌ Скасовано"
                        )

                    elif query.data == 'back':
                        await bot.send_message(
                            chat_id=chat_id,
                            text="🤖 Сигнальний бот\nОберіть дію:",
                            reply_markup={
                                'inline_keyboard': [
                                    [{'text': '📊 Статистика', 'callback_data': 'stats'}],
                                    [{'text': '⏳ Активні сигнали', 'callback_data': 'active'}],
                                    [{'text': '⏱ Таймфрейм', 'callback_data': 'timeframe'}],
                                    [{'text': 'ℹ️ Про бота', 'callback_data': 'info'}],
                                    [{'text': '🗑 Очистити статистику', 'callback_data': 'clear'}],
                                    [{'text': '🔴 Закрити всі сигнали', 'callback_data': 'clear_active'}],
                                ]
                            }
                        )

        except Exception as e:
            print(f"Помилка обробки оновлень: {e}")
            await asyncio.sleep(5)

async def main():
    global active_timeframe
    bot = Bot(token=BOT_TOKEN, request=request)

    active_signals = await load_active()
    print(f"Завантажено {len(active_signals)} активних сигналів з диску")

    for signal in active_signals:
        if signal.get('chart_message_id'):
            asyncio.create_task(monitor_signal(bot, signal))
            print(f"🔄 Відновлено моніторинг: {signal['symbol']} {signal['timeframe']}")

            # Якщо немає stat_id — додаємо в статистику
            if not signal.get('stat_id'):
                signal_id = await add_signal(
                    signal['symbol'],
                    signal['timeframe'],
                    signal['direction'],
                    signal['entry'],
                    signal.get('tier', '🟢')
                )
                signal['stat_id'] = signal_id
                print(f"📊 Додано в статистику: {signal['symbol']} {signal['timeframe']}")

    # Зберігаємо оновлені stat_id
    await save_active(active_signals)

    # Синхронізуємо кількість активних в статистиці
    stats = await load_stats()
    stats['active'] = len(active_signals)
    await save_stats(stats)
    print(f"📊 Синхронізовано статистику: {len(active_signals)} активних")

    async def loop_5m():
        while True:
            try:
                if active_timeframe in ('all', '5m'):
                    print("Сканування 5m...")
                    await scan_and_send(bot, active_signals, ['5m'])
            except Exception as e:
                print(f"Помилка loop_5m: {e}")
            await asyncio.sleep(5 * 60)

    async def loop_15m():
        while True:
            try:
                if active_timeframe in ('all', '15m'):
                    print("Сканування 15m...")
                    await scan_and_send(bot, active_signals, ['15m'])
            except Exception as e:
                print(f"Помилка loop_15m: {e}")
            await asyncio.sleep(15 * 60)

    async def loop_30m():
        while True:
            try:
                if active_timeframe in ('all', '30m'):
                    print("Сканування 30m...")
                    await scan_and_send(bot, active_signals, ['30m'])
            except Exception as e:
                print(f"Помилка loop_30m: {e}")
            await asyncio.sleep(30 * 60)

    async def loop_1h():
        while True:
            try:
                if active_timeframe in ('all', '1h'):
                    print("Сканування 1h...")
                    await scan_and_send(bot, active_signals, ['1h'])
            except Exception as e:
                print(f"Помилка loop_1h: {e}")
            await asyncio.sleep(30 * 60)

    async def loop_4h():
        while True:
            try:
                if active_timeframe in ('all', '4h'):
                    print("Сканування 4h...")
                    await scan_and_send(bot, active_signals, ['4h'])
            except Exception as e:
                print(f"Помилка loop_4h: {e}")
            await asyncio.sleep(60 * 60)

    async def loop_1d():
        while True:
            try:
                if active_timeframe in ('all', '1d'):
                    print("Сканування 1d...")
                    await scan_and_send(bot, active_signals, ['1d'])
            except Exception as e:
                print(f"Помилка loop_1d: {e}")
            await asyncio.sleep(4 * 60 * 60)

    async def daily_stats():
        while True:
            await asyncio.sleep(24 * 60 * 60)
            summary = await get_summary()
            try:
                await bot.send_message(chat_id=CHAT_ID, text=summary)
            except Exception as e:
                print(f"Помилка відправки статистики: {e}")

    await asyncio.gather(
        loop_5m(),
        loop_15m(),
        loop_30m(),
        loop_1h(),
        loop_4h(),
        loop_1d(),
        daily_stats(),
        handle_updates(bot, active_signals),
    )

async def safe_main():
    try:
        await main()
    except Exception as e:
        print(f"КРИТИЧНА ПОМИЛКА: {e}")
        import traceback
        traceback.print_exc()

asyncio.run(safe_main())