import asyncio
from tradebot.types import Trade
from tradebot.constants import WSType
from tradebot.strategy import Strategy
from tradebot.exchange.okx import OkxAccountType, OkxExchangeManager
from tradebot.exchange.okx.connector import OkxPublicConnector


class Demo(Strategy):
    def __init__(self):
        super().__init__()
        self.market = {}

    def on_trade(self, trade: Trade):
        self.market[trade.symbol] = trade

    def on_tick(self, tick):
        spot = self.market.get("BTC/USDT", None)
        linear = self.market.get("BTC/USDT:USDT", None)
        if spot and linear:
            ratio = linear.price / spot.price - 1
            print(f"ratio: {ratio}")


async def main():
    try:
        exchange = OkxExchangeManager({"exchange_id": "okx"})
        await exchange.load_markets()  # get `market` and `market_id` data

        okx_conn = OkxPublicConnector(
            OkxAccountType.DEMO,
            exchange.market,
            exchange.market_id,
        )

        demo = Demo()
        demo.add_ws_manager(WSType.OKX_DEMO, okx_conn)

        await demo.subscribe_trade(WSType.OKX_DEMO, "BTC/USDT")
        await demo.subscribe_trade(WSType.OKX_DEMO, "BTC/USDT:USDT")

        await demo.run()

    except asyncio.CancelledError:
        await exchange.close()
        okx_conn.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
