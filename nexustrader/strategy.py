from typing import Dict, List, Set, Callable, Literal
from decimal import Decimal
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from nexustrader.core.log import SpdLog
from nexustrader.base import ExchangeManager
from nexustrader.core.entity import TaskManager
from nexustrader.core.cache import AsyncCache
from nexustrader.error import StrategyBuildError, OrderError
from nexustrader.base import ExecutionManagementSystem, PrivateConnector
from nexustrader.core.nautilius_core import MessageBus, UUID4
from nexustrader.schema import (
    BookL1,
    Trade,
    Kline,
    Order,
    OrderSubmit,
    InstrumentId,
    BaseMarket,
    AccountBalance,
)
from nexustrader.constants import (
    DataType,
    OrderSide,
    OrderType,
    TimeInForce,
    PositionSide,
    AccountType,
    SubmitType,
    ExchangeType,
    KlineInterval,
    TriggerType,
)


class Strategy:
    def __init__(self):
        self.log = SpdLog.get_logger(
            name=type(self).__name__, level="DEBUG", flush=True
        )

        self._subscriptions: Dict[DataType, Dict[str, KlineInterval] | Set[str]] = {
            DataType.BOOKL1: set(),
            DataType.TRADE: set(),
            DataType.KLINE: {},
        }

        self._initialized = False
        self._scheduler = AsyncIOScheduler()

    def _init_core(
        self,
        exchanges: Dict[ExchangeType, ExchangeManager],
        private_connectors: Dict[AccountType, PrivateConnector],
        cache: AsyncCache,
        msgbus: MessageBus,
        task_manager: TaskManager,
        ems: Dict[ExchangeType, ExecutionManagementSystem],
    ):
        if self._initialized:
            return

        self.cache = cache

        self._ems = ems
        self._task_manager = task_manager
        self._msgbus = msgbus
        self._private_connectors = private_connectors
        self._exchanges = exchanges
        self._msgbus.subscribe(topic="trade", handler=self.on_trade)
        self._msgbus.subscribe(topic="bookl1", handler=self.on_bookl1)
        self._msgbus.subscribe(topic="kline", handler=self.on_kline)

        self._msgbus.register(endpoint="pending", handler=self.on_pending_order)
        self._msgbus.register(endpoint="accepted", handler=self.on_accepted_order)
        self._msgbus.register(
            endpoint="partially_filled", handler=self.on_partially_filled_order
        )
        self._msgbus.register(endpoint="filled", handler=self.on_filled_order)
        self._msgbus.register(endpoint="canceling", handler=self.on_canceling_order)
        self._msgbus.register(endpoint="canceled", handler=self.on_canceled_order)
        self._msgbus.register(endpoint="failed", handler=self.on_failed_order)
        self._msgbus.register(
            endpoint="cancel_failed", handler=self.on_cancel_failed_order
        )

        self._msgbus.register(endpoint="balance", handler=self.on_balance)

        self._initialized = True

    def schedule(
        self,
        func: Callable,
        trigger: Literal["interval", "cron"] = "interval",
        **kwargs,
    ):
        """
        cron: run at a specific time second, minute, hour, day, month, year
        interval: run at a specific interval  seconds, minutes, hours, days, weeks, months, years
        """
        if not self._initialized:
            raise RuntimeError(
                "Strategy not initialized, please use `schedule` in `on_start` method"
            )

        self._scheduler.add_job(func, trigger=trigger, **kwargs)

    def market(self, symbol: str) -> BaseMarket:
        instrument_id = InstrumentId.from_str(symbol)
        exchange = self._exchanges[instrument_id.exchange]
        return exchange.market[instrument_id.symbol]

    def amount_to_precision(
        self,
        symbol: str,
        amount: float,
        mode: Literal["round", "ceil", "floor"] = "round",
    ) -> Decimal:
        instrument_id = InstrumentId.from_str(symbol)
        ems = self._ems[instrument_id.exchange]
        return ems._amount_to_precision(instrument_id.symbol, amount, mode)

    def price_to_precision(
        self,
        symbol: str,
        price: float,
        mode: Literal["round", "ceil", "floor"] = "round",
    ) -> Decimal:
        instrument_id = InstrumentId.from_str(symbol)
        ems = self._ems[instrument_id.exchange]
        return ems._price_to_precision(instrument_id.symbol, price, mode)
    
    def create_order(
        self,
        symbol: str,
        side: OrderSide,
        type: OrderType,
        amount: Decimal,
        price: Decimal | None = None,
        time_in_force: TimeInForce | None = TimeInForce.GTC,
        position_side: PositionSide | None = None,
        trigger_price: Decimal | None = None,
        trigger_type: TriggerType = TriggerType.LAST_PRICE,
        account_type: AccountType | None = None,
        **kwargs,
    ) -> str:
        if type.is_stop_loss:
            submit_type = SubmitType.STOP_LOSS
        elif type.is_take_profit:
            submit_type = SubmitType.TAKE_PROFIT
        else:
            submit_type = SubmitType.CREATE
        
        order = OrderSubmit(
            symbol=symbol,
            instrument_id=InstrumentId.from_str(symbol),
            submit_type=submit_type,
            side=side,
            type=type,
            amount=amount,
            price=price,
            time_in_force=time_in_force,
            position_side=position_side,
            trigger_price=trigger_price,
            trigger_type=trigger_type,
            kwargs=kwargs,
        )
        self._ems[order.instrument_id.exchange]._submit_order(order, account_type)
        return order.uuid

    def cancel_order(
        self, symbol: str, uuid: str, account_type: AccountType | None = None, **kwargs
    ) -> str:
        order = OrderSubmit(
            symbol=symbol,
            instrument_id=InstrumentId.from_str(symbol),
            submit_type=SubmitType.CANCEL,
            uuid=uuid,
            kwargs=kwargs,
        )
        self._ems[order.instrument_id.exchange]._submit_order(order, account_type)
        return order.uuid
    
    def create_adp_maker(
        self,
        symbol: str,
        side: OrderSide,
        amount: Decimal,
        duration: int,
        wait: int,
        check_interval: float = 0.1,
        position_side: PositionSide | None = None,
        account_type: AccountType | None = None,
        trigger_tp_ratio: float | None = None,
        trigger_sl_ratio: float | None = None,
        tp_ratio: float | None = None,
        sl_ratio: float | None = None,
        **kwargs,
    ) -> str:
        """create adaptive maker order with tp and sl
        
        Args:
            symbol: str The symbol of the order
            side: OrderSide The side of the order
            amount: Decimal The amount of the order
            duration: int The duration of the order
            wait: int The waiting time for the maker order to be filled
            check_interval: float = 0.1 The interval to check the order status
            position_side: PositionSide | None = None The position side of the order
            account_type: AccountType | None = None The account type of the order
            trigger_tp_ratio: float | None = None The trigger price ratio of the take profit order
            trigger_sl_ratio: float | None = None The trigger price ratio of the stop loss order
            tp_ratio: float | None = None The take profit ratio of the take profit order
            sl_ratio: float | None = None The stop loss ratio of the stop loss order
            kwargs: Dict[str, Any] = {} The additional arguments for the order
        """
        if wait > duration:
            raise OrderError("wait must be less than duration")
        
        order = OrderSubmit(
            symbol=symbol,
            instrument_id=InstrumentId.from_str(symbol),
            uuid=f"ALGO-{UUID4().value}",
            submit_type=SubmitType.ADP_MAKER,
            side=side,
            amount=amount,
            duration=duration,
            wait=wait,
            check_interval=check_interval,
            position_side=position_side,
            trigger_tp_ratio=trigger_tp_ratio,
            trigger_sl_ratio=trigger_sl_ratio,
            tp_ratio=tp_ratio,
            sl_ratio=sl_ratio,
            kwargs=kwargs,
        )
        self._ems[order.instrument_id.exchange]._submit_order(order, account_type)
        return order.uuid
    
    def cancel_adp_maker(
        self, symbol: str, uuid: str, account_type: AccountType | None = None
    ) -> str:
        order = OrderSubmit(
            symbol=symbol,
            instrument_id=InstrumentId.from_str(symbol),
            submit_type=SubmitType.CANCEL_ADP_MAKER,
            uuid=uuid,
        )
        self._ems[order.instrument_id.exchange]._submit_order(order, account_type)
        return order.uuid

    def create_twap(
        self,
        symbol: str,
        side: OrderSide,
        amount: Decimal,
        duration: int,
        wait: int,
        check_interval: float = 0.1,
        position_side: PositionSide | None = None,
        account_type: AccountType | None = None,
        **kwargs,
    ) -> str:
        order = OrderSubmit(
            symbol=symbol,
            instrument_id=InstrumentId.from_str(symbol),
            uuid=f"ALGO-{UUID4().value}",
            submit_type=SubmitType.TWAP,
            side=side,
            amount=amount,
            duration=duration,
            wait=wait,
            check_interval=check_interval,
            position_side=position_side,
            kwargs=kwargs,
        )
        self._ems[order.instrument_id.exchange]._submit_order(order, account_type)
        return order.uuid

    def cancel_twap(
        self, symbol: str, uuid: str, account_type: AccountType | None = None
    ) -> str:
        order = OrderSubmit(
            symbol=symbol,
            instrument_id=InstrumentId.from_str(symbol),
            submit_type=SubmitType.CANCEL_TWAP,
            uuid=uuid,
        )
        self._ems[order.instrument_id.exchange]._submit_order(order, account_type)
        return order.uuid

    def subscribe_bookl1(self, symbols: List[str]):
        """
        Subscribe to level 1 book data for the given symbols.

        Args:
            symbols (List[str]): The symbols to subscribe to.
        """
        if not self._initialized:
            raise StrategyBuildError(
                "Strategy not initialized, please use `subscribe_bookl1` in `on_start` method"
            )

        for symbol in symbols:
            self._subscriptions[DataType.BOOKL1].add(symbol)

    def subscribe_trade(self, symbols: List[str]):
        """
        Subscribe to trade data for the given symbols.

        Args:
            symbols (List[str]): The symbols to subscribe to.
        """
        if not self._initialized:
            raise StrategyBuildError(
                "Strategy not initialized, please use `subscribe_trade` in `on_start` method"
            )

        for symbol in symbols:
            self._subscriptions[DataType.TRADE].add(symbol)

    def subscribe_kline(self, symbols: List[str], interval: KlineInterval):
        """
        Subscribe to kline data for the given symbols.

        Args:
            symbols (List[str]): The symbols to subscribe to.
            interval (str): The interval of the kline data
        """
        if not self._initialized:
            raise StrategyBuildError(
                "Strategy not initialized, please use `subscribe_kline` in `on_start` method"
            )

        for symbol in symbols:
            self._subscriptions[DataType.KLINE][symbol] = interval

    def linear_info(
        self, exchange: ExchangeType, base: str | None = None, quote: str | None = None
    ) -> List[str]:
        exchange: ExchangeManager = self._exchanges[exchange]
        return exchange.linear(base, quote)

    def spot_info(
        self, exchange: ExchangeType, base: str | None = None, quote: str | None = None
    ) -> List[str]:
        exchange: ExchangeManager = self._exchanges[exchange]
        return exchange.spot(base, quote)

    def future_info(
        self, exchange: ExchangeType, base: str | None = None, quote: str | None = None
    ) -> List[str]:
        exchange: ExchangeManager = self._exchanges[exchange]
        return exchange.future

    def inverse_info(
        self, exchange: ExchangeType, base: str | None = None, quote: str | None = None
    ) -> List[str]:
        exchange: ExchangeManager = self._exchanges[exchange]
        return exchange.inverse

    def on_start(self):
        pass

    def on_trade(self, trade: Trade):
        pass

    def on_bookl1(self, bookl1: BookL1):
        pass

    def on_kline(self, kline: Kline):
        pass

    def on_pending_order(self, order: Order):
        pass

    def on_accepted_order(self, order: Order):
        pass

    def on_partially_filled_order(self, order: Order):
        pass

    def on_filled_order(self, order: Order):
        pass

    def on_canceling_order(self, order: Order):
        pass

    def on_canceled_order(self, order: Order):
        pass

    def on_failed_order(self, order: Order):
        pass

    def on_cancel_failed_order(self, order: Order):
        pass

    def on_balance(self, balance: AccountBalance):
        pass
