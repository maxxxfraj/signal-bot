from settings import get_setting, set_setting, get_exchange_client
from settings_menu import (
    main_settings_keyboard,
    get_settings_text,
    pairs_keyboard,
    timeframes_keyboard,
    risk_keyboard,
    filters_keyboard,
    ALL_PAIRS
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
import scanner
from keep_alive import keep_alive
from database import (
    init_db,
    save_active_signals, load_active_signals,
    remove_active_signal, clear_active_signals,
    add_signal_stat, close_signal_stat,
    get_stats_summary, clear_stats,
    get_fees_for_exchange,
    to_native_float, to_native_int
)
import os
import io
import sys
import urllib3
import time
from datetime import datetime, timezone  # ДОДАНО ІМПОРТ timezone ДЛЯ UTC

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

exchange = get_exchange_client(async_mode=False)
active_timeframe = get_setting('active_timeframe')

TIMEFRAME_OPTIONS = {
    'all': ['5m', '15m', '30m', '1h', '4h', '1d'],
    '5m': ['5m'], '15m': ['15m'], '30m': ['30m'],
    '1h': ['1h'], '4h': ['4h'], '1d': ['1d'],
}

ALL_TIMEFRAMES = ['5m', '15m', '30m', '1h', '4h', '1d']


# ─────────────────────────────────────────────
# GLOBAL KEYBOARDS
# ─────────────────────────────────────────────

def main_menu_keyboard():
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
# CHARTS & FORMATTING
# ─────────────────────────────────────────────

is_scanning = False

def log_skip_main(msg, scan_logs=None):
    print(msg)
    if scan_logs is not None:
        scan_logs.append(msg)

def get_exchange_link(symbol):
    exchange_name = get_setting('exchange_name') or 'binance'
    symbol_clean = symbol.replace('/', '').upper()
    if exchange_name == 'mexc':
        base = symbol_clean[:-4]
        return f"https://futures.mexc.com/exchange/{base}_USDT"
    else:
        return f"https://www.binance.com/en/futures/{symbol_clean}"

def calculate_position_size_v2(entry, stop_loss, dobar_low=None, dobar_high=None):
    portfolio_size = get_setting('portfolio_size') or 1000.0
    risk_pct = get_setting('risk_pct') or 1.0
    leverage = get_setting('leverage') or 20
    use_dobar = get_setting('use_dobar')
    if use_dobar is None:
        use_dobar = True

    risk_amount = portfolio_size * (risk_pct / 100.0)
    
    actual_entry = entry
    is_averaged = False
    dobar_price = 0.0
    
    if use_dobar and dobar_low is not None and dobar_high is not None:
        dobar_price = (dobar_low + dobar_high) / 2.0
        actual_entry = (entry + dobar_price) / 2.0
        is_averaged = True

    stop_distance_pct = abs(actual_entry - stop_loss) / actual_entry
    if stop_distance_pct == 0:
        return 0.0, 0.0, 0.0, 0.0, False, 0.0
        
    position_size_usd = risk_amount / stop_distance_pct
    position_size_contracts = position_size_usd / actual_entry
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
                  tps, stats, hit_tps=[], tier='🟢', stop_loss=None, correlation=None,
                  funding_rate=None, open_interest=None, mode='swing',
                  pos_usd=None, pos_contracts=None, margin_required=None):
    dir_emoji = "📈" if direction == "LONG" else "📉"
    exchange_name = get_setting('exchange_name') or 'binance'
    leverage = get_setting('leverage') or 20
    
    link = get_exchange_link(symbol)
    
    lines = []
    if mode == 'scalp':
        lines.append(f"⚡️ <a href='{link}'>#{symbol}</a> {timeframe} {tier} (on {exchange_name.upper()} SCALPER)")
    else:
        lines.append(f"🟢 <a href='{link}'>#{symbol}</a> {timeframe} {tier} (on {exchange_name.upper()} SWING)")
        
    lines.append(f"💎 СТАТУС : {direction} {dir_emoji}")
    
    if correlation is not None:
        lines.append(f"🪙 Кореляція до BTC: <b>{correlation}</b>")
        
    if funding_rate is not None or open_interest is not None:
        oi_str = f"${open_interest / 1000000.0:.2f}M" if open_interest is not None else "N/A"
        fund_str = f"{funding_rate:.4f}%" if funding_rate is not None else "N/A"
        lines.append(f"📈 Open Interest: <b>{oi_str}</b> | Funding: <b>{fund_str}</b>")
        
    lines.append(f"")
    
    if pos_usd is not None and pos_contracts is not None:
        risk_usd = round(get_setting('portfolio_size') * (get_setting('risk_pct') / 100.0), 2)
        margin_required = margin_required or round(pos_usd / leverage, 2)
        is_averaged = get_setting('use_dobar')
        if is_averaged is None:
            is_averaged = True
        dobar_mid = round((dobar_low + dobar_high) / 2.0, 6)
        avg_entry = round((entry + dobar_mid) / 2.0, 6) if is_averaged else entry
    else:
        risk_usd, pos_usd, pos_contracts, margin_required, is_averaged, avg_entry = calculate_position_size_v2(
            entry, stop_loss, dobar_low, dobar_high
        )
    
    if is_averaged:
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
            lines.append(f"💵 : {risk_usd} USD")
            
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
# СЕРВІС СИНХРОНІЗАЦІЇ ІСТОРІЇ (Gap Reconciliation)
# ─────────────────────────────────────────────

async def reconcile_historical_gap(bot, signal, active_signals):
    symbol = signal['symbol']
    timeframe = signal['timeframe']
    direction = signal['direction']
    tps = signal['tps']
    entry = signal['entry']
    created_at_str = signal.get('created_at')
    
    if not created_at_str:
        return True
        
    try:
        # ВИПРАВЛЕНО ВРАЗЛИВІСТЬ ДО ЧАСОВИХ ПОЯСІВ:
        # Конвертуємо ISO рядок у UTC-datetime і видаляємо tz-інформацію для точного порівняння з df.index
        created_at = pd.to_datetime(created_at_str)
        if created_at.tz is not None:
            created_at_utc = created_at.tz_convert('UTC').tz_localize(None)
        else:
            created_at_utc = created_at
    except Exception as e:
        print(f"Помилка парсингу дати для {symbol}: {e}")
        created_at_utc = pd.to_datetime(created_at_str)
        
    ccxt_symbol = symbol[:-4] + '/USDT' if symbol.endswith('USDT') else symbol
    
    try:
        df = await get_candles_main(ccxt_symbol, timeframe, limit=1000)
        if df is None or len(df) == 0:
            return True
            
        gap_candles = df[df.index >= created_at_utc]
        if len(gap_candles) == 0:
            return True
            
        print(f"⏳ Синхронізація історії {symbol} {timeframe} за {len(gap_candles)} свічок...")
        
        hit_tps = set(signal.get('hit_tps', []))
        breakeven = 0 in hit_tps
        stop_loss = signal.get('stop_loss')
        
        use_dobar_setting = get_setting('use_dobar')
        if use_dobar_setting is None:
            use_dobar_setting = True
            
        avg_entry = entry
        if use_dobar_setting and signal.get('dobar_low') is not None and signal.get('dobar_high') is not None:
            dobar_mid = (signal['dobar_low'] + signal['dobar_high']) / 2.0
            avg_entry = (entry + dobar_mid) / 2.0
            
        for timestamp, row in gap_candles.iterrows():
            high = row['high']
            low = row['low']
            
            if stop_loss:
                sl_hit = (direction == 'SHORT' and high >= stop_loss) or \
                         (direction == 'LONG' and low <= stop_loss)
                if sl_hit:
                    elapsed_min = int((timestamp - created_at_utc).total_seconds() / 60)
                    elapsed_str = f"{elapsed_min // 1440}d {(elapsed_min % 1440) // 60}h {elapsed_min % 60}m"
                    
                    msg_lines = [
                        f"#{symbol} {timeframe} {'LONG 📈' if direction == 'LONG' else 'SHORT 📉'}",
                        f"",
                    ]
                    
                    if breakeven and hit_tps:
                        tp_summary_lines = [f"✅ TP{i+1}: {tps[i][0]} | +{tps[i][2]}%" for i in sorted(hit_tps)]
                        msg_lines.extend(tp_summary_lines)
                        msg_lines.append(f"")
                        
                        pos_contracts = signal.get('pos_contracts', 0.0)
                        maker, taker = get_fees_for_exchange()
                        entry_fee = avg_entry * pos_contracts * taker
                        exit_fee = avg_entry * pos_contracts * taker
                        pnl_usd = - (entry_fee + exit_fee)
                        
                        msg_lines.append(f"↩️ Сигнал історично закрився у беззбиток ({elapsed_str})")
                        msg_lines.append(f"💰 Загальний прибуток: +{round(sum(tps[i][2] for i in hit_tps), 1)}% (PnL: <b>${pnl_usd:.2f}</b>)")
                        close_signal_stat(signal.get('stat_id'), 'tp', round(sum(tps[i][2] for i in hit_tps), 1), avg_entry)
                    else:
                        sl_pct = round(abs(stop_loss - entry) / entry * 100, 1)
                        
                        pos_contracts = signal.get('pos_contracts', 0.0)
                        maker, taker = get_fees_for_exchange()
                        entry_fee = avg_entry * pos_contracts * taker
                        exit_fee = stop_loss * pos_contracts * taker
                        gross_loss = (stop_loss - avg_entry) * pos_contracts if direction == 'LONG' else (avg_entry - stop_loss) * pos_contracts
                        pnl_usd = -abs(gross_loss) - (entry_fee + exit_fee)
                        
                        msg_lines.append(f"🛑 Stop-Loss історично спрацював ({elapsed_str}) | -{sl_pct}%")
                        msg_lines.append(f"💸 TP не було досягнуто")
                        msg_lines.append(f"❌ Сигнал закрився по стопу (PnL: <b>${pnl_usd:.2f}</b>)")
                        close_signal_stat(signal.get('stat_id'), 'sl', -sl_pct, stop_loss)
                        
                    try:
                        await bot.send_message(chat_id=CHAT_ID, text="\n".join(msg_lines), parse_mode='HTML')
                    except Exception as ex:
                        print(f"Помилка відправки іст. закриття {symbol}: {ex}")
                        
                    remove_active_signal(symbol, timeframe)
                    if signal in active_signals:
                        active_signals.remove(signal)
                    return False
                    
            new_hits = set()
            for i, (tp_price, _, _) in enumerate(tps):
                if direction == 'SHORT' and low <= tp_price:
                    new_hits.add(i)
                elif direction == 'LONG' and high >= tp_price:
                    new_hits.add(i)
                    
            if new_hits - hit_tps:
                hit_tps = hit_tps | new_hits
                signal['hit_tps'] = hit_tps
                
                if 0 in hit_tps and not breakeven:
                    breakeven = True
                    signal['stop_loss'] = avg_entry
                    stop_loss = avg_entry
                    
                if len(hit_tps) == len(tps):
                    elapsed_min = int((timestamp - created_at_utc).total_seconds() / 60)
                    elapsed_str = f"{elapsed_min // 1440}d {(elapsed_min % 1440) // 60}h {elapsed_min % 60}m"
                    
                    tp_summary_lines = [f"✅ TP{i+1}: {tps[i][0]} | +{tps[i][2]}%" for i in sorted(hit_tps)]
                    msg_lines = [
                        f"#{symbol} {timeframe} {'LONG 📈' if direction == 'LONG' else 'SHORT 📉'}",
                        f"",
                    ]
                    msg_lines.extend(tp_summary_lines)
                    msg_lines.append(f"")
                    
                    pos_contracts = signal.get('pos_contracts', 0.0)
                    maker, taker = get_fees_for_exchange()
                    entry_fee = avg_entry * pos_contracts * taker
                    exit_price_tp4 = tps[3][0]
                    exit_fee = exit_price_tp4 * pos_contracts * maker
                    gross_pnl = (exit_price_tp4 - avg_entry) * pos_contracts if direction == 'LONG' else (avg_entry - exit_price_tp4) * pos_contracts
                    pnl_usd = gross_pnl - (entry_fee + exit_fee)
                    
                    msg_lines.append(f"🎯 TP4: {tps[3][0]} ✅ ({elapsed_str})")
                    total_profit = sum(tps[i][2] for i in hit_tps)
                    msg_lines.append(f"💰 Загальний прибуток: +{round(total_profit, 1)}% (<b>+${pnl_usd:.2f}</b>)")
                    msg_lines.append(f"🏁 Сигнал історично закрився")
                    
                    try:
                        await bot.send_message(chat_id=CHAT_ID, text="\n".join(msg_lines), parse_mode='HTML')
                    except Exception as ex:
                        print(f"Помилка відправки іст. закриття {symbol}: {ex}")
                        
                    close_signal_stat(signal.get('stat_id'), 'tp', round(total_profit, 1), exit_price_tp4)
                    remove_active_signal(symbol, timeframe)
                    if signal in active_signals:
                        active_signals.remove(signal)
                    return False
                    
        signal['hit_tps'] = hit_tps
        signal['stop_loss'] = stop_loss
        save_active_signals(active_signals)
        return True
        
    except Exception as e:
        print(f"Помилка синхронізації пропущеної історії {symbol}: {e}")
        return True


# ─────────────────────────────────────────────
# SIGNAL MONITORING
# ─────────────────────────────────────────────

async def monitor_signal(bot, signal, active_signals):
    symbol = signal['symbol']
    direction = signal['direction']
    tps = signal['tps']
    
    hit_tps = set(signal.get('hit_tps', []))
    chart_message_id = signal['chart_message_id']
    start_time = datetime.now(timezone.utc)
    
    breakeven = 0 in hit_tps

    ccxt_symbol = symbol[:-4] + '/USDT' if symbol.endswith('USDT') else symbol
    print(f"Моніторинг {symbol} {signal['timeframe']} {direction}...")

    def elapsed_str():
        elapsed_min = int((datetime.now(timezone.utc) - start_time).total_seconds() / 60)
        d = elapsed_min // 1440
        h = (elapsed_min % 1440) // 60
        m = elapsed_min % 60
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
                    
                    pos_contracts = signal.get('pos_contracts', 0.0)
                    maker, taker = get_fees_for_exchange()
                    entry_fee = entry * pos_contracts * taker
                    exit_fee = price * pos_contracts * taker
                    
                    use_dobar_setting = get_setting('use_dobar')
                    if use_dobar_setting is None:
                        use_dobar_setting = True
                    avg_entry = entry
                    if use_dobar_setting and signal.get('dobar_low') is not None and signal.get('dobar_high') is not None:
                        dobar_mid = (signal['dobar_low'] + signal['dobar_high']) / 2.0
                        avg_entry = (entry + dobar_mid) / 2.0
                    
                    if direction == 'LONG':
                        gross_pnl = (price - avg_entry) * pos_contracts
                    else:
                        gross_pnl = (avg_entry - price) * pos_contracts
                    pnl_usd = gross_pnl - (entry_fee + exit_fee)

                    msg_lines.append(f"↩️ Сигнал закрився у беззбиток ({elapsed})")
                    msg_lines.append(f"💰 Загальний прибуток: +{round(total_profit, 1)}% (PnL: <b>${pnl_usd:.2f}</b>)")
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
                else:
                    sl_pct = round(abs(price - entry) / entry * 100, 1)
                    
                    pos_contracts = signal.get('pos_contracts', 0.0)
                    maker, taker = get_fees_for_exchange()
                    
                    use_dobar_setting = get_setting('use_dobar')
                    if use_dobar_setting is None:
                        use_dobar_setting = True
                    avg_entry = entry
                    if use_dobar_setting and signal.get('dobar_low') is not None and signal.get('dobar_high') is not None:
                        dobar_mid = (signal['dobar_low'] + signal['dobar_high']) / 2.0
                        avg_entry = (entry + dobar_mid) / 2.0
                        
                    entry_fee = avg_entry * pos_contracts * taker
                    exit_fee = price * pos_contracts * taker
                    
                    gross_loss = (price - avg_entry) * pos_contracts if direction == 'LONG' else (avg_entry - price) * pos_contracts
                    pnl_usd = gross_loss - (entry_fee + exit_fee)

                    msg_lines.append(f"🛑 Stop-Loss спрацював ({elapsed}) | -{sl_pct}%")
                    msg_lines.append(f"💸 TP не було досягнуто")
                    msg_lines.append(f"❌ Сигнал закрився по стопу (PnL: <b>${pnl_usd:.2f}</b>)")
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
                    
                    close_signal_stat(signal.get('stat_id'), 'sl', -sl_pct, price)

                remove_active_signal(symbol, signal['timeframe'])
                if signal in active_signals:
                    active_signals.remove(signal)
                break

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
                
                pos_contracts = signal.get('pos_contracts', 0.0)
                maker, taker = get_fees_for_exchange()
                
                use_dobar_setting = get_setting('use_dobar')
                if use_dobar_setting is None:
                    use_dobar_setting = True
                avg_entry = entry
                if use_dobar_setting and signal.get('dobar_low') is not None and signal.get('dobar_high') is not None:
                    dobar_mid = (signal['dobar_low'] + signal['dobar_high']) / 2.0
                    avg_entry = (entry + dobar_mid) / 2.0
                    
                entry_fee = avg_entry * pos_contracts * taker
                exit_fee = price * pos_contracts * taker
                
                if direction == 'LONG':
                    gross_pnl = (price - avg_entry) * pos_contracts
                else:
                    gross_pnl = (avg_entry - price) * pos_contracts
                pnl_usd = gross_pnl - (entry_fee + exit_fee)

                msg_lines.append(f"↩️ Сигнал закрився у беззбиток ({elapsed})")
                msg_lines.append(f"💰 Загальний прибуток: +{round(total_profit, 1)}% (PnL: <b>${pnl_usd:.2f}</b>)")

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

        new_hits = set()
        for i, (tp_price, prob, pct) in enumerate(tps):
            if direction == 'SHORT' and price <= tp_price:
                new_hits.add(i)
            elif direction == 'LONG' and price >= tp_price:
                new_hits.add(i)

        if new_hits - hit_tps:
            hit_tps = hit_tps | new_hits
            
            signal['hit_tps'] = hit_tps
            save_active_signals(active_signals)
            
            elapsed = elapsed_str()
            print(f"✅ {symbol} досягнуто TP: {hit_tps}")

            if 0 in hit_tps and not breakeven:
                breakeven = True
                signal['show_dobar'] = False
                
                use_dobar_setting = get_setting('use_dobar')
                if use_dobar_setting is None:
                    use_dobar_setting = True
                    
                if use_dobar_setting and signal.get('dobar_low') is not None and signal.get('dobar_high') is not None:
                    dobar_mid = (signal['dobar_low'] + signal['dobar_high']) / 2.0
                    avg_entry = (entry + dobar_mid) / 2.0
                    signal['stop_loss'] = avg_entry
                else:
                    signal['stop_loss'] = entry
                
                save_active_signals(active_signals)
                print(f"🔄 {symbol} стоп переведено в БУ: {signal['stop_loss']}")

            new_text = format_signal(
                symbol, signal['timeframe'], direction,
                signal['entry'], signal['dobar_low'], signal['dobar_high'],
                tps, signal['stats'], hit_tps,
                tier=signal.get('tier', '🟢'),
                stop_loss=signal.get('stop_loss'),
                correlation=signal.get('correlation'),
                funding_rate=signal.get('funding_rate'),
                open_interest=signal.get('open_interest'),
                mode=signal.get('mode', 'swing'),
                pos_usd=signal.get('pos_usd'),
                pos_contracts=signal.get('pos_contracts'),
                margin_required=signal.get('margin_required')
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
                    parse_mode='HTML',
                    read_timeout=30, write_timeout=30, connect_timeout=30,
                )
            except Exception as e:
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
                
                pos_contracts = signal.get('pos_contracts', 0.0)
                maker, taker = get_fees_for_exchange()
                
                use_dobar_setting = get_setting('use_dobar')
                if use_dobar_setting is None:
                    use_dobar_setting = True
                avg_entry = entry
                if use_dobar_setting and signal.get('dobar_low') is not None and signal.get('dobar_high') is not None:
                    dobar_mid = (signal['dobar_low'] + signal['dobar_high']) / 2.0
                    avg_entry = (entry + dobar_mid) / 2.0
                    
                entry_fee = avg_entry * pos_contracts * taker
                exit_price_tp4 = tps[3][0]
                exit_fee = exit_price_tp4 * pos_contracts * maker
                
                if direction == 'LONG':
                    gross_pnl = (exit_price_tp4 - avg_entry) * pos_contracts
                else:
                    gross_pnl = (avg_entry - exit_price_tp4) * pos_contracts
                pnl_usd = gross_pnl - (entry_fee + exit_fee)

                msg_lines.append(f"🎯 TP{last_tp+1}: {tps[last_tp][0]} ✅ ({elapsed_str})")
                msg_lines.append(f"💰 Загальний прибуток: +{round(total_profit, 1)}% (<b>+${pnl_usd:.2f}</b>)")
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

async def scan_and_send(bot, active_signals, timeframes, scan_logs=None):
    global recently_sent, is_scanning
    
    if is_scanning:
        log_skip_main("⚠️ Сканування вже виконується іншим процесом, пропускаємо", scan_logs)
        return False
        
    is_scanning = True
    try:
        all_signals = await scan_all(timeframes, scan_logs=scan_logs)
        new_count = 0
        max_signals = get_setting('max_active_signals')

        for signal in all_signals:
            if new_count >= 3:
                break

            if len(active_signals) >= max_signals:
                log_skip_main(f"⚠️ Досягнуто ліміт активних сигналів ({max_signals})", scan_logs)
                break

            symbol_clean = signal['symbol'].replace('/', '')
            is_already_monitored = any(
                s['symbol'].replace('/', '') == symbol_clean for s in active_signals
            )

            if is_already_monitored or symbol_clean in recently_sent:
                log_skip_main(f"⚠️ {symbol_clean} вже моніториться — пропускаємо", scan_logs)
                continue

            try:
                risk_usd, pos_usd, pos_contracts, margin_required, is_averaged, avg_entry = calculate_position_size_v2(
                    signal['entry'], signal.get('stop_loss'), signal.get('dobar_low'), signal.get('dobar_high')
                )
                
                signal['pos_usd'] = pos_usd
                signal['pos_contracts'] = pos_contracts
                signal['margin_required'] = margin_required

                signal_text = format_signal(
                    symbol_clean, signal['timeframe'],
                    signal['direction'], signal['entry'],
                    signal['dobar_low'], signal['dobar_high'],
                    signal['tps'], signal['stats'],
                    tier=signal.get('tier', '🟢'),
                    stop_loss=signal.get('stop_loss'),
                    correlation=signal.get('correlation'),
                    funding_rate=signal.get('funding_rate'),
                    open_interest=signal.get('open_interest'),
                    mode=signal.get('mode', 'swing'),
                    pos_usd=pos_usd,
                    pos_contracts=pos_contracts,
                    margin_required=margin_required
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
                    parse_mode='HTML',
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
                        
                        scan_logs = []
                        active_tfs = get_setting('active_timeframes')
                        success = await scan_and_send(bot, active_signals, active_tfs, scan_logs=scan_logs)
                        
                        if success:
                            if scan_logs:
                                import html
                                escaped_logs = [html.escape(line) for line in scan_logs]
                                log_text = "\n".join(escaped_logs)
                                
                                if len(log_text) > 3900:
                                    log_text = log_text[:3800] + "\n\n⚠️ <i>(Лог обрізано через ліміт повідомлення Telegram)</i>"
                                
                                await bot.send_message(
                                    chat_id=chat_id,
                                    text=f"📋 <b>Результати сканування:</b>\n\n{log_text}\n\n✅ <b>Ручне сканування успішно завершено!</b>",
                                    parse_mode='HTML'
                                )
                            else:
                                await bot.send_message(
                                    chat_id=chat_id, 
                                    text="✅ <b>Ручне сканування успішно завершено! Нових сигналів не знайдено.</b>", 
                                    parse_mode='HTML'
                                )

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
                    data = query.data

                    if str(user_id) != str(ADMIN_ID):
                        await bot.answer_callback_query(query.id, text="⛔ Доступ заборонено")
                        continue

                    await bot.answer_callback_query(query.id)

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
                                await bot.send_message(
                                    chat_id=chat_id, 
                                    text="⏳ Сканування вже виконується іншим процесом. Будь ласка, зачекайте..."
                                )
                                continue
                            
                            await bot.send_message(
                                chat_id=chat_id, 
                                text="🔍 Запущено позачергове ручне сканування для всіх активних таймфреймів..."
                            )
                            
                            scan_logs = []
                            active_tfs = get_setting('active_timeframes')
                            success = await scan_and_send(bot, active_signals, active_tfs, scan_logs=scan_logs)
                            
                            if success:
                                if scan_logs:
                                    log_text = "\n".join(scan_logs)
                                    if len(log_text) > 3900:
                                        log_text = log_text[:3800] + "\n\n⚠️ <i>(Лог обрізано через ліміт повідомлення Telegram)</i>"
                                    
                                    await bot.send_message(
                                        chat_id=chat_id,
                                        text=f"📋 <b>Результати сканування:</b>\n\n{log_text}\n\n✅ <b>Ручне сканування успішно завершено!</b>",
                                        parse_mode='HTML'
                                    )
                                else:
                                    await bot.send_message(
                                        chat_id=chat_id, 
                                        text="✅ <b>Ручне сканування успішно завершено! Нових сигналів не знайдено.</b>", 
                                        parse_mode='HTML'
                                    )

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

                    elif data == 'toggle_exchange':
                        current = get_setting('exchange_name') or 'binance'
                        new_exchange = 'mexc' if current == 'binance' else 'binance'
                        set_setting('exchange_name', new_exchange)
                        
                        try:
                            if hasattr(scanner, 'exchange') and scanner.exchange:
                                asyncio.create_task(scanner.exchange.close())
                        except Exception as e:
                            print(f"Помилка закриття старої сесії: {e}")
                        
                        exchange = get_exchange_client(async_mode=False)
                        scanner.exchange = get_exchange_client(async_mode=True)
                        print(f"🏛 Біржу успішно перемикнуто на: {new_exchange.upper()}")
                        
                        await bot.send_message(
                            chat_id=chat_id,
                            text=get_settings_text(),
                            reply_markup=main_settings_keyboard(),
                            parse_mode='HTML'
                        )

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

                    elif data in ['risk_stop_info', 'risk_tp1_info', 'risk_max_info', 'risk_depo_info', 'risk_pct_info', 'risk_lev_info', 'filter_prob_info', 'filter_htf_info', 'filter_funding_info', 'filter_oi_info']:
                        pass

                    elif data == 'toggle_htf':
                        current = get_setting('htf_bias_enabled')
                        set_setting('htf_bias_enabled', not current)
                        text, markup = filters_keyboard()
                        await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, parse_mode='HTML')

                    elif data == 'toggle_btc_filter':
                        current = get_setting('btc_filter_enabled')
                        if current is None:
                            current = True
                        set_setting('btc_filter_enabled', not current)
                        text, markup = filters_keyboard()
                        await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, parse_mode='HTML')

                    elif data == 'toggle_regime_filter':
                        current = get_setting('regime_filter_enabled')
                        if current is None:
                            current = True
                        set_setting('regime_filter_enabled', not current)
                        text, markup = filters_keyboard()
                        await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, parse_mode='HTML')

                    elif data == 'toggle_scalp_mode':
                        current = get_setting('scalper_mode_enabled')
                        if current is None:
                            current = True
                        set_setting('scalper_mode_enabled', not current)
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

                    elif data == 'toggle_funding_filter':
                            current = get_setting('funding_filter_enabled')
                            if current is None:
                                current = True
                            set_setting('funding_filter_enabled', not current)
                            text, markup = filters_keyboard()
                            await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, parse_mode='HTML')

                    elif data == 'filter_funding_up':
                            v = round((get_setting('funding_max_limit') or 0.05) + 0.005, 3)
                            set_setting('funding_max_limit', min(v, 0.5))
                            text, markup = filters_keyboard()
                            await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, parse_mode='HTML')

                    elif data == 'filter_funding_down':
                            v = round((get_setting('funding_max_limit') or 0.05) - 0.005, 3)
                            set_setting('funding_max_limit', max(v, 0.005))
                            text, markup = filters_keyboard()
                            await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, parse_mode='HTML')

                    elif data == 'toggle_oi_filter':
                        current = get_setting('oi_filter_enabled')
                        if current is None:
                            current = True
                        set_setting('oi_filter_enabled', not current)
                        text, markup = filters_keyboard()
                        await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, parse_mode='HTML')

                    elif data == 'filter_oi_up':
                        v = (get_setting('oi_min_limit') or 10.0) + 1.0
                        set_setting('oi_min_limit', min(v, 100.0))
                        text, markup = filters_keyboard()
                        await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, parse_mode='HTML')

                    elif data == 'filter_oi_down':
                        v = (get_setting('oi_min_limit') or 10.0) - 1.0
                        set_setting('oi_min_limit', max(v, 1.0))
                        text, markup = filters_keyboard()
                        await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, parse_mode='HTML')

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
    now = time.time()
    remaining = interval_seconds - (now % interval_seconds)
    await asyncio.sleep(remaining + 1.0)


async def main():
    global active_timeframe
    init_db()
    bot = Bot(token=BOT_TOKEN, request=request)

    active_signals = load_active_signals()
    print(f"Завантажено {len(active_signals)} активних сигналів з диску")

    for signal in list(active_signals):
        if signal.get('chart_message_id'):
            is_still_active = await reconcile_historical_gap(bot, signal, active_signals)
            
            if is_still_active:
                asyncio.create_task(monitor_signal(bot, signal, active_signals))
                print(f"🔄 Відновлено живий моніторинг: {signal['symbol']} {signal['timeframe']}")
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
        daily_stats(), clear_recently_sent(),
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