# reconciler.py
import asyncio
import logging
from database import remove_active_signal, get_connection, close_signal_stat
from settings import get_setting

logger = logging.getLogger("TradingBot.Reconciler")

class ReconciliationWorker:
    def __init__(
        self,
        bot,
        chat_id: str,
        get_auth_exchange_client_fn,
        resolve_ccxt_futures_symbol_fn,
        get_active_position_qty_fn,
        cancel_all_exchange_orders_for_symbol_fn,
        active_signals_ref: list,
        active_signals_lock: asyncio.Lock,
        active_monitors: dict,
        interval_seconds: int = 120
    ):
        self.bot = bot
        self.chat_id = chat_id
        self.get_auth_exchange_client = get_auth_exchange_client_fn
        self.resolve_ccxt_futures_symbol = resolve_ccxt_futures_symbol_fn
        self.get_active_position_qty = get_active_position_qty_fn
        self.cancel_all_exchange_orders_for_symbol = cancel_all_exchange_orders_for_symbol_fn
        self.active_signals = active_signals_ref
        self.active_signals_lock = active_signals_lock
        self.active_monitors = active_monitors
        self.interval = interval_seconds
        self.is_running = False

    async def start(self):
        self.is_running = True
        asyncio.create_task(self._loop())
        logger.info("✅ Асинхронний ReconciliationWorker успішно запущено.")

    async def stop(self):
        self.is_running = False

    async def _loop(self):
        await asyncio.sleep(15)
        while self.is_running:
            try:
                await self.reconcile()
            except Exception as e:
                logger.error(f"Помилка під час примирення станів: {e}", exc_info=True)
            await asyncio.sleep(self.interval)

    async def reconcile(self):
        async_ex = None
        try:
            async_ex = await self.get_auth_exchange_client()
            
            # 1. Завантажуємо реальні відкриті позиції на біржі
            positions_data = await async_ex.fetch_positions()
            exchange_positions = {}
            for pos in positions_data:
                # КАСКАДНИЙ ПАРСЕР ОБ'ЄМУ ДЛЯ ПОВНОЇ СУМІСНОСТІ З БУДЬ-ЯКИМИ ВЕРСІЯМИ CCXT!
                raw_qty = pos.get('contracts') or pos.get('positionAmt') or pos.get('size') or pos.get('info', {}).get('positionAmt', 0.0)
                contracts = abs(float(raw_qty or 0.0))
                
                if contracts > 0:
                    symbol_clean = pos.get('symbol', '').replace('/', '').split(':')[0]
                    exchange_positions[symbol_clean] = {
                        'contracts': contracts,
                        'side': pos.get('side', pos.get('info', {}).get('positionSide', '')).upper(),
                        'symbol_ccxt': pos.get('symbol')
                    }

            # 2. Отримуємо копію активних сигналів з пам'яті
            async with self.active_signals_lock:
                local_signals_snapshot = list(self.active_signals)

            local_symbols = {s['symbol'].replace('/', '') for s in local_signals_snapshot}

            # --- ЕТАП А: Детекція фантомних позицій ---
            if get_setting('trading_enabled'):
                for ex_symbol_clean, ex_data in exchange_positions.items():
                    if ex_symbol_clean not in local_symbols:
                        logger.critical(f"🚨 [PHANTOM] Виявлено неконтрольовану позицію по {ex_symbol_clean}! Аварійне закриття...")
                        try:
                            await self.cancel_all_exchange_orders_for_symbol(async_ex, ex_symbol_clean, ex_data['symbol_ccxt'])
                            await asyncio.sleep(1.0)
                            close_side = "sell" if ex_data['side'] == "LONG" else "buy"
                            await async_ex.create_order(
                                symbol=ex_data['symbol_ccxt'],
                                type='market',
                                side=close_side,
                                amount=ex_data['contracts'],
                                params={'reduceOnly': True}
                            )
                        except Exception as phantom_err:
                            logger.error(f"⚠️ Не вдалося закрити фантомну позицію {ex_symbol_clean}: {phantom_err}")

            # --- ЕТАП Б: Покроковий аудит та самолікування захисних стопів за ЦІНОЮ ---
            for signal in local_signals_snapshot:
                symbol = signal['symbol']
                symbol_clean = symbol.replace('/', '')
                timeframe = signal['timeframe']
                ccxt_futures_symbol = self.resolve_ccxt_futures_symbol(async_ex, symbol)

                ex_pos = exchange_positions.get(symbol_clean)

                # Ситуація 1: Позиція повністю закрилася на біржі (об'єм = 0)
                if not ex_pos:
                    has_real_orders = bool(signal.get('stop_loss_id')) or bool(signal.get('tp_order_ids'))
                    if has_real_orders:
                        logger.warning(f"🧹 Позиція {symbol} закрита на біржі. Очищення ліміток...")
                        await self.cancel_all_exchange_orders_for_symbol(async_ex, symbol_clean, ccxt_futures_symbol)
                        
                        # === РОЗРАХУНОК ФІНАНСОВОГО ЗВІТУ ПРИ ПРИМИРЕННІ (LIVE CLOSE REPORT) ===
                        hit_tps = set(signal.get('hit_tps', []))
                        entry = signal['entry']
                        
                        if 0 in hit_tps:
                            result = 'be'
                            pct = 0.0 # Закрито в безубиток
                            status_display = "БЕЗЗБИТОК (BE) ↩️"
                        else:
                            result = 'sl'
                            sl_price = signal.get('stop_loss') or entry
                            pct = -round(abs(sl_price - entry) / entry * 100, 1)
                            status_display = "STOP-LOSS 🛑"
                            
                        # Закриваємо статистику в базі даних та отримуємо чистий PnL у USD
                        pnl_usd = close_signal_stat(signal.get('stat_id'), result, pct)
                        
                        async with self.active_signals_lock:
                            remove_active_signal(symbol, timeframe)
                            if signal in self.active_signals:
                                self.active_signals.remove(signal)
                            self.active_monitors.pop((symbol, timeframe), None)
                            
                        # Надсилаємо детальний фінансовий звіт з PnL у Telegram!
                        await self.bot.send_message(
                            chat_id=self.chat_id,
                            text=f"🏁 <b>Угоду по #{symbol_clean} ({timeframe}) успішно примирено!</b>\n\n"
                                 f"📉 Позицію було закрито на біржі по <b>{status_display}</b>.\n"
                                 f"💰 Фінансовий результат: <b>{'+' if pnl_usd >= 0 else ''}${pnl_usd:.2f} ({pct:+.1f}%)</b>\n\n"
                                 f"🧹 Усі залишкові лімітки та стопи автоматично зачищені.",
                            parse_mode='HTML'
                        )
                    else:
                        # Віртуальний або застарілий збійний сигнал просто видаляємо
                        async with self.active_signals_lock:
                            remove_active_signal(symbol, timeframe)
                            if signal in self.active_signals:
                                self.active_signals.remove(signal)
                            self.active_monitors.pop((symbol, timeframe), None)
                    continue

                # Ситуація 2: Позиція відкрита. Проводимо живий аудит ціни STOP_MARKET ордера на біржі
                expected_contracts = float(signal.get('pos_contracts', 0.0))
                expected_qty_rounded = float(async_ex.amount_to_precision(ccxt_futures_symbol, expected_contracts))
                
                # Очікувана ціна стопу
                expected_sl_price = float(signal.get('stop_loss') or 0.0)
                
                # ЗАХИСНИЙ ЗАПОБІЖНИК: якщо ціна стопу в базі 0 або відсутня — не перевіряємо
                if expected_sl_price <= 0:
                    logger.warning(f"⚠️ [RECONCILER] Для {symbol} очікувана ціна стопу відсутня в базі. Пропускаємо.")
                    continue
                
                try:
                    # Адаптивне зчитування умовних ордерів (STOP_MARKET)
                    if 'binance' in async_ex.id.lower():
                        market = async_ex.market(ccxt_futures_symbol)
                        symbol_id = market['id']
                        
                        raw_algo_orders = await async_ex.fapiPrivateGetOpenAlgoOrders({'symbol': symbol_id})
                        
                        sl_orders_on_exchange = [
                            {
                                'id': o.get('algoId'),
                                'amount': float(o.get('quantity', 0.0)),
                                'type': o.get('algoType'),
                                'stop_price': float(o.get('triggerPrice') or o.get('stopPrice', 0.0))
                            }
                            for o in raw_algo_orders
                            if o.get('algoType') == 'CONDITIONAL'
                        ]
                    else:
                        open_orders = await async_ex.fetch_open_orders(ccxt_futures_symbol)
                        sl_orders_on_exchange = [
                            {
                                'id': o.get('id'),
                                'amount': float(o.get('amount', 0.0)),
                                'type': o.get('type'),
                                'stop_price': float(o.get('stopPrice', o.get('triggerPrice', 0.0)))
                            }
                            for o in open_orders 
                            if o.get('type', '').upper() in ['STOP_MARKET', 'STOP']
                        ]
                    
                    # ПЕРЕВІРКА ТА АВТО-ЛІКУВАННЯ (Self-Healing за ціною стопу)
                    if len(sl_orders_on_exchange) == 1:
                        # На біржі рівно один стоп. Перевіряємо його ЦІНУ активації
                        sl_order = sl_orders_on_exchange[0]
                        sl_order_price = sl_order['stop_price']
                        
                        if sl_order_price <= 0:
                            continue
                            
                        # Округляємо обидві ціни до кроку біржі перед порівнянням, щоб усунути float-похибку!
                        sl_order_price_rounded = float(async_ex.price_to_precision(ccxt_futures_symbol, sl_order_price))
                        expected_sl_price_rounded = float(async_ex.price_to_precision(ccxt_futures_symbol, expected_sl_price))
                        
                        # Якщо ціна активації змінилася
                        if sl_order_price_rounded != expected_sl_price_rounded:
                            logger.info(f"🩹 [RECONCILER] Ціна стопу змінилася з {sl_order_price_rounded} до {expected_sl_price_rounded} (перехід у БУ). Оновлюємо ордер...")
                            try:
                                if 'binance' in async_ex.id.lower():
                                    await async_ex.fapiPrivateDeleteAlgoOrder({
                                        'symbol': symbol_id,
                                        'algoId': sl_order['id']
                                    })
                                else:
                                    await async_ex.cancel_order(sl_order['id'], ccxt_futures_symbol)
                                logger.info(f"🧹 [RECONCILER] Успішно скасовано застарілий Stop-Loss (ID: {sl_order['id']})")
                            except Exception as e:
                                logger.warning(f"⚠️ Не вдалося видалити застарілий Stop-Loss {sl_order['id']}: {e}")
                                
                            await self._create_new_sl_order(async_ex, ccxt_futures_symbol, signal, expected_qty_rounded)
                            
                    elif len(sl_orders_on_exchange) > 1 or len(sl_orders_on_exchange) == 0:
                        # Аномалія: або дубльовані стопи, або стоп взагалі злетів!
                        logger.warning(f"🚨 [RECONCILER] Збій стопів для {symbol}! Знайдено {len(sl_orders_on_exchange)} STOP_MARKET ордерів. Запуск авто-лікування...")
                        
                        # 1. Повністю видаляємо всі STOP_MARKET ордери по монеті на біржі для очищення аномалії
                        for sl_order in sl_orders_on_exchange:
                            try:
                                if 'binance' in async_ex.id.lower():
                                    await async_ex.fapiPrivateDeleteAlgoOrder({
                                        'symbol': symbol_id,
                                        'algoId': sl_order['id']
                                    })
                                else:
                                    await async_ex.cancel_order(sl_order['id'], ccxt_futures_symbol)
                                logger.info(f"🧹 [RECONCILER] Скасовано зайвий Stop-Loss (ID: {sl_order['id']})")
                            except Exception as cancel_err:
                                logger.error(f"⚠️ [RECONCILER] Не вдалося скасувати старий Stop-Loss (ID: {sl_order['id']}): {cancel_err}")
                        
                        # 2. Виставляємо один чистий, правильний Stop-Loss на повний 100% об'єм!
                        await self._create_new_sl_order(async_ex, ccxt_futures_symbol, signal, expected_qty_rounded)
                        
                        # Повідомляємо в Telegram про успішне авто-лікування дублікатів!
                        await self.bot.send_message(
                            chat_id=self.chat_id,
                            text=f"🛡️ <b>[САМОЛІКУВАННЯ] Виправлено аномалію по #{symbol_clean}!</b>\n\n"
                                 f"⚠️ Кількість виявлених Stop-Loss на біржі становила: <b>{len(sl_orders_on_exchange)}</b>\n"
                                 f"🧹 <b>Дія:</b> Усі дублікати автоматично скасовані.\n"
                                 f"🛑 <b>Результат:</b> Створено один чистий Stop-Loss під об'єм <b>{expected_qty_rounded} {symbol_clean[:-4]}</b>.",
                            parse_mode='HTML'
                        )
                except Exception as audit_err:
                    logger.error(f"❌ Помилка під час аудиту стопів для {symbol}: {audit_err}")

        except Exception as e:
            logger.error(f"Помилка під час примирення станів: {e}", exc_info=True)

    async def _create_new_sl_order(self, async_ex, ccxt_futures_symbol, signal, actual_qty_rounded):
        """Створює новий Stop-Loss ордер та оновлює його ID у базі"""
        direction = signal['direction']
        exit_side = "sell" if direction == "LONG" else "buy"
        sl_price = signal.get('stop_loss')
        sl_price_str = async_ex.price_to_precision(ccxt_futures_symbol, sl_price)
        
        sl_params = {
            'stopPrice': float(sl_price_str),
            'reduceOnly': True
        }
        new_sl_order = await async_ex.create_order(
            symbol=ccxt_futures_symbol,
            type='STOP_MARKET',
            side=exit_side,
            amount=actual_qty_rounded,
            params=sl_params
        )
        
        # Оновлюємо ID в RAM та БД
        signal['stop_loss_id'] = new_sl_order['id']
        self._update_db_sl_id(signal['db_id'], new_sl_order['id'])

    def _update_db_sl_id(self, db_id: int, new_sl_id: str):
        """Оновлює тільки ID стоп-лосса у PostgreSQL"""
        if not db_id:
            return
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("UPDATE signals SET stop_loss_id = %s WHERE id = %s", (new_sl_id, db_id))
            conn.commit()
            cursor.close()
        except Exception as e:
            logger.error(f"Помилка оновлення SL ID у БД: {e}")
        finally:
            conn.close()