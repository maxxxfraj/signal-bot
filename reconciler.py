# reconciler.py
import asyncio
import logging
from database import remove_active_signal, get_connection
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
                contracts = abs(float(pos.get('contracts', 0.0)))
                if contracts > 0:
                    symbol_clean = pos.get('symbol', '').replace('/', '').split(':')[0]
                    exchange_positions[symbol_clean] = {
                        'contracts': contracts,
                        'side': pos.get('side', '').upper(),
                        'symbol_ccxt': pos.get('symbol')
                    }

            # 2. Блокуємо таск для роботи з активними сигналами
            async with self.active_signals_lock:
                local_signals_snapshot = list(self.active_signals)

            local_symbols = {s['symbol'].replace('/', '') for s in local_signals_snapshot}

            # --- ЕТАП А: Детекція фантомних позицій ---
            if get_setting('trading_enabled'):
                for ex_symbol_clean, ex_data in exchange_positions.items():
                    if ex_symbol_clean not in local_symbols:
                        logger.critical(f"🚨 [PHANTOM] Виявлено неконтрольовану позицію по {ex_symbol_clean}! Аварійне закриття...")
                        
                        # ІЗОЛЮЄМО ЗАКРИТТЯ ФАНТОМА, ЩОБ ПОМИЛКИ НЕПІДТРИМУВАНИХ МОНЕТ BINANCE DEMO НЕ ЗУПИНЯЛИ РЕКОНСИЛІАТОР
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
                            logger.error(f"⚠️ Не вдалося автоматично закрити фантомну позицію {ex_symbol_clean}: {phantom_err}")
                            # Відправляємо м'яке попередження в Telegram замість падіння процесу
                            await self.bot.send_message(
                                chat_id=self.chat_id,
                                text=f"⚠️ <b>[ФАНТОМ] Виявлено позицію по #{ex_symbol_clean}, але автоматичне закриття не вдалося!</b>\n\n"
                                     f"Причина: <i>{phantom_err}</i>\n"
                                     f"💡 <i>Це може бути через те, що цей актив не підтримується у Демо-режимі Binance, або обмежений вашим акаунтом.</i>",
                                parse_mode='HTML'
                            )

            # --- ЕТАП Б: Примирення локальних сигналів ---
            for signal in local_signals_snapshot:
                symbol = signal['symbol']
                symbol_clean = symbol.replace('/', '')
                timeframe = signal['timeframe']
                ccxt_futures_symbol = self.resolve_ccxt_futures_symbol(async_ex, symbol)

                ex_pos = exchange_positions.get(symbol_clean)

                # Ситуація 1: Угода закрита на біржі, але локально активна
                if not ex_pos:
                    # ЗАХИСТ ВІРТУАЛЬНИХ СИГНАЛІВ: Очищуємо та закриваємо тільки якщо угода БУЛА фізично відкрита
                    has_real_orders = bool(signal.get('stop_loss_id')) or bool(signal.get('tp_order_ids'))
                    if has_real_orders:
                        logger.warning(f"🧹 Сигнал {symbol} закрився офлайн. Очищення...")
                        await self.cancel_all_exchange_orders_for_symbol(async_ex, symbol_clean, ccxt_futures_symbol)
                        
                        async with self.active_signals_lock:
                            remove_active_signal(symbol, timeframe)
                            if signal in self.active_signals:
                                self.active_signals.remove(signal)
                            self.active_monitors.pop((symbol, timeframe), None)
                            
                        await self.bot.send_message(
                            chat_id=self.chat_id,
                            text=f"🏁 <b>Сигнал #{symbol} ({timeframe}) успішно примирено (Reconciled).</b>\n\n"
                                 f"📉 Позицію було закрито на біржі без участі бота.\n"
                                 f"🧹 Усі залишкові лімітки та стопи автоматично зачищені.",
                            parse_mode='HTML'
                        )
                        continue
                    else:
                        # Це чисто віртуальний (інформаційний) сигнал, ігноруємо відсутність позиції на біржі
                        continue

                # Ситуація 2: Примирення об'ємів з урахуванням Dobar та тейків
                expected_contracts = float(signal.get('pos_contracts', 0.0))
                actual_contracts = ex_pos['contracts']
                hit_tps = set(signal.get('hit_tps', []))
                dobar_filled_state = bool(signal.get('dobar_filled_state', False))

                # === ОНОВЛЕННЯ: ФІЗИЧНЕ ПРЕД-ДЕТЕКТУВАННЯ ТЕЙКІВ ПЕРЕД ПЕРЕВІРКОЮ ОБ'ЄМІВ ===
                if get_setting('trading_enabled') and signal.get('tp_order_ids'):
                    new_detected_hits = set()
                    for idx, tp_id in enumerate(signal['tp_order_ids']):
                        if idx in hit_tps:
                            continue
                        try:
                            order_info = await async_ex.fetch_order(tp_id, ccxt_futures_symbol)
                            if order_info.get('status') == 'closed': #  Ордер виконався на біржі!
                                new_detected_hits.add(idx)
                                logger.info(f"🎯 [RECONCILER] Виявлено виконання TP{idx+1} (ID: {tp_id}) для {symbol} під час фонового примирення.")
                        except Exception:
                            pass
                            
                    if new_detected_hits:
                        hit_tps = hit_tps | new_detected_hits
                        signal['hit_tps'] = hit_tps
                        
                        # Перевід в БУ при першому тейку (З динамічним розрахунком ціни)
                        if 0 in hit_tps and signal.get('stop_loss') != signal['entry']:
                            #  РОЗРАХУНОК ДИНАМІЧНОГО БЕЗУБИТКУ ПРИ РЕКОНСИЛІАЦІЇ
                            if dobar_filled_state:
                                dobar_mid = (signal['dobar_low'] + signal['dobar_high']) / 2.0
                                avg_entry = (signal['entry'] + dobar_mid) / 2.0
                            else:
                                avg_entry = signal['entry']
                                
                            signal['stop_loss'] = avg_entry
                                
                            # Оновлюємо стан в PostgreSQL
                            hit_tps_str = ",".join(map(str, sorted(list(hit_tps))))
                            conn = get_connection()
                            try:
                                cursor = conn.cursor()
                                cursor.execute(
                                    "UPDATE signals SET hit_tps = %s, stop_loss = %s WHERE id = %s",
                                    (hit_tps_str, avg_entry, signal['db_id'])
                                )
                                conn.commit()
                                cursor.close()
                            except Exception as db_err:
                                logger.error(f"Помилка оновлення hit_tps в БД під час реконсиліації: {db_err}")
                            finally:
                                conn.close()
                # =========================================================================

                # --- МАТЕМАТИЧНЕ КОРЕГУВАННЯ ОБ'ЄМУ DOBAR ТА ТЕЙКІВ ---
                use_dobar_setting = get_setting('use_dobar')
                if use_dobar_setting is None:
                    use_dobar_setting = True

                dobar_factor = 0.5 if (use_dobar_setting and not dobar_filled_state) else 1.0
                
                # Масив оригінальних часток
                original_percentages = [0.50, 0.20, 0.15, 0.15]
                
                # Сума оригінальних часток для невиконаних тейків
                remaining_pct_sum = sum(original_percentages[i] for i in range(4) if i not in hit_tps)
                if remaining_pct_sum <= 0:
                    remaining_pct_sum = 0.25  # запобіжник

                # Точний кінцевий очікуваний об'єм на біржі
                expected_on_exchange = expected_contracts * dobar_factor * remaining_pct_sum

                # Перевіряємо відхилення фактичного об'єму від очікуваного (поріг розбіжності > 2%)
                if expected_on_exchange > 0 and abs(actual_contracts - expected_on_exchange) / expected_on_exchange > 0.02:
                    logger.warning(
                        f"⚠️ Розбіжність об'єму для {symbol}. "
                        f"Очікувалось: {expected_on_exchange} (Добір: {dobar_filled_state}, Тейків hit: {len(hit_tps)}), "
                        f"Біржа: {actual_contracts}. Адаптація..."
                    )
                    
                    # Перебудовуємо всю сітку
                    await self._rebuild_protective_orders(signal, actual_contracts, ccxt_futures_symbol, async_ex)

                    await self.bot.send_message(
                        chat_id=self.chat_id,
                        text=f"🔄 <b>Адаптація та повне відновлення сітки для #{symbol} ({timeframe})!</b>\n\n"
                             f"📊 Фактичний об'єм на біржі: <b>{actual_contracts}</b> (Очікувалось: {expected_on_exchange})\n"
                             f"🛡️ <b>Результат:</b> Захисна сітка ордерів успішно перевиставлена під новий об'єм.",
                        parse_mode='HTML'
                    )

        except Exception as e:
            logger.error(f"Помилка під час примирення станів: {e}", exc_info=True)
        finally:
            pass

    async def _rebuild_protective_orders(self, signal: dict, actual_qty: float, ccxt_symbol: str, async_ex):
        """Перевиставляє весь пакет ордерів з ізольованою обробкою помилок та нелінійним розподілом"""
        try:
            # 1. Повністю очищуємо всі ордери
            await self.cancel_all_exchange_orders_for_symbol(async_ex, signal['symbol'].replace('/', ''), ccxt_symbol)
            await asyncio.sleep(1.5)

            direction = signal['direction']
            entry_side = "buy" if direction == "LONG" else "sell"
            exit_side = "sell" if direction == "LONG" else "buy"
            
            # --- ВІДНОВЛЕННЯ 1: STOP-LOSS (STOP_MARKET) З АВТО-ЛІКУВАННЯМ БУ ---
            hit_tps = set(signal.get('hit_tps', []))
            use_dobar_setting = get_setting('use_dobar') or True
            dobar_filled_state = bool(signal.get('dobar_filled_state', False))
            
            if 0 in hit_tps:
                # Динамічно та безпомилково перераховуємо БУ на випадок розбіжностей у БД
                if dobar_filled_state:
                    dobar_mid = (signal['dobar_low'] + signal['dobar_high']) / 2.0
                    correct_sl = (signal['entry'] + dobar_mid) / 2.0
                else:
                    correct_sl = signal['entry']
                    
                sl_price = correct_sl
                signal['stop_loss'] = correct_sl
                logger.info(f"🩹 [HEALING] Реконсиліатор виявив та вилікував БУ-стоп для {signal['symbol']} на рівень {correct_sl}")
            else:
                sl_price = signal.get('stop_loss')
                
            new_sl_id = None
            if sl_price:
                try:
                    sl_price_str = async_ex.price_to_precision(ccxt_symbol, sl_price)
                    sl_params = {
                        'stopPrice': float(sl_price_str),
                        'reduceOnly': True
                    }
                    new_sl_order = await async_ex.create_order(
                        symbol=ccxt_symbol,
                        type='STOP_MARKET',
                        side=exit_side,
                        amount=actual_qty,
                        params=sl_params
                    )
                    new_sl_id = new_sl_order['id']
                    logger.info(f"🛡️ [REBUILD] Stop-Loss успішно відновлено на {actual_qty} для {ccxt_symbol}")
                except Exception as sl_err:
                    logger.critical(f"❌ [CRITICAL] Не вдалося перевстановити Stop-Loss для {ccxt_symbol}: {sl_err}")
                    await self.bot.send_message(
                        chat_id=self.chat_id,
                        text=f"⚠️ <b>[КРИТИЧНО] Не вдалося виставити Stop-Loss для #{signal['symbol']}!</b>\n"
                             f"Помилка біржі: <i>{sl_err}</i>\n"
                             f"🚨 Позиція незахищена! Перевірте кабінет вручну.",
                        parse_mode='HTML'
                    )

            # --- ВІДНОВЛЕННЯ 2: DOBAR LIMIT ORDER (З УРАХУВАННЯМ DOBAR-CANCEL RULE) ---
            new_dobar_id = signal.get('dobar_order_id')
            
            # Відновлюємо Dobar лімітку ТІЛЬКИ якщо TP1 ще не був досягнутий! (Правило Dobar-Cancel)
            if use_dobar_setting and not dobar_filled_state and (0 not in hit_tps):
                try:
                    dobar_low = signal.get('dobar_low')
                    dobar_high = signal.get('dobar_high')
                    if dobar_low is not None and dobar_high is not None:
                        dobar_mid = (dobar_low + dobar_high) / 2.0
                        dobar_price_str = async_ex.price_to_precision(ccxt_symbol, dobar_mid)
                        
                        logger.info(f"⏳ Перевиставлення лімітки Dobar на {actual_qty} за ціною {dobar_price_str}")
                        dobar_order = await async_ex.create_order(
                            symbol=ccxt_symbol,
                            type='limit',
                            side=entry_side,
                            amount=actual_qty,
                            price=float(dobar_price_str)
                        )
                        new_dobar_id = dobar_order['id']
                except Exception as dobar_err:
                    logger.warning(f"⚠️ Не вдалося перевстановити Dobar лімітку для {ccxt_symbol}: {dobar_err}")
            else:
                new_dobar_id = None

            # --- ВІДНОВЛЕННЯ 3: TAKE-PROFIT LIMIT ORDERS ---
            new_tp_ids = []
            tps = signal.get('tps', [])
            
            remaining_tps_count = 4 - len(hit_tps)
            if remaining_tps_count > 0:
                try:
                    market = async_ex.market(ccxt_symbol)
                    min_qty = market['limits']['amount']['min'] or 1.0
                    
                    # Масив оригінальних часток
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
                    
                    planned_step_volume = actual_qty * (next_tp_pct / remaining_pct_sum)
                    estimated_step_notional = planned_step_volume * next_tp_price
                    
                    # Запобігання помилкам лотності та номіналу (Lot & Notional Guard)
                    if planned_step_volume < min_qty or estimated_step_notional < 5.1:
                        logger.info(f"⚠️ [LOT/NOTIONAL GUARD] Крок ({planned_step_volume} / {estimated_step_notional:.2f} USD) нижче лімітів. Об'єднуємо тейки в один.")
                        tp_price_str = async_ex.price_to_precision(ccxt_symbol, next_tp_price)
                        tp_order = await async_ex.create_order(
                            symbol=ccxt_symbol,
                            type='limit',
                            side=exit_side,
                            amount=actual_qty,
                            price=float(tp_price_str),
                            params={'reduceOnly': True}
                        )
                        new_tp_ids.append(tp_order['id'])
                    else:
                        #  Стандартна розділена сітка
                        tp_step_volume = float(async_ex.amount_to_precision(ccxt_symbol, planned_step_volume))
                        tp_counter = 0
                        for idx, (tp_price, _, _) in enumerate(tps[:4]):
                            if idx in hit_tps:
                                continue
                                
                            share = original_percentages[idx] / remaining_pct_sum
                            current_tp_vol = float(async_ex.amount_to_precision(ccxt_symbol, actual_qty * share))
                            
                            if tp_counter == remaining_tps_count - 1:
                                current_tp_vol = float(async_ex.amount_to_precision(
                                    ccxt_symbol, actual_qty - accumulated_vol
                                ))
                                
                            if current_tp_vol <= 0:
                                continue
                                
                            accumulated_vol += current_tp_vol
                            tp_price_str = async_ex.price_to_precision(ccxt_symbol, tp_price)
                            logger.info(f"🎯 Перевиставлення TP{idx+1} (частка {share*100:.0f}%) на {current_tp_vol} за ціною {tp_price_str}")
                            
                            tp_order = await async_ex.create_order(
                                symbol=ccxt_symbol,
                                type='limit',
                                side=exit_side,
                                amount=current_tp_vol,
                                price=float(tp_price_str),
                                params={'reduceOnly': True}
                            )
                            new_tp_ids.append(tp_order['id'])
                            tp_counter += 1
                except Exception as tp_err:
                    logger.warning(f"⚠️ Не вдалося перевстановити Take-Profits для {ccxt_symbol}: {tp_err}")

            # Визначаємо коефіцієнт залишкових тейків для збереження пропорцій у БД
            remaining_factor = remaining_pct_sum

            # Розрахунок відновленого масштабованого об'єму для запису в БД
            if use_dobar_setting and not dobar_filled_state:
                db_qty_val = (actual_qty / remaining_factor) * 2.0
            else:
                db_qty_val = actual_qty / remaining_factor

            # 4. Записуємо оновлені ID ордерів та ВИПРАВЛЕНУ ціну стопу у PostgreSQL
            self._update_db_orders_state(
                db_id=signal['db_id'],
                actual_qty=db_qty_val,
                new_sl_id=new_sl_id,
                new_dobar_id=new_dobar_id,
                new_tp_ids=new_tp_ids,
                dobar_filled=dobar_filled_state,
                stop_loss_price=sl_price
            )
            
            # 5. Оновлюємо in-memory копію сигналу в оперативній пам'яті
            signal['pos_contracts'] = db_qty_val
            signal['stop_loss_id'] = new_sl_id
            signal['dobar_order_id'] = new_dobar_id
            signal['tp_order_ids'] = new_tp_ids

        except Exception as e:
            logger.error(f"❌ Критична помилка перебудови сітки для {ccxt_symbol}: {e}")

    def _update_db_orders_state(self, db_id: int, actual_qty: float, new_sl_id: str, new_dobar_id: str, new_tp_ids: list, dobar_filled: bool, stop_loss_price: float):
        """Оновлює стан всіх ордерів та ціну стопу у PostgreSQL після повної перебудови сітки"""
        if not db_id:
            return
        conn = get_connection()
        try:
            cursor = conn.cursor()
            tp_ids_str = ",".join(new_tp_ids) if new_tp_ids else ""
            dobar_filled_val = 1 if dobar_filled else 0
            cursor.execute(
                "UPDATE signals SET pos_contracts = %s, stop_loss_id = %s, dobar_order_id = %s, tp_order_ids = %s, dobar_filled_state = %s, stop_loss = %s WHERE id = %s",
                (actual_qty, new_sl_id, new_dobar_id, tp_ids_str, dobar_filled_val, stop_loss_price, db_id)
            )
            conn.commit()
            cursor.close()
        except Exception as e:
            logger.error(f"Помилка оновлення станів ордерів у БД: {e}")
        finally:
            conn.close()