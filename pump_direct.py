# pump_direct.py — Direct on-chain sell for pump.fun bonding curve tokens
#
# Pump.fun's Feb 2026 cashback upgrade added a required `bonding_curve_v2` PDA
# to all bonding curve instructions. PumpPortal and Jupiter haven't updated for
# this, so they both fail with Custom(6024). This module builds the sell tx
# directly so bonding curve tokens can be sold without any third-party API.
#
# Reference: https://allenhark.com/blog/pumpfun-bonding-curve-custom-6024-overflow-fix-cashback-upgrade-guide

import struct
import requests
import config
import wallet as w

# ─── Program addresses ────────────────────────────────────────────────────────

PUMP_PROGRAM    = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
FEE_PROGRAM     = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
SPL_TOKEN       = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022      = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
SYSTEM_PROGRAM  = "11111111111111111111111111111111"
ATA_PROGRAM     = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"

# fee_config seed key (same for buy AND sell on bonding curve program)
FEE_CONFIG_KEY = bytes([
    1, 86, 224, 246, 147, 102, 90, 207, 68, 219, 21, 104, 191, 23, 91, 170,
    81, 137, 203, 151, 245, 210, 255, 59, 101, 93, 43, 182, 253, 109, 24, 176,
])

# sell discriminator (Anchor method id for sell instruction)
SELL_DISCRIMINATOR = bytes([51, 230, 133, 164, 1, 127, 131, 173])


def sell_bonding_curve(mint: str, amount: float, decimals: int) -> dict:
    """
    Sell tokens directly on pump.fun bonding curve, bypassing PumpPortal/Jupiter.
    Handles both cashback and non-cashback tokens correctly.

    Args:
        mint:     Token mint address
        amount:   Human-readable token amount (e.g. 51880.29)
        decimals: Token decimals (from _get_token_decimals)

    Returns: {"success": bool, "tx": str, "sol_received": float, "error": str}
    """
    from solders.pubkey import Pubkey
    from solders.instruction import AccountMeta, Instruction
    from solders.transaction import VersionedTransaction
    from solders.message import MessageV0
    from solders.hash import Hash

    try:
        client  = w.get_client()
        keypair = w.get_keypair()
        user_pk = Pubkey.from_string(w.get_pubkey())

        pump_pk    = Pubkey.from_string(PUMP_PROGRAM)
        fee_prog   = Pubkey.from_string(FEE_PROGRAM)
        spl_pk     = Pubkey.from_string(SPL_TOKEN)
        t22_pk     = Pubkey.from_string(TOKEN_2022)
        sys_pk     = Pubkey.from_string(SYSTEM_PROGRAM)
        mint_pk    = Pubkey.from_string(mint)

        # ── Derive PDAs ───────────────────────────────────────────────────────

        global_pda, _       = Pubkey.find_program_address([b"global"], pump_pk)
        bonding_curve, _    = Pubkey.find_program_address([b"bonding-curve", bytes(mint_pk)], pump_pk)
        bonding_curve_v2, _ = Pubkey.find_program_address([b"bonding-curve-v2", bytes(mint_pk)], pump_pk)
        event_authority, _  = Pubkey.find_program_address([b"__event_authority"], pump_pk)
        fee_config, _       = Pubkey.find_program_address([b"fee_config", FEE_CONFIG_KEY], fee_prog)

        # ── Read bonding curve account ────────────────────────────────────────

        bc_info = client.get_account_info(bonding_curve)
        if not bc_info.value:
            return {"success": False, "tx": "", "sol_received": 0,
                    "error": "Bonding curve account not found — token may already be graduated"}

        bc_data       = bc_info.value.data
        cashback      = len(bc_data) > 82 and bc_data[82] != 0
        print(f"[pump_direct] cashback_enabled={cashback}")

        # Creator is at bytes 49–80 in bonding curve data
        if len(bc_data) < 81:
            return {"success": False, "tx": "", "sol_received": 0,
                    "error": "Bonding curve data too short — unexpected format"}
        creator_pk = Pubkey.from_bytes(bc_data[49:81])

        # ── Determine token program ───────────────────────────────────────────

        mint_info = client.get_account_info(mint_pk)
        if not mint_info.value:
            return {"success": False, "tx": "", "sol_received": 0, "error": "Mint not found"}
        token_program = t22_pk if str(mint_info.value.owner) == TOKEN_2022 else spl_pk

        # ── Derive ATAs ───────────────────────────────────────────────────────

        def find_ata(owner: Pubkey, mint: Pubkey, token_prog: Pubkey) -> Pubkey:
            ata_prog = Pubkey.from_string(ATA_PROGRAM)
            pda, _   = Pubkey.find_program_address(
                [bytes(owner), bytes(token_prog), bytes(mint)],
                ata_prog
            )
            return pda

        assoc_bonding_curve = find_ata(bonding_curve, mint_pk, token_program)
        assoc_user          = find_ata(user_pk, mint_pk, token_program)

        creator_vault, _ = Pubkey.find_program_address(
            [b"creator-vault", bytes(creator_pk)], pump_pk
        )

        # ── User volume accumulator (cashback only) ───────────────────────────

        user_vol_acc, _ = Pubkey.find_program_address(
            [b"user_volume_accumulator", bytes(user_pk)], pump_pk
        )

        # ── Build instruction data ────────────────────────────────────────────

        amount_raw  = int(amount * (10 ** decimals))
        min_sol_out = 0  # accept any amount (slippage fully open for direct sell)

        ix_data  = SELL_DISCRIMINATOR
        ix_data += struct.pack("<Q", amount_raw)   # token amount (u64 LE)
        ix_data += struct.pack("<Q", min_sol_out)  # min SOL out (u64 LE)

        # ── Build account list ────────────────────────────────────────────────
        # Non-cashback: 15 accounts
        # Cashback:     16 accounts (user_vol_acc inserted before bonding_curve_v2)

        def ro(pk):  return AccountMeta(pubkey=pk, is_signer=False, is_writable=False)
        def rw(pk):  return AccountMeta(pubkey=pk, is_signer=False, is_writable=True)
        def sig(pk): return AccountMeta(pubkey=pk, is_signer=True,  is_writable=True)

        accounts = [
            ro(global_pda),           # 0  global
            rw(Pubkey.from_string(_get_fee_recipient())),  # 1  fee_recipient
            ro(mint_pk),              # 2  mint
            rw(bonding_curve),        # 3  bonding_curve
            rw(assoc_bonding_curve),  # 4  associated_bonding_curve
            rw(assoc_user),           # 5  associated_user
            sig(user_pk),             # 6  user
            ro(sys_pk),               # 7  system_program
            rw(creator_vault),        # 8  creator_vault
            ro(token_program),        # 9  token_program
            ro(event_authority),      # 10 event_authority
            ro(pump_pk),              # 11 program
            ro(fee_config),           # 12 fee_config
            ro(fee_prog),             # 13 fee_program
        ]

        if cashback:
            accounts.append(rw(user_vol_acc))   # 14 user_volume_accumulator (cashback only)

        accounts.append(ro(bonding_curve_v2))   # last: bonding_curve_v2 (always)

        # ── Build and send transaction ────────────────────────────────────────

        ix = Instruction(
            program_id=pump_pk,
            accounts=accounts,
            data=bytes(ix_data),
        )

        blockhash_resp = client.get_latest_blockhash()
        blockhash      = blockhash_resp.value.blockhash

        msg = MessageV0.try_compile(
            payer=user_pk,
            instructions=[_compute_budget_ix(), ix],
            address_lookup_table_accounts=[],
            recent_blockhash=blockhash,
        )

        tx      = VersionedTransaction(msg, [keypair])
        resp    = client.send_raw_transaction(bytes(tx))
        tx_hash = str(resp.value)
        print(f"[pump_direct] TX sent: {tx_hash}")

        _wait_confirm(tx_hash)
        return {"success": True, "tx": tx_hash, "sol_received": 0, "error": ""}

    except Exception as e:
        print(f"[pump_direct] Sell error: {e}")
        return {"success": False, "tx": "", "sol_received": 0, "error": str(e)}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_fee_recipient() -> str:
    """Fetch fee_recipient from the global config PDA."""
    try:
        from solders.pubkey import Pubkey
        client   = w.get_client()
        pump_pk  = Pubkey.from_string(PUMP_PROGRAM)
        global_pda, _ = Pubkey.find_program_address([b"global"], pump_pk)
        info = client.get_account_info(global_pda)
        if info.value and len(info.value.data) >= 72:
            # fee_recipient is at bytes 40–72 in global config
            return str(Pubkey.from_bytes(info.value.data[40:72]))
    except Exception as e:
        print(f"[pump_direct] fee_recipient fetch error: {e}")
    # Fallback to known fee recipient
    return "CebN5WGQ4jvEPvsVU4EoHEpgznyQHeQsNkZQHs8CbGqq"


def _compute_budget_ix():
    """Set compute unit price for priority fee."""
    from solders.pubkey import Pubkey
    from solders.instruction import AccountMeta, Instruction
    import struct

    COMPUTE_BUDGET = Pubkey.from_string("ComputeBudget111111111111111111111111111111")
    # SetComputeUnitPrice discriminator = 3, price = 100_000 microlamports
    data = bytes([3]) + struct.pack("<Q", 100_000)
    return Instruction(program_id=COMPUTE_BUDGET, accounts=[], data=data)


def _wait_confirm(tx_hash: str, timeout: int = 30):
    import time
    from solders.signature import Signature
    client = w.get_client()
    sig    = Signature.from_string(tx_hash)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp   = client.get_signature_statuses([sig])
            status = resp.value[0]
            if status and status.confirmation_status:
                print(f"[pump_direct] Confirmed: {status.confirmation_status}")
                return
        except Exception:
            pass
        time.sleep(2)
    print(f"[pump_direct] TX not confirmed in {timeout}s — may still land")
