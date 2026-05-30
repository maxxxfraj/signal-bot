# main.py
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
from telegram import Bot, ReplyKeyboardMarkup, KeyboardButton
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
from database import get_daily_pnl_usd, get_consecutive_losses_count, get_last_trade_closed_at
from database import get_rejected_stats_summary
from executor import FuturesExecutor
import os
import io
import sys
import urllib3
import time
import html  # Безпечне екранування логів
from datetime import datetime, timezone

# Вимикає довгі технічні попередження про неперевірений SSL у консолі
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

sys.stdout.reconfigure(line_buffering=True)

# ГЛОБАЛЬНІ ОБ'ЄКТИ ДЛЯ БЕЗПЕКИ ТА ПЕРСИСТЕНТНОСТІ (ФАЗА Б)
recently_sent = set()
active_monitors = {}                       # Глобальний реєстр унікальних тасків
active_signals_lock = asyncio.Lock()        # Мютекс захисту пам'яті від гонок процесів
global_executor = None                      # Персистентний торговий виконавець
async_exchange = None                       # Глобальний Singleton-клієнт CCXT

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

# Ініціалізація динамічного клієнта біржі для малювання графіків (синхронний)
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
# CHARTS & FORMATTING (ДИНАМІЧНИЙ РИЗИК ТА ПОСИЛАННЯ)
# ─────────────────────────────────────────────

is_scanning = False  # Глобальний запобіжник паралельних сканувань

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


async def get_returns_for_correlation(symbol, timeframe="1h", limit=30):
    """Швидко завантажує ретерни для розрахунку Pearson correlation"""
    try:
        ccxt_futures_symbol = resolve_ccxt_futures_symbol(exchange, symbol)
        df = await get_candles_main(ccxt_futures_symbol, timeframe, limit=limit)
        if df is not None and len(df) > 1:
            return df['close'].pct_change().dropna()
    except Exception as e:
        print(f"Помилка завантаження ретернів для кореляції {symbol}: {e}")
    return None


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

# main.py

def format_signal(
    symbol, timeframe, direction, entry, dobar_low, dobar_high,
    tps, stats, hit_tps=None, tier='🟢', stop_loss=None, correlation=None,
    funding_rate=None, open_interest=None, mode='swing',
    pos_usd=None, pos_contracts=None, margin_required=None
):
    """Генерує професійний HTML-шаблон для відправки сигналу в Telegram з підтримкою динамічних сигнатур"""
    # Якщо hit_tps не передано (новий сигнал), ініціалізуємо його порожнім списком
    if hit_tps is None:
        hit_tps = []
        
    dir_emoji = "📈 LONG" if direction == "LONG" else "📉 SHORT"
    
    lines = [
        f"🚨 <b>НОВИЙ СИГНАЛ: {tier} #{symbol} ({timeframe})</b>",
        f"⚖️ Напрямок: <b>{dir_emoji}</b>",
        f"📊 Режим стратегії: <b>{mode.upper()}</b>",
        f"",
        f"💵 Вхід (Entry): <b>{entry}</b>"
    ]
    
    if dobar_low is not None and dobar_high is not None:
        lines.append(f"↩️ Зона добору (Dobar): <b>{dobar_low} - {dobar_high}</b>")
        
    if stop_loss:
        lines.append(f"🛑 Stop-Loss: <b>{stop_loss}</b>")
        
    lines.append("")
    lines.append("🎯 <b>Take-Profit цілі:</b>")
    
    tp_labels = ["TP1", "TP2", "TP3", "TP4"]
    for i, tp in enumerate(tps):
        tp_price = tp[0]
        prob = tp[1] if len(tp) > 1 else 50
        pct = tp[2] if len(tp) > 2 else 0.0
        
        is_hit = i in hit_tps
        checkmark = "✅ " if is_hit else "⬜ "
        lines.append(f"  {checkmark}{tp_labels[i]}: <b>{tp_price}</b> | Ймовірність: <b>{prob}%</b> | Прибуток: <b>+{pct}%</b>")
        
    if pos_usd or pos_contracts or margin_required:
        lines.append("")
        lines.append("💼 <b>Ризик-параметри (Sizing):</b>")
        if pos_usd:
            try:
                lines.append(f"  💵 Об'єм позиції: <b>${float(pos_usd):.2f}</b>")
            except ValueError:
                lines.append(f"  💵 Об'єм позиції: <b>${pos_usd}</b>")
        if pos_contracts:
            try:
                lines.append(f"  📦 Контракти: <b>{float(pos_contracts):.4f} {symbol[:-4]}</b>")
            except ValueError:
                lines.append(f"  📦 Контракти: <b>{pos_contracts} {symbol[:-4]}</b>")
        if margin_required:
            try:
                lines.append(f"  ⚡ Необхідна маржа: <b>${float(margin_required):.2f}</b>")
            except ValueError:
                lines.append(f"  ⚡ Необхідна маржа: <b>${margin_required}</b>")
            
    if correlation is not None or funding_rate is not None or open_interest is not None:
        lines.append("")
        lines.append("🌡 <b>Деривативні метрики ринку:</b>")
        if correlation is not None:
            try:
                lines.append(f"  🪙 Кореляція з BTC: <b>{float(correlation):.2f}</b>")
            except ValueError:
                lines.append(f"  🪙 Кореляція з BTC: <b>{correlation}</b>")
        if funding_rate is not None:
            try:
                lines.append(f"  💵 Ставка фінансування (Funding): <b>{float(funding_rate):.4f}%</b>")
            except ValueError:
                lines.append(f"  💵 Ставка фінансування (Funding): <b>{funding_rate}%</b>")
        if open_interest is not None:
            try:
                lines.append(f"  📈 Відкритий інтерес (OI): <b>${float(open_interest)/1_000_000:.2f}M</b>")
            except ValueError:
                lines.append(f"  📈 Відкритий інтерес (OI): <b>${open_interest}</b>")
            
    exchange_name = get_setting('exchange_name') or 'binance'
    symbol_clean = symbol.replace('/', '').upper()
    if exchange_name == 'mexc':
        base = symbol_clean[:-4]
        link = f"https://futures.mexc.com/exchange/{base}_USDT"
    else:
        link = f"https://www.binance.com/en/futures/{symbol_clean}"
        
    lines.append("")
    lines.append(f"🔗 <a href='{link}'>Торгувати на {exchange_name.upper()}</a>")
    
    return "\n".join(lines)


# ─────────────────────────────────────────────
# ВСПОМІЖНИЙ МЕТОД ДЛЯ АВТОРИЗОВАНИХ КЛІЄНТІВ
# ─────────────────────────────────────────────

# Глобальний Singleton-об'єкт підключення
async_exchange = None

async def get_auth_exchange_client():
    """Повертає єдиний глобальний асинхронний клієнт CCXT (Singleton-патерн)"""
    global async_exchange
    if async_exchange is not None:
        return async_exchange
        
    import ccxt.async_support as ccxt_async
    
    exchange_name = get_setting('exchange_name') or 'binance'
    testnet_mode = get_setting('testnet_enabled')
    
    if testnet_mode:
        api_key = os.getenv("TESTNET_API_KEY")
        secret = os.getenv("TESTNET_API_SECRET")
    else:
        api_key = os.getenv("PROD_API_KEY")
        secret = os.getenv("PROD_API_SECRET")
        
    if not api_key or not secret:
        raise ValueError("Критична помилка: в системних змінних Render/.env не знайдено API-ключів!")
        
    if exchange_name == 'binance':
        client = ccxt_async.binanceusdm({
            'apiKey': api_key,
            'secret': secret,
            'enableRateLimit': True,
            'aiohttp_trust_env': False
        })
        client.options['warnOnFetchOpenOrdersWithoutSymbol'] = False
        if testnet_mode:
            client.enable_demo_trading(True)
    else:
        client = ccxt_async.mexc({
            'apiKey': api_key,
            'secret': secret,
            'enableRateLimit': True,
            'options': {'defaultType': 'swap'},
            'aiohttp_trust_env': False
        })
        if testnet_mode:
            # Виправлено для сумісності з MEXC
            client.set_sandbox_mode(True)
            
    # ФІНАЛЬНИЙ БЛОК ІНІЦІАЛІЗАЦІЇ (Обов'язковий)
    try:
        await client.load_markets()
    except Exception as e:
        await client.close()
        raise e
        
    async_exchange = client
    return async_exchange

# ─────────────────────────────────────────────
# УНІФІКОВАНИЙ ХЕЛПЕР РЕЗОЛВУ СИМВОЛУ Ф'ЮЧЕРСІВ CCXT
# ─────────────────────────────────────────────

def resolve_ccxt_futures_symbol(exchange_client, db_symbol):
    """
    Уніфікований розбір символу під конкретну версію CCXT (з урахуванням чи без суфіксу :USDT)
    Усуває помилку BadSymbol для ICPUSDT та інших альткоїнів.
    """
    if not db_symbol.endswith('USDT'):
        return db_symbol
        
    base = db_symbol[:-4]
    
    sym_tz = f"{base}/USDT:USDT"
    if sym_tz in exchange_client.markets:
        return sym_tz
        
    # ВИПРАВЛЕНО БАГ №2: Спот-фолбек виключено для запобігання торгівлі на спотовому ринку!
    sym_spot = f"{base}/USDT"
    if sym_spot in exchange_client.markets:
        market_info = exchange_client.markets[sym_spot]
        if market_info.get('type') in ['swap', 'future', 'linear']:
            return sym_spot
        
    return sym_tz


# ─────────────────────────────────────────────
# УНІФІКОВАНИЙ ХЕЛПЕР ОТРИМАННЯ ОБ'ЄМУ ПОЗИЦІЇ
# ─────────────────────────────────────────────

async def get_active_position_qty(async_ex, symbol_clean, ccxt_symbol):
    """
    Каскадний асинхронний метод визначення об'єму позиції на біржі.
    Сумісний з будь-якими версіями CCXT для Binance та MEXC.
    """
    try:
        positions = await async_ex.fetch_positions([ccxt_symbol])
        for pos in positions:
            pos_sym = pos.get('symbol', '').replace('/', '').split(':')[0]
            if pos_sym == symbol_clean:
                return abs(float(pos.get('contracts', 0.0)))
    except Exception:
        pass
        
    try:
        balance = await async_ex.fetch_balance()
        
        # 2.1 Format Binance
        raw_positions = balance.get('info', {}).get('positions', [])
        for pos in raw_positions:
            if pos.get('symbol') == symbol_clean:
                return abs(float(pos.get('positionAmt', 0.0)))
                
        # 2.2 Format MEXC
        raw_mexc = balance.get('info', {}).get('data', [])
        if isinstance(raw_mexc, list):
            for pos in raw_mexc:
                if pos.get('symbol') == symbol_clean:
                    return abs(float(pos.get('positionAmt', 0.0)))
                    
        # 2.3 Стандартний узагальнений масив CCXT
        unified_positions = balance.get('positions', [])
        for pos in unified_positions:
            pos_sym = pos.get('symbol', '').replace('/', '').split(':')[0]
            if pos_sym == symbol_clean:
                return abs(float(pos.get('contracts', 0.0)))
                
    except Exception as e:
        print(f"Помилка розрахунку об'єму позиції для {symbol_clean}: {e}")
    return 0.0


# ─────────────────────────────────────────────
# УНІВЕРСАЛЬНИЙ ХЕЛПЕР ПОВНОГО ОЧИЩЕННЯ СІТКИ (БЕЗПЕЧНО ДЛЯ Ф'ЮЧЕРСІВ)
# ─────────────────────────────────────────────


# main.py

# main.py

async def cancel_all_exchange_orders_for_symbol(async_ex, symbol_clean, ccxt_symbol):
    """
    Універсальний, 100% завадостійкий метод очищення абсолютно всіх ордерів по монеті.
    Скасовує базові лімітки та алгоритмічні умовні ордери (Stop-Loss/Take-Profit).
    Оптимізовано під специфіку шлюзів Binance та MEXC.
    """
    print(f"🧹 Запуск повного очищення ліміток та тригерних ордерів для {symbol_clean} ({ccxt_symbol})...")
    try:
        if 'binance' in async_ex.id.lower():
            market = async_ex.market(ccxt_symbol)
            
            # 1. Скасовуємо стандартні лімітні ордери (Dobar, Limit TPs)
            try:
                await async_ex.fapiPrivateDeleteAllOpenOrders({'symbol': market['id']})
                print(f"✅ Успішно скасовано базові лімітні ордери на Binance для {symbol_clean}")
            except Exception as e:
                print(f"⚠️ Попередження під час видалення базових ордерів на Binance: {e}")
                
            # 2. Скасовуємо алгоритмічні умовні ордери (STOP_MARKET Stop-Loss, Algo TPs)
            try:
                await async_ex.fapiPrivateDeleteAlgoOpenOrders({'symbol': market['id']})
                print(f"✅ Успішно скасовано умовні Algo-ордери (Stop-Loss) на Binance для {symbol_clean}")
            except Exception as e:
                print(f"⚠️ Попередження під час видалення умовних Algo-ордерів на Binance: {e}")
                
            print(f"🧹 Повне очищення Binance для {symbol_clean} завершено.")
            
        elif 'mexc' in async_ex.id.lower():
            try:
                await async_ex.cancel_all_orders(ccxt_symbol)
            except Exception:
                pass
            # Поштучно зачищаємо умовні стопи MEXC, якщо вони залишились
            open_orders = await async_ex.fetch_open_orders(ccxt_symbol)
            for order in open_orders:
                try:
                    await async_ex.cancel_order(order['id'], ccxt_symbol)
                except Exception:
                    pass
        else:
            # Загальний фолбек для інших підключень
            await async_ex.cancel_all_orders(ccxt_symbol)
    except Exception as e:
        print(f"⚠️ Попередження під час групового скасування ордерів для {symbol_clean}: {e}")
        # Фолбек-зачистка поштучно в разі виникнення помилок
        try:
            open_orders = await async_ex.fetch_open_orders(ccxt_symbol)
            for order in open_orders:
                try:
                    await async_ex.cancel_order(order['id'], ccxt_symbol)
                except Exception:
                    pass
        except Exception as fe:
            print(f"❌ Критична помилка фолбек-зачистки для {symbol_clean}: {fe}")

# ─────────────────────────────────────────────
# ФАЗА Б: СТАНЦІЯ ПРИМИРЕННЯ ТА ЛІКУВАННЯ ПОЗИЦІЙ ПРИ СТАРТІ (УЛЬТИМАТИВНИЙ GRID RESET З ЗАТРИМКОЮ)
# ─────────────────────────────────────────────

async def reconcile_active_signals_state(bot, active_signals):
    """
    Сканує всі активні сигнали при старті програми, звіряє їхній стан з біржею, 
    повністю зачищає старі дублікати (як стандартні, так і умовні Algo-ордери)
    та перевиставляє ідеальну чисту сітку (Stop-Loss, Dobar, Take-Profits) з авто-лікуванням.
    """
    print("🔍 Запуск примирення (Reconciliation) та лікування позицій при старті...")
    
    for signal in list(active_signals):
        symbol = signal['symbol']
        timeframe = signal['timeframe']
        
        async_ex = None
        try:
            async_ex = await get_auth_exchange_client()
            ccxt_futures_symbol = resolve_ccxt_futures_symbol(async_ex, symbol)
            
            # 1. Отримуємо фактичний об'єм позиції на біржі
            active_qty = await get_active_position_qty(async_ex, symbol, ccxt_futures_symbol)
            
            # --- ВАРІАНТ 1: ПОЗИЦІЮ ЗАКРИТО ОФЛАЙН (Об'єм на біржі = 0) ---
            if active_qty == 0:
                print(f"🧹 Позицію по {symbol} було закрито на біржі офлайн. Очищення залишкових ордерів...")
                await cancel_all_exchange_orders_for_symbol(async_ex, symbol.replace('/', ''), ccxt_futures_symbol)
                
                async with active_signals_lock:
                    remove_active_signal(symbol, timeframe)
                    if signal in active_signals:
                        active_signals.remove(signal)
                    active_monitors.pop((symbol, timeframe), None)
                    
                await bot.send_message(
                    chat_id=CHAT_ID,
                    text=f"🏁 <b>Позицію по #{symbol} ({timeframe}) було закрито на біржі офлайн.</b>\n"
                         f"🧹 Всі залишені ордери успішно асинхронно очищені при старті бота.",
                    parse_mode='HTML'
                )
                continue
                
            # --- ВАРІАНТ 2: ПОВНИЙ GRID RESET ПРИ СТАРТІ ---
            print(f"🔄 Лікування при старті: Повна перебудова сітки для {symbol} ({timeframe})...")
            
            # 1. Повністю скасовуємо всі ордери по монеті на біржі ( Стандарті та Алгоритмічні )
            await cancel_all_exchange_orders_for_symbol(async_ex, symbol.replace('/', ''), ccxt_futures_symbol)
            await asyncio.sleep(1.5)
            
            direction = signal['direction']
            entry_side = "buy" if direction == "LONG" else "sell"
            exit_side = "sell" if direction == "LONG" else "buy"
            
            # --- ВІДНОВЛЕННЯ 1: STOP-LOSS (STOP_MARKET) З АВТО-ЛІКУВАННЯМ БУ ---
            hit_tps = set(signal.get('hit_tps', []))
            use_dobar_setting = get_setting('use_dobar')
            if use_dobar_setting is None:
                use_dobar_setting = True
                
            if 0 in hit_tps:
                # Якщо TP1 вже зафіксовано в базі, примусово та динамічно виправляємо ціну БУ на правильну
                dobar_filled_state = bool(signal.get('dobar_filled_state', False))
                if use_dobar_setting and dobar_filled_state:
                    dobar_mid = (signal['dobar_low'] + signal['dobar_high']) / 2.0
                    correct_sl = (signal['entry'] + dobar_mid) / 2.0
                else:
                    correct_sl = signal['entry']
                
                sl_price = correct_sl
                signal['stop_loss'] = correct_sl
                print(f"🩹 [HEALING] Виправлено пошкоджений БУ-стоп для {symbol} на правильний рівень: {correct_sl}")
            else:
                sl_price = signal.get('stop_loss')
                
            new_sl_id = None
            if sl_price:
                sl_price_str = async_ex.price_to_precision(ccxt_futures_symbol, sl_price)
                sl_params = {
                    'stopPrice': float(sl_price_str),
                    'reduceOnly': True
                }
                new_sl_order = await async_ex.create_order(
                    symbol=ccxt_futures_symbol,
                    type='STOP_MARKET',
                    side=exit_side,
                    amount=active_qty,
                    params=sl_params
                )
                new_sl_id = new_sl_order["id"]
                
            # --- ВІДНОВЛЕННЯ 2: DOBAR ---
            dobar_filled_state = bool(signal.get('dobar_filled_state', False))
            total_expected = float(async_ex.amount_to_precision(ccxt_futures_symbol, signal.get('pos_contracts', 0.0)))
            
            # Якщо БУ ще не був активований у базі, але на біржі об'єм вирівнявся до 100% (добор виконано офлайн)
            if not dobar_filled_state and active_qty >= total_expected * 0.95:
                signal['dobar_filled_state'] = True
                dobar_filled_state = True
                print(f"↩️ Виявлено виконання добору (Dobar) для {symbol} офлайн при старті.")

            new_dobar_id = signal.get('dobar_order_id')
            if use_dobar_setting and not dobar_filled_state and (0 not in hit_tps):
                dobar_low = signal.get('dobar_low')
                dobar_high = signal.get('dobar_high')
                if dobar_low is not None and dobar_high is not None:
                    dobar_mid = (dobar_low + dobar_high) / 2.0
                    dobar_price_str = async_ex.price_to_precision(ccxt_futures_symbol, dobar_mid)
                    print(f"⏳ Відновлення Dobar лімітки на {active_qty} за ціною {dobar_price_str}")
                    dobar_order = await async_ex.create_order(
                        symbol=ccxt_futures_symbol,
                        type='limit',
                        side=entry_side,
                        amount=active_qty,
                        price=float(dobar_price_str)
                    )
                    new_dobar_id = dobar_order["id"]
            else:
                new_dobar_id = None
                    
# --- ВІДНОВЛЕННЯ 3: TAKE-PROFIT (З НЕЛІНІЙНИМ РОЗПОДІЛОМ 50/20/15/15) ---
                new_tp_ids = []
                tps = signal.get('tps', [])
                
                remaining_tps_count = 4 - len(hit_tps)
                if remaining_tps_count > 0:
                    original_percentages = [0.50, 0.20, 0.15, 0.15]
                    remaining_pct_sum = sum(original_percentages[i] for i in range(4) if i not in hit_tps)
                    if remaining_pct_sum <= 0:
                        remaining_pct_sum = 0.25
                        
                    # Отримуємо найближчу невідкриту ціль тейку
                    next_tp_price = tps[3][0]
                    for idx, (tp_price, _, _) in enumerate(tps[:4]):
                        if idx not in hit_tps:
                            next_tp_price = tp_price
                            break
                            
                    # Знаходимо індекс для розрахунку наступного кроку
                    next_tp_idx = 0
                    for i, t_val in enumerate(tps):
                        if t_val[0] == next_tp_price:
                            next_tp_idx = i
                            break
                    next_tp_pct = original_percentages[next_tp_idx]
                    
                    planned_step_volume = active_qty * (next_tp_pct / remaining_pct_sum)
                    estimated_step_notional = planned_step_volume * next_tp_price
                    
                    if estimated_step_notional < 5.1:
                        print(f"⚠️ [NOTIONAL GUARD] Крок менший за ліміт $5. Об'єднуємо тейки в один.")
                        tp_price_str = async_ex.price_to_precision(ccxt_futures_symbol, next_tp_price)
                        tp_order = await async_ex.create_order(
                            symbol=ccxt_futures_symbol,
                            type='limit',
                            side=exit_side,
                            amount=active_qty,
                            price=float(tp_price_str),
                            params={'reduceOnly': True}
                        )
                        new_tp_ids.append(tp_order['id'])
                    else:
                        accumulated_vol = 0.0
                        tp_counter = 0
                        for idx, (tp_price, _, _) in enumerate(tps[:4]):
                            if idx in hit_tps:
                                continue
                                
                            share = original_percentages[idx] / remaining_pct_sum
                            current_tp_vol = float(async_ex.amount_to_precision(ccxt_futures_symbol, active_qty * share))
                            
                            if tp_counter == remaining_tps_count - 1:
                                current_tp_vol = float(async_ex.amount_to_precision(
                                    ccxt_futures_symbol, active_qty - accumulated_vol
                                ))
                            if current_tp_vol <= 0:
                                continue
                                
                            accumulated_vol += current_tp_vol
                            tp_price_str = async_ex.price_to_precision(ccxt_futures_symbol, tp_price)
                            print(f"🎯 Відновлення TP{idx+1} (частка {share*100:.0f}%) на {current_tp_vol} за ціною {tp_price_str}")
                            tp_order = await async_ex.create_order(
                                ccxt_futures_symbol,
                                type='limit',
                                side=exit_side,
                                amount=current_tp_vol,
                                price=float(tp_price_str),
                                params={'reduceOnly': True}
                            )
                            new_tp_ids.append(tp_order["id"])
                            tp_counter += 1
                
            # Оновлюємо стан ордерів у базі
            def update_startup_db_orders(db_id, actual_vol, sl_id, dobar_id, tp_ids, stop_loss_price):
                from database import get_connection
                conn = get_connection()
                try:
                    cursor = conn.cursor()
                    tp_ids_str = ",".join(tp_ids) if tp_ids else ""
                    dobar_filled_val = 1 if dobar_filled_state else 0
                    cursor.execute(
                        "UPDATE signals SET pos_contracts = %s, stop_loss_id = %s, dobar_order_id = %s, tp_order_ids = %s, dobar_filled_state = %s, stop_loss = %s WHERE id = %s",
                        (actual_vol if dobar_filled_state else actual_vol * 2.0, sl_id, dobar_id, tp_ids_str, dobar_filled_val, stop_loss_price, db_id)
                    )
                    conn.commit()
                    cursor.close()
                except Exception as ex:
                    print(f"Помилка оновлення стартових ордерів у БД: {ex}")
                finally:
                    conn.close()
                    
            if signal.get('db_id'):
                update_startup_db_orders(signal['db_id'], active_qty, new_sl_id, new_dobar_id, new_tp_ids, sl_price)
                
            signal['pos_contracts'] = active_qty if dobar_filled_state else active_qty * 2.0
            signal['stop_loss_id'] = new_sl_id
            signal['dobar_order_id'] = new_dobar_id
            signal['tp_order_ids'] = new_tp_ids
                
        except Exception as rec_err:
            print(f"Помилка примирення при старті для {symbol}: {rec_err}")
        finally:
            pass
                
    save_active_signals(active_signals)
    print("✅ Фонове примирення при старті завершено успішно!")


# ─────────────────────────────────────────────
# СЕРВІС СИНХРОНІЗАЦІЇ ІСТОРІЇ (Gap Reconciliation)
# ─────────────────────────────────────────────

async def reconcile_historical_gap(bot, signal, active_signals):
    """Синхронізує та відновлює упущені цілі/стопи з урахуванням локального часового поясу комп'ютера"""
    symbol = signal['symbol']
    timeframe = signal['timeframe']
    direction = signal['direction']
    tps = signal['tps']
    entry = signal['entry']
    created_at_str = signal.get('created_at')
    
    if not created_at_str:
        return True
        
    try:
        created_at = pd.to_datetime(created_at_str)
        if created_at.tz is not None:
            created_at_utc = created_at.tz_convert('UTC').tz_localize(None)
        else:
            created_at_utc = created_at
    except Exception as e:
        print(f"Помилка розбору дати упущення для {symbol}: {e}")
        created_at_utc = pd.to_datetime(created_at_str)
        
    ccxt_futures_symbol = resolve_ccxt_futures_symbol(exchange, symbol)
    
    try:
        df = await get_candles_main(ccxt_futures_symbol, timeframe, limit=1000)
        if df is None or len(df) == 0:
            return True
            
        gap_candles = df[df.index >= created_at_utc]
        if len(gap_candles) == 0:
            return True
            
        print(f"⏳ ССинхронізація історії {symbol} {timeframe} за {len(gap_candles)} свічок...")
        
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
                        
                    async with active_signals_lock:
                        remove_active_signal(symbol, timeframe)
                        if signal in active_signals:
                            active_signals.remove(signal)
                        active_monitors.pop((symbol, timeframe), None)
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

# 🟢 ПОВНІСТЮ ЗАМІНИ ТІЛО ФУНКЦІЇ monitor_signal У main.py ЦИМ КОДОМ:

async def monitor_signal(bot, signal, active_signals):
    symbol = signal['symbol']
    direction = signal['direction']
    tps = signal['tps']
    
    # Визначаємо сторону виходу на самому початку функції для захисту від NameError
    exit_side = "sell" if direction == "LONG" else "buy"
    
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

        # --- ЗАПОБІЖНИК САМОЗАВЕРШЕННЯ (Self-Termination Guard) ---
        is_alive = any(s['symbol'] == symbol and s['timeframe'] == signal['timeframe'] for s in active_signals)
        if not is_alive:
            print(f"🛑 [SELF-TERMINATION] Таск для {symbol} ({signal['timeframe']}) самозавершився...")
            break

        try:
            price = await get_price(ccxt_symbol)
        except Exception as e:
            print(f"Помилка отримання ціни {symbol}: {e}")
            continue

        stop_loss = signal.get('stop_loss')
        entry = signal['entry']
        exit_side = "sell" if direction == "LONG" else "buy"

# --- ДИНАМІЧНИЙ ДЕТЕКТОР ДОБОРУ (DOBAR-SENSING) ---
        dobar_filled_state = signal.get('dobar_filled_state', False)
        use_dobar_setting = get_setting('use_dobar')
        if use_dobar_setting is None:
            use_dobar_setting = True

        if not dobar_filled_state and get_setting('trading_enabled') and use_dobar_setting:
            async_ex = None
            try:
                # Використовуємо наш новий асинхронний клієнт
                async_ex = await get_auth_exchange_client()
                ccxt_futures_symbol = resolve_ccxt_futures_symbol(async_ex, symbol)
                symbol_clean = symbol.replace('/', '')
                
                # Перевіряємо фактичний об'єм позиції на біржі
                active_qty = await get_active_position_qty(async_ex, symbol_clean, ccxt_futures_symbol)
                total_expected = float(async_ex.amount_to_precision(ccxt_futures_symbol, signal.get('pos_contracts', 0.0)))
                
                # Якщо об'єм вирівнявся до 100% очікуваного об'єму (усереднення виконано)
                if active_qty >= total_expected * 0.95:
                    print(f"↩️ Виявлено виконання лімітки добору (Dobar) для {symbol}!")
                    
                    # 1. Скасовуємо старі 50%-ві Take-Profit лімітки
                    old_tp_ids = signal.get('tp_order_ids', [])
                    for tp_id in old_tp_ids:
                        try:
                            await async_ex.cancel_order(tp_id, ccxt_futures_symbol)
                        except Exception:
                            pass
                    
                    # 2. Виставляємо нові лімітні тейки на ПОВНИЙ об'єм (100%) з нелінійним розподілом 50/20/15/15
                    new_tp_ids = []
                    percentages = [0.50, 0.20, 0.15, 0.15]
                    accumulated_vol = 0.0
                    
                    for idx, (tp_price, _, _) in enumerate(tps[:4]):
                        current_tp_vol = float(async_ex.amount_to_precision(ccxt_futures_symbol, total_expected * percentages[idx]))
                        if idx == 3:
                            # Останній тейк забирає весь залишок через округлення
                            current_tp_vol = float(async_ex.amount_to_precision(
                                ccxt_futures_symbol, total_expected - accumulated_vol
                            ))
                        
                        if current_tp_vol <= 0:
                            continue
                            
                        accumulated_vol += current_tp_vol
                        tp_price_str = async_ex.price_to_precision(ccxt_futures_symbol, tp_price)
                        print(f"🎯 Оновлення: виставлення повного TP{idx+1} (нелінійний {percentages[idx]*100:.0f}%) на {current_tp_vol} за ціною {tp_price_str}")
                        tp_order = await async_ex.create_order(
                            symbol=ccxt_futures_symbol,
                            type='limit',
                            side=exit_side,
                            amount=current_tp_vol,
                            price=float(tp_price_str),
                            params={'reduceOnly': True}
                        )
                        new_tp_ids.append(tp_order["id"])
                    
                    # 3. Оновлюємо стан сигналу
                    signal['dobar_filled_state'] = True
                    signal['tp_order_ids'] = new_tp_ids
                    save_active_signals(active_signals)
                    
                    # 4. Повідомляємо в Telegram відповіддю під графіком
                    await bot.send_message(
                        chat_id=CHAT_ID,
                        text=(
                            f"↩️ <b>Добір (Dobar) виконано на біржі!</b>\n"
                            f"📦 Позицію збільшено до повного об'єму: <b>{total_expected} {symbol[:-4]}</b>\n"
                            f"🎯 Лімітні Take-Profit ордери автоматично оновлено на повний об'єм (нелінійний розподіл 50/20/15/15)."
                        ),
                        parse_mode='HTML',
                        reply_to_message_id=chart_message_id
                    )
                
            except Exception as dobar_err:
                print(f"Помилка відстеження добору для {symbol}: {dobar_err}")

        # --- КРОК 1: Перевірка Stop Loss ---
        if stop_loss:
            sl_hit = (direction == 'SHORT' and price >= stop_loss) or \
                     (direction == 'LONG' and price <= stop_loss)
            if sl_hit:
                # Повністю видалено віртуальне закриття за ціною! 
                # Закриттям СЛ на 100% керуватиме виключно біржа та наш реконсиліатор в reconciler.py
                pass

        # --- КРОК 2: Перевірка безубитку (БУ) ---
        if breakeven and 0 in hit_tps:
            be_hit = (direction == 'SHORT' and price >= entry) or \
                     (direction == 'LONG' and price <= entry)
            if be_hit:
                # Повністю видалено віртуальне закриття за ціною! 
                # Закриттям БУ на 100% керуватиме виключно біржа та наш реконсиліатор в reconciler.py
                pass

        # --- КРОК 3: Перевірка Take Profit (Гібридне фізичне/віртуальне детектування) ---
        new_hits = set()
        if get_setting('trading_enabled') and signal.get('tp_order_ids'):
            async_ex = None
            try:
                async_ex = await get_auth_exchange_client()
                ccxt_futures_symbol = resolve_ccxt_futures_symbol(async_ex, symbol)
                for idx, tp_order_id in enumerate(signal['tp_order_ids']):
                    if idx in hit_tps:
                        continue
                    try:
                        order_info = await async_ex.fetch_order(tp_order_id, ccxt_futures_symbol)
                        if order_info.get('status') == 'closed':
                            new_hits.add(idx)
                            print(f"🎯 [ФІЗИЧНИЙ ТЕЙК] Біржа підтвердила закриття TP{idx+1} (ID: {tp_order_id}) по {symbol}")
                    except Exception:
                        pass
            except Exception as ex_err:
                print(f"Помилка відстеження статусів ліміток для {symbol}: {ex_err}")

        else:
            for i, (tp_price, _, _) in enumerate(tps):
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

            # Переведення стопу в БУ при першому тейку
            if 0 in hit_tps and not breakeven:
                breakeven = True
                signal['show_dobar'] = False
                
                if use_dobar_setting and dobar_filled_state and signal.get('dobar_low') is not None and signal.get('dobar_high') is not None:
                    dobar_mid = (signal['dobar_low'] + signal['dobar_high']) / 2.0
                    avg_entry = (entry + dobar_mid) / 2.0
                else:
                    avg_entry = entry
                    
                signal['stop_loss'] = avg_entry
                
                # Переведення стопу в БУ на самій біржі (БЕЗ скасування ліміток Take-Profit!)
                if get_setting('trading_enabled'):
                    async_ex = None
                    try:
                        async_ex = await get_auth_exchange_client()
                        ccxt_futures_symbol = resolve_ccxt_futures_symbol(async_ex, symbol)
                        symbol_clean = symbol.replace('/', '')
                        
                        # 1. Скасовуємо ТІЛЬКИ старий Stop-Loss ордер за його Algo ID
                        old_sl_id = signal.get('stop_loss_id')
                        if old_sl_id:
                            try:
                                await async_ex.fapiPrivateDeleteAlgoOrder({
                                    'symbol': symbol_clean,
                                    'algoId': old_sl_id
                                })
                                print(f"🧹 [BREAKEVEN] Скасовано старий Stop-Loss (ID: {old_sl_id}) для {symbol}")
                            except Exception:
                                pass
                        
                        # 2. Виставляємо новий Stop-Loss строго на ПОВНИЙ 100% об'єм контракту з бази даних!
                        total_expected = float(async_ex.amount_to_precision(ccxt_futures_symbol, signal.get('pos_contracts', 0.0)))
                        sl_price_str = async_ex.price_to_precision(ccxt_futures_symbol, avg_entry)
                        
                        sl_params = {
                            'stopPrice': float(sl_price_str),
                            'reduceOnly': True
                        }
                        new_sl_order = await async_ex.create_order(
                            symbol=ccxt_futures_symbol,
                            type='STOP_MARKET',
                            side=exit_side,
                            amount=total_expected, # 100% об'єму!
                            params=sl_params
                        )
                        signal['stop_loss_id'] = new_sl_order["id"]
                        print(f"🔄 [BREAKEVEN] Stop-Loss переведено в безубиток (BE) на об'єм {total_expected} для {symbol}")
                    except Exception as sl_be_err:
                        print(f"❌ Помилка переведення стопу в БУ на біржі для {symbol}: {sl_be_err}")
                else:
                    signal['stop_loss'] = entry
                
                save_active_signals(active_signals)

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
        # === ВПРОВАДЖЕННЯ PORTFOLIO RISK CIRCUIT BREAKERS ===
        portfolio_size = get_setting('portfolio_size') or 1000.0
        trading_blocked_by_circuit_breaker = False
        block_reason = ""
        
        # 1. Запобіжник: Максимальний ліміт використаної маржі (Margin Cap)
        max_margin_pct = get_setting('max_portfolio_margin_pct') or 50.0
        max_allowed_margin_usd = portfolio_size * (max_margin_pct / 100.0)
        
        current_used_margin = sum(float(s.get('margin_required', 0.0)) for s in active_signals)
        if current_used_margin >= max_allowed_margin_usd:
            trading_blocked_by_circuit_breaker = True
            block_reason = f"Використана маржа (${current_used_margin:.2f}) перевищує ліміт (${max_allowed_margin_usd:.2f})"

        # 2. Запобіжник: Денний ліміт збитків (Daily Drawdown Breaker)
        if not trading_blocked_by_circuit_breaker:
            daily_pnl = get_daily_pnl_usd()
            max_daily_loss_pct = get_setting('max_daily_loss_pct') or 3.0
            max_allowed_daily_loss_usd = portfolio_size * (max_daily_loss_pct / 100.0)
            
            if daily_pnl < 0 and abs(daily_pnl) >= max_allowed_daily_loss_usd:
                trading_blocked_by_circuit_breaker = True
                block_reason = f"Добовий збиток (${daily_pnl:.2f}) перевищив ліміт (${max_allowed_daily_loss_usd:.2f})"

        # 3. Запобіжник: Серія збиткових угод поспіль (Consecutive Loss Cooldown)
        if not trading_blocked_by_circuit_breaker:
            losses_limit = get_setting('consecutive_losses_limit') or 3
            consecutive_losses = get_consecutive_losses_count(limit=losses_limit)
            
            if consecutive_losses >= losses_limit:
                last_closed_str = get_last_trade_closed_at()
                if last_closed_str:
                    try:
                        last_closed = pd.to_datetime(last_closed_str)
                        if last_closed.tz is None:
                            last_closed = last_closed.tz_localize('UTC')
                        
                        now_utc = datetime.now(timezone.utc)
                        elapsed_hours = (now_utc - last_closed).total_seconds() / 3600.0
                        cooldown_hours = get_setting('cooldown_hours') or 12
                        
                        if elapsed_hours < cooldown_hours:
                            remaining_hours = cooldown_hours - elapsed_hours
                            trading_blocked_by_circuit_breaker = True
                            block_reason = f"Серія з {consecutive_losses} стопів. Павза діятиме ще {remaining_hours:.1f} год."
                    except Exception as cooldown_err:
                        print(f"Помилка розрахунку часу кулдауну: {cooldown_err}")

        # Логування стану блокування в консоль та лог-файл
        if trading_blocked_by_circuit_breaker:
            print(f"⚠️ [RISK SHIELD] Реальні торги заблоковані: {block_reason}. Бот працює в режимі сканера.")
            if scan_logs is not None:
                scan_logs.append(f"ℹ️ [RISK SHIELD] Торги призупинені: {block_reason}. Увімкнено режим сканера.")
        # ====================================================

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
# --- ІНТЕГРАЦІЯ PORTFOLIO RISK ENGINE (CPF) ---
                # 1. Отримуємо ретерни для цільової монети
                target_returns = await get_returns_for_correlation(signal['symbol'], timeframe="1h", limit=30)
                
                # 2. Отримуємо ретерни для всіх вже відкритих угод
                active_returns_list = []
                for active_sig in active_signals:
                    act_ret = await get_returns_for_correlation(active_sig['symbol'], timeframe="1h", limit=30)
                    if act_ret is not None:
                        active_returns_list.append(act_ret)
                        
                # 3. Обчислюємо Correlation Penalty Factor (CPF)
                from risk_engine import PortfolioRiskEngine
                if target_returns is not None:
                    cpf, avg_corr = PortfolioRiskEngine.calculate_cpf(target_returns, active_returns_list)
                else:
                    cpf, avg_corr = 1.0, 0.0
                    
                # 4. ВИЗНАЧАЄМО ПАРАМЕТРИ РИЗИКУ ДО ВИКЛИКУ МЕТОДУ ОБЧИСЛЕННЯ (Виправлення NameError)
                portfolio_size = get_setting('portfolio_size') or 1000.0
                risk_pct = get_setting('risk_pct') or 1.0
                leverage = get_setting('leverage') or 20
                use_dobar = get_setting('use_dobar')
                if use_dobar is None:
                    use_dobar = True
                    
                # 5. Обчислюємо фінальні параметри ризику з урахуванням Kaufman ER та типу стратегії
                sizing_res = PortfolioRiskEngine.calculate_position_size_v3(
                    portfolio_size=portfolio_size,
                    risk_pct=risk_pct,
                    leverage=leverage,
                    entry=signal['entry'],
                    stop_loss=signal.get('stop_loss'),
                    cpf=cpf,
                    use_dobar=use_dobar,
                    er=signal.get('er', 0.50),  # Передаємо Кількісну трендовість
                    strategy_type=signal.get('strategy_type', 'ema_rsi'), # Передаємо тип стратегії
                    dobar_low=signal.get('dobar_low'),
                    dobar_high=signal.get('dobar_high')
                )
                
                risk_usd = sizing_res["risk_usd"]
                pos_usd = sizing_res["pos_usd"]
                pos_contracts = sizing_res["pos_contracts"]
                margin_required = sizing_res["margin_required"]
                is_averaged = sizing_res["is_averaged"]
                avg_entry = sizing_res["actual_entry"]
                rmf = sizing_res.get("rmf", 1.0) # Отримуємо коефіцієнт RMF

                if cpf < 1.0:
                    print(f"⚠️ [CPF PENALTY] Кореляція {avg_corr:.2f} з портфелем! Зменшено ризик на {(1-cpf)*100:.1f}%. Штрафний CPF: {cpf:.2f}")
                # -----------------------------------------------------
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
                # Додаємо інформацію про масштабування ризику за фазою ринку в Telegram-звіт
                if rmf < 1.0 and rmf > 0:
                    signal_text += f"\n\n⚖️ <b>Ризик-масштаб (RMF):</b> <b>{rmf*100:.0f}%</b> (Знижено об'єм через фазу ринку ER=<i>{signal.get('er', 0.5):.2f}</i>)."

                # Додаємо попередження у Telegram, якщо торгівля призупинена лімітами
                if trading_blocked_by_circuit_breaker:
                    signal_text += f"\n\n🛑 <b>Увага:</b> Ордери на біржі не виставлялись через запобіжник ризику (<i>{block_reason}</i>)."

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

                async with active_signals_lock:
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

                # === НАДСИЛАННЯ ОРДЕРІВ НА БІРЖУ ===
                # Угода відкривається ТІЛЬКИ якщо торгівля увімкнена і НЕ заблокована запобіжниками
                if get_setting('trading_enabled') and not trading_blocked_by_circuit_breaker:
                    testnet_mode = get_setting('testnet_enabled')
                    
                    try:
                        logger_msg = "TESTNET" if testnet_mode else "PROD"
                        print(f"⚡  Надсилання ордерів на {logger_msg} для {symbol_clean}...")
                        
                        trade_report = await global_executor.execute_order_grid(
                            symbol=symbol_clean,
                            direction=signal['direction'],
                            entry_price=signal['entry'],
                            stop_loss=signal.get('stop_loss'),
                            tps=signal['tps'],
                            dobar_low=signal.get('dobar_low'),
                            dobar_high=signal.get('dobar_high'),
                            pos_contracts=pos_contracts,
                            use_dobar=get_setting('use_dobar')
                        )
                        
                        if trade_report["status"] == "success":
                            print(f"✅ Сітка ордерів успішно активована для {symbol_clean}!")
                            # Розраховуємо чисту затримку мережевого виконання (latency в ms)
                            try:
                                signal_created_dt = pd.to_datetime(signal.get('created_at', datetime.now(timezone.utc).isoformat()))
                                exec_dt = pd.to_datetime(trade_report.get('executed_at_ms', time.time() * 1000), unit='ms', utc=True)
                                latency_ms = int((exec_dt - signal_created_dt.tz_convert('UTC')).total_seconds() * 1000.0)
                            except Exception:
                                latency_ms = 0
                                
                            # Записуємо звіт про прослизання та затримку виконання у базу
                            from database import log_order_execution
                            log_order_execution(
                                signal_id=signal['stat_id'],
                                symbol=symbol_clean,
                                order_type='entry_market',
                                side=signal['direction'],
                                requested_price=signal['entry'],
                                executed_price=trade_report.get('entry_fill_price', signal['entry']),
                                executed_qty=trade_report.get('entry_fill_qty', pos_contracts * 0.5 if get_setting('use_dobar') else pos_contracts),
                                fee_paid=trade_report.get('entry_fee', 0.0),
                                latency_ms=latency_ms
                            )

                            signal['tp_order_ids'] = trade_report["take_profit_ids"]
                            signal['dobar_order_id'] = trade_report["entry_dobar_id"]
                            signal['stop_loss_id'] = trade_report["stop_loss_id"]
                            signal['dobar_filled_state'] = False
                            save_active_signals(active_signals)
                            
                            await bot.send_message(
                                chat_id=CHAT_ID,
                                text=(
                                    f"🚀 <b>Позицію успішно відкрито на біржі ({logger_msg})!</b>\n\n"
                                    f"📦 Токен: <b>#{symbol_clean}</b> | {signal['timeframe']}\n"
                                    f"⚖️ Напрямок: <b>{signal['direction']}</b>\n"
                                    f"💵 Об'єм: <b>${pos_usd:.2f}</b> ({pos_contracts:.4f} {symbol_clean[:-4]})\n"
                                    f"🛑 Stop-Loss та Take-Profit ордери виставлені на біржі."
                                ),
                                parse_mode='HTML',
                                reply_to_message_id=sent.message_id
                            )
                        else:
                            print(f"❌ Не вдалося виставити ордери для {symbol_clean}: {trade_report['error']}")
                            
                    except Exception as exec_err:
                        print(f"❌ Критичний збій торгового модуля для {symbol_clean}: {exec_err}")
                # ============================================

                task_key = (symbol_clean, signal['timeframe'])
                if task_key not in active_monitors or active_monitors[task_key].done():
                    task = asyncio.create_task(monitor_signal(bot, signal, active_signals))
                    active_monitors[task_key] = task
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

# Оновлена та збалансована клавіатура у файлі main.py:

def main_reply_keyboard():
    """Створює постійне нижнє Reply-меню для зручної навігації"""
    keyboard = [
        [KeyboardButton("🔍 Сканувати зараз"), KeyboardButton("📊 Статистика")],
        [KeyboardButton("⏳ Активні сигнали"), KeyboardButton("🔄 Звірити з біржею")],
        [KeyboardButton("💵 Стан ринку"), KeyboardButton("⚙️ Налаштування")],
        [KeyboardButton("📈  Аналітика виконання"), KeyboardButton("ℹ️ Про бота")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=True)


async def scheduled_report_loop(bot):
    """
    Фоновий таймер, що автоматично надсилає звіт виконання (Slippage & Latency)
    та аналітику ринкових режимів 2 рази на добу строго о 09:00 та 21:00 за UTC.
    """
    print("📢 Запуск сервісу автоматичної розсилки аналітичних звітів (2 рази на добу)...")
    while True:
        now = datetime.now(timezone.utc)
        
        # Розраховуємо час до наступного автоматичного звіту (09:00 або 21:00 UTC)
        if now.hour < 9:
            next_report = now.replace(hour=9, minute=0, second=0, microsecond=0)
        elif now.hour < 21:
            next_report = now.replace(hour=21, minute=0, second=0, microsecond=0)
        else:
            # Наступний день о 09:00 UTC
            next_report = (now + pd.Timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
            
        sleep_seconds = (next_report - now).total_seconds()
        await asyncio.sleep(sleep_seconds + 5)
        
        try:
            # 1. Надсилаємо глобальний стан ринку
            from scanner import get_market_regime_distribution
            regime_report = await get_market_regime_distribution()
            await bot.send_message(
                chat_id=CHAT_ID,
                text=regime_report,
                parse_mode='HTML'
            )
            
            # 2. Надсилаємо аналітику затримок та прослизання угод
            from database import get_execution_analytics_summary
            report_text = get_execution_analytics_summary()
            await bot.send_message(
                chat_id=CHAT_ID,
                text=f"📢 <b>АВТОМАТИЧНИЙ ДОБОВИЙ ЗВІТ ВИКОНАННЯ</b>\n\n{report_text}",
                parse_mode='HTML'
            )
            print("📢 Автоматичний аналітичний звіт успішно надіслано.")
        except Exception as e:
            print(f"Помилка автоматичної розсилки звітів: {e}")

async def handle_updates(bot, active_signals):
    global active_timeframe, exchange, global_executor, active_monitors, async_exchange
    offset = None

    while True:
        try:
            updates = await bot.get_updates(offset=offset, timeout=10)
            for update in updates:
                offset = update.update_id + 1

                # 1. ОБРОБКА ТЕКСТОВИХ ПОВІДОМЛЕНЬ ТА КНОПОК МЕНЮ
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
                            text="🤖 <b>Сигнальний бот успішно запущено!</b>\nВикористовуйте зручне меню кнопок внизу екрана для керування.",
                            reply_markup=main_reply_keyboard(),
                            parse_mode='HTML'
                        )

                    elif text == '📊 Статистика' or text == '/stats':
                        summary = get_stats_summary()
                        await bot.send_message(chat_id=chat_id, text=summary, parse_mode='HTML')
                        
                        # Додаємо автоматичний вивід телеметрії відхилень разом із фінансовим звітом!
                        rejected_summary = get_rejected_stats_summary()
                        await bot.send_message(chat_id=chat_id, text=rejected_summary, parse_mode='HTML')

                    elif text == '📈  Аналітика виконання':
                        from database import get_execution_analytics_summary
                        report = get_execution_analytics_summary()
                        await bot.send_message(chat_id=chat_id, text=report, parse_mode='HTML')

                    elif text == '💵 Стан ринку' or text == '/regime':
                        await bot.send_message(
                            chat_id=chat_id, 
                            text="⏳ Розраховую поточні фази ринку для BTC та 37 активів. Будь ласка, зачекайте..."
                        )
                        try:
                            from scanner import get_market_regime_distribution
                            report = await get_market_regime_distribution()
                            await bot.send_message(chat_id=chat_id, text=report, parse_mode='HTML')
                        except Exception as e:
                            print(f"Помилка ручного запиту стану ринку: {e}")
                            await bot.send_message(chat_id=chat_id, text="❌ Не вдалося отримати звіт стану ринку.")

                    elif text == '🔍 Сканувати зараз' or text == '/scan':
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

                    # 1. ОБРОБКА КНОПКИ "⏳ Активні сигнали" (Швидкий показ з локальної БД)
                    elif text == '⏳ Активні сигнали' or text == '/active':
                        if not active_signals:
                            await bot.send_message(chat_id=chat_id, text="⏳ Активних сигналів немає в локальній базі бота.")
                            continue
                        
                        lines = ["⏳ <b>АКТИВНІ СИГНАЛИ В БАЗІ БОТА:</b>\n"]
                        for s in active_signals:
                            lines.append(
                                f"{s.get('tier','🟢')} <b>#{s['symbol']}</b> "
                                f"({s['timeframe']}) | <b>{s['direction']}</b>\n"
                                f"  💵 Вхід (БД): <b>{s['entry']}</b> | Stop-Loss: <b>{s.get('stop_loss')}</b>"
                            )
                        msg = "\n".join(lines)
                        await bot.send_message(chat_id=chat_id, text=msg, parse_mode='HTML')


                    # 2. ОБРОБКА КНОПКИ "🔄 Звірити з біржею" (Глибока звірка та PnL)
                    elif text == '🔄 Звірити з біржею' or text == '/sync':
                        await bot.send_message(chat_id=chat_id, text="🔄 Запущено примусову звірку позицій з біржею та коригування Stop-Loss...")
                        
                        # Викликаємо твою нову логіку реконсиліатора
                        try:
                            if 'reconciler' in globals() and reconciler is not None:
                                await reconciler.reconcile()
                        except Exception as rec_err:
                            print(f"Помилка примусової реконсиліації: {rec_err}")
                            
                        await asyncio.sleep(1.0) # Даємо базі оновитися
                        
                        # Виводимо інформацію про реальні позиції та плаваючий PnL
                        async_ex = None
                        try:
                            async_ex = await get_auth_exchange_client()
                            positions_data = await async_ex.fetch_positions()
                            exchange_positions = {}
                            for pos in positions_data:
                                contracts = abs(float(pos.get('contracts', 0.0)))
                                if contracts > 0:
                                    symbol_clean = pos.get('symbol', '').replace('/', '').split(':')[0]
                                    exchange_positions[symbol_clean] = {
                                        'contracts': contracts,
                                        'entryPrice': float(pos.get('entryPrice', 0.0)),
                                        'currentPrice': float(pos.get('markPrice', 0.0)),
                                        'unrealizedPnl': float(pos.get('unrealizedPnl', 0.0)),
                                        'side': pos.get('side', '').upper(),
                                        'percentage': float(pos.get('percentage', 0.0))
                                    }
                            
                            if not exchange_positions:
                                await bot.send_message(chat_id=chat_id, text="✅ <b>На біржі немає відкритих позицій.</b>", parse_mode='HTML')
                                continue
                                
                            lines = ["⏳ <b>АКТУАЛЬНИЙ СТАН ПОЗИЦІЙ НА БІРЖІ (LIVE PnL):</b>\n"]
                            for s in active_signals:
                                symbol = s['symbol']
                                symbol_clean = symbol.replace('/', '')
                                timeframe = s['timeframe']
                                direction = s['direction']
                                entry = s['entry']
                                tier = s.get('tier', '🟢')
                                
                                ex_pos = exchange_positions.get(symbol_clean)
                                if ex_pos:
                                    pnl_usd = ex_pos['unrealizedPnl']
                                    pnl_pct = ex_pos['percentage']
                                    cur_price = ex_pos['currentPrice']
                                    pnl_emoji = "🟢" if pnl_usd >= 0 else "🔴"
                                    sign = "+" if pnl_usd >= 0 else ""
                                    
                                    lines.append(
                                        f"{tier} <b>#{symbol_clean}</b> ({timeframe}) | <b>{direction}</b>\n"
                                        f"  💵 Вхід (БД): <b>{entry}</b> | Біржа: <b>{ex_pos['entryPrice']:.4f}</b>\n"
                                        f"  📈 Поточна ціна: <b>{cur_price:.4f}</b> | Об'єм: <b>{ex_pos['contracts']} {symbol_clean[:-4]}</b>\n"
                                        f"  {pnl_emoji} <b>Unrealized PnL: {sign}${pnl_usd:.2f} ({sign}{pnl_pct:.2f}%)</b>\n"
                                    )
                                else:
                                    lines.append(
                                        f"{tier} <b>#{symbol_clean}</b> ({timeframe}) | <b>{direction}</b>\n"
                                        f"  💵 Вхід (БД): <b>{entry}</b>\n"
                                        f"  ℹ️ <i>Віртуальний моніторинг (немає активної позиції на біржі)</i>\n"
                                    )
                            
                            await bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode='HTML')
                            
                        except Exception as e:
                            print(f"Помилка виведення позицій з біржі: {e}")
                            await bot.send_message(chat_id=chat_id, text="❌ Помилка зчитування інформації про позиції з біржі.")

                    elif text == '⚙️ Налаштування' or text == '/settings':
                        await bot.send_message(
                            chat_id=chat_id,
                            text=get_settings_text(),
                            reply_markup=main_settings_keyboard(),
                            parse_mode='HTML'
                        )

                    elif text == 'ℹ️ Про бота' or text == '/info':
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

                # 2. ОБРОБКА CALLBACK QUERY (КНОПКИ НА ПОВІДОМЛЕННЯХ)
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
                            text=f"✅  Таймфрейм змінено на: {tf}\n⏳ Запускаю сканування...",
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
                            print(f"Помилка закриття старої сесії сканера: {e}")
                        
                        exchange = get_exchange_client(async_mode=False)
                        scanner.exchange = get_exchange_client(async_mode=True)
                        
                        # Singleton при зміні біржі (Рядок global async_exchange прибрано)
                        if async_exchange:
                            try:
                                await async_exchange.close()
                            except Exception:
                                pass
                            async_exchange = None

                        # Оновлюємо глобальний ексекутор (Рядок global прибрано)
                        if global_executor:
                            try:
                                await global_executor.close()
                            except Exception:
                                pass
                        global_executor = FuturesExecutor(exchange_id=new_exchange, testnet=get_setting('testnet_enabled'))
                        await global_executor.initialize()
                        
                        print(f"🏛 Біржу успішно перемикнуто на: {new_exchange.upper()}. Singleton CCXT оновлено.")
                        
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

                    elif data == 'toggle_trading':
                        current = get_setting('trading_enabled')
                        if current is None:
                            current = True
                        set_setting('trading_enabled', not current)
                        text, markup = risk_keyboard()
                        await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, parse_mode='HTML')

                    elif data == 'toggle_testnet':
                        current = get_setting('testnet_enabled')
                        if current is None:
                            current = True
                        new_testnet = not current
                        set_setting('testnet_enabled', new_testnet)

                        # Singleton при зміні режиму торгівлі (Рядок global async_exchange прибрано)
                        if async_exchange:
                            try:
                                await async_exchange.close()
                            except Exception:
                                pass
                            async_exchange = None

                        # Оновлюємо глобальний ексекутор (Рядок global прибрано)
                        if global_executor:
                            try:
                                await global_executor.close()
                            except Exception:
                                pass
                        exchange_name = get_setting('exchange_name') or 'binance'
                        global_executor = FuturesExecutor(exchange_id=exchange_name, testnet=new_testnet)
                        await global_executor.initialize()

                        mode_label = "TESTNET / DEMO" if new_testnet else "⚠️ РЕАЛЬНІ ТОРГІВ"
                        print(f"🔄 Режим торгів змінено на: {mode_label}. Singleton CCXT перезапущено.")
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
                        await bot.send_message(
                            chat_id=chat_id, 
                            text="⚠️ <b>[HARD KILL-SWITCH] Запуск екстреної ліквідації...</b> Скасовую всі ордери та закрию позиції по ринку на біржі.", 
                            parse_mode='HTML'
                        )
                        
                        async_ex = None
                        closed_symbols = []
                        try:
                            async_ex = await get_auth_exchange_client()
                            
                            # 1. Отримуємо всі відкриті позиції на біржі прямо зараз
                            positions_data = await async_ex.fetch_positions()
                            
                            for pos in positions_data:
                                contracts = abs(float(pos.get('contracts', 0.0)))
                                if contracts > 0:
                                    symbol_ccxt = pos.get('symbol')
                                    symbol_clean = symbol_ccxt.replace('/', '').split(':')[0]
                                    
                                    # Скасовуємо абсолютно всю сітку ордерів (лімітки, Dobar, умовні стопи)
                                    await cancel_all_exchange_orders_for_symbol(async_ex, symbol_clean, symbol_ccxt)
                                    await asyncio.sleep(1.0)
                                    
                                    # Надсилаємо аварійний ринковий ордер на закриття (reduceOnly)
                                    close_side = "sell" if pos.get('side', '').upper() == "LONG" else "buy"
                                    print(f"🚨 [KILL-SWITCH] Екстрене маркет-закриття {symbol_ccxt}, об'єм: {contracts}")
                                    await async_ex.create_order(
                                        symbol=symbol_ccxt,
                                        type='market',
                                        side=close_side,
                                        amount=contracts,
                                        params={'reduceOnly': True}
                                    )
                                    closed_symbols.append(symbol_clean)
                                    
                        except Exception as kill_err:
                            print(f"Помилка під час роботи Hard Kill-Switch на біржі: {kill_err}")
                            await bot.send_message(
                                chat_id=chat_id, 
                                text=f"❌ Помилка екстреного закриття на біржі: <i>{kill_err}</i>", 
                                parse_mode='HTML'
                            )
                            
                        # 2. Очищуємо базу даних та вбиваємо всі запущені асинхронні таски моніторингу
                        clear_active_signals()
                        
                        for task_key, task in list(active_monitors.items()):
                            if task and not task.done():
                                task.cancel()
                        active_monitors.clear()
                        active_signals.clear()
                        
                        # Складаємо фінальний звіт про екстрене вимкнення
                        closed_list_str = ", ".join([f"#{s}" for s in closed_symbols]) if closed_symbols else "відсутні"
                        await bot.send_message(
                            chat_id=chat_id,
                            text=f"🔥 <b>[HARD KILL-SWITCH] АВАРІЙНУ ЗУПИНКУ ВИКОНАНО!</b>\n\n"
                                 f"🚫 <b>Закриті позиції по ринку:</b> {closed_list_str}\n"
                                 f"🧹 <b>Сітка ордерів:</b> Повністю зачищена (лімітки, Dobar, стопи скасовані).\n"
                                 f"🗑️ <b>База даних:</b> Повністю очищена.\n"
                                 f"💤 <b>Процеси:</b> Усі фонові таски моніторингу успішно видалені з оперативної пам'яті.",
                            parse_mode='HTML'
                        )
                        
                        # ПРИМУСОВО ВБИВАЄМО ВСІ АКТИВНІ ТАСКИ АСИНХРОННОСТІ (Запобігання зацикленню)
                        for task_key, task in list(active_monitors.items()):
                            if task and not task.done():
                                task.cancel()
                        active_monitors.clear()
                        active_signals.clear()
                        
                        await bot.send_message(
                            chat_id=chat_id, 
                            text="✅ Всі активні сигнали закрито в базі, а їхні фонові таски успішно видалені з пам'яті!"
                        )

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
    global active_timeframe, global_executor, exchange, recently_sent
    init_db()
    bot = Bot(token=BOT_TOKEN, request=request)

    try:
        # Попередньо завантажуємо ринки для синхронного клієнта малювання графіків
        await asyncio.to_thread(exchange.load_markets)
    except Exception as e:
        print(f"Попередження завантаження ринків sync клієнта: {e}")

    active_signals = load_active_signals()
    print(f"Завантажено {len(active_signals)} active сигналів з диску")

    # Відновлюємо recently_sent з активних сигналів при старті
    recently_sent = {s['symbol'] for s in active_signals}
    print(f"Синхронізація recently_sent при старті: {recently_sent}")

    # Ініціалізуємо персистентного глобального виконавця
    testnet_mode = get_setting('testnet_enabled')
    exchange_name = get_setting('exchange_name') or 'binance'
    global_executor = FuturesExecutor(exchange_id=exchange_name, testnet=testnet_mode)
    await global_executor.initialize()

    # === ФАЗА Б: ЗАПУСК ПРИМИРЕННЯ ТА ЛІКУВАННЯ ПОЗИЦІЙ ПРИ СТАРТІ ===
    await reconcile_active_signals_state(bot, active_signals)
    # ================================================================

    # --- ІНІЦІАЛІЗАЦІЯ BACKGROUND RECONCILIATION WORKER ---
    from reconciler import ReconciliationWorker
    reconciler = ReconciliationWorker(
        bot=bot,
        chat_id=CHAT_ID,
        get_auth_exchange_client_fn=get_auth_exchange_client,
        resolve_ccxt_futures_symbol_fn=resolve_ccxt_futures_symbol,
        get_active_position_qty_fn=get_active_position_qty,
        cancel_all_exchange_orders_for_symbol_fn=cancel_all_exchange_orders_for_symbol,
        active_signals_ref=active_signals,
        active_signals_lock=active_signals_lock,
        active_monitors=active_monitors,
        interval_seconds=120  # Звіряти стан кожні 2 хвилини
    )
    await reconciler.start()
    # ------------------------------------------------------

    for signal in list(active_signals):
        if signal.get('chart_message_id'):
            is_still_active = await reconcile_historical_gap(bot, signal, active_signals)
            
            if is_still_active:
                # Захищений запуск моніторингу унікальним таском
                task_key = (signal['symbol'], signal['timeframe'])
                if task_key not in active_monitors or active_monitors[task_key].done():
                    task = asyncio.create_task(monitor_signal(bot, signal, active_signals))
                    active_monitors[task_key] = task
                    print(f"🔄 Відновлено живий моніторинг: {signal['symbol']} {signal['timeframe']}")
                else:
                    print(f"⚠️ Моніторинг для {signal['symbol']} вже активний, пропуск дублюючого таска.")

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
        scheduled_report_loop(bot)  # Автоматичний звіт 2 рази на добу
    )

# Безпечно закриваємо ресурси Singleton сесії перед виходом
    global async_exchange
    if async_exchange:
        await async_exchange.close()
        print("🏛 Глобальну Singleton-сесію CCXT успішно закрито.")

async def safe_main():
    try:
        await main()
    except Exception as e:
        print(f"КРИТИЧНА ПОМИЛКА: {e}")
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    asyncio.run(safe_main())