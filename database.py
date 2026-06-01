import os
import logging
import asyncpg
from typing import Optional

# Configure logging for production observability
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Global connection pool
_pool: Optional[asyncpg.Pool] = None

async def get_pool() -> asyncpg.Pool:
    """
    Initializes and returns the asyncpg connection pool.
    Ensures a singleton pool is used across the application.
    """
    global _pool
    if _pool is None:
        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            raise ValueError("Critical Error: DATABASE_URL environment variable is not set.")
        
        try:
            # Render managed PostgreSQL typically requires SSL for external connections.
            # The sslmode can be configured via the DATABASE_URL DSN.
            _pool = await asyncpg.create_pool(
                dsn=db_url,
                min_size=2,
                max_size=20,
                command_timeout=60,
                server_settings={'application_name': 'web3_tg_bot'}
            )
            logger.info("PostgreSQL connection pool created successfully.")
        except Exception as e:
            logger.error(f"Failed to initialize database pool: {e}")
            raise
    return _pool

async def init_db() -> None:
    """
    Connects to the database and initializes required tables atomically.
    """
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        telegram_id BIGINT PRIMARY KEY,
                        sol_wallet TEXT,
                        xp INT DEFAULT 0,
                        level INT DEFAULT 1,
                        balance_lamports BIGINT DEFAULT 0,
                        trust_score INT DEFAULT 100,
                        streak_count INT DEFAULT 0,
                        last_check_in TIMESTAMP
                    );
                """)
                
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS agencies (
                        agency_id BIGINT PRIMARY KEY,
                        company_name TEXT,
                        balance_lamports BIGINT DEFAULT 0,
                        api_key TEXT,
                        status TEXT DEFAULT 'pending'
                    );
                """)
                
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS tasks (
                        task_id SERIAL PRIMARY KEY,
                        owner_id BIGINT,
                        prompt_text TEXT,
                        reward_lamports BIGINT,
                        required_consensus INT,
                        status TEXT DEFAULT 'active'
                    );
                """)
                
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS submissions (
                        submission_id SERIAL PRIMARY KEY,
                        task_id INT REFERENCES tasks(task_id) ON DELETE CASCADE,
                        user_id BIGINT REFERENCES users(telegram_id) ON DELETE CASCADE,
                        user_answer TEXT,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS payouts (
                        payout_id SERIAL PRIMARY KEY,
                        user_id BIGINT REFERENCES users(telegram_id) ON DELETE CASCADE,
                        destination_wallet TEXT,
                        amount_lamports BIGINT,
                        tx_signature TEXT,
                        status TEXT
                    );
                """)
        logger.info("Database schema initialized successfully.")
    except Exception as e:
        logger.error(f"Error initializing database schema: {e}")
        raise

async def register_user(telegram_id: int, wallet: str) -> Optional[asyncpg.Record]:
    """
    Registers a new user or updates their Solana wallet if they already exist.
    """
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            query = """
                INSERT INTO users (telegram_id, sol_wallet)
                VALUES ($1, $2)
                ON CONFLICT (telegram_id) DO UPDATE
                SET sol_wallet = EXCLUDED.sol_wallet
                RETURNING *;
            """
            return await conn.fetchrow(query, telegram_id, wallet)
    except Exception as e:
        logger.error(f"Error registering user {telegram_id}: {e}")
        raise

async def get_user(telegram_id: int) -> Optional[asyncpg.Record]:
    """
    Retrieves a user's record by their Telegram ID.
    """
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            query = "SELECT * FROM users WHERE telegram_id = $1;"
            return await conn.fetchrow(query, telegram_id)
    except Exception as e:
        logger.error(f"Error fetching user {telegram_id}: {e}")
        raise

async def update_balance(telegram_id: int, amount_lamports: int) -> Optional[int]:
    """
    Atomically updates a user's balance (supports both positive and negative amounts).
    Returns the new balance.
    """
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            query = """
                UPDATE users
                SET balance_lamports = balance_lamports + $2
                WHERE telegram_id = $1
                RETURNING balance_lamports;
            """
            return await conn.fetchval(query, telegram_id, amount_lamports)
    except Exception as e:
        logger.error(f"Error updating balance for user {telegram_id}: {e}")
        raise

async def add_task(owner_id: int, prompt_text: str, reward_lamports: int, required_consensus: int) -> Optional[int]:
    """
    Creates a new task and returns the generated task_id.
    """
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            query = """
                INSERT INTO tasks (owner_id, prompt_text, reward_lamports, required_consensus)
                VALUES ($1, $2, $3, $4)
                RETURNING task_id;
            """
            return await conn.fetchval(query, owner_id, prompt_text, reward_lamports, required_consensus)
    except Exception as e:
        logger.error(f"Error adding task for owner {owner_id}: {e}")
        raise

async def get_available_task(user_id: int) -> Optional[asyncpg.Record]:
    """
    Fetches an active task that the user has not yet submitted an answer for.
    """
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            query = """
                SELECT * FROM tasks
                WHERE status = 'active'
                  AND task_id NOT IN (
                      SELECT task_id FROM submissions WHERE user_id = $1
                  )
                ORDER BY task_id ASC
                LIMIT 1;
            """
            return await conn.fetchrow(query, user_id)
    except Exception as e:
        logger.error(f"Error fetching available task for user {user_id}: {e}")
        raise

async def submit_work(task_id: int, user_id: int, user_answer: str) -> Optional[int]:
    """
    Records a user's submission for a task atomically.
    Returns the generated submission_id.
    """
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                query = """
                    INSERT INTO submissions (task_id, user_id, user_answer)
                    VALUES ($1, $2, $3)
                    RETURNING submission_id;
                """
                return await conn.fetchval(query, task_id, user_id, user_answer)
    except Exception as e:
        logger.error(f"Error submitting work for task {task_id} by user {user_id}: {e}")
        raise

async def close_db() -> None:
    """
    Gracefully closes the database connection pool.
    """
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("Database connection pool closed.")