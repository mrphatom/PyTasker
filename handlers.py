import os
import logging
from datetime import datetime, timedelta
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ConversationHandler,
)
from solders.pubkey import Pubkey

import database
import solana_engine

logger = logging.getLogger(__name__)

# Conversation States
(
    WAITING_FOR_WALLET,
    WAITING_FOR_AGENCY_NAME,
    WAITING_FOR_DEPOSIT_SIG,
    WAITING_FOR_TASK_UPLOAD,
    WAITING_FOR_AUDIT_ID,
    WAITING_FOR_BROADCAST_MSG,
) = range(6)

def get_admin_ids() -> list[int]:
    try:
        return [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
    except Exception:
        return []

def get_main_menu() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("📋 Start Work", callback_data="user_start_work"),
            InlineKeyboardButton("📆 Daily Check-in", callback_data="user_checkin")
        ],
        [
            InlineKeyboardButton("💰 Cash Out", callback_data="user_withdraw"),
            InlineKeyboardButton("🏆 Leaderboard", callback_data="user_leaderboard")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_agency_menu() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("🏢 Register Agency", callback_data="agency_register")],
        [InlineKeyboardButton("💳 Fund Escrow Pool", callback_data="agency_fund")],
        [InlineKeyboardButton("📤 Bulk Task Upload", callback_data="agency_upload")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_admin_menu() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("🔓 Approve Pending Agencies", callback_data="admin_approve_agencies")],
        [InlineKeyboardButton("🔍 Audit User Profile", callback_data="admin_audit_user")],
        [InlineKeyboardButton("📢 Global Broadcast", callback_data="admin_broadcast")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        user_id = update.effective_user.id
        user = await database.get_user(user_id)
        
        if user and user.get("sol_wallet"):
            await update.message.reply_text(
                "Welcome back to the Dashboard!",
                reply_markup=get_main_menu()
            )
            return ConversationHandler.END
        else:
            await update.message.reply_text(
                "Welcome! To get started, please reply with your Solana wallet address."
            )
            return WAITING_FOR_WALLET
    except Exception as e:
        logger.error(f"Error in cmd_start: {e}")
        await update.message.reply_text("An error occurred. Please try again later.")
        return ConversationHandler.END

async def handle_wallet_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        wallet_str = update.message.text.strip()
        user_id = update.effective_user.id
        
        try:
            Pubkey.from_string(wallet_str)
        except Exception:
            await update.message.reply_text("Invalid Solana wallet address. Please try again.")
            return WAITING_FOR_WALLET
            
        await database.register_user(user_id, wallet_str)
        await update.message.reply_text(
            "Wallet registered successfully! Welcome to the Dashboard.",
            reply_markup=get_main_menu()
        )
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in handle_wallet_setup: {e}")
        await update.message.reply_text("Failed to register wallet. Please try again.")
        return ConversationHandler.END

async def cmd_agency(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        await update.message.reply_text(
            "🏢 Agency Portal\nSelect an option below:",
            reply_markup=get_agency_menu()
        )
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in cmd_agency: {e}")
        return ConversationHandler.END

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        user_id = update.effective_user.id
        if user_id not in get_admin_ids():
            await update.message.reply_text("Unauthorized access.")
            return ConversationHandler.END
            
        await update.message.reply_text(
            "🛡️ Admin Control Panel\nSelect an option below:",
            reply_markup=get_admin_menu()
        )
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in cmd_admin: {e}")
        return ConversationHandler.END

async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    try:
        if data == "user_checkin":
            pool = await database.get_pool()
            async with pool.acquire() as conn:
                user = await conn.fetchrow("SELECT last_check_in, streak_count FROM users WHERE telegram_id = $1", user_id)
                now = datetime.utcnow()
                
                if user and user['last_check_in']:
                    diff = now - user['last_check_in']
                    if diff < timedelta(hours=24):
                        await query.edit_message_text(
                            f"You already checked in recently. Try again in {24 - diff.seconds//3600} hours.",
                            reply_markup=get_main_menu()
                        )
                        return ConversationHandler.END
                    elif diff > timedelta(hours=48):
                        streak = 1
                    else:
                        streak = user['streak_count'] + 1
                else:
                    streak = 1
                
                reward = 1000 * streak
                await conn.execute(
                    "UPDATE users SET last_check_in = $1, streak_count = $2, balance_lamports = balance_lamports + $3, xp = xp + 10 WHERE telegram_id = $4",
                    now, streak, reward, user_id
                )
                
            await query.edit_message_text(
                f"✅ Checked in! Streak: {streak} days.\nReward: {reward} lamports & 10 XP.",
                reply_markup=get_main_menu()
            )
            return ConversationHandler.END

        elif data == "user_start_work":
            task = await database.get_available_task(user_id)
            if not task:
                await query.edit_message_text("No tasks available at the moment. Check back later!", reply_markup=get_main_menu())
                return ConversationHandler.END
                
            keyboard = [
                [
                    InlineKeyboardButton("Option A", callback_data=f"task_ans:{task['task_id']}:A"),
                    InlineKeyboardButton("Option B", callback_data=f"task_ans:{task['task_id']}:B")
                ],
                [
                    InlineKeyboardButton("Option C", callback_data=f"task_ans:{task['task_id']}:C"),
                    InlineKeyboardButton("Option D", callback_data=f"task_ans:{task_id}:D")
                ]
            ]
            await query.edit_message_text(
                f"📋 Task #{task['task_id']}\n\n{task['prompt_text']}\n\nReward: {task['reward_lamports']} lamports",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return ConversationHandler.END

        elif data.startswith("task_ans:"):
            _, task_id_str, answer = data.split(":")
            task_id = int(task_id_str)
            
            pool = await database.get_pool()
            async with pool.acquire() as conn:
                task = await conn.fetchrow("SELECT reward_lamports FROM tasks WHERE task_id = $1", task_id)
                if not task:
                    await query.edit_message_text("Task no longer exists.", reply_markup=get_main_menu())
                    return ConversationHandler.END
                    
                await database.submit_work(task_id, user_id, answer)
                await database.update_balance(user_id, task['reward_lamports'])
                
            await query.edit_message_text(
                f"✅ Answer submitted successfully! You earned {task['reward_lamports']} lamports.",
                reply_markup=get_main_menu()
            )
            return ConversationHandler.END

        elif data == "user_withdraw":
            user = await database.get_user(user_id)
            balance = user['balance_lamports']
            if balance <= 0:
                await query.edit_message_text("Your balance is 0 lamports.", reply_markup=get_main_menu())
                return ConversationHandler.END
                
            await query.edit_message_text("Initiating withdrawal... Please wait.")
            try:
                sig = await solana_engine.send_payout(user['sol_wallet'], balance)
                await database.update_balance(user_id, -balance)
                await query.edit_message_text(
                    f"✅ Withdrawal successful!\nAmount: {balance} lamports\nSignature: `{sig}`",
                    parse_mode="Markdown",
                    reply_markup=get_main_menu()
                )
            except Exception as e:
                logger.error(f"Withdrawal failed for {user_id}: {e}")
                await query.edit_message_text("Withdrawal failed. Please try again later.", reply_markup=get_main_menu())
            return ConversationHandler.END

        elif data == "user_leaderboard":
            pool = await database.get_pool()
            async with pool.acquire() as conn:
                leaders = await conn.fetch("SELECT telegram_id, xp, level FROM users ORDER BY xp DESC LIMIT 5")
            
            msg = "🏆 Top 5 Workers 🏆\n\n"
            for i, l in enumerate(leaders, 1):
                msg += f"{i}. User `{l['telegram_id']}` - Lvl {l['level']} ({l['xp']} XP)\n"
                
            await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=get_main_menu())
            return ConversationHandler.END

        elif data == "agency_register":
            await query.edit_message_text("Please reply with your Company Name:")
            return WAITING_FOR_AGENCY_NAME

        elif data == "agency_fund":
            master_address = solana_engine.get_master_address()
            await query.edit_message_text(
                f"To fund your escrow, send SOL to our master wallet:\n`{master_address}`\n\n"
                "After sending, please reply with the transaction signature.",
                parse_mode="Markdown"
            )
            return WAITING_FOR_DEPOSIT_SIG

        elif data == "agency_upload":
            await query.edit_message_text(
                "Please reply with your task details in the following format:\n"
                "`Prompt Text | Reward in Lamports | Required Consensus`\n\n"
                "Example: `Classify this image | 5000 | 3`",
                parse_mode="Markdown"
            )
            return WAITING_FOR_TASK_UPLOAD

        elif data == "admin_approve_agencies":
            if user_id not in get_admin_ids():
                return ConversationHandler.END
            pool = await database.get_pool()
            async with pool.acquire() as conn:
                pending = await conn.fetch("SELECT agency_id, company_name FROM agencies WHERE status = 'pending' LIMIT 5")
            
            if not pending:
                await query.edit_message_text("No pending agencies.", reply_markup=get_admin_menu())
                return ConversationHandler.END
                
            keyboard = []
            for p in pending:
                keyboard.append([InlineKeyboardButton(f"Approve: {p['company_name']}", callback_data=f"admin_apprv:{p['agency_id']}")])
            keyboard.append([InlineKeyboardButton("Back", callback_data="admin_back")])
            
            await query.edit_message_text("Pending Agencies:", reply_markup=InlineKeyboardMarkup(keyboard))
            return ConversationHandler.END

        elif data.startswith("admin_apprv:"):
            if user_id not in get_admin_ids():
                return ConversationHandler.END
            agency_id = int(data.split(":")[1])
            pool = await database.get_pool()
            async with pool.acquire() as conn:
                await conn.execute("UPDATE agencies SET status = 'approved' WHERE agency_id = $1", agency_id)
            await query.edit_message_text(f"Agency {agency_id} approved.", reply_markup=get_admin_menu())
            return ConversationHandler.END

        elif data == "admin_audit_user":
            if user_id not in get_admin_ids():
                return ConversationHandler.END
            await query.edit_message_text("Reply with the Telegram ID of the user to audit:")
            return WAITING_FOR_AUDIT_ID

        elif data == "admin_broadcast":
            if user_id not in get_admin_ids():
                return ConversationHandler.END
            await query.edit_message_text("Reply with the message you want to broadcast to all users:")
            return WAITING_FOR_BROADCAST_MSG

        elif data == "admin_back":
            await query.edit_message_text("🛡️ Admin Control Panel", reply_markup=get_admin_menu())
            return ConversationHandler.END

    except Exception as e:
        logger.error(f"Error in handle_callbacks: {e}")
        await query.edit_message_text("An error occurred processing your request.")
        return ConversationHandler.END

async def handle_agency_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        company_name = update.message.text.strip()
        user_id = update.effective_user.id
        
        pool = await database.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO agencies (agency_id, company_name, status) 
                VALUES ($1, $2, 'pending') 
                ON CONFLICT (agency_id) DO UPDATE SET company_name = EXCLUDED.company_name
                """,
                user_id, company_name
            )
            
        await update.message.reply_text(
            f"Agency '{company_name}' registered and is pending admin approval.",
            reply_markup=get_agency_menu()
        )
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in handle_agency_name: {e}")
        await update.message.reply_text("Failed to register agency.")
        return ConversationHandler.END

async def handle_deposit_sig(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        sig = update.message.text.strip()
        user_id = update.effective_user.id
        
        await update.message.reply_text("Verifying transaction on-chain... This may take a moment.")
        
        is_valid = await solana_engine.verify_deposit(sig, expected_sol=0.1)
        
        if is_valid:
            pool = await database.get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE agencies SET balance_lamports = balance_lamports + 100000000 WHERE agency_id = $1",
                    user_id
                )
            await update.message.reply_text("✅ Deposit verified and escrow funded!", reply_markup=get_agency_menu())
        else:
            await update.message.reply_text("❌ Deposit verification failed. Ensure the transaction is confirmed and amounts match.", reply_markup=get_agency_menu())
            
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in handle_deposit_sig: {e}")
        await update.message.reply_text("Error verifying deposit.")
        return ConversationHandler.END

async def handle_task_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        text = update.message.text.strip()
        user_id = update.effective_user.id
        
        parts = [p.strip() for p in text.split("|")]
        if len(parts) != 3:
            await update.message.reply_text("Invalid format. Use: Prompt | Reward | Consensus")
            return ConversationHandler.END
            
        prompt, reward_str, consensus_str = parts
        reward = int(reward_str)
        consensus = int(consensus_str)
        
        pool = await database.get_pool()
        async with pool.acquire() as conn:
            agency = await conn.fetchrow("SELECT balance_lamports, status FROM agencies WHERE agency_id = $1", user_id)
            if not agency or agency['status'] != 'approved':
                await update.message.reply_text("Your agency is not approved or not found.")
                return ConversationHandler.END
            if agency['balance_lamports'] < reward:
                await update.message.reply_text("Insufficient escrow balance for this task reward.")
                return ConversationHandler.END
                
            await conn.execute("UPDATE agencies SET balance_lamports = balance_lamports - $1 WHERE agency_id = $2", reward, user_id)
            
        await database.add_task(user_id, prompt, reward, consensus)
        await update.message.reply_text("✅ Task uploaded successfully!", reply_markup=get_agency_menu())
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in handle_task_upload: {e}")
        await update.message.reply_text("Failed to upload task. Check your format and try again.")
        return ConversationHandler.END

async def handle_audit_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        target_id = int(update.message.text.strip())
        user = await database.get_user(target_id)
        
        if not user:
            await update.message.reply_text("User not found.", reply_markup=get_admin_menu())
            return ConversationHandler.END
            
        msg = (
            f"🔍 Audit for `{target_id}`\n"
            f"Wallet: `{user['sol_wallet']}`\n"
            f"Balance: {user['balance_lamports']} lamports\n"
            f"XP: {user['xp']} | Level: {user['level']}\n"
            f"Trust Score: {user['trust_score']}\n"
            f"Streak: {user['streak_count']}\n"
            f"Last Check-in: {user['last_check_in']}"
        )
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=get_admin_menu())
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("Invalid ID format.")
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in handle_audit_id: {e}")
        await update.message.reply_text("Error fetching user data.")
        return ConversationHandler.END

async def handle_broadcast_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        message_text = update.message.text.strip()
        pool = await database.get_pool()
        async with pool.acquire() as conn:
            users = await conn.fetch("SELECT telegram_id FROM users")
            
        success_count = 0
        for u in users:
            try:
                await context.bot.send_message(chat_id=u['telegram_id'], text=f"📢 Broadcast:\n\n{message_text}")
                success_count += 1
            except Exception:
                pass
                
        await update.message.reply_text(f"Broadcast sent to {success_count}/{len(users)} users.", reply_markup=get_admin_menu())
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in handle_broadcast_msg: {e}")
        await update.message.reply_text("Failed to send broadcast.")
        return ConversationHandler.END

def get_core_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            CommandHandler("agency", cmd_agency),
            CommandHandler("admin", cmd_admin),
            CallbackQueryHandler(handle_callbacks)
        ],
        states={
            WAITING_FOR_WALLET: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_wallet_setup)],
            WAITING_FOR_AGENCY_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_agency_name)],
            WAITING_FOR_DEPOSIT_SIG: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_deposit_sig)],
            WAITING_FOR_TASK_UPLOAD: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_task_upload)],
            WAITING_FOR_AUDIT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_audit_id)],
            WAITING_FOR_BROADCAST_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_broadcast_msg)],
        },
        fallbacks=[
            CommandHandler("start", cmd_start),
            CommandHandler("agency", cmd_agency),
            CommandHandler("admin", cmd_admin),
        ],
        per_message=False
    )