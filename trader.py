# trader.py — Buy/sell tokens via Jupiter Aggregator + PumpPortal (pump.fun coins)

import time
import base64
import requests
import config
import wallet as w

HEADERS = {"Content-Type": "application/json"}

# Optional sell-failure callback — set this from your bot:
# Call trader.setup_alerts() after importing both modules to wire automatically.
#   import trader; trader.on_sell_failed = my_handler
# Signature: on_sell_failed(mint: str, error: str)
on_sell_failed = None


def setup_alerts():
    """Wire telegram_bot.send_sell_failed_alert as the sell-failure callback.
    Call this once at bot startup after importing both trader and telegram_bot.
    """
    import telegram_bot
    global on_sell_failed
    on_sell_failed = telegram_bot.send_sell_failed_alert
    print("[trader] Sell-failure alerts wired to Telegram.")



def _jupiter_headers() -> dict:
    """Build Jupiter request headers — includes API key if configured."""
    h = {"Accept": "application/json"}
    if hasattr(config, "JUPITER_API_KEY") and config.JUPITER_API_KEY:
        h["x-api-key"] = config.JUPITER_API_KEY
    return h


def get_sol_price_usd() -> float:
    """Get current SOL/USD price from CoinGecko (free, no key)."""
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "solana", "vs_currencies": "usd"},
            timeout=8
        )
        return float(r.json()["solana"]["usd"])
    except Exception:
        return 150.0


def usd_to_lamports(usd: float) -> int:
    """Convert USD amount to lamports."""
    sol_price = get_sol_price_usd()
    sol_amount = usd / sol_price
    return int(sol_amount * 1_000_000_000)


def buy_token(mint: str, usd_amount: float = None) -> dict:
    """
    Buy a token using SOL via Jupiter.
    Returns: {"success": bool, "tx": str, "amount_out": float, "error": str}
    """
    if usd_amount is None:
        usd_amount = config.BUY_AMOUNT_USD

    lamports = usd_to_lamports(usd_amount)
    pubkey   = w.get_pubkey()

    print(f"[trader] Buying ${usd_amount} of {mint} ({lamports} lamports)...")

    quote = _get_quote(
        input_mint=config.SOL_MINT,
        output_mint=mint,
        amount=lamports,
        slippage_bps=config.SLIPPAGE_BPS
    )
    if not quote:
        return {"success": False, "tx": "", "amount_out": 0, "error": "Quote failed"}

    swap_tx = _get_swap_tx(quote, pubkey)
    if not swap_tx:
        return {"success": False, "tx": "", "amount_out": 0, "error": "Swap TX failed"}

    tx_hash = _sign_and_send(swap_tx)
    if not tx_hash:
        return {"success": False, "tx": "", "amount_out": 0, "error": "TX send failed"}

    amount_out = int(quote.get("outAmount", 0))
    return {"success": True, "tx": tx_hash, "amount_out": amount_out, "error": ""}


def sell_token(mint: str, amount: float | None = None) -> dict:
    """
    Sell a token for SOL.
    - pump.fun coins (mint ends with 'pump'): tries PumpPortal trade-local first, falls back to Jupiter.
    - All other coins: Jupiter only.
    On total failure, fires the on_sell_failed callback if set.
    Returns: {"success": bool, "tx": str, "sol_received": float, "error": str}
    """
    if mint.endswith("pump"):
        print(f"[trader] pump.fun coin — checking graduation status...")
        # Bonding curve tokens can't be sold via PumpPortal or Jupiter after Feb 2026 upgrade
        # Fail fast with a clear message rather than burning 4 retry attempts
        bc_status = _get_bonding_curve_status(mint)
        graduated = not bc_status["exists"] or bc_status["complete"]

        if not graduated:
            # Use direct on-chain tx for bonding curve tokens (PumpPortal/Jupiter broken post Feb 2026)
            print(f"[trader] Token on bonding curve — using pump_direct sell...")
            return _sell_bonding_curve_direct(mint, amount)

        print(f"[trader] Token graduated — trying PumpPortal sell...")
        result = _sell_pump_fun(mint, amount)
        if result["success"]:
            return result
        print(f"[trader] PumpPortal failed ({result['error']}) — falling back to Jupiter...")

    result = _sell_jupiter(mint, amount)

    if not result["success"]:
        _fire_sell_failed(mint, result["error"])

    return result


def _fire_sell_failed(mint: str, error: str):
    """Invoke the on_sell_failed callback if registered."""
    if callable(on_sell_failed):
        try:
            on_sell_failed(mint, error)
        except Exception as e:
            print(f"[trader] on_sell_failed callback error: {e}")
    else:
        print(f"[trader] ⚠️  SELL FAILED for {mint} — no callback registered. Error: {error}")


PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

def _get_bonding_curve_status(mint: str) -> dict:
    """
    Read the pump.fun bonding curve account for this mint.
    Returns: {"exists": bool, "complete": bool}
    - exists=False  → token not on pump.fun bonding curve (e.g. already graduated/migrated)
    - complete=True → bonding curve filled, token graduated to PumpSwap AMM
    - complete=False → still on bonding curve (SELLS WILL FAIL — pump.fun Feb 2026 upgrade broke PumpPortal/Jupiter for these)
    Bonding curve layout (post-cashback upgrade, 151 bytes):
      byte[48] = complete (bool)
      byte[82] = cashback_enabled (bool)
    """
    try:
        from solders.pubkey import Pubkey
        client = w.get_client()
        pump_pk = Pubkey.from_string(PUMP_PROGRAM)
        mint_pk = Pubkey.from_string(mint)
        # Bonding curve PDA: seeds = ["bonding-curve", mint]
        bc_pda, _ = Pubkey.find_program_address(
            [b"bonding-curve", bytes(mint_pk)],
            pump_pk
        )
        info = client.get_account_info(bc_pda)
        if not info.value:
            return {"exists": False, "complete": False, "cashback": False}
        data = info.value.data
        complete  = len(data) > 48 and data[48] != 0
        cashback  = len(data) > 82 and data[82] != 0
        return {"exists": True, "complete": complete, "cashback": cashback}
    except Exception as e:
        print(f"[trader] Bonding curve check error: {e}")
        return {"exists": False, "complete": False, "cashback": False}


def is_graduated(mint: str) -> bool:
    """Return True if token has graduated from bonding curve to PumpSwap AMM."""
    status = _get_bonding_curve_status(mint)
    # Not on bonding curve at all, or bonding curve marked complete = graduated
    return not status["exists"] or status["complete"]


def _sell_pump_fun(mint: str, amount: float | None = None) -> dict:
    """
    Sell a pump.fun token using PumpPortal trade-local API.
    Only works for GRADUATED tokens (PumpSwap AMM pool).
    Bonding curve tokens (not yet graduated) cannot be sold via PumpPortal or Jupiter
    due to the Feb 2026 pump.fun cashback upgrade — they require a direct on-chain tx.
    """
    pubkey = w.get_pubkey()

    if amount is None:
        amount = w.get_token_balance(mint)

    if amount <= 0:
        return {"success": False, "tx": "", "sol_received": 0, "error": "Zero balance"}

    # Check graduation status — bonding curve tokens will always fail post-Feb 2026 upgrade
    bc_status = _get_bonding_curve_status(mint)
    graduated = not bc_status["exists"] or bc_status["complete"]
    print(f"[trader] Bonding curve: exists={bc_status['exists']} complete={bc_status['complete']} graduated={graduated}")

    if not graduated:
        return {
            "success": False, "tx": "", "sol_received": 0,
            "error": "Token still on bonding curve — PumpPortal/Jupiter broken for bonding curve tokens after Feb 2026 upgrade. Manual sell required on pump.fun."
        }

    pool = "pump-amm"
    print(f"[trader] Token graduated → PumpPortal pool={pool}")

    try:
        # Token-2022 transfer fee workaround — reduce amount by 1% so the
        # transfer fee deduction doesn't cause 'insufficient funds' on-chain
        amount = amount * 0.99

        print(f"[trader] PumpPortal sell: {amount:.4f} tokens of {mint}...")

        response = requests.post(
            url="https://pumpportal.fun/api/trade-local",
            data={
                "publicKey":        pubkey,
                "action":           "sell",
                "mint":             mint,
                "amount":           amount,
                "denominatedInSol": "false",
                "slippage":         25,
                "priorityFee":      0.00005,
                "pool":             pool,
            },
            timeout=15
        )

        if response.status_code != 200:
            return {"success": False, "tx": "", "sol_received": 0,
                    "error": f"PumpPortal API error: {response.status_code}"}

        # Sign using PumpPortal's exact documented method
        from solders.transaction import VersionedTransaction
        from solders.commitment_config import CommitmentLevel
        from solders.rpc.requests import SendVersionedTransaction
        from solders.rpc.config import RpcSendTransactionConfig

        keypair    = w.get_keypair()
        tx         = VersionedTransaction(VersionedTransaction.from_bytes(response.content).message, [keypair])
        commitment = CommitmentLevel.Confirmed
        cfg        = RpcSendTransactionConfig(preflight_commitment=commitment)
        tx_payload = SendVersionedTransaction(tx, cfg)

        rpc_resp = requests.post(
            url=config.RPC_URL,
            headers={"Content-Type": "application/json"},
            data=tx_payload.to_json(),
            timeout=30
        )

        tx_hash = rpc_resp.json().get("result")
        if not tx_hash:
            err = rpc_resp.json().get("error", "No tx signature returned")
            return {"success": False, "tx": "", "sol_received": 0, "error": str(err)}

        print(f"[trader] PumpPortal TX sent: {tx_hash}")
        _wait_confirm(tx_hash)
        return {"success": True, "tx": tx_hash, "sol_received": 0, "error": ""}

    except Exception as e:
        print(f"[trader] PumpPortal sell error: {e}")
        return {"success": False, "tx": "", "sol_received": 0, "error": str(e)}


def _sell_bonding_curve_direct(mint: str, amount: float | None = None) -> dict:
    """
    Sell a bonding curve token directly on-chain, bypassing PumpPortal/Jupiter.
    Used for pump.fun tokens not yet graduated (post Feb 2026 cashback upgrade).
    Falls back to Jupiter if direct sell fails (e.g. token graduated mid-attempt).
    """
    import pump_direct

    if amount is None:
        amount = w.get_token_balance(mint)

    if amount <= 0:
        return {"success": False, "tx": "", "sol_received": 0, "error": "Zero balance"}

    decimals = _get_token_decimals(mint)
    result   = pump_direct.sell_bonding_curve(mint, amount, decimals)

    if result["success"]:
        return result

    # If direct sell failed, token may have just graduated — try Jupiter as fallback
    print(f"[trader] pump_direct failed ({result['error']}) — checking if graduated, trying Jupiter...")
    return _sell_jupiter(mint, amount)


def _sell_jupiter(mint: str, amount: float | None = None) -> dict:
    """
    Sell a token for SOL via Jupiter Aggregator.
    Retries with escalating slippage. For Token-2022 tokens, forces direct
    routes only to avoid pools that don't support the Token-2022 program.
    """
    pubkey = w.get_pubkey()

    if amount is None:
        amount = w.get_token_balance(mint)

    if amount <= 0:
        return {"success": False, "tx": "", "sol_received": 0, "error": "Zero balance"}

    decimals   = _get_token_decimals(mint)
    amount_raw = int(amount * (10 ** decimals))

    if amount_raw <= 0:
        return {"success": False, "tx": "", "sol_received": 0, "error": "Amount too small"}

    # Check if graduated — bonding curve tokens fail on Jupiter post-Feb 2026 upgrade
    graduated = is_graduated(mint)
    if not graduated:
        print(f"[trader] ⚠️  Token still on bonding curve — Jupiter will fail (0x1788). Skipping.")
        return {"success": False, "tx": "", "sol_received": 0,
                "error": "Token on bonding curve — Jupiter broken for bonding curve post Feb 2026. Manual sell on pump.fun required."}

    # Detect Token-2022 — use direct routes only to avoid pools that don't support
    # Token-2022 transfer fees. This fixes 0x1788 errors on PumpSwap AMM tokens.
    is_token_2022 = False
    try:
        from solders.pubkey import Pubkey
        mint_info = w.get_client().get_account_info(Pubkey.from_string(mint))
        if mint_info.value:
            is_token_2022 = str(mint_info.value.owner) == "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
    except Exception:
        pass
    if is_token_2022:
        print(f"[trader] Token-2022 detected — using direct routes only for sell")

    slippage_ladder = [
        config.SLIPPAGE_BPS,   # attempt 1: configured default (e.g. 25%)
        5000,                  # attempt 2: 50%
        7500,                  # attempt 3: 75%
        9000,                  # attempt 4: 90%
    ]

    last_error = "Unknown error"

    for attempt, slippage_bps in enumerate(slippage_ladder):
        if attempt > 0:
            print(f"[trader] Jupiter sell retry {attempt} with {slippage_bps/100:.0f}% slippage for {mint}...")
            time.sleep(3)

        print(f"[trader] Jupiter selling {amount:.4f} tokens of {mint} (slippage {slippage_bps/100:.0f}%)...")

        quote = _get_quote(
            input_mint=mint,
            output_mint=config.SOL_MINT,
            amount=amount_raw,
            slippage_bps=slippage_bps,
            only_direct_routes=is_token_2022,
        )
        if not quote:
            last_error = "Quote failed"
            continue

        # Log route so we can debug pool issues
        route_labels = [s["swapInfo"]["label"] for s in quote.get("routePlan", [])]
        print(f"[trader] Jupiter route: {route_labels}")

        swap_tx = _get_swap_tx(quote, pubkey)
        if not swap_tx:
            last_error = "Swap TX failed"
            continue

        tx_hash = _sign_and_send(swap_tx)
        if not tx_hash:
            last_error = "TX send failed"
            continue

        sol_out = int(quote.get("outAmount", 0)) / 1_000_000_000
        if attempt > 0:
            print(f"[trader] Jupiter sell succeeded on retry {attempt} at {slippage_bps/100:.0f}% slippage")
        return {"success": True, "tx": tx_hash, "sol_received": sol_out, "error": ""}

    print(f"[trader] Jupiter sell FAILED after {len(slippage_ladder)} attempts: {last_error}")
    return {"success": False, "tx": "", "sol_received": 0, "error": f"All retries failed: {last_error}"}


# ─── Jupiter helpers ──────────────────────────────────────────────────────────

def _get_quote(input_mint, output_mint, amount, slippage_bps,
               only_direct_routes: bool = False) -> dict | None:
    try:
        params = {
            "inputMint":   input_mint,
            "outputMint":  output_mint,
            "amount":      amount,
            "slippageBps": slippage_bps,
        }
        if only_direct_routes:
            params["onlyDirectRoutes"] = "true"

        r = requests.get(
            f"{config.JUPITER_QUOTE}/quote",
            params=params,
            headers=_jupiter_headers(),
            timeout=10
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[trader] Quote error: {e}")
        return None


def _get_swap_tx(quote: dict, user_pubkey: str) -> str | None:
    try:
        h = {**HEADERS, **_jupiter_headers()}
        r = requests.post(
            f"{config.JUPITER_QUOTE}/swap",
            json={
                "quoteResponse":             quote,
                "userPublicKey":             user_pubkey,
                "wrapAndUnwrapSol":          True,
                "dynamicComputeUnitLimit":   True,
                "prioritizationFeeLamports": 50_000,
            },
            headers=h,
            timeout=15
        )
        r.raise_for_status()
        return r.json().get("swapTransaction")
    except Exception as e:
        print(f"[trader] Swap TX error: {e}")
        return None


def _sign_and_send(swap_tx_b64: str) -> str | None:
    """Sign the versioned transaction and send it to the network."""
    from solders.transaction import VersionedTransaction

    client  = w.get_client()
    keypair = w.get_keypair()

    try:
        raw_tx = base64.b64decode(swap_tx_b64)
        tx     = VersionedTransaction.from_bytes(raw_tx)

        # Correct Jupiter v6 signing — preserves any existing signatures
        # (e.g. Jupiter's fee account sig). Do NOT use .populate() as it
        # wipes all other signers and causes custom program error 0x1789.
        signed_tx = VersionedTransaction(tx.message, [keypair])

        resp = client.send_raw_transaction(bytes(signed_tx))
        tx_hash = str(resp.value)
        print(f"[trader] TX sent: {tx_hash}")

        _wait_confirm(tx_hash)
        return tx_hash

    except Exception as e:
        print(f"[trader] Sign/send error: {e}")
        return None


def _wait_confirm(tx_hash: str, timeout: int = 30):
    """Poll for transaction confirmation."""
    client = w.get_client()
    from solders.signature import Signature
    sig = Signature.from_string(tx_hash)

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = client.get_signature_statuses([sig])
            status = resp.value[0]
            if status and status.confirmation_status:
                print(f"[trader] Confirmed: {status.confirmation_status}")
                return
        except Exception:
            pass
        time.sleep(2)
    print(f"[trader] TX not confirmed in {timeout}s — may still land")


def _get_token_decimals(mint: str) -> int:
    """Get token decimals from on-chain data."""
    try:
        from solders.pubkey import Pubkey
        client = w.get_client()
        mint_pk = Pubkey.from_string(mint)
        info = client.get_account_info(mint_pk)
        if info.value:
            data = info.value.data
            if len(data) >= 45:
                return data[44]
    except Exception:
        pass
    return 6
