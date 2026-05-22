from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from settings import get_setting, set_setting

ALL_PAIRS = [
    'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT',
    'XRP/USDT', 'DOGE/USDT', 'ADA/USDT', 'AVAX/USDT',
    'DOT/USDT', 'POL/USDT', 'LINK/USDT', 'UNI/USDT',
    'ATOM/USDT', 'LTC/USDT', 'ETC/USDT', 'FIL/USDT',
]

ALL_TIMEFRAMES = ['5m', '15m', '30m', '1h', '4h', '1d']


def main_settings_keyboard():
    """Головне меню налаштувань (додано перемикач біржі)"""
    exchange_name = get_setting('exchange_name') or 'binance'
    
    keyboard = [
        [InlineKeyboardButton(f"🏛 Біржа: {exchange_name.upper()} 🔄", callback_data="toggle_exchange")],
        [InlineKeyboardButton("📋 Пари (watchlist)", callback_data="cfg_pairs")],
        [InlineKeyboardButton("⏱ Таймфрейми", callback_data="cfg_timeframes")],
        [InlineKeyboardButton("🎯 Ризик-менеджмент", callback_data="cfg_risk")],
        [InlineKeyboardButton("🔍 Фільтри стратегій", callback_data="cfg_filters")],
        [InlineKeyboardButton("❌ Закрити", callback_data="cfg_close")],
    ]
    return InlineKeyboardMarkup(keyboard)


def pairs_keyboard():
    """Меню вибору пар"""
    watchlist = get_setting('watchlist')
    keyboard = []
    row = []
    for i, pair in enumerate(ALL_PAIRS):
        symbol = pair.replace('/', '')
        is_active = pair in watchlist
        label = f"✅ {symbol}" if is_active else f"⬜ {symbol}"
        row.append(InlineKeyboardButton(label, callback_data=f"toggle_pair_{pair}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="cfg_back")])
    return InlineKeyboardMarkup(keyboard)


def timeframes_keyboard():
    """Меню вибору таймфреймів"""
    active_tfs = get_setting('active_timeframes')
    keyboard = []
    row = []
    for i, tf in enumerate(ALL_TIMEFRAMES):
        is_active = tf in active_tfs
        label = f"✅ {tf}" if is_active else f"⬜ {tf}"
        row.append(InlineKeyboardButton(label, callback_data=f"toggle_tf_{tf}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="cfg_back")])
    return InlineKeyboardMarkup(keyboard)


def risk_keyboard():
    """Меню ризик-менеджменту (додано Депозит та % Ризику)"""
    stop = get_setting('stop_atr_mult')
    tp1 = get_setting('tp1_atr_mult')
    max_sig = get_setting('max_active_signals')
    
    portfolio_size = get_setting('portfolio_size') or 1000.0
    risk_pct = get_setting('risk_pct') or 1.0
    risk_usd = portfolio_size * (risk_pct / 100.0)

    text = (
        f"🎯 Ризик-менеджмент\n\n"
        f"🛑 Стоп: ATR × {stop}\n"
        f"🎯 TP1: ATR × {tp1}\n"
        f"📊 Макс. активних сигналів: {max_sig}\n"
        f"💰 Депозит: <b>${portfolio_size:.0f}</b>\n"
        f"💸 Ризик на угоду: <b>{risk_pct:.1f}%</b>\n"
        f"💵 Сума під ризиком: <b>${risk_usd:.2f}</b>\n"
    )
    keyboard = [
        [
            InlineKeyboardButton("Стоп −", callback_data="risk_stop_down"),
            InlineKeyboardButton(f"ATR×{stop}", callback_data="risk_stop_info"),
            InlineKeyboardButton("Стоп +", callback_data="risk_stop_up"),
        ],
        [
            InlineKeyboardButton("TP1 −", callback_data="risk_tp1_down"),
            InlineKeyboardButton(f"ATR×{tp1}", callback_data="risk_tp1_info"),
            InlineKeyboardButton("TP1 +", callback_data="risk_tp1_up"),
        ],
        [
            InlineKeyboardButton("Макс −", callback_data="risk_max_down"),
            InlineKeyboardButton(f"Макс {max_sig}", callback_data="risk_max_info"),
            InlineKeyboardButton("Макс +", callback_data="risk_max_up"),
        ],
        [
            InlineKeyboardButton("Депо −", callback_data="risk_depo_down"),
            InlineKeyboardButton(f"${portfolio_size:.0f}", callback_data="risk_depo_info"),
            InlineKeyboardButton("Депо +", callback_data="risk_depo_up"),
        ],
        [
            InlineKeyboardButton("Ризик −", callback_data="risk_pct_down"),
            InlineKeyboardButton(f"{risk_pct:.1f}%", callback_data="risk_pct_info"),
            InlineKeyboardButton("Ризик +", callback_data="risk_pct_up"),
        ],
        [InlineKeyboardButton("◀️ Назад", callback_data="cfg_back")],
    ]
    return text, InlineKeyboardMarkup(keyboard)


def filters_keyboard():
    """Меню фільтрів стратегій (повертає і текст, і клавіатуру)"""
    htf = get_setting('htf_bias_enabled')
    min_prob = get_setting('min_tp1_prob')
    htf_thresh = get_setting('htf_diff_threshold')

    text = (
        f"🔍 Фільтри стратегій\n\n"
        f"HTF bias фільтр: {'увімк.' if htf else 'вимк.'}\n"
        f"Мін. ймовірність TP1: <b>{min_prob}%</b>\n"
        f"HTF поріг (різниця EMA): <b>{htf_thresh}%</b>\n\n"
        f"Використовуй ➖/➕ для зміни"
    )
    keyboard = [
        [InlineKeyboardButton(
            f"{'✅' if htf else '⬜'} HTF bias фільтр",
            callback_data="toggle_htf"
        )],
        [
            InlineKeyboardButton("Мін. TP1% −", callback_data="filter_prob_down"),
            InlineKeyboardButton(f"TP1 ≥ {min_prob}%", callback_data="filter_prob_info"),
            InlineKeyboardButton("Мін. TP1% +", callback_data="filter_prob_up"),
        ],
        [
            InlineKeyboardButton("HTF поріг −", callback_data="filter_htf_down"),
            InlineKeyboardButton(f"HTF {htf_thresh}%", callback_data="filter_htf_info"),
            InlineKeyboardButton("HTF поріг +", callback_data="filter_htf_up"),
        ],
        [InlineKeyboardButton("◀️ Назад", callback_data="cfg_back")],
    ]
    return text, InlineKeyboardMarkup(keyboard)


def get_settings_text():
    """Текст головного меню налаштувань"""
    watchlist = get_setting('watchlist')
    tfs = get_setting('active_timeframes')
    stop = get_setting('stop_atr_mult')
    tp1 = get_setting('tp1_atr_mult')
    min_prob = get_setting('min_tp1_prob')
    htf = get_setting('htf_bias_enabled')
    max_sig = get_setting('max_active_signals')
    
    portfolio_size = get_setting('portfolio_size') or 1000.0
    risk_pct = get_setting('risk_pct') or 1.0
    exchange_name = get_setting('exchange_name') or 'binance'

    return (
        f"⚙️ Налаштування бота\n\n"
        f"🏛 Активна біржа: <b>{exchange_name.upper()}</b>\n"
        f"📋 Пари: {len(watchlist)} активних\n"
        f"⏱ Таймфрейми: {', '.join(tfs)}\n"
        f"📍 Стоп: ATR × {stop}\n"
        f"🎯 TP1: ATR × {tp1}\n"
        f"🔢 Мін. TP1 prob: {min_prob}%\n"
        f"🔍 HTF фільтр: {'увімк.' if htf else 'вимк.'}\n"
        f"📊 Макс. сигналів: {max_sig}\n"
        f"💰 Депозит: ${portfolio_size:.0f} (Ризик {risk_pct:.1f}%)"
    )