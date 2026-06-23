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

        # Only record and charge the bankroll for what ACTUALLY filled. In LIVE,
        # an unfilled / partially-filled limit must not be booked as a full bet
        # (the previous code did this, so the ledger diverged from the wallet).
        filled = float(result.get("filled_size", 0.0) or 0.0)
        if filled <= 0:
            result["recorded"] = False
            return result

        # Record the position at the REAL filled size/price.
        rec_signal = signal
        if filled < signal.size_usd - 1e-6:
            # partial fill — record only the filled portion
            from dataclasses import replace as _replace
            try:
                rec_signal = _replace(signal, size_usd=round(filled, 2))
            except Exception:
                pass
        store.record_trade(rec_signal, result)
        try:
            from . import bankroll
            bankroll.deduct_stake(
                filled,
                note=f"{signal.side} {signal.market.question[:40]}",
            )
        except Exception as e:
            # Do NOT silently swallow: a failed deduction after the trade is
            # booked desyncs the ledger. Surface it loudly so it's visible in the
            # run log / CI instead of quietly corrupting the bankroll.
            print(f"[executor] WARNING: bankroll.deduct_stake failed after booking "
                  f"trade (${filled} {signal.side}): {e} — LEDGER MAY BE OUT OF SYNC")
            result["bankroll_error"] = str(e)
        result["recorded"] = True
        return result

    # ------------------------------------------------------------------
    def _execute_paper(self, signal: Signal) -> dict:
        # Paper fills are NOT free money: a resting limit BELOW the ask only fills
        # when a seller crosses down to us, and in practice that fill lands at a
        # WORSE price than our bid (adverse selection). The honest, conservative
        # model: if the signal carries the live ASK, assume we pay the ASK (the
        # realistic price a marketable-ish limit actually clears at) rather than the
        # optimistic bid. Fall back to bid + PAPER_SLIPPAGE only when no book
        # context is available. We never assume a fill BETTER than our own bid.
        bid = signal.market_prob
        slip = getattr(config, "PAPER_SLIPPAGE", 0.0)
        ask = getattr(signal, "ask_price", None)
        fill_price = ask if (ask and ask >= bid) else (bid + slip)
        fill_price = min(0.999, round(max(bid, fill_price), 4))
        return {
            "mode": "PAPER",
            "status": "simulated",
            "filled_size": signal.size_usd,
            "price": fill_price,
        }

    def _execute_live(self, signal: Signal) -> dict:
        """
        Place a REAL limit order, then poll its status and report the ACTUAL
        matched size/price — never assume a full fill. Unfilled limits are
        cancelled so they don't rest and fill later unexpectedly. A pre-check
        guards against ordering more than the live USDC balance allows.
        """
        price = signal.market_prob

        # --- LIVE SAFETY RAILS (fail closed; block real money on any breach) ---
        # Checked FIRST — before importing the trading library or touching the
        # client — so the kill-switch works even if py_clob_client is unavailable.
        # 0) emergency kill-switch: blocks ALL real orders instantly.
        if getattr(config, "LIVE_KILL_SWITCH", False):
            return {"mode": "LIVE", "status": "blocked_kill_switch",
                    "filled_size": 0.0, "price": price, "order_id": None}
        # 1) per-bet hard cap (absolute USDC ceiling for the trial).
        max_bet = getattr(config, "LIVE_MAX_BET_USD", 0.0)
        if max_bet and signal.size_usd > max_bet + 1e-9:
            return {"mode": "LIVE", "status": f"blocked_over_bet_cap_{max_bet:g}",
                    "filled_size": 0.0, "price": price, "order_id": None}
        # 2) daily spend cap: today's real stake + this bet must stay under the cap.
        max_daily = getattr(config, "LIVE_MAX_DAILY_USD", 0.0)
        if max_daily:
            try:
                spent = store.live_spend_today()
            except Exception:
                spent = max_daily   # fail closed: can't verify -> assume capped
            if spent + signal.size_usd > max_daily + 1e-9:
                return {"mode": "LIVE",
                        "status": f"blocked_daily_cap_{max_daily:g}_spent_{spent:g}",
                        "filled_size": 0.0, "price": price, "order_id": None}

        from py_clob_client.clob_types import OrderArgs
        from py_clob_client.order_builder.constants import BUY

        token_id = (
            signal.market.token_id_yes
            if signal.side == "YES"
            else signal.market.token_id_no
        )
        shares = round(signal.size_usd / price, 2) if price else 0.0

        # --- affordability check against REAL on-chain USDC (not just SQLite) ---
        if not self._has_funds(signal.size_usd):
            return {"mode": "LIVE", "status": "insufficient_usdc",
                    "filled_size": 0.0, "price": price, "order_id": None}

        order_args = OrderArgs(token_id=token_id, price=price, size=shares, side=BUY)
        try:
            signed = self._client.create_order(order_args)
            resp = self._client.post_order(signed)
        except Exception as e:
            return {"mode": "LIVE", "status": f"post_failed:{e}",
                    "filled_size": 0.0, "price": price, "order_id": None}

        order_id = resp.get("orderID") or resp.get("order_id")
        # Read the ACTUAL matched size from the order status.
        filled_shares, avg_price = self._read_fill(order_id, fallback_price=price)
        # Cancel any unfilled remainder so it doesn't rest on the book.
        if order_id and filled_shares < shares - 1e-6:
            try:
                self._client.cancel(order_id)
            except Exception:
                pass

        filled_usd = round(filled_shares * (avg_price or price), 2)
        return {
            "mode": "LIVE",
            "status": resp.get("status", "submitted"),
            "order_id": order_id,
            "filled_size": filled_usd,            # REAL filled dollars (may be 0)
            "filled_shares": filled_shares,
            "price": avg_price or price,
            "raw": resp,
        }

    def _read_fill(self, order_id, fallback_price):
        """Return (matched_shares, avg_price) for an order, polling briefly."""
        if not order_id:
            return 0.0, fallback_price
        import time as _t
        for _ in range(5):
            try:
                od = self._client.get_order(order_id)
            except Exception:
                od = None
            if od:
                matched = float(od.get("size_matched", 0) or 0)
                if matched > 0:
                    avg = float(od.get("price", fallback_price) or fallback_price)
                    # if the order is fully resolved, stop early
                    if od.get("status") in ("matched", "filled", "complete"):
                        return matched, avg
                # not matched yet; brief wait then re-check
            _t.sleep(0.5)
        # final read
        try:
            od = self._client.get_order(order_id)
            matched = float(od.get("size_matched", 0) or 0)
            avg = float(od.get("price", fallback_price) or fallback_price)
            return matched, avg
        except Exception:
            return 0.0, fallback_price

    def _has_funds(self, usd: float) -> bool:
        """Check real USDC collateral before ordering. FAIL-CLOSED: if we cannot
        read the on-chain balance, we DON'T submit (refusing to risk real money
        on an unknown balance is safer than failing open)."""
        try:
            bal = self._client.get_balance_allowance()
            # py-clob-client returns balances in USDC base units (6 decimals)
            avail = float(bal.get("balance", 0)) / 1e6
            return avail >= usd
        except Exception:
            return False  # fail-closed: no confirmed funds -> no order
