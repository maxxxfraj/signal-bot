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

            # --- ЕТАП Б: Покрокове примирення за новою логікою ---
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
                        
                        # Скасовуємо абсолютно всі ордери на біржі тільки тепер!
                        await self.cancel_all_exchange_orders_for_symbol(async_ex, symbol_clean, ccxt_futures_symbol)
                        
                        async with self.active_signals_lock:
                            remove_active_signal(symbol, timeframe)
                            if signal in self.active_signals:
                                self.active_signals.remove(signal)
                            self.active_monitors.pop((symbol, timeframe), None)
                            
                        await self.bot.send_message(
                            chat_id=self.chat_id,
                            text=f"🏁 <b>Позицію по #{symbol} ({timeframe}) успішно закрито!</b>\n\n"
                                 f"🧹 Усі залишкові лімітні Take-Profit та Stop-Loss ордери повністю зачищені з біржі.",
                            parse_mode='HTML'
                        )
                    continue

                # Ситуація 2: Позиція відкрита. Перевіряємо та коригуємо ТІЛЬКИ об'єм Stop-Loss ордера
                actual_contracts = ex_pos['contracts']
                
                # Округляємо фактичний об'єм до специфікації біржі, щоб уникнути похибок float
                actual_qty_rounded = float(async_ex.amount_to_precision(ccxt_futures_symbol, actual_contracts))
                
                # Отримуємо об'єм, на який виставлено поточний Stop-Loss
                current_sl_id = signal.get('stop_loss_id')
                sl_needs_update = False
                
                if current_sl_id:
                    try:
                        sl_order_info = await async_ex.fetch_order(current_sl_id, ccxt_futures_symbol)
                        sl_order_amount = float(sl_order_info.get('amount', 0.0))
                        sl_order_amount_rounded = float(async_ex.amount_to_precision(ccxt_futures_symbol, sl_order_amount))
                        
                        # Якщо фактичний об'єм позиції відрізняється від об'єму в ордері Stop-Loss — потрібне коригування!
                        if actual_qty_rounded != sl_order_amount_rounded:
                            sl_needs_update = True
                    except Exception:
                        sl_needs_update = True # Якщо ордер не знайдено, перевстановимо його
                else:
                    sl_needs_update = True

                if sl_needs_update and get_setting('trading_enabled'):
                    logger.info(f"🩹 [RECONCILER] Об'єм змінився до {actual_qty_rounded}. Коригуємо Stop-Loss для {symbol}...")
                    
                    # 1. Видаляємо старий Stop-Loss
                    if current_sl_id:
                        try:
                            await async_ex.cancel_order(current_sl_id, ccxt_futures_symbol)
                        except Exception:
                            pass
                    
                    # 2. Виставляємо новий Stop-Loss строго під фактичний об'єм позиції на біржі
                    try:
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
                        
                        # Записуємо новий ID в пам'ять та БД
                        signal['stop_loss_id'] = new_sl_order['id']
                        self._update_db_sl_id(signal['db_id'], new_sl_order['id'])
                        
                        logger.info(f"✅ Stop-Loss успішно відкориговано під об'єм {actual_qty_rounded} для {symbol}")
                    except Exception as sl_err:
                        logger.error(f"❌ Не вдалося відкоригувати Stop-Loss для {symbol}: {sl_err}")

        except Exception as e:
            logger.error(f"Помилка під час примирення станів: {e}", exc_info=True)

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