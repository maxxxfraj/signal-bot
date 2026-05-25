# executor.py
import os
import asyncio
import logging
from typing import Dict, Any, List, Optional
import ccxt.async_support as ccxt
from settings import get_setting, to_native_float, to_native_int

logger = logging.getLogger("TradingBot.Executor")

class FuturesExecutor:
    def __init__(self, exchange_id: str, testnet: bool = True):
        self.exchange_id = exchange_id
        self.testnet = testnet
        self.exchange: Optional[ccxt.Exchange] = None

    async def initialize(self):
        """Асинхронна авторизація та завантаження ринків"""
        if self.testnet:
            api_key = os.getenv("TESTNET_API_KEY")
            secret = os.getenv("TESTNET_API_SECRET")
            logger.info(f"Ініціалізація {self.exchange_id.upper()} у режимі TESTNET / DEMO")
        else:
            api_key = os.getenv("PROD_API_KEY")
            secret = os.getenv("PROD_API_SECRET")
            logger.info(f"УВАГА: Ініціалізація {self.exchange_id.upper()} у режимі РЕАЛЬНИХ ТОРГІВ")

        if not api_key or not secret:
            logger.error(f"❌ Помилка: Не знайдено API-ключів для {self.exchange_id.upper()}!")
            return False

        if self.exchange_id == "mexc":
            self.exchange = ccxt.mexc({
                'apiKey': api_key,
                'secret': secret,
                'enableRateLimit': True,
                'options': {'defaultType': 'swap'},
                'aiohttp_trust_env': False
            })
        else:
            self.exchange = ccxt.binanceusdm({
                'apiKey': api_key,
                'secret': secret,
                'enableRateLimit': True,
                'aiohttp_trust_env': False
            })

        if self.testnet:
            if self.exchange_id == "binance":
                self.exchange.enable_demo_trading(True)
                logger.info(f"⚙️ Режим Demo Trading активовано для {self.exchange_id.upper()}")
            else:
                self.exchange.set_sandbox_mode(True)
                logger.info(f"⚙️ Режим Sandbox/Testnet активовано для {self.exchange_id.upper()}")

        await self.exchange.load_markets()
        return True

    async def close(self):
        if self.exchange:
            await self.exchange.close()

    async def execute_order_grid(
        self,
        symbol: str,
        direction: str,  # 'LONG' або 'SHORT'
        entry_price: float,
        stop_loss: float,
        tps: List[tuple],
        dobar_low: float,
        dobar_high: float,
        pos_contracts: float,
        use_dobar: bool = True
    ) -> Dict[str, Any]:
        """
        Виставляє сітку ордерів на біржі з початковим лімітним об'ємом тейків
        """
        if not self.exchange:
            success = await self.initialize()
            if not success:
                return {"status": "failed", "error": "API initialization failed"}

        ccxt_symbol = f"{symbol[:-4]}/USDT:USDT" if symbol.endswith("USDT") else symbol
        
        try:
            market = self.exchange.market(ccxt_symbol)
        except Exception as e:
            ccxt_symbol = f"{symbol[:-4]}/USDT"
            try:
                market = self.exchange.market(ccxt_symbol)
            except Exception:
                return {"status": "failed", "error": f"Symbol {symbol} not found on exchange"}

        total_contracts = float(self.exchange.amount_to_precision(ccxt_symbol, pos_contracts))

        # Перевірка мінімального об'єму ордера
        min_qty = market['limits']['amount']['min']
        if total_contracts < min_qty:
            err_msg = f"Order size ({total_contracts}) is below exchange minimum limit ({min_qty}) for {symbol}."
            logger.warning(f"⚠️ {err_msg}")
            return {"status": "failed", "error": err_msg}

        # --- НАЛАШТУВАННЯ КРЕДИТНОГО ПЛЕЧА ТА ІЗОЛЬОВАНОЇ МАРЖІ ---
        try:
            leverage = get_setting('leverage') or 20
            logger.info(f"⚙️ Встановлення кредитного плеча {leverage}x для {ccxt_symbol}")
            await self.exchange.set_leverage(leverage, ccxt_symbol)
        except Exception as e:
            logger.warning(f"⚠️ Не вдалося встановити кредитне плече: {e}")

        try:
            logger.info(f"⚙️ Встановлення ізольованої маржі (ISOLATED) для {ccxt_symbol}")
            await self.exchange.set_margin_mode('isolated', ccxt_symbol)
        except Exception as e:
            err_str = str(e)
            if "already" in err_str.lower() or "No need to change margin type" in err_str or "-4046" in err_str:
                logger.info(f"ℹ️ Ізольована маржа вже активована для {ccxt_symbol}")
            else:
                logger.warning(f"⚠️ Попередження при встановленні маржинального режиму: {e}")

        entry_side = "buy" if direction == "LONG" else "sell"
        exit_side = "sell" if direction == "LONG" else "buy"

        result_report = {
            "status": "failed",
            "entry_market_id": None,
            "entry_dobar_id": None,
            "stop_loss_id": None,
            "take_profit_ids": [],
            "error": None
        }

        try:
            initial_volume = total_contracts
            dobar_volume = 0.0

            if use_dobar:
                initial_volume = float(self.exchange.amount_to_precision(ccxt_symbol, total_contracts * 0.5))
                dobar_volume = float(self.exchange.amount_to_precision(ccxt_symbol, total_contracts - initial_volume))

            if initial_volume <= 0:
                return {"status": "failed", "error": "Position volume is below exchange limits"}

            # --- КРОК 1: Ринковий вхід (Market Entry) ---
            logger.info(f"🛒 Відкриття ринкового ордера: {entry_side.upper()} {initial_volume} {ccxt_symbol}")
            market_order = await self.exchange.create_order(
                symbol=ccxt_symbol,
                type='market',
                side=entry_side,
                amount=initial_volume
            )
            result_report["entry_market_id"] = market_order["id"]
            result_report["entry_fill_price"] = market_order.get('average') or market_order.get('price') or entry_price
            result_report["entry_fill_qty"] = market_order.get('filled') or initial_volume
            result_report["entry_fee"] = market_order.get('fee', {}).get('cost', 0.0) if market_order.get('fee') else 0.0
            result_report["executed_at_ms"] = market_order.get('timestamp') or (time.time() * 1000)
            
            # --- КРОК 2: Dobar лімітний вхід ---
            if use_dobar and dobar_volume > 0:
                dobar_mid = (dobar_low + dobar_high) / 2.0
                dobar_price_str = self.exchange.price_to_precision(ccxt_symbol, dobar_mid)
                
                logger.info(f"⏳ Виставлення Dobar лімітки: {entry_side.upper()} {dobar_volume} за ціною {dobar_price_str}")
                dobar_order = await self.exchange.create_order(
                    symbol=ccxt_symbol,
                    type='limit',
                    side=entry_side,
                    amount=dobar_volume,
                    price=float(dobar_price_str)
                )
                result_report["entry_dobar_id"] = dobar_order["id"]

            # --- КРОК 3: Stop-Loss (STOP_MARKET з класичним reduceOnly на весь об'єм) ---
            # Виправлено -4130: Повертаємо класичний reduceOnly з вказівкою об'єму для повної сумісності
            sl_price_str = self.exchange.price_to_precision(ccxt_symbol, stop_loss)
            logger.info(f"🛑 Виставлення Stop-Loss на {total_contracts} за ціною {sl_price_str}")
            
            sl_params = {
                'stopPrice': float(sl_price_str),
                'reduceOnly': True
            }
            sl_order = await self.exchange.create_order(
                symbol=ccxt_symbol,
                type='STOP_MARKET',
                side=exit_side,
                amount=total_contracts,
                params=sl_params
            )
            result_report["stop_loss_id"] = sl_order["id"]

# --- КРОК 4: 4 Take-Profits (З автоматичним Lot & Notional Guard) ---
            new_tp_ids = []
            
            # Отримуємо мінімальний лот контракту на біржі
            min_qty = market['limits']['amount']['min'] or 1.0
            
            # Розраховуємо плановий об'єм кроку та його номінал
            planned_step_volume = initial_volume / 4
            estimated_step_notional = planned_step_volume * tps[0][0]
            
            # Якщо крок менший за мінімальний лот АБО за мінімальний номінал у 5.1 USDT:
            # Об'єднуємо всі залишкові тейки в один великий ордер на першу ціль
            if planned_step_volume < min_qty or estimated_step_notional < 5.1:
                logger.info(f"⚠️ [LOT/NOTIONAL GUARD] Крок ({planned_step_volume} / {estimated_step_notional:.2f} USD) нижче лімітів біржі. Об'єднуємо тейки в один.")
                tp_price_str = self.exchange.price_to_precision(ccxt_symbol, tps[0][0])
                tp_order = await self.exchange.create_order(
                    symbol=ccxt_symbol,
                    type='limit',
                    side=exit_side,
                    amount=initial_volume,
                    price=float(tp_price_str),
                    params={'reduceOnly': True}
                )
                result_report["take_profit_ids"].append(tp_order["id"])
            else:
                # Стандартна розділена сітка з 4 тейків
                tp_step_volume = float(self.exchange.amount_to_precision(ccxt_symbol, planned_step_volume))
                for idx, (tp_price, _, _) in enumerate(tps[:4]):
                    current_tp_vol = tp_step_volume
                    if idx == 3:
                        current_tp_vol = float(self.exchange.amount_to_precision(
                            ccxt_symbol, initial_volume - (tp_step_volume * 3)
                        ))

                    if current_tp_vol <= 0:
                        continue

                    tp_price_str = self.exchange.price_to_precision(ccxt_symbol, tp_price)
                    logger.info(f"🎯 Виставлення початкового TP{idx+1} на {current_tp_vol} за ціною {tp_price_str}")
                    
                    tp_order = await self.exchange.create_order(
                        symbol=ccxt_symbol,
                        type='limit',
                        side=exit_side,
                        amount=current_tp_vol,
                        price=float(tp_price_str),
                        params={'reduceOnly': True}
                    )
                    result_report["take_profit_ids"].append(tp_order["id"])

            result_report["status"] = "success"
            logger.info(f"✅ Вхідна сітка успішно виставлена для {ccxt_symbol}")

        except Exception as e:
            logger.error(f"❌ Критична помилка під час виконання ордерів для {ccxt_symbol}: {e}", exc_info=True)
            result_report["status"] = "failed"
            result_report["error"] = str(e)
            
            try:
                await self.exchange.cancel_all_orders(ccxt_symbol)
            except Exception as cancel_err:
                logger.error(f"Не вдалося виконати екстрене скасування ордерів: {cancel_err}")

        return result_report