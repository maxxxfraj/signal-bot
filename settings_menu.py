# settings_menu.py
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from settings import get_setting, set_setting, ALL_PAIRS

ALL_TIMEFRAMES = ['5m', '15m', '30m', '1h', '4h', '1d']


def main_settings_keyboard():
    """Головне меню налаштувань"""
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
    """Меню ризик-менеджменту з інтеграцією автоматичної торгівлі та тестнету"""
    stop = get_setting('stop_atr_mult')
    tp1 = get_setting('tp1_atr_mult')
    max_sig = get_setting('max_active_signals')
    
    portfolio_size = get_setting('portfolio_size') or 1000.0
    risk_pct = get_setting('risk_pct') or 1.0
    risk_usd = portfolio_size * (risk_pct / 100.0)
    
    leverage = get_setting('leverage') or 20
    use_dobar = get_setting('use_dobar')
    if use_dobar is None:
        use_dobar = True

    # Нові параметри автоматизації торгівлі
    testnet = get_setting('testnet_enabled')
    if testnet is None:
        testnet = True
    trading = get_setting('trading_enabled')
    if trading is None:
        trading = True

    text = (
        f"🎯 Ризик-менеджмент\n\n"
        f"🛑 Стоп: ATR × {stop}\n"
        f"🎯 TP1: ATR × {tp1}\n"
        f"📊 Макс. активних сигналів: {max_sig}\n"
        f"💰 Депозит: <b>${portfolio_size:.0f}</b>\n"
        f"💸 Ризик на угоду: <b>{risk_pct:.1f}%</b>\n"
        f"💵 Сума під ризиком: <b>${risk_usd:.2f}</b>\n"
        f"⚡ Кредитне плече: <b>{leverage}x</b>\n"
        f"↩️ Усереднення (Добір): <b>{'УВІМКНЕНО ✅' if use_dobar else 'ВИМКНЕНО ⬜'}</b>\n\n"
        f"🛒 Авто-торгівля: <b>{'УВІМКНЕНО ✅' if trading else 'ВИМКНЕНО ⬜'}</b>\n"
        f"🧪 Режим торгівлі: <b>{'TESTNET (Demo) 🧪' if testnet else 'PROD (Реал) ⚠️'}</b>"
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
        [
            InlineKeyboardButton("Плече −", callback_data="risk_lev_down"),
            InlineKeyboardButton(f"{leverage}x", callback_data="risk_lev_info"),
            InlineKeyboardButton("Плече +", callback_data="risk_lev_up"),
        ],
        [
            InlineKeyboardButton(
                f"{'🟢' if use_dobar else '⬜'} Усереднення (Добір)",
                callback_data="toggle_dobar"
            )
        ],
        [
            InlineKeyboardButton(
                f"{'🛒' if trading else '⬜'} Торгівля: {'Увімк.' if trading else 'Вимк.'}",
                callback_data="toggle_trading"
            ),
            InlineKeyboardButton(
                f"{'🧪' if testnet else '⚠️'} Режим: {'TESTNET' if testnet else 'PROD'}",
                callback_data="toggle_testnet"
            )
        ],
        [InlineKeyboardButton("◀️ Назад", callback_data="cfg_back")],
    ]
    return text, InlineKeyboardMarkup(keyboard)


def filters_keyboard():
    """Меню фільтрів стратегій"""
    htf = get_setting('htf_bias_enabled')
    min_prob = get_setting('min_tp1_prob')
    htf_thresh = get_setting('htf_diff_threshold')
    
    btc_filt = get_setting('btc_filter_enabled')
    if btc_filt is None:
        btc_filt = True
    regime_filt = get_setting('regime_filter_enabled')
    if regime_filt is None:
        regime_filt = True
        
    funding_filt = get_setting('funding_filter_enabled')
    if funding_filt is None:
        funding_filt = True
    funding_max = get_setting('funding_max_limit') or 0.05
    
    oi_filt = get_setting('oi_filter_enabled')
    if oi_filt is None:
        oi_filt = True
    oi_min = get_setting('oi_min_limit') or 10.0
    
    scalp_mode = get_setting('scalper_mode_enabled')
    if scalp_mode is None:
        scalp_mode = True

    text = (
        f"🔍 Фільтри стратегій\n\n"
        f"⚡️ Скальперський режим (15m-1h): <b>{'УВІМКНЕНO ✅' if scalp_mode else 'ВИМКНЕНО ⬜'}</b>\n"
        f"🪙 Фільтр BTC (BTC Trend): <b>{'УВІМКНЕНO ✅' if btc_filt else 'ВИМКНЕНО ⬜'}</b>\n"
        f"📊 Режим ринку (ADX): <b>{'УВІМКНЕНO ✅' if regime_filt else 'ВИМКНЕНО ⬜'}</b>\n"
        f"💵 Фільтр Фандингу: <b>{'УВІВМКНЕНO ✅' if funding_filt else 'ВИМКНЕНО ⬜'}</b>\n"
        f"🌡 Макс. Фандинг ліміт: <b>{funding_max:.3f}%</b>\n"
        f"📈 Фільтр мін. OI: <b>{'УВІМКНЕНO ✅' if oi_filt else 'ВИМКНЕНО ⬜'}</b>\n"
        f"📉 Мін. OI ліміт: <b>${oi_min:.1f}M</b>\n"
        f"⏱ HTF bias фільтр: <b>{'УВІМКНЕНO ✅' if htf else 'ВИМКНЕНО ⬜'}</b>\n"
        f"🔢 Мін. ймовірність TP1: <b>{min_prob}%</b>\n"
        f"📐 HTF поріг (EMA різниця): <b>{htf_thresh}%</b>\n\n"
        f"Використовуй ➖/➕ або кнопки-перемикачі нижче:"
    )
    keyboard = [
        [InlineKeyboardButton(f"{'⚡️' if scalp_mode else '⬜'} Скальперський режим (15m-1h)", callback_data="toggle_scalp_mode")],
        [InlineKeyboardButton(f"{'✅' if btc_filt else '⬜'} Фільтр Біткоїна", callback_data="toggle_btc_filter")],
        [InlineKeyboardButton(f"{'✅' if regime_filt else '⬜'} Класифікатор ринку", callback_data="toggle_regime_filter")],
        [InlineKeyboardButton(f"{'✅' if funding_filt else '⬜'} Фільтр Фандингу", callback_data="toggle_funding_filter")],
        [
            InlineKeyboardButton("Фандинг −", callback_data="filter_funding_down"),
            InlineKeyboardButton(f"{funding_max:.3f}%", callback_data="filter_funding_info"),
            InlineKeyboardButton("Фандинг +", callback_data="filter_funding_up"),
        ],
        [InlineKeyboardButton(f"{'✅' if oi_filt else '⬜'} Фільтр мінімального OI", callback_data="toggle_oi_filter")],
        [
            InlineKeyboardButton("OI −", callback_data="filter_oi_down"),
            InlineKeyboardButton(f"${oi_min:.1f}M", callback_data="filter_oi_info"),
            InlineKeyboardButton("OI +", callback_data="filter_oi_up"),
        ],
        [InlineKeyboardButton(f"{'✅' if htf else '⬜'} HTF bias фільтр", callback_data="toggle_htf")],
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
    leverage = get_setting('leverage') or 20
    use_dobar = get_setting('use_dobar')
    if use_dobar is None:
        use_dobar = True
        
    btc_filt = get_setting('btc_filter_enabled')
    if btc_filt is None:
        btc_filt = True
    regime_filt = get_setting('regime_filter_enabled')
    if regime_filt is None:
        regime_filt = True
        
    funding_filt = get_setting('funding_filter_enabled')
    if funding_filt is None:
        funding_filt = True
    funding_max = get_setting('funding_max_limit') or 0.05
    
    oi_filt = get_setting('oi_filter_enabled')
    if oi_filt is None:
        oi_filt = True
    oi_min = get_setting('oi_min_limit') or 10.0
    
    scalp_mode = get_setting('scalper_mode_enabled')
    if scalp_mode is None:
        scalp_mode = True
        
    exchange_name = get_setting('exchange_name') or 'binance'

    # Додано для інформаційного виводу
    testnet = get_setting('testnet_enabled')
    trading = get_setting('trading_enabled')

    return (
        f"⚙️ Налаштування бота\n\n"
        f"🏛 Активна біржа: <b>{exchange_name.upper()}</b>\n"
        f"📋 Пари: {len(watchlist)} active\n"
        f"⏱ Таймфрейми: {', '.join(tfs)}\n"
        f"📍 Стоп: ATR × {stop}\n"
        f"🎯 TP1: ATR × {tp1}\n"
        f"🔢 Мін. TP1 prob: {min_prob}%\n"
        f"🔍 HTF фільтр: {'увімк.' if htf else 'вимк.'}\n"
        f"🪙 Фільтр BTC: {'увімк.' if btc_filt else 'вимк.'}\n"
        f"📊 Режим ринку: {'увімк.' if regime_filt else 'вимк.'}\n"
        f"⚡️ Скальперський режим: <b>{'увімк.' if scalp_mode else 'вимк.'}</b>\n"
        f"💵 Фільтр Фандингу: {'увімк.' if funding_filt else 'вимк.'} (ліміт {funding_max:.3f}%)\n"
        f"📈 Фільтр мін. OI: {'увімк.' if oi_filt else 'вимк.'} (ліміт ${oi_min:.1f}M)\n"
        f"🛒 Авто-торгівля: <b>{'УВІМКНЕНO' if trading else 'ВИМКНЕНО'}</b>\n"
        f"🧪 Режим торгівлі: <b>{'TESTNET' if testnet else 'REAL-PROD'}</b>\n\n"
        f"💰 Депозит: ${portfolio_size:.0f} (Ризик {risk_pct:.1f}% | {leverage}x | {'Добір' if use_dobar else 'Без добору'})"
    )