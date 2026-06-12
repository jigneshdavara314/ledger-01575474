"""
Places trades — either simulated (PAPER) or real (LIVE).

PAPER: just records the intended trade in the SQLite DB. No money moves.
LIVE : signs and submits a real order via py-clob-client. Needs a funded
       Polygon wallet (USDC) and POLYGON_WALLET_PRIVATE_KEY in .env.
"""
from . import config
from .strategy import Signal
from . import store


class Executor:
    def __init__(self):
        self.mode = config.MODE
        self._client = None
        if self.mode == "LIVE":
            self._client = self._build_live_client()

    def _build_live_client(self):
        """Lazily build an authenticated CLOB client for real trading."""
        if not config.POLYGON_WALLET_PRIVATE_KEY:
            raise RuntimeError(
                "MODE=LIVE but POLYGON_WALLET_PRIVATE_KEY is missing in .env"
            )
        from py_clob_client.client import ClobClient
        from py_clob_client.constants import POLYGON

        client = ClobClient(
            host=config.CLOB_HOST,
            key=config.POLYGON_WALLET_PRIVATE_KEY,
            chain_id=POLYGON,
        )
        # Derive / create API credentials for L2 auth, required to post orders.
        client.set_api_creds(client.create_or_derive_api_creds())
        return client

    def execute(self, signal: Signal) -> dict:
        if self.mode == "LIVE":
            result = self._execute_live(signal)
        else:
            result = self._execute_paper(signal)
        store.record_trade(signal, result)
        return result

    # ------------------------------------------------------------------
    def _execute_paper(self, signal: Signal) -> dict:
        return {
            "mode": "PAPER",
            "status": "simulated",
            "filled_size": signal.size_usd,
            "price": signal.market_prob,
        }

    def _execute_live(self, signal: Signal) -> dict:
        from py_clob_client.clob_types import OrderArgs
        from py_clob_client.order_builder.constants import BUY

        token_id = (
            signal.market.token_id_yes
            if signal.side == "YES"
            else signal.market.token_id_no
        )
        # shares to buy = dollars / price
        shares = round(signal.size_usd / signal.market_prob, 2)

        order_args = OrderArgs(
            token_id=token_id,
            price=signal.market_prob,
            size=shares,
            side=BUY,
        )
        signed = self._client.create_order(order_args)
        resp = self._client.post_order(signed)
        return {
            "mode": "LIVE",
            "status": resp.get("status", "submitted"),
            "order_id": resp.get("orderID"),
            "filled_size": signal.size_usd,
            "price": signal.market_prob,
            "raw": resp,
        }
