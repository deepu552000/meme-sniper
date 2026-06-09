# wallet.py — Solana wallet helpers

import base58
from solders.keypair import Keypair
from solana.rpc.api import Client
import config

_keypair: Keypair | None = None
_client: Client | None = None


def get_keypair() -> Keypair:
    global _keypair
    if _keypair is None:
        raw = base58.b58decode(config.PRIVATE_KEY)
        _keypair = Keypair.from_bytes(raw)
    return _keypair


def get_client() -> Client:
    global _client
    if _client is None:
        _client = Client(config.RPC_URL)
    return _client


def get_pubkey() -> str:
    return str(get_keypair().pubkey())


def get_sol_balance() -> float:
    """Return SOL balance of the wallet."""
    client = get_client()
    pubkey = get_keypair().pubkey()
    resp = client.get_balance(pubkey)
    lamports = resp.value
    return lamports / 1_000_000_000


def get_token_balance(mint: str) -> float:
    """Return token balance for a specific mint."""
    from solders.pubkey import Pubkey
    client = get_client()
    pubkey = get_keypair().pubkey()
    try:
        mint_pk = Pubkey.from_string(mint)
        resp = client.get_token_accounts_by_owner(
            pubkey,
            {"mint": mint_pk},
            {"encoding": "jsonParsed"},
            commitment="confirmed",
        )
        accounts = resp.value
        if not accounts:
            return 0.0
        info = accounts[0].account.data.parsed["info"]["tokenAmount"]
        return float(info["uiAmount"] or 0)
    except Exception:
        return 0.0
