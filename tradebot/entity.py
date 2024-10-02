import sys
import asyncio
import socket
import collections


from pathlib import Path


from decimal import Decimal
from collections import defaultdict
from typing import Literal, Callable, Union, Optional
from typing import Dict, List, Any
from dataclasses import dataclass, fields, asdict


import redis
import orjson
import spdlog as spd


@dataclass
class OrderResponse:
    raw: Dict[str, Any] 
    success: bool
    exchange: str
    id: str
    client_order_id: str
    timestamp: int
    symbol: str
    type: Literal['limit', 'market']
    side: Literal['buy', 'sell']
    status: Literal['new', 'partially_filled', 'filled', 'canceled', 'expired', 'failed']
    price: Optional[float] = None
    average: Optional[float] = None
    last_filled_price: Optional[float] = None
    amount: Optional[Decimal] = None
    filled: Optional[Decimal] = None
    last_filled: Optional[Decimal] = None
    remaining: Optional[Decimal] = None
    fee: Optional[float] = None
    fee_currency: Optional[str] = None
    cost: Optional[float] = None
    last_trade_timestamp: Optional[int] = None
    reduce_only: Optional[bool] = None
    position_side: Optional[str] = None  # 可以为 None，表示现货交易
    time_in_force: Optional[str] = None
    leverage: Optional[int] = None

    def __post_init__(self):
        decimal_fields = ['amount', 'filled', 'last_filled', 'remaining']
        
        for field in decimal_fields:
            if getattr(self, field) is not None:
                setattr(self, field, Decimal(str(getattr(self, field))))
    
    
class EventSystem:
    _listeners: Dict[str, List[Callable]] = {}

    @classmethod
    def on(cls, event: str, callback: Callable):
        if event not in cls._listeners:
            cls._listeners[event] = []
        cls._listeners[event].append(callback)

    @classmethod
    async def emit(cls, event: str, *args: Any, **kwargs: Any):
        if event in cls._listeners:
            for callback in cls._listeners[event]:
                if asyncio.iscoroutinefunction(callback):
                    await callback(*args, **kwargs)
                else:
                    callback(*args, **kwargs)
                    
                    
class RedisPool:
    def __init__(self):
        if self._is_in_docker():
            self.pool = redis.ConnectionPool(host='redis', db=0, password='password')
        else:
            self.pool = redis.ConnectionPool(host='localhost', port=6379, db=0, password='password')
    
    def _is_in_docker(self):
        try:
            socket.gethostbyname('redis')
            return True
        except socket.gaierror:
            return False
    
    def get_client(self) -> redis.Redis:
        return redis.Redis(connection_pool=self.pool)
    
    def close(self):
        self.pool.close()


@dataclass
class Account:
    def __init__(self, user: str, account_type: str, redis_client: redis.Redis):
        self.r = redis_client
        self.account_type = f"{user}:account:{account_type}"
        self.load_account()

    def __setattr__(self, key, value):
        super().__setattr__(key, value)
        if key not in ['r', 'account_type']:
            self.r.hset(f"{self.account_type}", key, str(value))
            
    def __getattr__(self, key):
        if key not in ['r', 'account_type']:
            value = self.r.hget(f"{self.account_type}", key)
            if value is not None:
                return Decimal(value.decode())
            return Decimal('0')
        raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{key}'")

    def __getitem__(self, key):
        return getattr(self, key)

    def __setitem__(self, key, value):
        setattr(self, key, value)

    def keys(self):
        return [k for k in self.__dict__.keys() if k not in ['r', 'account_type']]

    def load_account(self):
        for key, value in self.r.hgetall(f"{self.account_type}").items():
            setattr(self, key.decode(), Decimal(value.decode()))


@dataclass
class Position:
    symbol: str = None
    amount: Decimal = Decimal('0')
    last_price: Decimal = Decimal('0')
    avg_price: Decimal = Decimal('0')
    total_cost: Decimal = Decimal('0')

    def update(self, order_amount: Union[str, float, Decimal], order_price: Union[str, float, Decimal]):
        order_amount = Decimal(str(order_amount))
        order_price = Decimal(str(order_price))
        
        self.total_cost += order_amount * order_price
        self.amount += order_amount
        self.avg_price = self.total_cost / self.amount if self.amount != 0 else Decimal('0')
        self.last_price = order_price

    @classmethod
    def from_dict(cls, data):
        return cls(
            symbol=data['symbol'],
            amount=Decimal(data['amount']),
            last_price=Decimal(data['last_price']),
            avg_price=Decimal(data['avg_price']),
            total_cost=Decimal(data['total_cost'])
        )

    def to_dict(self):
        return {
            'symbol': self.symbol,
            'amount': str(self.amount),
            'last_price': str(self.last_price),
            'avg_price': str(self.avg_price),
            'total_cost': str(self.total_cost)
        }


class PositionDict:
    def __init__(self, user: str, redis_client: redis.Redis):
        self.user = user
        self.r = redis_client
        self.key = f"{user}:position"

    def __getitem__(self, symbol: str) -> Position:
        data = self.r.hget(self.key, symbol)
        if data:
            return Position.from_dict(orjson.loads(data))
        return Position(symbol=symbol)

    def __setitem__(self, symbol: str, position: Position):
        self.r.hset(self.key, symbol, orjson.dumps(position.to_dict()))

    def __delitem__(self, symbol: str):
        self.r.hdel(self.key, symbol)

    def __contains__(self, symbol: str):
        return self.r.hexists(self.key, symbol)

    def __iter__(self):
        return iter([k.decode('utf-8') for k in self.r.hkeys(self.key)])

    def update(self, symbol: str, order_amount: Union[str, float, Decimal], order_price: Union[str, float, Decimal]):
        position = self[symbol]
        position.update(order_amount, order_price)
        
        if abs(position.amount) <= Decimal('1e-8'):
            del self[symbol]
        else:
            self[symbol] = position

    def items(self) -> Dict[str, Position]:
        all_positions = self.r.hgetall(self.key)
        return {k.decode(): Position.from_dict(orjson.loads(v)) for k, v in all_positions.items()}

    def __repr__(self):
        return repr(self.items())

    @property
    def symbols(self):
        return list(self)


class Context:
    def __init__(self, user: str, redis_client: redis.Redis):
        self._redis_client = redis_client
        self._user = user
        self.portfolio_account = Account(user, 'portfolio', self._redis_client) # Portfolio-margin account    
        self.position = PositionDict(user, self._redis_client)

    def __setattr__(self, name, value):
        if name in ['_redis_client', '_user', 'portfolio_account', 'position']:
            super().__setattr__(name, value)
        else:
            self._redis_client.set(f"context:{self._user}:{name}", orjson.dumps(value))

    def __getattr__(self, name):
        if name in ['_redis_client', '_user', 'portfolio_account', 'position']:
            return super().__getattr__(name)
        
        value = self._redis_client.get(f"context:{self._user}:{name}")
        if value is not None:
            return orjson.loads(value)
        raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{name}'")

    def clear(self, name: str = None):
        if name is None:
            for key in self._redis_client.keys(f"context:{self._user}:*"):
                self._redis_client.delete(key)
        else:
            self._redis_client.delete(f"context:{self._user}:{name}")
            

class RollingMedian:
    def __init__(self, n=10):
        self.n = n
        self.data = collections.deque(maxlen=n)

    def input(self, value):
        # if value not in self.data:
        self.data.append(value)

        if len(self.data) == self.n:
            return self.get_median()
        return 0

    def get_median(self):
        sorted_data = sorted(self.data)
        mid = len(sorted_data) // 2

        if len(sorted_data) % 2 == 0:
            return (sorted_data[mid - 1] + sorted_data[mid]) / 2.0
        else:
            return sorted_data[mid]


class RollingDiffSum:
    def __init__(self, n):
        self.n = n
        self.prev_price = None
        self.curr_price = None
        self.diffs = collections.deque(maxlen=n)

    def input(self, price):
        if self.curr_price is not None:
            diff = price - self.curr_price
            self.diffs.append(diff)

        self.prev_price = self.curr_price
        self.curr_price = price

        if len(self.diffs) == self.n:
            rolling_sum = sum(self.diffs)
            return rolling_sum
        else:
            return 0
        
        
@dataclass(slots=True)
class Quote:
    _ask: str = '0'
    _bid: str = '0'
    _ask_vol: str = '0'
    _bid_vol: str = '0'
    
    @property
    def ask(self) -> Decimal:
        return Decimal(self._ask)

    @ask.setter
    def ask(self, value: str):
        self._ask = value

    @property
    def bid(self) -> Decimal:
        return Decimal(self._bid)

    @bid.setter
    def bid(self, value: str):
        self._bid = value

    @property
    def ask_vol(self) -> Decimal:
        return Decimal(self._ask_vol)

    @ask_vol.setter
    def ask_vol(self, value: str):
        self._ask_vol = value

    @property
    def bid_vol(self) -> Decimal:
        return Decimal(self._bid_vol)

    @bid_vol.setter
    def bid_vol(self, value: str):
        self._bid_vol = value
    


class MarketDataStore:
    def __init__(self):
        self.quote = defaultdict(Quote)
    
    def update(self, symbol: str, ask: str, bid: str, ask_vol: str, bid_vol: str):
        self.quote[symbol] = Quote(
            ask=ask,
            bid=bid,
            ask_vol=ask_vol,
            bid_vol=bid_vol
        )


class LogRegister:
    """
    1. spdlog.DailyLogger(name: str, filename: str, multithreaded: bool = False, hour: int = 0, minute: int = 0)
    2. spdlog.DailyLogger(name: str, filename: str, multithreaded: bool = False, hour: int = 0, minute: int = 0, async_mode: bool)
    """
    def __init__(self, log_dir=".logs"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.loggers = {}
        
        self.setup_error_handling()
    
    def setup_error_handling(self):
        self.error_logger = self.get_logger('error', level='ERROR', flush=True)

        def handle_exception(exc_type, exc_value, exc_traceback):
            if issubclass(exc_type, KeyboardInterrupt):
                sys.__excepthook__(exc_type, exc_value, exc_traceback)
                return
            self.error_logger.error(str(exc_value))
            self.error_logger.error("Traceback:", exc_info=(exc_type, exc_value, exc_traceback))

        sys.excepthook = handle_exception

        def handle_async_exception(loop, async_context):
            msg = async_context.get("exception", async_context["message"])
            self.error_logger.error(f"Caught async exception: {msg}")
            if "exception" in async_context:
                exception = async_context["exception"]
                self.error_logger.error("Traceback:", exc_info=(type(exception), exception, exception.__traceback__))
            else:
                self.error_logger.error(f"Context: {async_context}")

        asyncio.get_event_loop().set_exception_handler(handle_async_exception)

    def get_logger(self, name, level: Literal['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'] = 'INFO', flush: bool = False):
        if name not in self.loggers:
            logger_instance = spd.DailyLogger(name=name, filename=str(self.log_dir / f"{name}.log"), hour=0, minute=0, async_mode=True)
            logger_instance.set_level(self.parse_level(level))
            if flush:
                logger_instance.flush_on(self.parse_level(level))
            self.loggers[name] = logger_instance
        return self.loggers[name]
    
    def parse_level(self, level: Literal['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']):
        levels = {
            'DEBUG': spd.LogLevel.DEBUG,
            'INFO': spd.LogLevel.INFO,
            'WARNING': spd.LogLevel.WARN,
            'ERROR': spd.LogLevel.ERR,
            'CRITICAL': spd.LogLevel.CRITICAL
        }
        return levels[level]



redis_pool = RedisPool()
log_register = LogRegister()
market = MarketDataStore()
