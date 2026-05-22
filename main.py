from settings import get_setting, set_setting, get_exchange_client
from settings_menu import (
    main_settings_keyboard,
    get_settings_text,
    pairs_keyboard,
    timeframes_keyboard,
    risk_keyboard,
    filters_keyboard,
)
import matplotlib
matplotlib.use('Agg')
import ccxt
import asyncio
import pandas as pd
import mplfinance as mpf
import matplotlib.pyplot as plt
from telegram import Bot
from telegram.request import HTTPXRequest
from dotenv import load_dotenv
from scanner import scan_all
import scanner  # Імпортуємо для гарячої зміни біржі в сканері
from keep_alive import keep_alive
from database import (
    init_db,
    save_active_signals, load_active_signals,
    remove_active_signal, clear_active_signals,
    add_signal_stat, close_signal_stat,
    get_stats_summary, clear_stats
)
import os
import io
import sys
import urllib3
import time

# Вимикає довгі технічні попередження про неперевірений SSL у консолі
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

sys.stdout.reconfigure(line_buffering=True)

recently_sent = set()

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

# Ініціалізація динамічного клієнта біржі для малювання графіків
exchange = get_exchange_client(async_mode=False)

active_timeframe = get_setting('active_timeframe')

TIMEFRAME_OPTIONS = {
    'all': ['5m', '15m', '30m', '1h', '4h', '1d'],
    '5m': ['5m'], '15m': ['15m'], '30m': ['30m'],
    '1h': ['1h'], '4h': ['4h'], '1d': ['1d'],
}

ALL_PAIRS = [
    'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT',
    'XRP/USDT', 'DOGE/USDT', 'ADA/USDT', 'AVAX/USDT',
    'DOT/USDT', 'POL/USDT', 'LINK/USDT', 'UNI/USDT',
    'ATOM/USDT', 'LTC/USDT', 'ETC/USDT', 'FIL/USDT',
]

ALL_TIMEFRAMES = ['5m', '15m', '30m', '1h', '4h', '1d']


# ─────────────────────────────────────────────
# GLOBAL KEYBOARDS (Глобально видимі функції на початку файлу)
# ─────────────────────────────────────────────

def main_menu_keyboard():
    """Головне меню робота (з інтеграцією кнопки ручного сканування)"""
    return {
        'inline_keyboard': [
            [{'text': '🔍 Сканувати зараз', 'callback_data': 'cfg_scan_now'}],
            [{'text': '📊 Статистика', 'callback_data': 'stats'}],
            [{'text': '⏳ Активні сигнали', 'callback_data': 'active'}],
            [{'text': '⚙️ Налаштування', 'callback_data': 'cfg_main'}],
            [{'text': 'ℹ️ Про бота', 'callback_data': 'info'}],
            [{'text': '🗑 Очистити статистику', 'callback_data': 'clear'}],
            [{'text': '🔴 Закрити всі сигнали', 'callback_data': 'clear_active'}],
        ]
    }


# ─────────────────────────────────────────────
# CHARTS & FORMATTING (ДИНАМІЧНИЙ РИЗИК ТА ПОСИЛАННЯ)
# ─────────────────────────────────────────────

is_scanning = False  # Глобальний запобіжник паралельних сканувань

def get_exchange_link(symbol):
    """Генерує точне торгове посилання для ф'ючерсів Binance або MEXC"""
    exchange_name = get_setting('exchange_name') or 'binance'
    symbol_clean = symbol.replace('/', '').upper()
    if exchange_name == 'mexc':
        # Для MEXC використовується формат монета_USDT
        base = symbol_clean[:-4]
        return f"https://futures.mexc.com/exchange/{base}_USDT"
    else:
        return f"https://www.binance.com/en/futures/{symbol_clean}"

def calculate_position_size_v2(entry, stop_loss, dobar_low=None, dobar_high=None):
    """Розрахунок об'єму позиції та маржі з підтримкою кредитного плеча та тактики 1 усереднення (Добір)"""
    portfolio_size = get_setting('portfolio_size') or 1000.0
    risk_pct = get_setting('risk_pct') or 1.0
    leverage = get_setting('leverage') or 20
    use_dobar = get_setting('use_dobar')
    if use_dobar is None:
        use_dobar = True

    # Сума ризику в доларах
    risk_amount = portfolio_size * (risk_pct / 100.0)
    
    # Визначаємо середню ціну входу
    actual_entry = entry
    is_averaged = False
    dobar_price = 0.0
    
    if use_dobar and dobar_low is not None and dobar_high is not None:
        # Середня ціна зони добору
        dobar_price = (dobar_low + dobar_high) / 2.0
        # Оскільки входимо 50% на Entry та 50% на Dobar, середня ціна входу зміщується
        actual_entry = (entry + dobar_price) / 2.0
        is_averaged = True

    # Відсоткова відстань до стоп-лоссу від фактичної середньої ціни входу
    stop_distance_pct = abs(actual_entry - stop_loss) / actual_entry
    if stop_distance_pct == 0:
        return 0.0, 0.0, 0.0, 0.0, False, 0.0
        
    # Рекомендований повний об'єм позиції у USDT (з урахуванням плеча)
    position_size_usd = risk_amount / stop_distance_pct
    
    # Рекомендована кількість монет у контрактах
    position_size_contracts = position_size_usd / actual_entry
    
    # Необхідна маржа (колатерал) для відкриття позиції
    margin_required = position_size_usd / leverage
    
    return round(risk_amount, 2), round(position_size_usd, 2), round(position_size_contracts, 2), round(margin_required, 2), is_averaged, round(actual_entry, 6)

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

def generate_chart(symbol, timeframe, direction, entry, dobar_low, dobar_high,
                   tps, hit_tps=[], stop_loss=None, show_dobar=True, candles_df=None):
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
            x_right - x_range * 0.02, (dobar_low + dobar_high) / 2, 'ДОБОР',
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
        fontsize=13, va='center', ha='center', color='white', fontweight='bold',
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
            x_right - x_range * 0.05, tp_price, 'o', markersize=10,
            markerfacecolor='white', markeredgecolor='#1a1a1a',
            markeredgewidth=1.5, zorder=5
        )
        ax.text(
            x_right - x_range * 0.05, tp_price, '✓',
            fontsize=7, va='center', ha='center', color='#1a1a1a', zorder=6
        )

    dir_text = 'SHORT' if direction == 'SHORT' else 'LONG'
    ax.set_title(f'{symbol} · {timeframe} · {dir_text}', loc='right',
                 fontsize=11, color='#1a1a1a', pad=10)

    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight', facecolor=bg_color, dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf

def format_signal(symbol, timeframe, direction, entry, dobar_low, dobar_high,
                  tps, stats, hit_tps=[], tier='🟢', stop_loss=None):
    dir_emoji = "📈" if direction == "LONG" else "📉"
    exchange_name = get_setting('exchange_name') or 'binance'
    leverage = get_setting('leverage') or 20
    
    # Отримуємо торгове посилання під активну біржу
    link = get_exchange_link(symbol)
    
    lines = []
    lines.append(f"<a href='{link}'>#{symbol}</a> {timeframe} {tier} (on {exchange_name.upper()})")
    lines.append(f"💎 СТАТУС : {direction} {dir_emoji}")
    lines.append(f"")
    
    # Розраховуємо об'єми та середню точку входу
    risk_usd, pos_usd, pos_contracts, margin_required, is_averaged, avg_entry = calculate_position_size_v2(
        entry, stop_loss, dobar_low, dobar_high
    )
    
    if is_averaged:
        # Рахуємо точну середину зони добору для відображення в Telegram
        dobar_mid = round((dobar_low + dobar_high) / 2.0, 6)
        
        lines.append(f"👉 ENTRY (Avg): <b>{avg_entry}</b>")
        lines.append(f"👉 ДОБОР : {dobar_low} — {dobar_high} (Середина: <b>{dobar_mid}</b>)")
        lines.append(f"ℹ️ <i>(50% на {entry} + 50% на {dobar_mid})</i>")
    else:
        lines.append(f"👉 ENTRY : {entry}")
        lines.append(f"👉 ДОБОР : {dobar_low} — {dobar_high}")
        
    if stop_loss:
        lines.append(f"🛑 СТОП : {stop_loss}")
        
        if pos_usd > 0:
            lines.append(f"")
            lines.append(f"⚖️ <b>РИЗИК-МЕНЕДЖМЕНТ:</b>")
            lines.append(f"💵 Макс. Ризик: <b>${risk_usd}</b>")
            
            if is_averaged:
                dobar_mid = round((dobar_low + dobar_high) / 2.0, 6)
                lines.append(f"💼 Реком. Об'єм: <b>${pos_usd}</b> (або {pos_contracts:.1f} {symbol[:-4]})")
                lines.append(f"💵 <i>-> ${pos_usd/2:.2f} на {entry} + ${pos_usd/2:.2f} на {dobar_mid}</i>")
            else:
                lines.append(f"💼 Реком. Об'єм: <b>${pos_usd}</b> (або {pos_contracts:.1f} {symbol[:-4]})")
                
            lines.append(f"⚡ Необхідна маржа: <b>${margin_required}</b> (при плечі {leverage}x)")
            
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
        deviations = stats.get('deviations', {})
        for dev, cnt in deviations.items():
            lines.append(f"📉 Відхилення ≥ {dev}%: {cnt}")

    return "\n".join(lines)


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


# ─────────────────────────────────────────────
# SIGNAL MONITORING (УЗГОДЖЕНО ТРИ АРГУМЕНТИ ТА FALLBACKS)
# ─────────────────────────────────────────────

async def monitor_signal(bot, signal, active_signals):
    symbol = signal['symbol']
    direction = signal['direction']
    tps = signal['tps']
    
    # Відновлюємо стан досягнутих ТР
    hit_tps = set(signal.get('hit_tps', []))
    
    chart_message_id = signal['chart_message_id']
    start_time = asyncio.get_event_loop().time()
    
    # Відновлюємо стан беззбитку
    breakeven = 0 in hit_tps

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

                if breakeven and hit_tps:
                    msg_lines.extend(tp_summary_lines)
                    msg_lines.append(f"")
                    msg_lines.append(f"↩️ Сигнал закрито в беззбиток ({elapsed})")
                    msg_lines.append(f"💰 Загальний прибуток: +{round(total_profit, 1)}%")
                    try:
                        await bot.send_message(chat_id=CHAT_ID, text="\n".join(msg_lines),
                                               reply_to_message_id=chart_message_id, parse_mode='HTML')
                    except Exception as e:
                        if "Message to be replied not found" in str(e):
                            try:
                                await bot.send_message(chat_id=CHAT_ID, text="\n".join(msg_lines), parse_mode='HTML')
                            except Exception as ex:
                                print(f"Помилка закриття {symbol}: {ex}")
                        else:
                            print(f"Помилка відправки БУ {symbol}: {e}")
                    
                    # Закриваємо з точним розрахунком PnL у хмарі
                    close_signal_stat(signal.get('stat_id'), 'tp', round(total_profit, 1), price)
                else:
                    sl_pct = round(abs(price - entry) / entry * 100, 1)
                    msg_lines.append(f"🛑 Stop-Loss спрацював ({elapsed}) | -{sl_pct}%")
                    msg_lines.append(f"💸 TP не було досягнуто")
                    msg_lines.append(f"❌ Сигнал закрито по стопу")
                    try:
                        await bot.send_message(chat_id=CHAT_ID, text="\n".join(msg_lines),
                                               reply_to_message_id=chart_message_id, parse_mode='HTML')
                    except Exception as e:
                        if "Message to be replied not found" in str(e):
                            try:
                                await bot.send_message(chat_id=CHAT_ID, text="\n".join(msg_lines), parse_mode='HTML')
                            except Exception as ex:
                                print(f"Помилка закриття {symbol}: {ex}")
                        else:
                            print(f"Помилка відправки СЛ {symbol}: {e}")
                    
                    # Фіксуємо мінусовий PnL у базі даних
                    close_signal_stat(signal.get('stat_id'), 'sl', -sl_pct, price)

                remove_active_signal(symbol, signal['timeframe'])
                if signal in active_signals:
                    active_signals.remove(signal)
                break

        # Перевірка БУ після TP1
        if breakeven and 0 in hit_tps:
            be_hit = (direction == 'SHORT' and price >= entry) or \
                     (direction == 'LONG' and price <= entry)

            if be_hit:
                elapsed = elapsed_str()
                total_profit = sum(tps[i][2] for i in hit_tps)
                tp_summary_lines = [f"✅ TP{i+1}: {tps[i][0]} | +{tps[i][2]}%" for i in sorted(hit_tps)]

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
                    await bot.send_message(chat_id=CHAT_ID, text="\n".join(msg_lines),
                                           reply_to_message_id=chart_message_id, parse_mode='HTML')
                except Exception as e:
                    if "Message to be replied not found" in str(e):
                        try:
                            await bot.send_message(chat_id=CHAT_ID, text="\n".join(msg_lines), parse_mode='HTML')
                        except Exception as ex:
                            print(f"Помилка закриття {symbol}: {ex}")
                    else:
                        print(f"Помилка відправки БУ {symbol}: {e}")

                close_signal_stat(signal.get('stat_id'), 'tp', round(total_profit, 1), price)
                remove_active_signal(symbol, signal['timeframe'])
                if signal in active_signals:
                    active_signals.remove(signal)
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
            
            # Надійно фіксуємо ТР у базі даних
            signal['hit_tps'] = hit_tps
            save_active_signals(active_signals)
            
            elapsed = elapsed_str()
            print(f"✅ {symbol} досягнуто TP: {hit_tps}")

            if 0 in hit_tps and not breakeven:
                breakeven = True
                signal['stop_loss'] = entry
                signal['show_dobar'] = False
                
                # Фіксуємо беззбиток у базі даних
                save_active_signals(active_signals)
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
                    tps, list(hit_tps), signal.get('stop_loss'),
                    signal.get('show_dobar', True), candles_df
                )
                await bot.send_photo(
                    chat_id=CHAT_ID, photo=new_chart, caption=new_text,
                    reply_to_message_id=chart_message_id,
                    parse_mode='HTML',  # Вмикаємо рендер клікабельних посилань
                    read_timeout=30, write_timeout=30, connect_timeout=30,
                )
            except Exception as e:
                # Fallback: Якщо виникла помилка надсилання графіка (включаючи File must be non-empty)
                # або повідомлення видалено, ми просто відправляємо текст оновлення ТР
                try:
                    print(f"⚠️ Помилка фото {symbol} ({e}), відправляємо як чистий текст...")
                    await bot.send_message(
                        chat_id=CHAT_ID, text=new_text,
                        reply_to_message_id=chart_message_id,
                        parse_mode='HTML'
                    )
                except Exception as ex:
                    if "Message to be replied not found" in str(ex):
                        try:
                            await bot.send_message(chat_id=CHAT_ID, text=new_text, parse_mode='HTML')
                        except Exception as ex2:
                            print(f"Критична помилка відправки тексту {symbol}: {ex2}")

            if len(hit_tps) == len(tps):
                last_tp = max(hit_tps)
                total_profit = sum(tps[i][2] for i in hit_tps)
                tp_summary_lines = [f"✅ TP{i+1}: {tps[i][0]} | +{tps[i][2]}%" for i in sorted(hit_tps)]

                msg_lines = [
                    f"#{symbol} {signal['timeframe']} "
                    f"{'LONG 📈' if direction == 'LONG' else 'SHORT 📉'}",
                    f"",
                ]
                msg_lines.extend(tp_summary_lines)
                msg_lines.append(f"")
                msg_lines.append(f"🎯 TP{last_tp+1}: {tps[last_tp][0]} ✅ ({elapsed})")
                msg_lines.append(f"💰 Загальний прибуток: +{round(total_profit, 1)}%")
                msg_lines.append(f"🏁 Сигнал закрито")

                try:
                    await bot.send_message(chat_id=CHAT_ID, text="\n".join(msg_lines),
                                           reply_to_message_id=chart_message_id, parse_mode='HTML')
                except Exception as e:
                    if "Message to be replied not found" in str(e):
                        try:
                            await bot.send_message(chat_id=CHAT_ID, text="\n".join(msg_lines), parse_mode='HTML')
                        except Exception as ex:
                            print(f"Помилка закриття {symbol}: {ex}")
                    else:
                        print(f"Помилка відправки закриття {symbol}: {e}")

                close_signal_stat(signal.get('stat_id'), 'tp', round(total_profit, 1), price)
                remove_active_signal(symbol, signal['timeframe'])
                if signal in active_signals:
                    active_signals.remove(signal)
                break


# ─────────────────────────────────────────────
# SCAN & SEND
# ─────────────────────────────────────────────

async def scan_and_send(bot, active_signals, timeframes):
    global recently_sent, is_scanning
    
    # Якщо сканування вже виконується — блокуємо дублювання
    if is_scanning:
        print("⚠️ Сканування вже виконується іншим процесом, пропускаємо")
        return False
        
    is_scanning = True
    try:
        all_signals = await scan_all(timeframes)
        new_count = 0
        max_signals = get_setting('max_active_signals')

        for signal in all_signals:
            if new_count >= 3:
                break

            if len(active_signals) >= max_signals:
                print(f"⚠️ Досягнуто ліміт активних сигналів ({max_signals})")
                break

            symbol_clean = signal['symbol'].replace('/', '')
            is_already_monitored = any(
                s['symbol'].replace('/', '') == symbol_clean for s in active_signals
            )

            if is_already_monitored or symbol_clean in recently_sent:
                print(f"⚠️ {symbol_clean} вже моніториться — пропускаємо")
                continue

            try:
                # Отримуємо фактичні розраховані об'єми під добір для збереження в базу
                _, pos_usd, pos_contracts, _, _, avg_entry = calculate_position_size_v2(
                    signal['entry'], signal.get('stop_loss'), signal.get('dobar_low'), signal.get('dobar_high')
                )
                
                # Додаємо об'єми та середню ціну входу до об'єкта сигналу, щоб вони збереглися в хмарі
                signal['pos_usd'] = pos_usd
                signal['pos_contracts'] = pos_contracts
                
                # Якщо використовуємо добір, записуємо середню ціну входу для ідеального PnL трекінгу
                if get_setting('use_dobar'):
                    signal['entry'] = avg_entry

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
                    signal['tps'], [], signal.get('stop_loss'),
                    True, candles_df
                )

                sent = await bot.send_photo(
                    chat_id=CHAT_ID, photo=chart, caption=signal_text,
                    parse_mode='HTML',  # Клікабельні посилання в новом сигналі
                    read_timeout=30, write_timeout=30, connect_timeout=30,
                )

                signal['chart_message_id'] = sent.message_id
                signal['symbol'] = symbol_clean

                active_signals.append(signal)
                recently_sent.add(symbol_clean)
                save_active_signals(active_signals)
                new_count += 1

                signal_id = add_signal_stat(
                    symbol_clean, signal['timeframe'],
                    signal['direction'], signal['entry'],
                    signal.get('tier', '🟢')
                )
                signal['stat_id'] = signal_id

                asyncio.create_task(monitor_signal(bot, signal, active_signals))
                await asyncio.sleep(5)

            except Exception as e:
                print(f"Помилка відправки {symbol_clean}: {e}")
                await asyncio.sleep(10)

        if new_count > 0:
            print(f"Відправлено {new_count} нових сигналів.")
            
    finally:
        # Звільняємо замок сканування в будь-якому випадку (навіть при помилках)
        is_scanning = False
        
    return True


# ─────────────────────────────────────────────
# TELEGRAM UPDATE HANDLER
# ─────────────────────────────────────────────

async def handle_updates(bot, active_signals):
    global active_timeframe, exchange
    offset = None

    while True:
        try:
            updates = await bot.get_updates(offset=offset, timeout=10)
            for update in updates:
                offset = update.update_id + 1

                # ── Текстові команди ──────────────────────────
                if update.message and update.message.text:
                    text = update.message.text
                    chat_id = update.message.chat.id
                    user_id = update.message.from_user.id

                    if str(user_id) != str(ADMIN_ID):
                        await bot.send_message(chat_id=chat_id, text="⛔ Доступ заборонено")
                        continue

                    if text == '/start':
                        await bot.send_message(
                            chat_id=chat_id,
                            text="🤖 Сигнальний бот\nОберіть дію:",
                            reply_markup=main_menu_keyboard()
                        )

                    elif text == '/settings':
                        await bot.send_message(
                            chat_id=chat_id,
                            text=get_settings_text(),
                            reply_markup=main_settings_keyboard(),
                            parse_mode='HTML'
                        )

                    elif text == '/stats':
                        summary = get_stats_summary()
                        await bot.send_message(chat_id=chat_id, text=summary, parse_mode='HTML')

                    elif text == '/scan':
                        if is_scanning:
                            await bot.send_message(chat_id=chat_id, text="⏳ Сканування вже виконується автоматичним таймером або іншим процесом. Будь ласка, зачекайте...")
                            continue
                            
                        await bot.send_message(chat_id=chat_id, text="🔍 Запущено позачергове ручне сканування для всіх активних таймфреймів...")
                        active_tfs = get_setting('active_timeframes')
                        success = await scan_and_send(bot, active_signals, active_tfs)
                        
                        if success:
                            await bot.send_message(chat_id=chat_id, text="✅ Ручне сканування успішно завершено!")

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

                # ── Callback кнопки ───────────────────────────
                if update.callback_query:
                    query = update.callback_query
                    chat_id = query.message.chat.id
                    user_id = query.from_user.id
                    data = query.data

                    if str(user_id) != str(ADMIN_ID):
                        await bot.answer_callback_query(query.id, text="⛔ Доступ заборонено")
                        continue

                    await bot.answer_callback_query(query.id)

                    # ── Головне меню ──
                    if data == 'back' or data == 'cfg_back':
                        await bot.send_message(
                            chat_id=chat_id,
                            text="🤖 Сигнальний бот\nОберіть дію:",
                            reply_markup=main_menu_keyboard()
                        )

                    elif data == 'stats':
                        summary = get_stats_summary()
                        await bot.send_message(chat_id=chat_id, text=summary, parse_mode='HTML')

                    elif data == 'active':
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

                    elif data == 'cfg_scan_now':
                        if is_scanning:
                            await bot.send_message(chat_id=chat_id, text="⏳ Сканування вже виконується автоматичним таймером або іншим процесом. Будь ласка, зачекайте...")
                            continue
                            
                        await bot.send_message(chat_id=chat_id, text="🔍 Запущено позачергове ручне сканування для всіх активних таймфреймів...")
                        active_tfs = get_setting('active_timeframes')
                        success = await scan_and_send(bot, active_signals, active_tfs)
                        
                        if success:
                            await bot.send_message(chat_id=chat_id, text="✅ Ручне сканування успішно завершено!")

                    elif data == 'info':
                        htf = get_setting('htf_bias_enabled')
                        min_prob = get_setting('min_tp1_prob')
                        stop_mult = get_setting('stop_atr_mult')
                        msg = (
                            f"🤖 Сигнальний бот\n\n"
                            f"📊 Біржа: Binance / MEXC Futures\n"
                            f"⏱ Таймфрейми: 5m · 15m · 30m · 1h · 4h · 1d\n"
                            f"🔍 Індикатори: EMA · RSI · ATR · MACD · BB\n"
                            f"✅ Фільтр: TP1 ≥ {min_prob}% · мін. 12 угод\n"
                            f"🛑 Стоп-лосс: ATR × {stop_mult}\n"
                            f"↩️ БУ: після досягнення TP1\n"
                            f"🔍 HTF фільтр: {'увімк.' if htf else 'вимк.'}"
                        )
                        await bot.send_message(chat_id=chat_id, text=msg)

                    elif data == 'timeframe':
                        await bot.send_message(
                            chat_id=chat_id,
                            text=f"⏱ Поточний таймфрейм: {active_timeframe}\nОберіть таймфрейм:",
                            reply_markup={'inline_keyboard': get_timeframe_keyboard(active_timeframe)}
                        )

                    elif data.startswith('tf_'):
                        tf = data.replace('tf_', '')
                        active_timeframe = tf
                        set_setting('active_timeframe', tf)
                        print(f"⏱ Таймфрейм змінено на: {tf}")
                        await bot.send_message(
                            chat_id=chat_id,
                            text=f"✅ Таймфрейм змінено на: {tf}\n⏳ Запускаю сканування...",
                            reply_markup={'inline_keyboard': get_timeframe_keyboard(active_timeframe)}
                        )
                        try:
                            timeframes_to_scan = TIMEFRAME_OPTIONS.get(tf, [tf])
                            await scan_and_send(bot, active_signals, timeframes_to_scan)
                            await bot.send_message(chat_id=chat_id, text="✅ Сканування завершено!")
                        except Exception as e:
                            print(f"Помилка сканування після зміни таймфрейму: {e}")
                            await bot.send_message(chat_id=chat_id, text=f"❌ Помилка сканування: {e}")

                    # ── Налаштування — Меню ──
                    elif data == 'cfg_main':
                        await bot.send_message(
                            chat_id=chat_id,
                            text=get_settings_text(),
                            reply_markup=main_settings_keyboard(),
                            parse_mode='HTML'
                        )

                    elif data == 'cfg_pairs':
                        await bot.send_message(
                            chat_id=chat_id,
                            text="📋 Оберіть активні пари для сканування:",
                            reply_markup=pairs_keyboard()
                        )

                    elif data == 'cfg_timeframes':
                        await bot.send_message(
                            chat_id=chat_id,
                            text="⏱ Оберіть активні таймфрейми:",
                            reply_markup=timeframes_keyboard()
                        )

                    elif data == 'cfg_risk':
                        text, markup = risk_keyboard()
                        await bot.send_message(
                            chat_id=chat_id,
                            text=text,
                            reply_markup=markup,
                            parse_mode='HTML'
                        )

                    elif data == 'cfg_filters':
                        text, markup = filters_keyboard()
                        await bot.send_message(
                            chat_id=chat_id,
                            text=text,
                            reply_markup=markup,
                            parse_mode='HTML'
                        )

                    elif data == 'cfg_close':
                        await bot.send_message(
                            chat_id=chat_id,
                            text="⚙️ Меню налаштувань закрито."
                        )

                    # ── Гаряча зміна активної біржі в реальному часі (Binance <-> MEXC) ──
                    elif data == 'toggle_exchange':
                        current = get_setting('exchange_name') or 'binance'
                        new_exchange = 'mexc' if current == 'binance' else 'binance'
                        set_setting('exchange_name', new_exchange)
                        
                        # Безпечно закриваємо стару асинхронну сесію сокетів перед заміною
                        try:
                            if hasattr(scanner, 'exchange') and scanner.exchange:
                                # Створюємо фонове завдання для очищення сокетів
                                asyncio.create_task(scanner.exchange.close())
                        except Exception as e:
                            print(f"Помилка закриття старої сесії: {e}")
                        
                        # Гаряче оновлення клієнтів на нову біржу в реальному часі
                        exchange = get_exchange_client(async_mode=False)
                        scanner.exchange = get_exchange_client(async_mode=True)
                        print(f"🏛 Біржу успішно перемикнуто на: {new_exchange.upper()}")
                        
                        await bot.send_message(
                            chat_id=chat_id,
                            text=get_settings_text(),
                            reply_markup=main_settings_keyboard(),
                            parse_mode='HTML'
                        )

                    # ── Перемикання пар ──
                    elif data.startswith('toggle_pair_'):
                        pair = data.replace('toggle_pair_', '')
                        watchlist = get_setting('watchlist')
                        if pair in watchlist:
                            if len(watchlist) > 1:
                                watchlist.remove(pair)
                        else:
                            watchlist.append(pair)
                        set_setting('watchlist', watchlist)
                        await bot.send_message(
                            chat_id=chat_id,
                            text="📋 Оновлено список пар:",
                            reply_markup=pairs_keyboard()
                        )

                    # ── Перемикання таймфреймів ──
                    elif data.startswith('toggle_tf_'):
                        tf = data.replace('toggle_tf_', '')
                        tfs = get_setting('active_timeframes')
                        if tf in tfs:
                            if len(tfs) > 1:
                                tfs.remove(tf)
                        else:
                            tfs.append(tf)
                        set_setting('active_timeframes', tfs)
                        await bot.send_message(
                            chat_id=chat_id,
                            text="⏱ Оновлено таймфрейми:",
                            reply_markup=timeframes_keyboard()
                        )

                    # ── Ризик-менеджмент ──
                    elif data == 'risk_stop_up':
                        v = round(get_setting('stop_atr_mult') + 0.1, 1)
                        set_setting('stop_atr_mult', min(v, 5.0))
                        text, markup = risk_keyboard()
                        await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, parse_mode='HTML')

                    elif data == 'risk_stop_down':
                        v = round(get_setting('stop_atr_mult') - 0.1, 1)
                        set_setting('stop_atr_mult', max(v, 0.5))
                        text, markup = risk_keyboard()
                        await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, parse_mode='HTML')

                    elif data == 'risk_tp1_up':
                        v = round(get_setting('tp1_atr_mult') + 0.1, 1)
                        set_setting('tp1_atr_mult', min(v, 3.0))
                        text, markup = risk_keyboard()
                        await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, parse_mode='HTML')

                    elif data == 'risk_tp1_down':
                        v = round(get_setting('tp1_atr_mult') - 0.1, 1)
                        set_setting('tp1_atr_mult', max(v, 0.3))
                        text, markup = risk_keyboard()
                        await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, parse_mode='HTML')

                    elif data == 'risk_max_up':
                        v = get_setting('max_active_signals') + 1
                        set_setting('max_active_signals', min(v, 30))
                        text, markup = risk_keyboard()
                        await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, parse_mode='HTML')

                    elif data == 'risk_max_down':
                        v = get_setting('max_active_signals') - 1
                        set_setting('max_active_signals', max(v, 1))
                        text, markup = risk_keyboard()
                        await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, parse_mode='HTML')

                    # Налаштування депозиту та ризику %
                    elif data == 'risk_depo_up':
                        v = (get_setting('portfolio_size') or 1000.0) + 100.0
                        set_setting('portfolio_size', min(v, 1000000.0))
                        text, markup = risk_keyboard()
                        await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, parse_mode='HTML')

                    elif data == 'risk_depo_down':
                        v = (get_setting('portfolio_size') or 1000.0) - 100.0
                        set_setting('portfolio_size', max(v, 100.0))
                        text, markup = risk_keyboard()
                        await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, parse_mode='HTML')

                    elif data == 'risk_pct_up':
                        v = round((get_setting('risk_pct') or 1.0) + 0.1, 1)
                        set_setting('risk_pct', min(v, 10.0))
                        text, markup = risk_keyboard()
                        await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, parse_mode='HTML')

                    elif data == 'risk_pct_down':
                        v = round((get_setting('risk_pct') or 1.0) - 0.1, 1)
                        set_setting('risk_pct', max(v, 0.1))
                        text, markup = risk_keyboard()
                        await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, parse_mode='HTML')

                    # Нові кнопки зміни кредитного плеча та Добіру
                    elif data == 'risk_lev_up':
                        v = (get_setting('leverage') or 20) + 5
                        set_setting('leverage', min(v, 200))
                        text, markup = risk_keyboard()
                        await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, parse_mode='HTML')

                    elif data == 'risk_lev_down':
                        v = (get_setting('leverage') or 20) - 5
                        set_setting('leverage', max(v, 1))
                        text, markup = risk_keyboard()
                        await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, parse_mode='HTML')

                    elif data == 'toggle_dobar':
                        current = get_setting('use_dobar')
                        if current is None:
                            current = True
                        set_setting('use_dobar', not current)
                        text, markup = risk_keyboard()
                        await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, parse_mode='HTML')

                    elif data in ['risk_stop_info', 'risk_tp1_info', 'risk_max_info', 'risk_depo_info', 'risk_pct_info', 'risk_lev_info', 'filter_prob_info', 'filter_htf_info']:
                        pass  # інформаційні кнопки

                    # ── Фільтри стратегій ──
                    elif data == 'toggle_htf':
                        current = get_setting('htf_bias_enabled')
                        set_setting('htf_bias_enabled', not current)
                        text, markup = filters_keyboard()
                        await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, parse_mode='HTML')

                    elif data == 'filter_prob_up':
                        v = get_setting('min_tp1_prob') + 5
                        set_setting('min_tp1_prob', min(v, 90))
                        text, markup = filters_keyboard()
                        await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, parse_mode='HTML')

                    elif data == 'filter_prob_down':
                        v = get_setting('min_tp1_prob') - 5
                        set_setting('min_tp1_prob', max(v, 30))
                        text, markup = filters_keyboard()
                        await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, parse_mode='HTML')

                    elif data == 'filter_htf_up':
                        v = round(get_setting('htf_diff_threshold') + 0.5, 1)
                        set_setting('htf_diff_threshold', min(v, 5.0))
                        text, markup = filters_keyboard()
                        await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, parse_mode='HTML')

                    elif data == 'filter_htf_down':
                        v = round(get_setting('htf_diff_threshold') - 0.5, 1)
                        set_setting('htf_diff_threshold', max(v, 0.1))
                        text, markup = filters_keyboard()
                        await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, parse_mode='HTML')

                    # ── Очищення статистики ──
                    elif data == 'clear':
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

                    elif data == 'clear_confirm':
                        clear_stats()
                        await bot.send_message(chat_id=chat_id, text="✅ Статистику очищено!")

                    elif data == 'clear_cancel':
                        await bot.send_message(chat_id=chat_id, text="❌ Очищення скасовано")

                    # ── Закрити всі сигнали ──
                    elif data == 'clear_active':
                        await bot.send_message(
                            chat_id=chat_id,
                            text="⚠️ Закрити всі активні сигнали?",
                            reply_markup={
                                'inline_keyboard': [
                                    [
                                        {'text': '✅ Так, очистити', 'callback_data': 'clear_active_confirm'},
                                        {'text': '❌ Скасувати', 'callback_data': 'clear_active_cancel'},
                                    ]
                                ]
                            }
                        )

                    elif data == 'clear_active_confirm':
                        clear_active_signals()
                        active_signals.clear()
                        await bot.send_message(chat_id=chat_id, text="✅ Всі активні сигнали закрито!")

                    elif data == 'clear_active_cancel':
                        await bot.send_message(chat_id=chat_id, text="❌ Скасовано")

        except Exception as e:
            print(f"Помилка обробки оновлень: {e}")
            await asyncio.sleep(5)


# ─────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────

async def wait_until_next_boundary(interval_seconds):
    """Очікує точного закриття свічки на Binance з невеликим зазором для оновлення даних"""
    now = time.time()
    remaining = interval_seconds - (now % interval_seconds)
    # Додаємо 1 секунду зазору, щоб свічка на Binance точно встигла закритися
    await asyncio.sleep(remaining + 1.0)


async def main():
    global active_timeframe
    init_db()
    bot = Bot(token=BOT_TOKEN, request=request)

    active_signals = load_active_signals()
    print(f"Завантажено {len(active_signals)} активних сигналів з диску")

    for signal in active_signals:
        if signal.get('chart_message_id'):
            asyncio.create_task(monitor_signal(bot, signal, active_signals))
            print(f"🔄 Відновлено моніторинг: {signal['symbol']} {signal['timeframe']}")
            if not signal.get('stat_id'):
                signal_id = add_signal_stat(
                    signal['symbol'], signal['timeframe'],
                    signal['direction'], signal['entry'],
                    signal.get('tier', '🟢')
                )
                signal['stat_id'] = signal_id

    save_active_signals(active_signals)

    async def loop_5m():
        while True:
            try:
                tfs = get_setting('active_timeframes')
                if '5m' in tfs:
                    await wait_until_next_boundary(300)
                    print("Сканування 5m...")
                    await scan_and_send(bot, active_signals, ['5m'])
                else:
                    await asyncio.sleep(30)
            except Exception as e:
                print(f"Помилка loop_5m: {e}")
                await asyncio.sleep(10)

    async def loop_15m():
        while True:
            try:
                tfs = get_setting('active_timeframes')
                if '15m' in tfs:
                    await wait_until_next_boundary(900)
                    print("Сканування 15m...")
                    await scan_and_send(bot, active_signals, ['15m'])
                else:
                    await asyncio.sleep(30)
            except Exception as e:
                print(f"Помилка loop_15m: {e}")
                await asyncio.sleep(10)

    async def loop_30m():
        while True:
            try:
                tfs = get_setting('active_timeframes')
                if '30m' in tfs:
                    await wait_until_next_boundary(1800)
                    print("Сканування 30m...")
                    await scan_and_send(bot, active_signals, ['30m'])
                else:
                    await asyncio.sleep(30)
            except Exception as e:
                print(f"Помилка loop_30m: {e}")
                await asyncio.sleep(10)

    async def loop_1h():
        while True:
            try:
                tfs = get_setting('active_timeframes')
                if '1h' in tfs:
                    await wait_until_next_boundary(3600)
                    print("Сканування 1h...")
                    await scan_and_send(bot, active_signals, ['1h'])
                else:
                    await asyncio.sleep(30)
            except Exception as e:
                print(f"Помилка loop_1h: {e}")
                await asyncio.sleep(10)

    async def loop_4h():
        while True:
            try:
                tfs = get_setting('active_timeframes')
                if '4h' in tfs:
                    await wait_until_next_boundary(14400)
                    print("Сканування 4h...")
                    await scan_and_send(bot, active_signals, ['4h'])
                else:
                    await asyncio.sleep(30)
            except Exception as e:
                print(f"Помилка loop_4h: {e}")
                await asyncio.sleep(10)

    async def loop_1d():
        while True:
            try:
                tfs = get_setting('active_timeframes')
                if '1d' in tfs:
                    await wait_until_next_boundary(86400)
                    print("Сканування 1d...")
                    await scan_and_send(bot, active_signals, ['1d'])
                else:
                    await asyncio.sleep(30)
            except Exception as e:
                print(f"Помилка loop_1d: {e}")
                await asyncio.sleep(10)

    async def daily_stats():
        while True:
            await asyncio.sleep(24 * 60 * 60)
            summary = get_stats_summary()
            try:
                await bot.send_message(chat_id=CHAT_ID, text=summary)
            except Exception as e:
                print(f"Помилка відправки статистики: {e}")

    async def clear_recently_sent():
        global recently_sent
        while True:
            await asyncio.sleep(60 * 60)
            recently_sent.clear()
            print("🔄 Очищено recently_sent")

    await asyncio.gather(
        loop_5m(), loop_15m(), loop_30m(),
        loop_1h(), loop_4h(), loop_1d(),
        daily_stats(), core_recently_sent_fallback_chain() if False else clear_recently_sent(), # Безпечний запуск
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