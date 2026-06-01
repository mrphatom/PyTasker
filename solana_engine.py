import os
import json
import logging
import asyncio
import base58
from typing import TypeVar, Callable, Any, Coroutine

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.core import RPCException
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.message import MessageV0
from solders.transaction import VersionedTransaction

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

T = TypeVar("T")

async def _with_retry(
    coro_func: Callable[..., Coroutine[Any, Any, T]], 
    *args, 
    max_retries: int = 3, 
    delay: float = 2.0, 
    **kwargs
) -> T:
    """
    Executes an asynchronous function with exponential backoff retry logic.
    """
    last_exception = None
    for attempt in range(max_retries):
        try:
            return await coro_func(*args, **kwargs)
        except Exception as e:
            last_exception = e
            logger.warning(f"Attempt {attempt + 1}/{max_retries} failed for {coro_func.__name__}: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(delay * (2 ** attempt))
    
    logger.error(f"All {max_retries} attempts failed for {coro_func.__name__}.")
    raise last_exception

def _get_keypair() -> Keypair:
    """
    Securely loads the platform keypair from the environment.
    Supports both JSON byte array and Base58 string formats.
    """
    secret = os.getenv("PLATFORM_SECRET_KEY")
    if not secret:
        raise ValueError("Critical Error: PLATFORM_SECRET_KEY environment variable is missing.")
    
    try:
        secret_bytes = bytes(json.loads(secret))
    except (json.JSONDecodeError, TypeError, ValueError):
        try:
            secret_bytes = base58.b58decode(secret)
        except ValueError as e:
            raise ValueError("PLATFORM_SECRET_KEY must be a valid JSON byte array or Base58 string.") from e
    
    if len(secret_bytes) != 64:
        raise ValueError(f"Invalid secret key length: expected 64 bytes, got {len(secret_bytes)}")
    
    return Keypair.from_bytes(secret_bytes)

def _get_client() -> AsyncClient:
    """
    Initializes the Solana AsyncClient using the configured RPC URL.
    """
    rpc_url = os.getenv("SOLANA_RPC_URL")
    if not rpc_url:
        raise ValueError("Critical Error: SOLANA_RPC_URL environment variable is missing.")
    return AsyncClient(rpc_url, commitment=Confirmed)

def get_master_address() -> str:
    """
    Derives and returns the public wallet address of the platform escrow keypair.
    """
    keypair = _get_keypair()
    return str(keypair.pubkey())

async def verify_deposit(tx_signature: str, expected_sol: float) -> bool:
    """
    Scans the transaction on the network to verify that the specified amount of SOL 
    was successfully transferred to the master wallet address.
    """
    expected_lamports = int(expected_sol * 1_000_000_000)
    master_pubkey = _get_keypair().pubkey()
    client = _get_client()
    
    try:
        try:
            sig = Signature.from_string(tx_signature)
        except Exception as e:
            logger.error(f"Invalid transaction signature format: {tx_signature}")
            return False

        async def fetch_tx():
            resp = await client.get_transaction(sig, max_supported_transaction_version=0)
            if not resp or not resp.value:
                raise RPCException("Transaction not found or not confirmed yet.")
            return resp.value
        
        tx_data = await _with_retry(fetch_tx, max_retries=5, delay=2.0)
        
        if tx_data.transaction.meta.err is not None:
            logger.warning(f"Transaction {tx_signature} failed on-chain: {tx_data.transaction.meta.err}")
            return False
        
        meta = tx_data.transaction.meta
        message = tx_data.transaction.transaction.message
        
        if hasattr(message, 'account_keys'):
            account_keys = message.account_keys
        else:
            logger.error("Unsupported transaction message format.")
            return False
            
        try:
            master_index = account_keys.index(master_pubkey)
        except ValueError:
            logger.warning(f"Master wallet not found in transaction accounts for {tx_signature}.")
            return False
        
        pre_balance = meta.pre_balances[master_index]
        post_balance = meta.post_balances[master_index]
        
        actual_received = post_balance - pre_balance
        if actual_received >= expected_lamports:
            logger.info(f"Deposit verified: {actual_received} lamports received in {tx_signature}.")
            return True
        else:
            logger.warning(f"Insufficient deposit in {tx_signature}. Expected {expected_lamports}, got {actual_received}.")
            return False
            
    except Exception as e:
        logger.error(f"Error verifying deposit {tx_signature}: {e}")
        return False
    finally:
        await client.close()

async def send_payout(destination_wallet_str: str, amount_lamports: int) -> str:
    """
    Builds, signs, and executes a SystemProgram transfer instruction to push lamports 
    from the master keypair to a worker's destination wallet address.
    """
    keypair = _get_keypair()
    client = _get_client()
    
    try:
        try:
            dest_pubkey = Pubkey.from_string(destination_wallet_str)
        except Exception as e:
            raise ValueError(f"Invalid destination wallet address: {destination_wallet_str}") from e
        
        ix = transfer(
            TransferParams(
                from_pubkey=keypair.pubkey(),
                to_pubkey=dest_pubkey,
                lamports=amount_lamports
            )
        )
        
        async def get_blockhash():
            resp = await client.get_latest_blockhash()
            if not resp or not resp.value:
                raise RPCException("Failed to fetch latest blockhash.")
            return resp.value.blockhash
        
        recent_blockhash = await _with_retry(get_blockhash, max_retries=3, delay=1.0)
        
        msg = MessageV0.try_compile(
            payer=keypair.pubkey(),
            instructions=[ix],
            address_lookup_table_accounts=[],
            recent_blockhash=recent_blockhash
        )
        
        tx = VersionedTransaction(msg, [keypair])
        
        async def send_tx():
            resp = await client.send_transaction(tx)
            if not resp or not resp.value:
                raise RPCException("Failed to send transaction.")
            return resp.value
        
        signature = await _with_retry(send_tx, max_retries=3, delay=2.0)
        logger.info(f"Successfully sent {amount_lamports} lamports to {destination_wallet_str}. Signature: {signature}")
        return str(signature)
        
    except Exception as e:
        logger.error(f"Error sending payout to {destination_wallet_str}: {e}")
        raise
    finally:
        await client.close()