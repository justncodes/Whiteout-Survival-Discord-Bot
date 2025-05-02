import discord
from discord.ext import commands
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import hashlib
import json
from datetime import datetime
import sqlite3
from discord.ext import tasks
import asyncio
import re
import os
import traceback
import time
import random
import logging
import logging.handlers
from .alliance_member_operations import AllianceSelectView
from .alliance import PaginatedChannelView
from .gift_operationsapi import GiftCodeAPI
from .gift_captchasolver import GiftCaptchaSolver

class GiftOperations(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        
        # Logger Setup
        self.logger = logging.getLogger('gift_ops')
        self.logger.setLevel(logging.INFO)
        log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

        log_dir = 'log'
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        log_file_path = os.path.join(log_dir, 'gift_ops.log')
        self.log_directory = log_dir

        # Rotate after 3MB, keep 3 backup logs
        file_handler = logging.handlers.RotatingFileHandler(
            log_file_path, maxBytes=3*1024*1024, backupCount=3, encoding='utf-8'
        )
        file_handler.setFormatter(log_formatter)
        self.logger.addHandler(file_handler)

        self.logger.info("GiftOperations Cog initializing...")

        if hasattr(bot, 'conn'):
            self.conn = bot.conn
            self.cursor = self.conn.cursor()
        else:
            if not os.path.exists('db'):
                 os.makedirs('db')
            self.conn = sqlite3.connect('db/giftcode.sqlite')
            self.cursor = self.conn.cursor()

        # API Setup
        self.api = GiftCodeAPI(bot)

        # Gift Code Control Table
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS giftcodecontrol (
                alliance_id INTEGER PRIMARY KEY,
                status INTEGER DEFAULT 0
            )
        """)
        self.conn.commit()

        # Settings DB Connection
        if not os.path.exists('db'): os.makedirs('db')
        self.settings_conn = sqlite3.connect('db/settings.sqlite')
        self.settings_cursor = self.settings_conn.cursor()

        # Alliance DB Connection
        if not os.path.exists('db'): os.makedirs('db')
        self.alliance_conn = sqlite3.connect('db/alliance.sqlite')
        self.alliance_cursor = self.alliance_conn.cursor()

        # Gift Code Channel Table
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS giftcode_channel (
                alliance_id INTEGER,
                channel_id INTEGER,
                PRIMARY KEY (alliance_id)
            )
        """)
        self.conn.commit()

        # WOS API URLs and Key
        self.wos_player_info_url = "https://wos-giftcode-api.centurygame.com/api/player"
        self.wos_giftcode_url = "https://wos-giftcode-api.centurygame.com/api/gift_code"
        self.wos_captcha_url = "https://wos-giftcode-api.centurygame.com/api/captcha"
        self.wos_giftcode_redemption_url = "https://wos-giftcode.centurygame.com"
        self.wos_encrypt_key = "tB87#kPtkxqOS2"

        # Retry Configuration for Requests
        self.retry_config = Retry(
            total=10,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["POST", "GET"]
        )

        # Initialization of Locks and Cooldowns
        self.captcha_solver = None
        self._validation_lock = asyncio.Lock()
        self.last_validation_attempt_time = 0
        self.validation_cooldown = 5
        self.test_captcha_cooldowns = {} # User ID: last test timestamp for test button
        self.test_captcha_delay = 60 # Cooldown in seconds for test button per user

        # Captcha Solver Initialization Attempt
        try:
            self.settings_cursor.execute("""
                CREATE TABLE IF NOT EXISTS ocr_settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    enabled INTEGER DEFAULT 1, use_gpu INTEGER DEFAULT 0,
                    gpu_device INTEGER DEFAULT 0,
                    save_images INTEGER DEFAULT 0
                )""")
            self.settings_conn.commit()

            # Load latest OCR settings
            self.settings_cursor.execute("SELECT enabled, use_gpu, gpu_device, save_images FROM ocr_settings ORDER BY id DESC LIMIT 1")
            ocr_settings = self.settings_cursor.fetchone()

            if ocr_settings:
                enabled, use_gpu_setting, gpu_device, save_images = ocr_settings
                if enabled == 1:
                    actual_use_gpu = False
                    try:
                        import torch
                        gpu_available = torch.cuda.is_available()
                        if use_gpu_setting == 1 and gpu_available:
                            actual_use_gpu = True
                            self.logger.info(f"GiftOps __init__: GPU requested and available. Will use device {gpu_device}.")
                        elif use_gpu_setting == 1 and not gpu_available:
                            self.logger.warning("GiftOps __init__: GPU requested but not available. Forcing CPU.")
                        else:
                            self.logger.info("GiftOps __init__: GPU not requested. Using CPU.")

                    except ImportError:
                        self.logger.warning("GiftOps __init__: PyTorch not installed. Cannot use GPU, forcing CPU mode if solver initializes.")
                        actual_use_gpu = False

                    except Exception as torch_err:
                        self.logger.error(f"GiftOps __init__: Error checking torch/GPU: {torch_err}. Forcing CPU mode.")
                        actual_use_gpu = False

                    self.captcha_solver = GiftCaptchaSolver(
                        use_gpu=actual_use_gpu,
                        gpu_device=gpu_device if actual_use_gpu else None,
                        save_images=save_images
                    )
                else:
                    self.logger.info("GiftOps __init__: OCR is disabled in settings.")
            else:
                self.logger.warning("GiftOps __init__: No OCR settings found in DB. OCR will be disabled.")

        except ImportError as lib_err:
            self.logger.exception(f"GiftOps __init__: ERROR - Missing required library for OCR: {lib_err}. Captcha solving disabled.")
            self.captcha_solver = None
        except Exception as e:
            self.logger.exception(f"GiftOps __init__: Unexpected error during Captcha solver setup: {e}")
            self.logger.exception(f"Traceback: {traceback.format_exc()}")
            self.captcha_solver = None

    @commands.Cog.listener()
    async def on_ready(self):
        """
        Handles cog setup when the bot is ready.
        Initializes database tables, loads OCR settings, initializes the captcha solver if enabled,
        validates gift code channels, and starts the background task loop.
        """
        self.logger.info("GiftOps Cog: on_ready triggered.")
        try:
            # OCR Settings Table Setup
            self.logger.info("Setting up ocr_settings table...")
            self.settings_cursor.execute("""
                CREATE TABLE IF NOT EXISTS ocr_settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    enabled INTEGER DEFAULT 1,
                    use_gpu INTEGER DEFAULT 0,
                    gpu_device INTEGER DEFAULT 0,
                    save_images INTEGER DEFAULT 0 -- Default to 0 (None)
                )
            """)
            self.settings_conn.commit()
            self.logger.info("ocr_settings table checked/created.")

            # Initialize Default OCR Settings if Needed
            self.settings_cursor.execute("SELECT COUNT(*) FROM ocr_settings")
            count = self.settings_cursor.fetchone()[0]
            if count == 0:
                self.logger.info("No OCR settings found, inserting defaults...")
                self.settings_cursor.execute("""
                    INSERT INTO ocr_settings (enabled, use_gpu, gpu_device, save_images)
                    VALUES (1, 0, 0, 0) -- Corrected Default: Enabled=1, UseGPU=0, SaveImages=0 (None)
                """)
                self.settings_conn.commit()
                self.logger.info("Default OCR settings inserted.")
            else:
                self.logger.info(f"Found {count} existing OCR settings row(s). Using the latest.")

            # Load OCR Settings and Initialize Solver
            if self.captcha_solver is None:
                self.logger.warning("Captcha solver not initialized in __init__, attempting again in on_ready...")
                self.settings_cursor.execute("SELECT enabled, use_gpu, gpu_device, save_images FROM ocr_settings ORDER BY id DESC LIMIT 1")
                ocr_settings = self.settings_cursor.fetchone()

                if ocr_settings:
                    enabled, use_gpu_setting, gpu_device, save_images_setting = ocr_settings
                    self.logger.info(f"on_ready loaded settings: Enabled={enabled}, UseGPU={use_gpu_setting}, GPUDevice={gpu_device}, SaveImages={save_images_setting}")
                    if enabled == 1:
                        self.logger.info("OCR is enabled, attempting initialization...")
                        actual_use_gpu = False
                        try:
                            import torch
                            gpu_available = torch.cuda.is_available()
                            if use_gpu_setting == 1 and gpu_available:
                                actual_use_gpu = True
                            elif use_gpu_setting == 1 and not gpu_available:
                                 self.logger.warning("GPU requested but not available, forcing CPU.")
                        except ImportError:
                             self.logger.warning("PyTorch not installed, cannot use GPU.")
                             actual_use_gpu = False
                        except Exception as torch_err:
                             self.logger.error(f"Error checking torch/GPU: {torch_err}, forcing CPU.")
                             actual_use_gpu = False

                        try:
                            self.captcha_solver = GiftCaptchaSolver(
                                use_gpu=actual_use_gpu,
                                gpu_device=gpu_device if actual_use_gpu else None,
                                save_images=save_images_setting
                            )
                        except Exception as e:
                            self.logger.exception("Failed to initialize Captcha Solver in on_ready.")
                            self.captcha_solver = None
                    else:
                        self.logger.info("OCR is disabled in settings (checked in on_ready).")
                else:
                    self.logger.warning("Could not load OCR settings from database in on_ready.")
            else:
                 self.logger.info("Captcha solver was already initialized in __init__.")

            # Gift Code Channel Validation
            self.logger.info("Validating gift code channels...")
            self.cursor.execute("SELECT channel_id, alliance_id FROM giftcode_channel")
            channel_configs = self.cursor.fetchall()
            self.logger.info(f"Found {len(channel_configs)} gift code channel configurations in DB.")

            invalid_channels = []
            for channel_id, alliance_id in channel_configs:
                channel = self.bot.get_channel(channel_id)
                if not channel:
                    self.logger.warning(f"Channel ID {channel_id} (Alliance: {alliance_id}) is invalid or bot cannot access it. Marking for removal.")
                    invalid_channels.append(channel_id)
                elif not isinstance(channel, discord.TextChannel):
                     self.logger.warning(f"Channel ID {channel_id} (Alliance: {alliance_id}) is not a Text Channel. Marking for removal.")
                     invalid_channels.append(channel_id)
                elif not channel.permissions_for(channel.guild.me).send_messages:
                    self.logger.warning(f"Missing send message permissions in channel {channel_id}. Functionality may be limited.")

            if invalid_channels:
                unique_invalid_channels = list(set(invalid_channels))
                self.logger.info(f"Removing {len(unique_invalid_channels)} invalid channel configurations from database: {unique_invalid_channels}")
                placeholders = ','.join('?' * len(unique_invalid_channels))
                try:
                    self.cursor.execute(f"DELETE FROM giftcode_channel WHERE channel_id IN ({placeholders})", unique_invalid_channels)
                    self.conn.commit()
                    self.logger.info("Successfully removed invalid channel configurations.")
                except sqlite3.Error as db_err:
                    self.logger.exception(f"DATABASE ERROR removing invalid channels from database: {db_err}")
            else:
                self.logger.info("All configured gift code channels appear valid.")

            # Start Background Task Loop
            if not self.check_channels_loop.is_running():
                self.logger.info("Starting check_channels_loop background task...")
                self.check_channels_loop.start()
                self.logger.info("check_channels_loop started.")
            else:
                self.logger.info("check_channels_loop is already running.")

            self.logger.info("GiftOps Cog: on_ready setup finished successfully.")

        except sqlite3.Error as db_err:
             self.logger.exception(f"DATABASE ERROR during on_ready setup: {db_err}")
        except Exception as e:
            self.logger.exception(f"UNEXPECTED ERROR during on_ready setup: {e}")

    @discord.ext.commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        log_file_path = os.path.join(self.log_directory, 'giftlog.txt')
        try:
            if message.author.bot or not message.guild:
                return

            self.cursor.execute("SELECT alliance_id FROM giftcode_channel WHERE channel_id = ?", (message.channel.id,))
            channel_info = self.cursor.fetchone()
            if not channel_info:
                return

            try:
                self.logger.info(f"GiftOps: [on_message] Running API sync for channel {message.channel.id}")
                await self.api.validate_and_clean_giftcode_file()
                await self.api.sync_with_api()
                self.logger.info(f"GiftOps: [on_message] API sync complete.")
            except Exception as e:
                self.logger.exception(f"Error during API sync triggered by on_message: {str(e)}")

            content = message.content.strip()
            if not content:
                return

            # Extract potential gift code
            giftcode = None
            if len(content.split()) == 1:
                if re.match(r'^[a-zA-Z0-9]+$', content):
                    giftcode = content
            else:
                code_match = re.search(r'Code:\s*(\S+)', content, re.IGNORECASE)
                if code_match:
                    giftcode = code_match.group(1)
            if not giftcode:
                self.logger.debug(f"[on_message] No valid gift code format found in message {message.id}")
                return

            log_timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            self.logger.info(f"{log_timestamp} GiftOps: [on_message] Detected potential code '{giftcode}' in channel {message.channel.id} (Msg ID: {message.id})")

            # Use Lock for Validation 
            current_time = time.time()
            if current_time - self.last_validation_attempt_time < self.validation_cooldown:
                 wait_time = self.validation_cooldown - (current_time - self.last_validation_attempt_time)
                 self.logger.info(f"GiftOps: [on_message] Validation cooldown active. Waiting {wait_time:.1f}s before checking '{giftcode}'.")
                 await asyncio.sleep(wait_time)

            async with self._validation_lock:
                self.last_validation_attempt_time = time.time()
                self.logger.info(f"GiftOps: [on_message] Acquired validation lock for '{giftcode}'.")

                initial_check = await self.claim_giftcode_rewards_wos("244886619", giftcode)
                log_timestamp_after = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                self.logger.info(f"{log_timestamp_after} GiftOps: [on_message] Validation result for '{giftcode}': {initial_check}")

            log_timestamp_release = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            self.logger.info(f"{log_timestamp_release} GiftOps: [on_message] Released validation lock for '{giftcode}'.")

            # Handle Validation Results and Reply/React
            reply_embed = None
            reaction_to_add = None

            if initial_check == "USAGE_LIMIT":
                self.logger.info(f"GiftOps: [on_message] Code '{giftcode}' reached usage limit.")
                reaction_to_add = "❌"
                reply_embed = discord.Embed(title="❌ Gift Code Usage Limit", color=discord.Color.red())
                reply_embed.description=(
                        f"**Gift Code Details**\n━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"👤 **Sender:** {message.author.mention}\n"
                        f"🎁 **Gift Code:** `{giftcode}`\n"
                        f"❌ **Status:** Usage limit has been reached for this code\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n")

            elif initial_check == "TIME_ERROR":
                self.logger.info(f"GiftOps: [on_message] Code '{giftcode}' is expired.")
                reaction_to_add = "❌"
                reply_embed = discord.Embed(title="❌ Gift Code Expired", color=discord.Color.red())
                reply_embed.description=(
                        f"**Gift Code Details**\n━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"👤 **Sender:** {message.author.mention}\n"
                        f"🎁 **Gift Code:** `{giftcode}`\n"
                        f"❌ **Status:** This gift code has expired.\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n")

            elif initial_check == "CDK_NOT_FOUND":
                self.logger.info(f"GiftOps: [on_message] Code '{giftcode}' is invalid.")
                reaction_to_add = "❌"
                reply_embed = discord.Embed(title="❌ Invalid Gift Code", color=discord.Color.red())
                reply_embed.description=(
                        f"**Gift Code Details**\n━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"👤 **Sender:** {message.author.mention}\n"
                        f"🎁 **Gift Code:** `{giftcode}`\n"
                        f"❌ **Status:** This gift code was not found or is incorrect.\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n")

            elif initial_check in ["SUCCESS", "RECEIVED", "SAME TYPE EXCHANGE"]:
                self.logger.info(f"GiftOps: [on_message] Code '{giftcode}' is valid (status: {initial_check}). Checking DB...")
                reaction_to_add = "✅"
                self.cursor.execute("SELECT 1 FROM gift_codes WHERE giftcode = ?", (giftcode,))
                if not self.cursor.fetchone():
                    self.logger.info(f"GiftOps: [on_message] Adding new valid code '{giftcode}' to database.")
                    self.cursor.execute(
                        "INSERT INTO gift_codes (giftcode, date) VALUES (?, ?)",
                        (giftcode, datetime.now().strftime("%Y-%m-%d"))
                    )
                    self.conn.commit()

                    # Attempt to add via API as well
                    try:
                       asyncio.create_task(self.api.add_giftcode(giftcode))
                    except Exception as api_add_err:
                       self.logger.exception(f"GiftOps: [on_message] Error calling api.add_giftcode for '{giftcode}': {api_add_err}")

                    self.cursor.execute("SELECT alliance_id FROM giftcodecontrol WHERE status = 1")
                    auto_alliances = self.cursor.fetchall()
                    if auto_alliances:
                        self.logger.info(f"GiftOps: [on_message] Triggering auto-use for {len(auto_alliances)} alliances for code '{giftcode}'.")
                        for alliance in auto_alliances:
                            asyncio.create_task(self.use_giftcode_for_alliance(alliance[0], giftcode))
                    else:
                        self.logger.info(f"GiftOps: [on_message] No alliances configured for auto-use.")

                    reply_embed = discord.Embed(title="✅ Gift Code Successfully Added", color=discord.Color.green())
                    reply_embed.description=(
                        f"**Gift Code Details**\n━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"👤 **Sender:** {message.author.mention}\n"
                        f"🎁 **Gift Code:** `{giftcode}`\n"
                        f"📝 **Status:** Added to database and processing started.\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n")
                else:
                    self.logger.info(f"GiftOps: [on_message] Valid code '{giftcode}' already exists in database.")
                    reply_embed = discord.Embed(title="ℹ️ Gift Code Already Known", color=discord.Color.blue())
                    reply_embed.description=(
                            f"**Gift Code Details**\n━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"👤 **Sender:** {message.author.mention}\n"
                            f"🎁 **Gift Code:** `{giftcode}`\n"
                            f"📝 **Status:** Already in database.\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n")

            elif initial_check == "TIMEOUT_RETRY":
                self.logger.info(f"GiftOps: [on_message] Validation for '{giftcode}' hit rate limit (TIMEOUT_RETRY). Message reaction added.")
                reaction_to_add = "⏳"

            elif initial_check in ["OCR_DISABLED", "SOLVER_ERROR", "LOGIN_FAILED", "ERROR", "CAPTCHA_FETCH_ERROR", "MAX_CAPTCHA_ATTEMPTS_REACHED", "LOGIN_EXPIRED_MID_PROCESS", "UNKNOWN_API_RESPONSE"]:
                self.logger.info(f"GiftOps: [on_message] Validation failed for '{giftcode}' due to internal/API error: {initial_check}")
                reaction_to_add = "⚠️"
                reply_embed = discord.Embed(title="⚠️ Processing Error", color=discord.Color.orange())
                reply_embed.description=(
                        f"**Gift Code Details**\n━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"👤 **Sender:** {message.author.mention}\n"
                        f"🎁 **Gift Code:** `{giftcode}`\n"
                        f"❌ **Status:** Could not validate code due to an internal error ({initial_check}). Please try again later or report this.\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n")

            else:
                self.logger.exception(f"GiftOps: [on_message] Unhandled validation status for '{giftcode}': {initial_check}")
                reaction_to_add = "❓"

            if reaction_to_add:
                try:
                    await message.add_reaction(reaction_to_add)
                except (discord.Forbidden, discord.NotFound):
                    self.logger.error(f"GiftOps: [on_message] Failed to add reaction '{reaction_to_add}' to message {message.id}")
                except Exception as react_err:
                    self.logger.exception(f"GiftOps: [on_message] Unexpected error adding reaction: {react_err}")

            if reply_embed:
                try:
                    await message.reply(embed=reply_embed, mention_author=False)
                except (discord.Forbidden, discord.NotFound):
                    self.logger.error(f"GiftOps: [on_message] Failed to reply to message {message.id}")
                except Exception as reply_err:
                    self.logger.exception(f"GiftOps: [on_message] Unexpected error replying: {reply_err}")

        except Exception as e:
            self.logger.exception(f"GiftOps: UNEXPECTED Error in on_message handler: {str(e)}")
            traceback.print_exc()
            error_timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            error_details = traceback.format_exc()
            log_message_handler = (
                f"\n--- ERROR in on_message Handler ({error_timestamp}) ---\n"
                f"Message ID: {message.id if 'message' in locals() else 'N/A'}\n"
                f"Channel ID: {message.channel.id if 'message' in locals() else 'N/A'}\n"
                f"Error: {str(e)}\n"
                f"Traceback:\n{error_details}\n"
                f"---------------------------------------------------------\n"
            )
            try:
                with open(log_file_path, 'a', encoding='utf-8') as log_file:
                    log_file.write(log_message_handler)
            except Exception as log_e:
                self.logger.exception(f"GiftOps: CRITICAL - Failed to write on_message handler error log: {log_e}")

    def encode_data(self, data):
        secret = self.wos_encrypt_key
        sorted_keys = sorted(data.keys())
        encoded_data = "&".join(
            [
                f"{key}={json.dumps(data[key]) if isinstance(data[key], dict) else data[key]}"
                for key in sorted_keys
            ]
        )
        sign = hashlib.md5(f"{encoded_data}{secret}".encode()).hexdigest()
        return {"sign": sign, **data}

    def get_stove_info_wos(self, player_id):
        session = requests.Session()
        session.mount("https://", HTTPAdapter(max_retries=self.retry_config))

        headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/x-www-form-urlencoded",
            "origin": self.wos_giftcode_redemption_url,
        }

        data_to_encode = {
            "fid": f"{player_id}",
            "time": f"{int(datetime.now().timestamp())}",
        }
        data = self.encode_data(data_to_encode)

        response_stove_info = session.post(
            self.wos_player_info_url,
            headers=headers,
            data=data,
        )
        return session, response_stove_info

    async def claim_giftcode_rewards_wos(self, player_id, giftcode):
        log_file_path = os.path.join(self.log_directory, 'giftlog.txt')
        status = "ERROR"

        try:
            # Cache Check
            if player_id != "244886619":
                self.cursor.execute("SELECT status FROM user_giftcodes WHERE fid = ? AND giftcode = ?", (player_id, giftcode))
                existing_record = self.cursor.fetchone()
                if existing_record:
                    if existing_record[0] in ["SUCCESS", "RECEIVED", "SAME TYPE EXCHANGE", "TIME_ERROR", "CDK_NOT_FOUND", "USAGE_LIMIT"]:
                        self.logger.info(f"CACHE HIT - User {player_id} code '{giftcode}' status: {existing_record[0]}")
                        return existing_record[0]

            # Get Player Info
            session, response_stove_info = self.get_stove_info_wos(player_id=player_id)
            log_entry_player = f"\n{datetime.now()} API REQUEST - Player Info\nPlayer ID: {player_id}\n"
            try:
                response_json_player = response_stove_info.json()
                log_entry_player += f"Response Code: {response_stove_info.status_code}\nResponse JSON:\n{json.dumps(response_json_player, indent=2)}\n"
            except json.JSONDecodeError:
                log_entry_player += f"Response Code: {response_stove_info.status_code}\nResponse Text (Not JSON): {response_stove_info.text[:500]}...\n"
            log_entry_player += "-" * 50 + "\n"
            with open(log_file_path, 'a', encoding='utf-8') as log_file: log_file.write(log_entry_player)

            try: player_info_json = response_stove_info.json()
            except json.JSONDecodeError: player_info_json = {}
            login_successful = player_info_json.get("msg") == "success"

            if not login_successful:
                login_fail_msg = player_info_json.get("msg", f"Non-JSON/Unknown (Code: {response_stove_info.status_code})")
                log_message = f"{datetime.now()} Login failed for FID {player_id}: {login_fail_msg}\n"
                self.logger.info(log_message.strip())
                with open(log_file_path, 'a', encoding='utf-8') as log_file: log_file.write(log_message)
                return "LOGIN_FAILED"

            # Check if OCR Enabled and Solver Ready
            self.settings_cursor.execute("SELECT enabled FROM ocr_settings ORDER BY id DESC LIMIT 1")
            ocr_settings_row = self.settings_cursor.fetchone()
            ocr_enabled = ocr_settings_row[0] if ocr_settings_row else 0

            if not (ocr_enabled == 1 and self.captcha_solver):
                log_msg = f"{datetime.now()} Skipping captcha: OCR disabled (Enabled={ocr_enabled}) or Solver not ready ({self.captcha_solver is None}) for FID {player_id}.\n"
                self.logger.info(log_msg.strip())
                return "OCR_DISABLED" if ocr_enabled == 0 else "SOLVER_ERROR"

            # Captcha Fetching and Solving Loop
            self.logger.info(f"GiftOps: OCR enabled and solver initialized for FID {player_id}.")
            self.captcha_solver.reset_run_stats()
            max_ocr_attempts = 4

            for attempt in range(max_ocr_attempts):
                self.logger.info(f"GiftOps: Attempt {attempt + 1}/{max_ocr_attempts} to fetch/solve captcha for FID {player_id}")
                captcha_image_base64, error = await self.fetch_captcha(player_id, session)

                if error:
                    with open(log_file_path, 'a', encoding='utf-8') as log_file:
                        log_file.write(f"{datetime.now()} Captcha fetch error for {player_id}: {error}\n")
                    return "TIMEOUT_RETRY" if error == "CAPTCHA_TOO_FREQUENT" else "CAPTCHA_FETCH_ERROR"

                captcha_code, success, method, confidence = await self.captcha_solver.solve_captcha(
                    captcha_image_base64, fid=player_id, attempt=attempt)

                if not success:
                    with open(log_file_path, 'a', encoding='utf-8') as log_file:
                        log_file.write(f"{datetime.now()} OCR failed for FID {player_id} on attempt {attempt + 1}\n")
                    status = "OCR_FAILED_ATTEMPT"
                    if attempt == max_ocr_attempts - 1:
                         status = "MAX_CAPTCHA_ATTEMPTS_REACHED"
                    continue

                with open(log_file_path, 'a', encoding='utf-8') as log_file:
                    log_file.write(f"{datetime.now()} OCR solved for {player_id}: {captcha_code} (meth:{method}, conf:{confidence:.2f}, att:{attempt+1})\n")

                data_to_encode = {"fid":f"{player_id}", "cdk":giftcode, "captcha_code":captcha_code, "time":f"{int(datetime.now().timestamp()*1000)}"}
                data = self.encode_data(data_to_encode)
                response_giftcode = session.post(self.wos_giftcode_url, data=data)

                log_entry_redeem = f"\n{datetime.now()} API REQ - Gift Code Redeem\nFID:{player_id}, Code:{giftcode}, Captcha:{captcha_code}\n"
                try:
                    response_json_redeem = response_giftcode.json()
                    log_entry_redeem += f"Resp Code: {response_giftcode.status_code}\nResponse JSON:\n{json.dumps(response_json_redeem, indent=2)}\n"
                except json.JSONDecodeError:
                    response_json_redeem = {}
                    log_entry_redeem += f"Resp Code: {response_giftcode.status_code}\nResponse Text (Not JSON): {response_giftcode.text[:500]}...\n"
                log_entry_redeem += "-" * 50 + "\n"
                with open(log_file_path, 'a', encoding='utf-8') as log_file: log_file.write(log_entry_redeem)

                msg = response_json_redeem.get("msg", "Unknown Error").strip('.')
                err_code = response_json_redeem.get("err_code")

                if msg == "CAPTCHA CHECK ERROR" and err_code == 40103:
                    self.logger.warning(f"API rejected captcha for {player_id}. Returning CAPTCHA_INVALID.")
                    return "CAPTCHA_INVALID"

                elif msg == "CAPTCHA CHECK TOO FREQUENT" and err_code == 40101:
                    with open(log_file_path, 'a', encoding='utf-8') as log_file:
                        log_file.write(f"{datetime.now()} API rate limit on captcha check for {player_id}.\n")
                    return "TIMEOUT_RETRY"

                elif msg == "NOT LOGIN":
                    with open(log_file_path, 'a', encoding='utf-8') as log_file:
                        log_file.write(f"{datetime.now()} Session expired ('NOT LOGIN') for {player_id}.\n")
                    return "LOGIN_EXPIRED_MID_PROCESS"

                elif msg == "SUCCESS": status = "SUCCESS"
                elif msg == "RECEIVED" and err_code == 40008: status = "RECEIVED"
                elif msg == "SAME TYPE EXCHANGE" and err_code == 40011: status = "SAME TYPE EXCHANGE"
                elif msg == "TIME ERROR" and err_code == 40007: status = "TIME_ERROR"
                elif msg == "CDK NOT FOUND" and err_code == 40014: status = "CDK_NOT_FOUND"
                elif msg == "USED" and err_code == 40005: status = "USAGE_LIMIT"
                elif msg == "TIMEOUT RETRY" and err_code == 40004:
                    status = "TIMEOUT_RETRY"
                else:
                    with open(log_file_path, 'a', encoding='utf-8') as log_file:
                        log_file.write(f"{datetime.now()} Unknown API response for {player_id}: msg='{msg}', err_code={err_code}\n")
                    status = "UNKNOWN_API_RESPONSE"
                
                if player_id != "244886619" and status in ["SUCCESS", "RECEIVED", "SAME TYPE EXCHANGE"]:
                    try:
                        self.cursor.execute("""
                            INSERT OR REPLACE INTO user_giftcodes (fid, giftcode, status)
                            VALUES (?, ?, ?)
                        """, (player_id, giftcode, status))

                        self.conn.commit()
                        with open(log_file_path, 'a', encoding='utf-8') as log_file:
                            log_file.write(f"{datetime.now()} DATABASE - Saved/Updated status for User {player_id}, Code '{giftcode}', Status {status}\n")
                    except sqlite3.Error as db_err:
                        with open(log_file_path, 'a', encoding='utf-8') as log_file:
                            log_file.write(f"{datetime.now()} DATABASE ERROR saving/replacing status for {player_id}/{giftcode}: {db_err}\n")
                            log_file.write(f"STACK TRACE: {traceback.format_exc()}\n")
                    except Exception as e:
                        with open(log_file_path, 'a', encoding='utf-8') as log_file:
                            log_file.write(f"{datetime.now()} UNEXPECTED DB ERROR saving/replacing status for {player_id}/{giftcode}: {e}\n")
                            log_file.write(f"STACK TRACE: {traceback.format_exc()}\n")

                break

        except Exception as e:
            error_timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            error_details = traceback.format_exc()
            log_message = (
                f"\n--- UNEXPECTED ERROR in claim_giftcode_rewards_wos ({error_timestamp}) ---\n"
                f"Player ID: {player_id}, Gift Code: {giftcode}\nError: {str(e)}\n"
                f"Traceback:\n{error_details}\n"
                f"---------------------------------------------------------------------\n"
            )
            self.logger.exception(f"GiftOps: UNEXPECTED Error claiming code {giftcode} for FID {player_id}. Details logged.")
            try:
                with open(log_file_path, 'a', encoding='utf-8') as log_file: log_file.write(log_message)
            except Exception as log_e: self.logger.exception(f"GiftOps: CRITICAL - Failed to write unexpected error log: {log_e}")
            status = "ERROR"

        self.logger.info(f"Final status for FID {player_id} / Code '{giftcode}': {status}")
        return status

    @tasks.loop(seconds=300)
    async def check_channels_loop(self):
        log_file_path = os.path.join(self.log_directory, 'giftlog.txt')
        loop_start_time = datetime.now()
        self.logger.info(f"\nGiftOps: check_channels_loop running at {loop_start_time.strftime('%Y-%m-%d %H:%M:%S')}")

        try:
            # Step 1: Get Valid Channels
            self.cursor.execute("SELECT channel_id, alliance_id FROM giftcode_channel")
            channel_configs = self.cursor.fetchall()
            if not channel_configs: return

            valid_channels = []
            invalid_channel_ids_to_remove = []
            self.logger.info(f"GiftOps: [Loop] Validating {len(channel_configs)} configured channels...")
            for channel_id, alliance_id in channel_configs:
                channel = self.bot.get_channel(channel_id)
                if isinstance(channel, discord.TextChannel):
                    perms = channel.permissions_for(channel.guild.me)
                    if perms.read_message_history and perms.add_reactions and perms.send_messages:
                        valid_channels.append(channel)
                    else:
                        self.logger.warning(f"GiftOps: [Loop] WARNING - Missing permissions in channel {channel_id}. Skipping.")
                else:
                    self.logger.warning(f"GiftOps: [Loop] WARNING - Channel ID {channel_id} (Alliance: {alliance_id}) invalid. Marking for removal.")
                    invalid_channel_ids_to_remove.append(channel_id)

            if invalid_channel_ids_to_remove:
                unique_invalid_ids = list(set(invalid_channel_ids_to_remove))
                self.logger.info(f"GiftOps: [Loop] Removing {len(unique_invalid_ids)} invalid channel configs: {unique_invalid_ids}")
                placeholders = ','.join('?' * len(unique_invalid_ids))
                try:
                    self.cursor.execute(f"DELETE FROM giftcode_channel WHERE channel_id IN ({placeholders})", unique_invalid_ids)
                    self.conn.commit()
                except sqlite3.Error as db_err:
                    self.logger.exception(f"GiftOps: [Loop] DB ERROR removing invalid channels: {db_err}")

            if not valid_channels:
                self.logger.info("GiftOps: [Loop] No valid gift code channels found.")
                return

            # Step 2: Collect Processable Messages and Unique Codes
            all_processable_messages = []
            fetch_limit = 75

            for channel in valid_channels:
                self.logger.info(f"GiftOps: [Loop] Scanning channel {channel.id} ({channel.name})...")
                try:
                    # Find last definitive reaction timestamp (as before)
                    last_definitive_reaction_time = None
                    async for msg_hist in channel.history(limit=fetch_limit, oldest_first=False):
                        if msg_hist.reactions:
                            if any(reaction.me and str(reaction.emoji) in ["✅", "❌", "⚠️", "❓", "ℹ️"] for reaction in msg_hist.reactions):
                                last_definitive_reaction_time = msg_hist.created_at
                                break

                    # Determine messages needing potential processing
                    channel_messages = []
                    if last_definitive_reaction_time:
                        channel_messages = [msg async for msg in channel.history(limit=fetch_limit, after=last_definitive_reaction_time, oldest_first=True)]
                    else:
                        channel_messages = [msg async for msg in channel.history(limit=fetch_limit, oldest_first=True)]

                    all_processable_messages.extend(channel_messages)

                except discord.Forbidden as forbidden_err:
                    self.logger.exception(f"GiftOps: [Loop] ERROR - Perms error in channel {channel.id}: {forbidden_err}.")
                except Exception as channel_err:
                    self.logger.exception(f"GiftOps: [Loop] ERROR - scanning channel {channel.id}: {channel_err}")
                    traceback.print_exc()

            # Step 3: Filter Messages and Extract Unique Codes
            unique_codes_to_validate = set()
            processed_code_statuses = {}
            code_message_map = {}

            self.logger.info(f"GiftOps: [Loop] Processing {len(all_processable_messages)} potential messages across all channels.")
            for message in all_processable_messages:
                if message.author == self.bot.user or not message.content:
                    continue

                # Check reactions BEFORE extracting code
                bot_reactions = {str(reaction.emoji) for reaction in message.reactions if reaction.me}

                # Skip if definitively handled
                if bot_reactions.intersection(["✅", "❌", "⚠️", "❓", "ℹ️"]):
                    continue

                # Extract code
                content = message.content.strip()
                giftcode = None
                if len(content.split()) == 1:
                    if re.match(r'^[a-zA-Z0-9]+$', content):
                        giftcode = content
                else:
                    code_match = re.search(r'Code:\s*(\S+)', content, re.IGNORECASE)
                    if code_match:
                        potential_code = code_match.group(1)
                        if re.match(r'^[a-zA-Z0-9]+$', potential_code):
                             giftcode = potential_code

                if not giftcode:
                    continue

                # Add message to map for later processing
                if giftcode not in code_message_map:
                    code_message_map[giftcode] = []
                code_message_map[giftcode].append(message)

                # If code hasn't been processed *this run* and has no definitive reaction, mark for validation
                if giftcode not in processed_code_statuses:
                     unique_codes_to_validate.add(giftcode)

            self.logger.info(f"GiftOps: [Loop] Found {len(unique_codes_to_validate)} unique codes needing validation.")

            # Step 4: Validate Unique Codes
            validation_fid = "244886619"
            for code in unique_codes_to_validate:
                log_timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                self.logger.info(f"GiftOps: [Loop] Validating unique code: {code}")

                # Check DB Cache first
                self.cursor.execute("SELECT status FROM user_giftcodes WHERE fid = ? AND giftcode = ?", (validation_fid, code))
                cached_status = self.cursor.fetchone()
                if cached_status:
                    status = cached_status[0]
                    self.logger.info(f"GiftOps: [Loop] Code {code} found in validation cache with status: {status}")
                    processed_code_statuses[code] = status
                    continue

                # Check if already in main gift_codes DB (implies valid unless removed by validation)
                self.cursor.execute("SELECT 1 FROM gift_codes WHERE giftcode = ?", (code,))
                if self.cursor.fetchone():
                    self.logger.info(f"GiftOps: [Loop] Code {code} already in gift_codes DB. Assuming valid.")
                    processed_code_statuses[code] = "SUCCESS"
                    continue

                # Perform API Validation with lock and cooldown
                status = "ERROR"
                lock_acquired = False
                try:
                    current_time = time.time()
                    if current_time - self.last_validation_attempt_time < self.validation_cooldown:
                        wait_time = self.validation_cooldown - (current_time - self.last_validation_attempt_time)
                        self.logger.info(f"GiftOps: [Loop] Validation cooldown active. Waiting {wait_time:.1f}s before validating '{code}'.")
                        await asyncio.sleep(wait_time)

                    async with asyncio.timeout(30):
                         async with self._validation_lock:
                            lock_acquired = True
                            self.last_validation_attempt_time = time.time()
                            self.logger.info(f"GiftOps: [Loop] Acquired lock for validation FID check on '{code}'.")
                            with open(log_file_path, 'a', encoding='utf-8') as log_file:
                                log_file.write(f"{log_timestamp} [Loop-Unique] Acquiring lock for code '{code}'.\n")

                            status = await self.claim_giftcode_rewards_wos(validation_fid, code)
                            if status not in ["ERROR", "TIMEOUT_RETRY", "CAPTCHA_RETRY", "CAPTCHA_API_REJECTED", "OCR_DISABLED", "SOLVER_ERROR", "LOGIN_FAILED", "CAPTCHA_FETCH_ERROR", "MAX_CAPTCHA_ATTEMPTS_REACHED", "LOGIN_EXPIRED_MID_PROCESS", "UNKNOWN_API_RESPONSE"]: # Cache definitive results
                                try:
                                    self.cursor.execute("INSERT OR REPLACE INTO user_giftcodes (fid, giftcode, status) VALUES (?, ?, ?)", (validation_fid, code, status))
                                    self.conn.commit()
                                    self.logger.info(f"GiftOps: [Loop] Cached validation result '{status}' for code '{code}'.")
                                except sqlite3.Error as db_cache_err:
                                     self.logger.exception(f"GiftOps: [Loop] DB Error caching validation status for {code}: {db_cache_err}")

                            log_timestamp_after = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                            self.logger.info(f"GiftOps: [Loop] Validation result for '{code}': {status}")
                            with open(log_file_path, 'a', encoding='utf-8') as log_file:
                                log_file.write(f"{log_timestamp_after} [Loop-Unique] Validation result for '{code}': {status}\n")

                    log_timestamp_release = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    self.logger.info(f"GiftOps: [Loop] Released validation lock for '{code}'.")
                    with open(log_file_path, 'a', encoding='utf-8') as log_file:
                        log_file.write(f"{log_timestamp_release} [Loop-Unique] Released lock for code '{code}'.\n")
                    lock_acquired = False
                    processed_code_statuses[code] = status

                except asyncio.TimeoutError:
                     self.logger.exception(f"GiftOps: [Loop] Timeout acquiring lock for '{code}'. Marking as TIMEOUT_RETRY.")
                     processed_code_statuses[code] = "TIMEOUT_RETRY"
                except Exception as lock_err:
                     self.logger.exception(f"GiftOps: [Loop] Error during lock/validation for '{code}': {lock_err}")
                     traceback.print_exc()
                     processed_code_statuses[code] = "ERROR"
                finally:
                     if lock_acquired and self._validation_lock.locked():
                         try: self._validation_lock.release()
                         except RuntimeError: pass
                         self.logger.exception(f"GiftOps: [Loop] Force-released validation lock (error) for '{code}'.")

                # Delay between validating unique codes to ease API load
                await asyncio.sleep(random.uniform(2.0, 4.0))

            # Step 5: Apply Reactions and Actions Based on Determined Statuses
            self.logger.info(f"GiftOps: [Loop] Applying final statuses and reactions...")
            codes_added_this_run = set()

            for code, status in processed_code_statuses.items():
                if code not in code_message_map: continue

                reaction_to_add = None
                is_valid_and_new = False

                # Determine reaction based on final status
                if status in ["SUCCESS", "RECEIVED", "SAME TYPE EXCHANGE"]:
                    reaction_to_add = "✅"
                    self.cursor.execute("SELECT 1 FROM gift_codes WHERE giftcode = ?", (code,))
                    if not self.cursor.fetchone() and code not in codes_added_this_run:
                         is_valid_and_new = True
                elif status in ["TIME_ERROR", "CDK_NOT_FOUND", "USAGE_LIMIT"]:
                    reaction_to_add = "❌"
                elif status == "TIMEOUT_RETRY":
                    reaction_to_add = "⏳"
                elif status in ["ERROR", "OCR_DISABLED", "SOLVER_ERROR", "LOGIN_FAILED", "CAPTCHA_FETCH_ERROR", "MAX_CAPTCHA_ATTEMPTS_REACHED", "LOGIN_EXPIRED_MID_PROCESS", "UNKNOWN_API_RESPONSE", "CAPTCHA_API_REJECTED"]:
                     reaction_to_add = "⚠️"
                else:
                    reaction_to_add = "❓"

                # Apply reaction to ALL messages for this code
                for message in code_message_map[code]:
                    if message.reactions:
                        for reaction in message.reactions:
                            if reaction.me:
                                try:
                                    await message.remove_reaction(reaction.emoji, self.bot.user)
                                except (discord.Forbidden, discord.NotFound): pass
                                except Exception as rem_react_err: self.logger.exception(f"GiftOps: [Loop] Error removing reaction {reaction.emoji} from {message.id}: {rem_react_err}")

                    # Add the final reaction
                    if reaction_to_add:
                        try:
                            await message.add_reaction(reaction_to_add)
                        except (discord.Forbidden, discord.NotFound):
                            self.logger.exception(f"GiftOps: [Loop] Failed to add final reaction '{reaction_to_add}' to msg {message.id}")
                        except Exception as add_react_err:
                            self.logger.exception(f"GiftOps: [Loop] Error adding final reaction to {message.id}: {add_react_err}")

                # Add to DB and trigger actions if valid and new
                if is_valid_and_new:
                    self.logger.info(f"GiftOps: [Loop] Finalizing addition of new valid code '{code}' to DB & API.")
                    try:
                        self.cursor.execute("INSERT INTO gift_codes (giftcode, date) VALUES (?, ?)", (code, datetime.now().strftime("%Y-%m-%d")))
                        self.conn.commit()
                        codes_added_this_run.add(code)

                        try: asyncio.create_task(self.api.add_giftcode(code))
                        except Exception as api_add_err: self.logger.exception(f"GiftOps: [Loop] Error calling api.add_giftcode for '{code}': {api_add_err}")

                        self.cursor.execute("SELECT alliance_id FROM giftcodecontrol WHERE status = 1")
                        auto_alliances = self.cursor.fetchall()
                        if auto_alliances:
                            self.logger.info(f"GiftOps: [Loop] Triggering auto-use for {len(auto_alliances)} alliances for '{code}'.")
                            for alliance in auto_alliances:
                                asyncio.create_task(self.use_giftcode_for_alliance(alliance[0], code))
                    except sqlite3.Error as db_ins_err:
                        self.logger.exception(f"GiftOps: [Loop] DB ERROR inserting finalized code '{code}': {db_ins_err}")

            # Step 6: Periodic Full Validation
            self.logger.info("\nGiftOps: [Loop] Running periodic validation of existing codes in DB...")
            try:
                 await self.validate_gift_codes()
                 self.logger.info("GiftOps: [Loop] Periodic validation complete.")
            except Exception as val_err:
                 self.logger.exception(f"GiftOps: [Loop] ERROR during periodic validation: {val_err}")
                 traceback.print_exc()

            loop_end_time = datetime.now()
            self.logger.info(f"GiftOps: check_channels_loop finished at {loop_end_time.strftime('%Y-%m-%d %H:%M:%S')}. Duration: {loop_end_time - loop_start_time}\n")

        except Exception as e:
            self.logger.exception(f"GiftOps: [Loop] FATAL ERROR in check_channels_loop: {str(e)}")
            self.logger.exception(f"Traceback: {traceback.format_exc()}")
            error_timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            error_details = traceback.format_exc()
            log_message_loop = (
                 f"\n--- FATAL ERROR in check_channels_loop ({error_timestamp}) ---\n"
                 f"Error: {str(e)}\nTraceback:\n{error_details}\n"
                 f"------------------------------------------------------------\n"
            )
            try:
                with open(log_file_path, 'a', encoding='utf-8') as log_file:
                    log_file.write(log_message_loop)
            except Exception as log_e:
                 self.logger.exception(f"GiftOps: CRITICAL - Failed to write loop error log: {log_e}")

    @check_channels_loop.before_loop
    async def before_check_channels_loop(self):
         self.logger.info("GiftOps: Waiting for bot to be ready before starting check_channels_loop...")
         await self.bot.wait_until_ready()
         self.logger.info("GiftOps: Bot is ready, check_channels_loop starting.")

    async def fetch_captcha(self, player_id, session=None):
        """Fetch a captcha image for a player ID."""
        if session is None:
            session = requests.Session()
            session.mount("https://", HTTPAdapter(max_retries=self.retry_config))
            
        headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/x-www-form-urlencoded",
            "origin": self.wos_giftcode_redemption_url,
        }
        
        data_to_encode = {
            "fid": player_id,
            "time": f"{int(datetime.now().timestamp() * 1000)}",
            "init": "0"
        }
        data = self.encode_data(data_to_encode)
        
        try:
            response = session.post(
                self.wos_captcha_url,
                headers=headers,
                data=data,
            )
            
            if response.status_code == 200:
                captcha_data = response.json()
                if captcha_data.get("code") == 1 and captcha_data.get("msg") == "CAPTCHA GET TOO FREQUENT.":
                    return None, "CAPTCHA_TOO_FREQUENT"
                    
                if "data" in captcha_data and "img" in captcha_data["data"]:
                    return captcha_data["data"]["img"], None
            
            return None, "CAPTCHA_FETCH_ERROR"
        except Exception as e:
            self.logger.exception(f"Error fetching captcha: {e}")
            return None, f"CAPTCHA_EXCEPTION: {str(e)}"

    async def show_ocr_settings(self, interaction: discord.Interaction):
        """Show OCR settings menu."""
        try:
            self.settings_cursor.execute("SELECT is_initial FROM admin WHERE id = ?", (interaction.user.id,))
            admin_info = self.settings_cursor.fetchone()

            if not admin_info or admin_info[0] != 1:
                if interaction.response.is_done():
                    await interaction.followup.send(
                         "❌ You don't have permission to access OCR settings.",
                         ephemeral=True
                    )
                else:
                    await interaction.response.send_message(
                        "❌ You don't have permission to access OCR settings.",
                        ephemeral=True
                    )
                return

            # Get current OCR settings
            self.settings_cursor.execute("SELECT enabled, use_gpu, gpu_device, save_images FROM ocr_settings LIMIT 1")
            ocr_settings = self.settings_cursor.fetchone()

            if not ocr_settings:
                self.settings_cursor.execute("""
                    INSERT INTO ocr_settings (enabled, use_gpu, gpu_device, save_images)
                    VALUES (1, 0, 0, 1)
                """)
                self.settings_conn.commit()
                ocr_settings = (1, 0, 0, 1)

            enabled, use_gpu, gpu_device, save_images_setting = ocr_settings

            # Check if necessary libraries are installed
            ocr_libraries_installed = False
            try:
                import easyocr
                import cv2
                import numpy
                from PIL import Image
                try:
                    import torch
                    gpu_available = torch.cuda.is_available()
                except ImportError:
                    gpu_available = False
                ocr_libraries_installed = True
            except ImportError:
                 gpu_available = False

            save_options_text = {
                0: "❌ None",
                1: "⚠️ Failed Only",
                2: "✅ Success Only",
                3: "💾 All"
            }
            save_images_display = save_options_text.get(save_images_setting, "❌ None (Unknown)")

            # Create embed with current settings
            embed = discord.Embed(
                title="🔍 CAPTCHA Solver Settings",
                description=(
                    f"Configure the automatic CAPTCHA solver for gift code redemption\n\n"
                    f"**Current Settings**\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🤖 **OCR Enabled:** {'✅ Yes' if enabled == 1 else '❌ No'}\n"
                    f"🖥️ **GPU Acceleration:** {'✅ Yes' if use_gpu == 1 else '❌ No'}{' (⚠️ No GPU Detected)' if use_gpu == 1 and not gpu_available else ''}\n"
                    f"🎯 **GPU Device ID:** {gpu_device}\n"
                    f"💾 **Save CAPTCHA Images:** {save_images_display}\n"
                    f"📦 **Required Libraries:** {'✅ Installed' if ocr_libraries_installed else '❌ Missing'}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                ),
                color=discord.Color.blue()
            )

            # Add note about missing libraries when needed
            if not ocr_libraries_installed:
                embed.add_field(
                    name="⚠️ Missing Libraries",
                    value=(
                        "The following Python libraries are required for CAPTCHA solving:\n"
                        "• `easyocr`\n"
                        "• `opencv-python` (cv2)\n"
                        "• `numpy`\n"
                        "• `pillow` (PIL)\n"
                        "• `torch` (Optional, for GPU)\n\n"
                        "The bot owner needs to install these on the server."
                    ),
                    inline=False
                )

            # Stats if CAPTCHA solver is available
            if self.captcha_solver:
                stats = self.captcha_solver.get_stats()
                if stats["total_attempts"] > 0:
                    success_rate = (stats["successful_decodes"] / stats["total_attempts"]) * 100 if stats["total_attempts"] > 0 else 0
                    embed.add_field(
                        name="📊 OCR Statistics (Since Bot Start)",
                        value=(
                            f"• Total attempts: {stats['total_attempts']}\n"
                            f"• Successful decodes: {stats['successful_decodes']}\n"
                            f"• First try success: {stats['first_try_success']}\n"
                            f"• Retries: {stats['retries']}\n"
                            f"• Failures: {stats['failures']}\n"
                            f"• Success rate: {success_rate:.1f}%"
                        ),
                        inline=False
                    )
                    embed.add_field(
                        name="Important Note",
                        value="Saving images (especially 'All') can consume significant disk space over time.",
                        inline=False
                    )

            # Create view with toggle buttons
            view = OCRSettingsView(self, ocr_settings, ocr_libraries_installed)

            # Send or Edit the message
            if interaction.response.is_done():
                try:
                    await interaction.edit_original_response(embed=embed, view=view)
                except discord.NotFound:
                    await interaction.followup.send(embed=embed, view=view, ephemeral=True)
                except Exception as e_edit:
                    self.logger.exception(f"Error editing original response in show_ocr_settings: {e_edit}")
                    await interaction.followup.send(embed=embed, view=view, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        except Exception as e:
            self.logger.exception(f"Error showing OCR settings: {e}")
            traceback.print_exc()
            error_message = "❌ An error occurred while loading OCR settings."
            if interaction.response.is_done():
                await interaction.followup.send(error_message, ephemeral=True)
            else:
                await interaction.response.send_message(error_message, ephemeral=True)

    async def update_ocr_settings(self, interaction, enabled=None, use_gpu=None, gpu_device=None, save_images=None):
        """Update OCR settings in the database and reinitialize the solver if needed."""
        try:
            # Get current settings first to handle None values
            self.settings_cursor.execute("SELECT enabled, use_gpu, gpu_device, save_images FROM ocr_settings ORDER BY id DESC LIMIT 1")
            current_settings = self.settings_cursor.fetchone()
            if not current_settings:
                current_settings = (1, 0, 0, 0)

            # Determine new settings, using current values if specific ones aren't provided
            new_enabled = enabled if enabled is not None else current_settings[0]
            new_use_gpu = use_gpu if use_gpu is not None else current_settings[1]
            new_gpu_device = gpu_device if gpu_device is not None else current_settings[2]
            new_save_images = save_images if save_images is not None else current_settings[3]

            # Save the intended settings to the database
            self.settings_cursor.execute("""
                UPDATE ocr_settings SET
                enabled = ?, use_gpu = ?, gpu_device = ?, save_images = ?
                WHERE id = (SELECT MAX(id) FROM ocr_settings) -- Update the latest row
                """, (new_enabled, new_use_gpu, new_gpu_device, new_save_images))
            if self.settings_cursor.rowcount == 0:
                self.settings_cursor.execute("""
                    INSERT INTO ocr_settings (enabled, use_gpu, gpu_device, save_images)
                    VALUES (?, ?, ?, ?)
                    """, (new_enabled, new_use_gpu, new_gpu_device, new_save_images))
            self.settings_conn.commit()
            self.logger.info(f"GiftOps: Updated OCR settings in DB -> Enabled={new_enabled}, UseGPU={new_use_gpu}, GPUDevice={new_gpu_device}, SaveImages={new_save_images}")

            # Reinitialize Solver based on capability
            self.captcha_solver = None
            init_message = "CAPTCHA solver settings updated."

            if new_enabled == 1:
                self.logger.info("GiftOps: OCR is enabled, attempting solver reinitialization...")
                actual_use_gpu = False
                warning_message = ""
                try:
                    import torch
                    gpu_available = torch.cuda.is_available()

                    if new_use_gpu == 1:
                        if gpu_available:
                            actual_use_gpu = True
                            self.logger.info("GiftOps: Reinitializing with GPU (Available).")
                        else:
                            actual_use_gpu = False
                            warning_message = " (GPU requested but unavailable, using CPU)"
                            self.logger.warning(f"GiftOps: WARNING - Reinitializing with CPU (GPU requested but unavailable).")
                    else:
                        actual_use_gpu = False
                        self.logger.info("GiftOps: Reinitializing with CPU (GPU not requested).")

                    self.captcha_solver = GiftCaptchaSolver(
                        use_gpu=actual_use_gpu,
                        gpu_device=new_gpu_device if actual_use_gpu else None,
                        save_images=new_save_images
                    )
                    init_message += f" Solver reinitialized successfully{warning_message}."
                    self.logger.info("GiftOps: Solver reinitialized successfully.")
                    return True, init_message

                except ImportError as imp_err:
                    init_message += f" Solver initialization failed: Missing library ({imp_err})."
                    self.logger.exception(f"GiftOps: ERROR - Reinitialization failed: Missing library {imp_err}")
                    self.captcha_solver = None
                    return False, init_message
                except Exception as e:
                    init_message += f" Solver initialization failed: {e}."
                    self.logger.exception(f"GiftOps: ERROR - Reinitialization failed: {e}")
                    traceback.print_exc()
                    self.captcha_solver = None
                    return False, init_message
            else:
                init_message += " Solver is now disabled."
                self.logger.info("GiftOps: OCR disabled, solver instance removed.")
                self.captcha_solver = None
                return True, init_message

        except sqlite3.Error as db_err:
            self.logger.exception(f"Error updating OCR settings in database: {db_err}")
            traceback.print_exc()
            return False, f"Error updating OCR settings in database: {db_err}"
        except Exception as e:
            self.logger.info(f"Error updating OCR settings: {e}")
            traceback.print_exc()
            return False, f"Error updating OCR settings: {e}"

    async def validate_gift_codes(self):
        try:
            self.cursor.execute("SELECT giftcode FROM gift_codes")
            all_codes = self.cursor.fetchall()
            
            self.settings_cursor.execute("SELECT id FROM admin WHERE is_initial = 1")
            admin_ids = [row[0] for row in self.settings_cursor.fetchall()]
            
            for code in all_codes:
                giftcode = code[0]
                status = await self.claim_giftcode_rewards_wos("244886619", giftcode)
                
                if status in ["TIME_ERROR", "CDK_NOT_FOUND", "USAGE_LIMIT"]:
                    await self.api.remove_giftcode(giftcode, from_validation=True)
                    self.cursor.execute("DELETE FROM user_giftcodes WHERE giftcode = ?", (giftcode,))
                    self.cursor.execute("DELETE FROM gift_codes WHERE giftcode = ?", (giftcode,))
                    self.conn.commit()
                    
                    reason = "expired" if status == "TIME_ERROR" else "invalid" if status == "CDK_NOT_FOUND" else "usage limit reached"
                    admin_embed = discord.Embed(
                        title="🎁 Gift Code Removed",
                        description=(
                            f"**Gift Code Details**\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"🎁 **Gift Code:** `{giftcode}`\n"
                            f"❌ **Reason:** `Code {reason}`\n"
                            f"⏰ **Time:** <t:{int(datetime.now().timestamp())}:R>\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        ),
                        color=discord.Color.red()
                    )
                    
                    for admin_id in admin_ids:
                        try:
                            admin_user = await self.bot.fetch_user(admin_id)
                            if admin_user:
                                await admin_user.send(embed=admin_embed)
                        except Exception as e:
                            self.logger.exception(f"Error sending message to admin {admin_id}: {str(e)}")
                
                await asyncio.sleep(60)
                
        except Exception as e:
            self.logger.exception(f"Error in validate_gift_codes: {str(e)}")

    async def handle_success(self, message, giftcode):
        status = await self.claim_giftcode_rewards_wos("244886619", giftcode)
        
        if status in ["SUCCESS", "RECEIVED", "SAME TYPE EXCHANGE"]:
            self.cursor.execute("SELECT 1 FROM gift_codes WHERE giftcode = ?", (giftcode,))
            if not self.cursor.fetchone():
                self.cursor.execute("INSERT INTO gift_codes (giftcode, date) VALUES (?, ?)", (giftcode, datetime.now()))
                self.conn.commit()
                
                try:
                    asyncio.create_task(self.api.add_giftcode(giftcode))
                except:
                    pass
                
                await message.add_reaction("✅")
                await message.reply("Gift code successfully added.", mention_author=False)
        elif status == "TIME_ERROR":
            await self.handle_time_error(message)
        elif status == "CDK_NOT_FOUND":
            await self.handle_cdk_not_found(message)
        elif status == "USAGE_LIMIT":
            await message.add_reaction("❌")
            await message.reply("Usage limit has been reached for this code.", mention_author=False)

    async def handle_already_received(self, message, giftcode):
        status = await self.claim_giftcode_rewards_wos("244886619", giftcode)
        
        if status in ["SUCCESS", "RECEIVED", "SAME TYPE EXCHANGE"]:
            self.cursor.execute("SELECT 1 FROM gift_codes WHERE giftcode = ?", (giftcode,))
            if not self.cursor.fetchone():
                self.cursor.execute("INSERT INTO gift_codes (giftcode, date) VALUES (?, ?)", (giftcode, datetime.now()))
                self.conn.commit()
                
                try:
                    asyncio.create_task(self.api.add_giftcode(giftcode))
                except:
                    pass
                
                await message.add_reaction("✅")
                await message.reply("Gift code successfully added.", mention_author=False)
        elif status == "TIME_ERROR":
            await self.handle_time_error(message)
        elif status == "CDK_NOT_FOUND":
            await self.handle_cdk_not_found(message)
        elif status == "USAGE_LIMIT":
            await message.add_reaction("❌")
            await message.reply("Usage limit has been reached for this code.", mention_author=False)

    async def handle_cdk_not_found(self, message):
        await message.add_reaction("❌")
        await message.reply("The gift code is incorrect.", mention_author=False)

    async def handle_time_error(self, message):
        await message.add_reaction("❌")
        await message.reply("Gift code expired.", mention_author=False)

    async def handle_timeout_retry(self, message, giftcode):
        self.cursor.execute("SELECT 1 FROM gift_codes WHERE giftcode = ?", (giftcode,))
        if not self.cursor.fetchone():
            await message.add_reaction("⏳")

    async def get_admin_info(self, user_id):
        self.settings_cursor.execute("""
            SELECT id, is_initial FROM admin WHERE id = ?
        """, (user_id,))
        return self.settings_cursor.fetchone()

    async def get_alliance_names(self, user_id, is_global=False):
        if is_global:
            self.alliance_cursor.execute("SELECT name FROM alliance_list")
            return [row[0] for row in self.alliance_cursor.fetchall()]
        else:
            self.settings_cursor.execute("""
                SELECT alliances_id FROM adminserver WHERE admin = ?
            """, (user_id,))
            alliance_ids = [row[0] for row in self.settings_cursor.fetchall()]
            
            if alliance_ids:
                placeholders = ','.join('?' * len(alliance_ids))
                self.alliance_cursor.execute(f"""
                    SELECT name FROM alliance_list 
                    WHERE alliance_id IN ({placeholders})
                """, alliance_ids)
                return [row[0] for row in self.alliance_cursor.fetchall()]
            return []

    async def get_available_alliances(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        guild_id = interaction.guild_id if interaction.guild else None

        admin_info = await self.get_admin_info(user_id)
        if not admin_info:
            return []

        is_global = admin_info[1] == 1

        if is_global:
            self.alliance_cursor.execute("SELECT alliance_id, name FROM alliance_list")
            return self.alliance_cursor.fetchall()

        if guild_id:
            self.alliance_cursor.execute("""
                SELECT DISTINCT alliance_id, name 
                FROM alliance_list 
                WHERE discord_server_id = ?
            """, (guild_id,))
            guild_alliances = self.alliance_cursor.fetchall()

            self.settings_cursor.execute("""
                SELECT alliances_id FROM adminserver WHERE admin = ?
            """, (user_id,))
            special_alliance_ids = [row[0] for row in self.settings_cursor.fetchall()]

            if special_alliance_ids:
                placeholders = ','.join('?' * len(special_alliance_ids))
                self.alliance_cursor.execute(f"""
                    SELECT alliance_id, name FROM alliance_list 
                    WHERE alliance_id IN ({placeholders})
                """, special_alliance_ids)
                special_alliances = self.alliance_cursor.fetchall()
            else:
                special_alliances = []

            all_alliances = list(set(guild_alliances + special_alliances))
            return all_alliances

        return []

    async def setup_gift_channel(self, interaction: discord.Interaction):
        admin_info = await self.get_admin_info(interaction.user.id)
        if not admin_info:
            await interaction.response.send_message(
                "❌ You are not authorized to perform this action.",
                ephemeral=True
            )
            return

        available_alliances = await self.get_available_alliances(interaction)
        if not available_alliances:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="❌ No Available Alliances",
                    description="You don't have access to any alliances.",
                    color=discord.Color.red()
                ),
                ephemeral=True
            )
            return

        alliances_with_counts = []
        for alliance_id, name in available_alliances:
            with sqlite3.connect('db/users.sqlite') as users_db:
                cursor = users_db.cursor()
                cursor.execute("SELECT COUNT(*) FROM users WHERE alliance = ?", (alliance_id,))
                member_count = cursor.fetchone()[0]
                alliances_with_counts.append((alliance_id, name, member_count))

        self.cursor.execute("SELECT alliance_id, channel_id FROM giftcode_channel")
        current_channels = dict(self.cursor.fetchall())

        alliance_embed = discord.Embed(
            title="📢 Gift Code Channel Setup",
            description=(
                "Please select an alliance to set up gift code channel:\n\n"
                "**Alliance List**\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "Select an alliance from the list below:\n"
            ),
            color=discord.Color.blue()
        )

        view = AllianceSelectView(alliances_with_counts, self)

        async def alliance_callback(select_interaction: discord.Interaction):
            try:
                alliance_id = int(view.current_select.values[0])
                
                channel_embed = discord.Embed(
                    title="📢 Gift Code Channel Setup",
                    description=(
                        "**Instructions:**\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        "Please select a channel for gift codes\n\n"
                        "**Page:** 1/1\n"
                        f"**Total Channels:** {len(select_interaction.guild.text_channels)}"
                    ),
                    color=discord.Color.blue()
                )

                async def channel_select_callback(channel_interaction: discord.Interaction):
                    try:
                        channel_id = int(channel_interaction.data["values"][0])
                        
                        self.cursor.execute("""
                            INSERT OR REPLACE INTO giftcode_channel (alliance_id, channel_id)
                            VALUES (?, ?)
                        """, (alliance_id, channel_id))
                        self.conn.commit()

                        alliance_name = next((name for aid, name in available_alliances if aid == alliance_id), "Unknown Alliance")

                        success_embed = discord.Embed(
                            title="✅ Gift Code Channel Set",
                            description=(
                                f"Successfully set gift code channel:\n\n"
                                f"🏰 **Alliance:** {alliance_name}\n"
                                f"📝 **Channel:** <#{channel_id}>\n"
                            ),
                            color=discord.Color.green()
                        )

                        await channel_interaction.response.edit_message(
                            embed=success_embed,
                            view=None
                        )

                    except Exception as e:
                        self.logger.exception(f"Error setting gift code channel: {e}")
                        await channel_interaction.response.send_message(
                            "❌ An error occurred while setting the gift code channel.",
                            ephemeral=True
                        )

                channels = select_interaction.guild.text_channels
                channel_view = PaginatedChannelView(channels, channel_select_callback)

                if not select_interaction.response.is_done():
                    await select_interaction.response.edit_message(
                        embed=channel_embed,
                        view=channel_view
                    )
                else:
                    await select_interaction.message.edit(
                        embed=channel_embed,
                        view=channel_view
                    )

            except Exception as e:
                self.logger.exception(f"Error in alliance selection: {e}")
                if not select_interaction.response.is_done():
                    await select_interaction.response.send_message(
                        "❌ An error occurred while processing your selection.",
                        ephemeral=True
                    )
                else:
                    await select_interaction.followup.send(
                        "❌ An error occurred while processing your selection.",
                        ephemeral=True
                    )

        view.callback = alliance_callback

        await interaction.response.send_message(
            embed=alliance_embed,
            view=view,
            ephemeral=True
        )

    async def show_gift_menu(self, interaction: discord.Interaction):
        gift_menu_embed = discord.Embed(
            title="🎁 Gift Code Operations",
            description=(
                "Please select an operation:\n\n"
                "**Available Operations**\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "🎫 **Create Gift Code**\n"
                "└ Generate new gift codes\n\n"
                "📋 **List Gift Codes**\n"
                "└ View all active codes\n\n"
                "⚙️ **Auto Gift Settings**\n"
                "└ Configure automatic gift code usage\n\n"
                "❌ **Delete Gift Code**\n"
                "└ Remove existing codes\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━"
            ),
            color=discord.Color.gold()
        )

        view = GiftView(self)
        try:
            await interaction.response.edit_message(embed=gift_menu_embed, view=view)
        except discord.InteractionResponded:
            pass
        except Exception:
            pass

    async def create_gift_code(self, interaction: discord.Interaction):
        self.settings_cursor.execute("SELECT 1 FROM admin WHERE id = ?", (interaction.user.id,))
        if not self.settings_cursor.fetchone():
            await interaction.response.send_message(
                "❌ You are not authorized to create gift codes.",
                ephemeral=True
            )
            return

        modal = CreateGiftCodeModal(self)
        try:
            await interaction.response.send_modal(modal)
        except Exception as e:
            self.logger.exception(f"Error showing modal: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ An error occurred while showing the gift code creation form.",
                    ephemeral=True
                )

    async def list_gift_codes(self, interaction: discord.Interaction):
        self.cursor.execute("""
            SELECT 
                gc.giftcode,
                gc.date,
                COUNT(DISTINCT ugc.fid) as used_count
            FROM gift_codes gc
            LEFT JOIN user_giftcodes ugc ON gc.giftcode = ugc.giftcode
            GROUP BY gc.giftcode
            ORDER BY gc.date DESC
        """)
        
        codes = self.cursor.fetchall()
        
        if not codes:
            await interaction.response.send_message(
                "No gift codes found in the database.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title="🎁 Active Gift Codes",
            color=discord.Color.blue()
        )

        for code, date, used_count in codes:
            embed.add_field(
                name=f"Code: {code}",
                value=f"Created: {date}\nUsed by: {used_count} users",
                inline=False
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def delete_gift_code(self, interaction: discord.Interaction):
        try:
            settings_conn = sqlite3.connect('db/settings.sqlite')
            settings_cursor = settings_conn.cursor()
            
            settings_cursor.execute("""
                SELECT 1 FROM admin 
                WHERE id = ? AND is_initial = 1
            """, (interaction.user.id,))
            
            is_admin = settings_cursor.fetchone()
            settings_cursor.close()
            settings_conn.close()

            if not is_admin:
                await interaction.response.send_message(
                    embed=discord.Embed(
                        title="❌ Unauthorized Access",
                        description="This action requires Global Admin privileges.",
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )
                return

            self.cursor.execute("""
                SELECT 
                    gc.giftcode,
                    gc.date,
                    COUNT(DISTINCT ugc.fid) as used_count
                FROM gift_codes gc
                LEFT JOIN user_giftcodes ugc ON gc.giftcode = ugc.giftcode
                GROUP BY gc.giftcode
                ORDER BY gc.date DESC
            """)
            
            codes = self.cursor.fetchall()
            
            if not codes:
                await interaction.response.send_message(
                    embed=discord.Embed(
                        title="❌ No Gift Codes",
                        description="There are no gift codes in the database to delete.",
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )
                return

            select = discord.ui.Select(
                placeholder="Select a gift code to delete",
                options=[
                    discord.SelectOption(
                        label=f"Code: {code}",
                        description=f"Created: {date} | Used by: {used_count} users",
                        value=code
                    ) for code, date, used_count in codes
                ]
            )

            async def select_callback(select_interaction):
                selected_code = select_interaction.data["values"][0]
                
                confirm = discord.ui.Button(
                    style=discord.ButtonStyle.danger,
                    label="Confirm Delete",
                    custom_id="confirm"
                )
                cancel = discord.ui.Button(
                    style=discord.ButtonStyle.secondary,
                    label="Cancel",
                    custom_id="cancel"
                )

                async def button_callback(button_interaction):
                    try:
                        if button_interaction.data.get('custom_id') == "confirm":
                            try:
                                self.cursor.execute("DELETE FROM gift_codes WHERE giftcode = ?", (selected_code,))
                                self.cursor.execute("DELETE FROM user_giftcodes WHERE giftcode = ?", (selected_code,))
                                self.conn.commit()
                                
                                success_embed = discord.Embed(
                                    title="✅ Gift Code Deleted",
                                    description=(
                                        f"**Deletion Details**\n"
                                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                        f"🎁 **Gift Code:** `{selected_code}`\n"
                                        f"👤 **Deleted by:** {button_interaction.user.mention}\n"
                                        f"⏰ **Time:** <t:{int(datetime.now().timestamp())}:R>\n"
                                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                    ),
                                    color=discord.Color.green()
                                )
                                
                                await button_interaction.response.edit_message(
                                    embed=success_embed,
                                    view=None
                                )
                                
                            except Exception as e:
                                await button_interaction.response.send_message(
                                    "❌ An error occurred while deleting the gift code.",
                                    ephemeral=True
                                )

                        else:
                            cancel_embed = discord.Embed(
                                title="❌ Deletion Cancelled",
                                description="The gift code deletion was cancelled.",
                                color=discord.Color.red()
                            )
                            await button_interaction.response.edit_message(
                                embed=cancel_embed,
                                view=None
                            )

                    except Exception as e:
                        self.logger.exception(f"Button callback error: {str(e)}")
                        try:
                            await button_interaction.response.send_message(
                                "❌ An error occurred while processing the request.",
                                ephemeral=True
                            )
                        except:
                            await button_interaction.followup.send(
                                "❌ An error occurred while processing the request.",
                                ephemeral=True
                            )

                confirm.callback = button_callback
                cancel.callback = button_callback

                confirm_view = discord.ui.View()
                confirm_view.add_item(confirm)
                confirm_view.add_item(cancel)

                confirmation_embed = discord.Embed(
                    title="⚠️ Confirm Deletion",
                    description=(
                        f"**Gift Code Details**\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🎁 **Selected Code:** `{selected_code}`\n"
                        f"⚠️ **Warning:** This action cannot be undone!\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    ),
                    color=discord.Color.yellow()
                )

                await select_interaction.response.edit_message(
                    embed=confirmation_embed,
                    view=confirm_view
                )

            select.callback = select_callback
            view = discord.ui.View()
            view.add_item(select)

            initial_embed = discord.Embed(
                title="🗑️ Delete Gift Code",
                description=(
                    f"**Instructions**\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"1️⃣ Select a gift code from the menu below\n"
                    f"2️⃣ Confirm your selection\n"
                    f"3️⃣ The code will be permanently deleted\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                ),
                color=discord.Color.blue()
            )

            await interaction.response.send_message(
                embed=initial_embed,
                view=view,
                ephemeral=True
            )

        except Exception as e:
            self.logger.exception(f"Delete gift code error: {str(e)}")
            await interaction.response.send_message(
                "❌ An error occurred while processing the request.",
                ephemeral=True
            )

    async def delete_gift_channel(self, interaction: discord.Interaction):
        admin_info = await self.get_admin_info(interaction.user.id)
        if not admin_info:
            await interaction.response.send_message(
                "❌ You are not authorized to perform this action.",
                ephemeral=True
            )
            return

        available_alliances = await self.get_available_alliances(interaction)
        if not available_alliances:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="❌ No Available Alliances",
                    description="You don't have access to any alliances.",
                    color=discord.Color.red()
                ),
                ephemeral=True
            )
            return

        self.cursor.execute("SELECT alliance_id, channel_id FROM giftcode_channel")
        current_channels = dict(self.cursor.fetchall())

        alliances_with_counts = []
        for alliance_id, name in available_alliances:
            if alliance_id in current_channels:
                with sqlite3.connect('db/users.sqlite') as users_db:
                    cursor = users_db.cursor()
                    cursor.execute("SELECT COUNT(*) FROM users WHERE alliance = ?", (alliance_id,))
                    member_count = cursor.fetchone()[0]
                    alliances_with_counts.append((alliance_id, name, member_count))

        if not alliances_with_counts:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="❌ No Channels Set",
                    description="There are no gift code channels set for your alliances.",
                    color=discord.Color.red()
                ),
                ephemeral=True
            )
            return

        remove_embed = discord.Embed(
            title="🗑️ Remove Gift Code Channel",
            description=(
                "Select an alliance to remove its gift code channel:\n\n"
                "**Current Log Channels**\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "Select an alliance from the list below:\n"
            ),
            color=discord.Color.red()
        )

        view = AllianceSelectView(alliances_with_counts, self)

        async def alliance_callback(select_interaction: discord.Interaction):
            try:
                alliance_id = int(view.current_select.values[0])
                
                self.cursor.execute("SELECT channel_id FROM giftcode_channel WHERE alliance_id = ?", (alliance_id,))
                channel_id = self.cursor.fetchone()[0]
                
                alliance_name = next((name for aid, name in available_alliances if aid == alliance_id), "Unknown Alliance")
                
                confirm_embed = discord.Embed(
                    title="⚠️ Confirm Removal",
                    description=(
                        f"Are you sure you want to remove the gift code channel for:\n\n"
                        f"🏰 **Alliance:** {alliance_name}\n"
                        f"📝 **Channel:** <#{channel_id}>\n\n"
                        "This action cannot be undone!"
                    ),
                    color=discord.Color.yellow()
                )

                confirm_view = discord.ui.View()
                
                async def confirm_callback(button_interaction: discord.Interaction):
                    try:
                        self.cursor.execute("DELETE FROM giftcode_channel WHERE alliance_id = ?", (alliance_id,))
                        self.conn.commit()

                        success_embed = discord.Embed(
                            title="✅ Gift Code Channel Removed",
                            description=(
                                f"Successfully removed gift code channel for:\n\n"
                                f"🏰 **Alliance:** {alliance_name}\n"
                                f"📝 **Channel:** <#{channel_id}>"
                            ),
                            color=discord.Color.green()
                        )

                        await button_interaction.response.edit_message(
                            embed=success_embed,
                            view=None
                        )

                    except Exception as e:
                        self.logger.exception(f"Error removing gift code channel: {e}")
                        await button_interaction.response.send_message(
                            "❌ An error occurred while removing the gift code channel.",
                            ephemeral=True
                        )

                async def cancel_callback(button_interaction: discord.Interaction):
                    cancel_embed = discord.Embed(
                        title="❌ Removal Cancelled",
                        description="The gift code channel removal has been cancelled.",
                        color=discord.Color.red()
                    )
                    await button_interaction.response.edit_message(
                        embed=cancel_embed,
                        view=None
                    )

                confirm_button = discord.ui.Button(
                    label="Confirm",
                    emoji="✅",
                    style=discord.ButtonStyle.danger,
                    custom_id="confirm_remove"
                )
                confirm_button.callback = confirm_callback

                cancel_button = discord.ui.Button(
                    label="Cancel",
                    emoji="❌",
                    style=discord.ButtonStyle.secondary,
                    custom_id="cancel_remove"
                )
                cancel_button.callback = cancel_callback

                confirm_view.add_item(confirm_button)
                confirm_view.add_item(cancel_button)

                if not select_interaction.response.is_done():
                    await select_interaction.response.edit_message(
                        embed=confirm_embed,
                        view=confirm_view
                    )
                else:
                    await select_interaction.message.edit(
                        embed=confirm_embed,
                        view=confirm_view
                    )

            except Exception as e:
                self.logger.exception(f"Error in alliance selection: {e}")
                if not select_interaction.response.is_done():
                    await select_interaction.response.send_message(
                        "❌ An error occurred while processing your selection.",
                        ephemeral=True
                    )
                else:
                    await select_interaction.followup.send(
                        "❌ An error occurred while processing your selection.",
                        ephemeral=True
                    )

        view.callback = alliance_callback

        await interaction.response.send_message(
            embed=remove_embed,
            view=view,
            ephemeral=True
        )

    async def setup_giftcode_auto(self, interaction: discord.Interaction):
        admin_info = await self.get_admin_info(interaction.user.id)
        if not admin_info:
            await interaction.response.send_message(
                "❌ You are not authorized to perform this action.",
                ephemeral=True
            )
            return

        available_alliances = await self.get_available_alliances(interaction)
        if not available_alliances:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="❌ No Available Alliances",
                    description="You don't have access to any alliances.",
                    color=discord.Color.red()
                ),
                ephemeral=True
            )
            return

        self.cursor.execute("SELECT alliance_id, status FROM giftcodecontrol")
        current_status = dict(self.cursor.fetchall())

        alliances_with_counts = []
        for alliance_id, name in available_alliances:
            with sqlite3.connect('db/users.sqlite') as users_db:
                cursor = users_db.cursor()
                cursor.execute("SELECT COUNT(*) FROM users WHERE alliance = ?", (alliance_id,))
                member_count = cursor.fetchone()[0]
                alliances_with_counts.append((alliance_id, name, member_count))

        auto_gift_embed = discord.Embed(
            title="⚙️ Auto Gift Code Settings",
            description=(
                "Select an alliance to configure auto gift code:\n\n"
                "**Alliance List**\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "Select an alliance from the list below:\n"
            ),
            color=discord.Color.blue()
        )

        view = AllianceSelectView(alliances_with_counts, self)
        
        view.current_select.options.insert(0, discord.SelectOption(
            label="ENABLE ALL ALLIANCES",
            value="enable_all",
            description="Enable auto gift code for all alliances",
            emoji="✅"
        ))
        
        view.current_select.options.insert(1, discord.SelectOption(
            label="DISABLE ALL ALLIANCES",
            value="disable_all",
            description="Disable auto gift code for all alliances",
            emoji="❌"
        ))

        async def alliance_callback(select_interaction: discord.Interaction):
            try:
                selected_value = view.current_select.values[0]
                
                if selected_value in ["enable_all", "disable_all"]:
                    status = 1 if selected_value == "enable_all" else 0
                    
                    for alliance_id, _, _ in alliances_with_counts:
                        self.cursor.execute(
                            """
                            INSERT INTO giftcodecontrol (alliance_id, status) 
                            VALUES (?, ?) 
                            ON CONFLICT(alliance_id) 
                            DO UPDATE SET status = excluded.status
                            """,
                            (alliance_id, status)
                        )
                    self.conn.commit()

                    status_text = "enabled" if status == 1 else "disabled"
                    success_embed = discord.Embed(
                        title="✅ Auto Gift Code Setting Updated",
                        description=(
                            f"**Configuration Details**\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"🌐 **Scope:** All Alliances\n"
                            f"📊 **Status:** Auto gift code {status_text}\n"
                            f"👤 **Updated by:** {select_interaction.user.mention}\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        ),
                        color=discord.Color.green()
                    )
                    
                    await select_interaction.response.edit_message(
                        embed=success_embed,
                        view=None
                    )
                    return

                alliance_id = int(selected_value)
                alliance_name = next((name for aid, name in available_alliances if aid == alliance_id), "Unknown")

                current_setting = "enabled" if current_status.get(alliance_id, 0) == 1 else "disabled"
                
                confirm_embed = discord.Embed(
                    title="⚙️ Auto Gift Code Configuration",
                    description=(
                        f"**Alliance Details**\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🏰 **Alliance:** {alliance_name}\n"
                        f"📊 **Current Status:** Auto gift code is {current_setting}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"Do you want to enable or disable auto gift code for this alliance?"
                    ),
                    color=discord.Color.yellow()
                )

                confirm_view = discord.ui.View()
                
                async def button_callback(button_interaction: discord.Interaction):
                    try:
                        status = 1 if button_interaction.data['custom_id'] == "confirm" else 0
                        
                        self.cursor.execute(
                            """
                            INSERT INTO giftcodecontrol (alliance_id, status) 
                            VALUES (?, ?) 
                            ON CONFLICT(alliance_id) 
                            DO UPDATE SET status = excluded.status
                            """,
                            (alliance_id, status)
                        )
                        self.conn.commit()

                        status_text = "enabled" if status == 1 else "disabled"
                        success_embed = discord.Embed(
                            title="✅ Auto Gift Code Setting Updated",
                            description=(
                                f"**Configuration Details**\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"🏰 **Alliance:** {alliance_name}\n"
                                f"📊 **Status:** Auto gift code {status_text}\n"
                                f"👤 **Updated by:** {button_interaction.user.mention}\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            ),
                            color=discord.Color.green()
                        )
                        
                        await button_interaction.response.edit_message(
                            embed=success_embed,
                            view=None
                        )

                    except Exception as e:
                        self.logger.exception(f"Button callback error: {str(e)}")
                        if not button_interaction.response.is_done():
                            await button_interaction.response.send_message(
                                "❌ An error occurred while updating the settings.",
                                ephemeral=True
                            )
                        else:
                            await button_interaction.followup.send(
                                "❌ An error occurred while updating the settings.",
                                ephemeral=True
                            )

                confirm_button = discord.ui.Button(
                    label="Enable",
                    emoji="✅",
                    style=discord.ButtonStyle.success,
                    custom_id="confirm"
                )
                confirm_button.callback = button_callback

                deny_button = discord.ui.Button(
                    label="Disable",
                    emoji="❌",
                    style=discord.ButtonStyle.danger,
                    custom_id="deny"
                )
                deny_button.callback = button_callback

                confirm_view.add_item(confirm_button)
                confirm_view.add_item(deny_button)

                if not select_interaction.response.is_done():
                    await select_interaction.response.edit_message(
                        embed=confirm_embed,
                        view=confirm_view
                    )
                else:
                    await select_interaction.message.edit(
                        embed=confirm_embed,
                        view=confirm_view
                    )

            except Exception as e:
                self.logger.exception(f"Error in alliance selection: {e}")
                if not select_interaction.response.is_done():
                    await select_interaction.response.send_message(
                        "❌ An error occurred while processing your selection.",
                        ephemeral=True
                    )
                else:
                    await select_interaction.followup.send(
                        "❌ An error occurred while processing your selection.",
                        ephemeral=True
                    )

        view.callback = alliance_callback

        await interaction.response.send_message(
            embed=auto_gift_embed,
            view=view,
            ephemeral=True
        )

    async def use_giftcode_for_alliance(self, alliance_id, giftcode):
        MEMBER_PROCESS_DELAY = 1.0
        API_RATE_LIMIT_COOLDOWN = 60.0
        CAPTCHA_CYCLE_COOLDOWN = 60.0
        MAX_RETRY_CYCLES = 10

        self.logger.info(f"\nGiftOps: Starting use_giftcode_for_alliance for Alliance {alliance_id}, Code {giftcode}")

        try:
            # Initial Setup (Get channel, alliance name)
            self.alliance_cursor.execute("SELECT channel_id FROM alliancesettings WHERE alliance_id = ?", (alliance_id,))
            channel_result = self.alliance_cursor.fetchone()
            self.alliance_cursor.execute("SELECT name FROM alliance_list WHERE alliance_id = ?", (alliance_id,))
            name_result = self.alliance_cursor.fetchone()
            if not channel_result or not name_result: return False
            channel_id, alliance_name = channel_result[0], name_result[0]
            channel = self.bot.get_channel(channel_id)
            if not channel: return False

            # Initial Code Check
            self.cursor.execute("SELECT status FROM user_giftcodes WHERE fid = ? AND giftcode = ?", ("244886619", giftcode))
            validation_status = self.cursor.fetchone()
            if validation_status and validation_status[0] in ["TIME_ERROR", "CDK_NOT_FOUND", "USAGE_LIMIT"]: return False

            # Get Members
            with sqlite3.connect('db/users.sqlite') as users_conn:
                users_cursor = users_conn.cursor()
                users_cursor.execute("SELECT fid, nickname FROM users WHERE alliance = ?", (str(alliance_id),))
                members = users_cursor.fetchall()
            if not members: return False

            total_members = len(members)
            self.logger.info(f"GiftOps: Found {total_members} members for {alliance_name}.")

            # Initialize State
            processed_count = 0
            success_count = 0
            received_count = 0
            failed_count = 0
            successful_users = []
            already_used_users = []
            failed_users_dict = {}

            retry_queue = []
            active_members_to_process = []

            # Check Cache & Populate Initial List
            member_ids = [m[0] for m in members]
            placeholders = ','.join('?' * len(member_ids))
            self.cursor.execute(f"SELECT fid, status FROM user_giftcodes WHERE giftcode = ? AND fid IN ({placeholders})", (giftcode, *member_ids))
            cached_member_statuses = dict(self.cursor.fetchall())

            for fid, nickname in members:
                if fid in cached_member_statuses:
                    status = cached_member_statuses[fid]
                    if status in ["SUCCESS", "RECEIVED", "SAME TYPE EXCHANGE"]:
                        received_count += 1
                        already_used_users.append(nickname)
                    else:
                        failed_count += 1
                        failed_users_dict[fid] = (nickname, f"Cached Status: {status}", 0)
                    processed_count += 1
                else:
                    active_members_to_process.append((fid, nickname, 0))
            self.logger.info(f"GiftOps: Pre-processed {len(cached_member_statuses)} members from cache. {len(active_members_to_process)} remaining.")

            # Progress Embed
            embed = discord.Embed(title=f"🎁 Gift Code Redemption: {giftcode}", color=discord.Color.blue())
            def update_embed_description():
                return (
                    f"**Status for Alliance:** `{alliance_name}`\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"👥 **Total Members:** `{total_members}`\n"
                    f"✅ **Success:** `{success_count}`\n"
                    f"ℹ️ **Already Redeemed:** `{received_count}`\n"
                    f"🔄 **Retrying:** `{len(retry_queue)}`\n"
                    f"❌ **Failed:** `{failed_count}`\n"
                    f"⏳ **Processed:** `{processed_count}/{total_members}`\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                )
            embed.description = update_embed_description()
            try: status_message = await channel.send(embed=embed)
            except Exception as e: self.logger.exception(f"GiftOps: Error sending initial status embed: {e}"); return False

            # Main Processing Loop
            last_embed_update = time.time()

            while active_members_to_process or retry_queue:
                current_time = time.time()

                # Dequeue Ready Retries
                ready_to_retry = []
                remaining_in_queue = []
                for item in retry_queue:
                    if current_time >= item[3]:
                         ready_to_retry.append(item[:3])
                    else:
                         remaining_in_queue.append(item)
                retry_queue = remaining_in_queue
                active_members_to_process.extend(ready_to_retry)

                if not active_members_to_process:
                    if retry_queue:
                        next_retry_ts = min(item[3] for item in retry_queue)
                        wait_time = max(0.1, next_retry_ts - current_time)
                        await asyncio.sleep(wait_time)
                    else:
                        break
                    continue

                # Process One Member
                fid, nickname, current_cycle_count = active_members_to_process.pop(0)

                self.logger.info(f"GiftOps: Processing FID {fid} ({nickname}), Cycle {current_cycle_count + 1}/{MAX_RETRY_CYCLES}")

                response_status = "ERROR"
                try:
                    await asyncio.sleep(random.uniform(MEMBER_PROCESS_DELAY * 0.7, MEMBER_PROCESS_DELAY * 1.3))
                    response_status = await self.claim_giftcode_rewards_wos(fid, giftcode)
                except Exception as claim_err:
                     self.logger.exception(f"GiftOps: Unexpected error during claim for {fid}: {claim_err}")
                     response_status = "ERROR"

                # Handle Response
                mark_processed = False
                add_to_failed = False
                queue_for_retry = False
                retry_delay = 0
                next_cycle_count = current_cycle_count
                fail_reason = ""

                if response_status == "SUCCESS":
                    success_count += 1
                    successful_users.append(nickname)
                    mark_processed = True
                elif response_status in ["RECEIVED", "SAME TYPE EXCHANGE"]:
                    received_count += 1
                    already_used_users.append(nickname)
                    mark_processed = True
                elif response_status in ["TIME_ERROR", "CDK_NOT_FOUND", "USAGE_LIMIT", "LOGIN_FAILED", "LOGIN_EXPIRED_MID_PROCESS", "ERROR", "UNKNOWN_API_RESPONSE", "OCR_DISABLED", "SOLVER_ERROR", "CAPTCHA_FETCH_ERROR"]: # Permanent failures
                    add_to_failed = True
                    mark_processed = True
                    fail_reason = f"Processing Error ({response_status})"
                elif response_status == "TIMEOUT_RETRY":
                    queue_for_retry = True
                    retry_delay = API_RATE_LIMIT_COOLDOWN
                    fail_reason = "API Rate Limited"
                    next_cycle_count = current_cycle_count
                elif response_status in ["CAPTCHA_INVALID", "MAX_CAPTCHA_ATTEMPTS_REACHED", "OCR_FAILED_ATTEMPT"]:
                     next_cycle_count = current_cycle_count + 1
                     if next_cycle_count < MAX_RETRY_CYCLES:
                         queue_for_retry = True
                         retry_delay = CAPTCHA_CYCLE_COOLDOWN
                         fail_reason = "Captcha Cycle Failed"
                         self.logger.info(f"GiftOps: FID {fid} failed captcha cycle {next_cycle_count}. Queuing for retry cycle {next_cycle_count + 1} in {retry_delay}s.")
                     else:
                         add_to_failed = True
                         mark_processed = True
                         fail_reason = f"Failed after {MAX_RETRY_CYCLES} captcha cycles (Last Status: {response_status})"
                         self.logger.info(f"GiftOps: Max ({MAX_RETRY_CYCLES}) retry cycles reached for FID {fid}. Marking as failed.")

                # Update State Based on Outcome
                if mark_processed:
                    processed_count += 1
                    if add_to_failed:
                        failed_count += 1
                        failed_users_dict[fid] = (nickname, fail_reason, next_cycle_count)
                elif queue_for_retry:
                     retry_after_ts = time.time() + retry_delay
                     retry_queue.append((fid, nickname, next_cycle_count, retry_after_ts))


                # Update Embed Periodically
                current_time = time.time()
                if current_time - last_embed_update > 5:
                     embed.description = update_embed_description()
                     try:
                         await status_message.edit(embed=embed)
                         last_embed_update = current_time
                     except Exception as embed_edit_err:
                         self.logger.warning(f"GiftOps: WARN - Failed to edit progress embed: {embed_edit_err}")

            # Final Embed Update
            embed.title = f"🎁 Gift Code Process Complete: {giftcode}"
            embed.color = discord.Color.green() if failed_count == 0 else discord.Color.orange() if success_count > 0 else discord.Color.red()
            embed.description = update_embed_description()
            try: await status_message.edit(embed=embed)
            except Exception as final_embed_err: self.logger.warning(f"GiftOps: WARN - Failed to edit final progress embed: {final_embed_err}")

            summary_lines = [
                "\n",
                "--- Redemption Summary Start ---",
                f"Alliance: {alliance_name} ({alliance_id})",
                f"Gift Code: {giftcode}",
                f"Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                "------------------------",
                f"Total Members: {total_members}",
                f"Successful: {success_count}",
                f"Already Redeemed: {received_count}",
                f"Failed: {failed_count}",
                "------------------------",
            ]

            if successful_users:
                summary_lines.append(f"\nSuccessful Users ({len(successful_users)}):")
                summary_lines.extend(successful_users)

            if already_used_users:
                summary_lines.append(f"\nAlready Used/Cached Users ({len(already_used_users)}):")
                summary_lines.extend(already_used_users)

            if failed_users_dict:
                summary_lines.append(f"\nFailed Users ({len(failed_users_dict)}):")
                for fid, (nick, reason, cycles) in failed_users_dict.items():
                     summary_lines.append(f"- {nick} ({fid}): {reason} (Cycles Attempted: {cycles})")

            summary_lines.append("--- Redemption Summary End ---\n")
            summary_log_message = "\n".join(summary_lines)
            self.logger.info(summary_log_message)
            return True
        
        except Exception as e:
            self.logger.exception(f"GiftOps: UNEXPECTED ERROR in use_giftcode_for_alliance for {alliance_id}/{giftcode}: {str(e)}")
            self.logger.exception(f"Traceback: {traceback.format_exc()}")
            try:
                if 'channel' in locals() and channel: await channel.send(f"⚠️ An unexpected error occurred processing `{giftcode}` for {alliance_name}.")
            except Exception: pass
            return False

class CreateGiftCodeModal(discord.ui.Modal):
    def __init__(self, cog):
        super().__init__(title="Create Gift Code")
        self.cog = cog
        
        self.giftcode = discord.ui.TextInput(
            label="Gift Code",
            placeholder="Enter the gift code",
            required=True,
            min_length=4,
            max_length=20
        )
        self.add_item(self.giftcode)
    
    async def on_submit(self, interaction: discord.Interaction):
        logger = self.cog.logger
        logger.info("[CreateGiftCodeModal] on_submit started.")

        await interaction.response.defer(ephemeral=True)
        logger.info("[CreateGiftCodeModal] Interaction deferred.")

        code = self.giftcode.value
        logger.info(f"[CreateGiftCodeModal] Code entered: {code}")

        try:
            logger.info(f"[CreateGiftCodeModal] Calling claim_giftcode_rewards_wos for code: {code}")
            status = await self.cog.claim_giftcode_rewards_wos("244886619", code)
            logger.info(f"[CreateGiftCodeModal] claim_giftcode_rewards_wos returned status: {status}")

            embed = discord.Embed(title="🎁 Gift Code Creation")

            if status in ["SUCCESS", "RECEIVED", "SAME TYPE EXCHANGE"]:
                logger.info(f"[CreateGiftCodeModal] Status is SUCCESS/RECEIVED/SAME TYPE for {code}.")
                self.cog.cursor.execute("SELECT 1 FROM gift_codes WHERE giftcode = ?", (code,))
                if not self.cog.cursor.fetchone():
                    logger.info(f"[CreateGiftCodeModal] Code {code} is new. Inserting into DB.")
                    date = datetime.now().strftime("%Y-%m-%d")
                    try:
                        self.cog.cursor.execute(
                            "INSERT INTO gift_codes (giftcode, date) VALUES (?, ?)",
                            (code, date)
                        )
                        self.cog.conn.commit()
                        logger.info(f"[CreateGiftCodeModal] Code '{code}' inserted successfully.")

                        try:
                            logger.info(f"[CreateGiftCodeModal] Creating task to add code '{code}' to API.")
                            asyncio.create_task(self.cog.api.add_giftcode(code))
                            logger.info(f"[CreateGiftCodeModal] API add task created for '{code}'.")
                        except Exception as api_err:
                            logger.exception(f"[CreateGiftCodeModal] Error creating task for api.add_giftcode for '{code}': {api_err}")

                        embed.title = "✅ Gift Code Created"
                        embed.description = (
                            f"**Gift Code Details**\n━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"🎁 **Gift Code:** `{code}`\n"
                            f"✅ **Status:** Successfully created and added.\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        )
                        embed.color = discord.Color.green()

                    except sqlite3.Error as db_err:
                         logger.exception(f"[CreateGiftCodeModal] Error inserting gift code '{code}' (DB Insert): {db_err}")
                         embed.title = "❌ Database Error"
                         embed.description = f"Failed to save gift code `{code}` to the database. Please check logs."
                         embed.color = discord.Color.red()

                else:
                    logger.info(f"[CreateGiftCodeModal] Code {code} already exists in DB.")
                    embed.title = "ℹ️ Gift Code Exists"
                    embed.description = (
                        f"**Gift Code Details**\n━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🎁 **Gift Code:** `{code}`\n"
                        f"❌ **Status:** Already exists in database.\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    )
                    embed.color = discord.Color.blue()

            elif status == "TIME_ERROR":
                logger.warning(f"[CreateGiftCodeModal] Code {code} reported as expired.")
                embed.title = "❌ Invalid Code"
                embed.description = (
                    f"**Gift Code Details**\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🎁 **Gift Code:** `{code}`\n"
                    f"❌ **Status:** Gift code has expired.\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                )
                embed.color = discord.Color.red()

            elif status == "CDK_NOT_FOUND":
                logger.warning(f"[CreateGiftCodeModal] Code {code} reported as not found.")
                embed.title = "❌ Invalid Code"
                embed.description = (
                    f"**Gift Code Details**\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🎁 **Gift Code:** `{code}`\n"
                    f"❌ **Status:** Invalid gift code.\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                )
                embed.color = discord.Color.red()

            elif status == "USAGE_LIMIT":
                logger.warning(f"[CreateGiftCodeModal] Code {code} reported as usage limit reached.")
                embed.title = "❌ Invalid Code"
                embed.description = (
                    f"**Gift Code Details**\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🎁 **Gift Code:** `{code}`\n"
                    f"❌ **Status:** Usage limit has been reached.\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                )
                embed.color = discord.Color.red()
            else:
                logger.warning(f"[CreateGiftCodeModal] Unhandled status '{status}' for code {code}.")
                embed.title = "⚠️ Validation Warning"
                embed.description = (
                    f"**Gift Code Details**\n━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🎁 **Gift Code:** `{code}`\n"
                    f"⚠️ **Status:** Could not definitively validate the code status (`{status}`). It was **not** added. Please check logs or try again later.\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                )
                embed.color = discord.Color.orange()

            logger.info(f"[CreateGiftCodeModal] Attempting to send followup for code {code} with status {status}.")
            await interaction.followup.send(embed=embed, ephemeral=True)
            logger.info(f"[CreateGiftCodeModal] Followup sent successfully for code {code}.")

        except sqlite3.IntegrityError:
            logger.error(f"[CreateGiftCodeModal] IntegrityError for code {code} - Already exists (should have been caught).")
            await interaction.followup.send(
                "❌ This gift code already exists in the database!",
                ephemeral=True
            )
        except Exception as e:
            logger.exception(f"[CreateGiftCodeModal] Uncaught exception processing code {code}: {e}")
            try:
                await interaction.followup.send(
                    "❌ An unexpected error occurred while processing the gift code creation. Please check bot logs.",
                    ephemeral=True
                )
            except Exception as followup_err:
                logger.error(f"[CreateGiftCodeModal] Failed to send error followup to user: {followup_err}")

class DeleteGiftCodeModal(discord.ui.Modal, title="Delete Gift Code"):
    def __init__(self, cog):
        super().__init__()
        self.cog = cog
        
    giftcode = discord.ui.TextInput(
        label="Gift Code",
        placeholder="Enter the gift code to delete",
        required=True
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        code = self.giftcode.value
        
        self.cog.cursor.execute("SELECT 1 FROM gift_codes WHERE giftcode = ?", (code,))
        if not self.cog.cursor.fetchone():
            await interaction.response.send_message(
                "❌ Gift code not found!",
                ephemeral=True
            )
            return
            
        self.cog.cursor.execute("DELETE FROM gift_codes WHERE giftcode = ?", (code,))
        self.cog.cursor.execute("DELETE FROM user_giftcodes WHERE giftcode = ?", (code,))
        self.cog.conn.commit()
        
        embed = discord.Embed(
            title="✅ Gift Code Deleted",
            description=f"Gift code `{code}` has been deleted successfully.",
            color=discord.Color.green()
        )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

class GiftView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Create Gift Code",
        style=discord.ButtonStyle.green,
        custom_id="create_gift",
        emoji="🎫",
        row=0
    )
    async def create_gift(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.create_gift_code(interaction)
        
    @discord.ui.button(
        label="CAPTCHA Settings",
        style=discord.ButtonStyle.primary,
        custom_id="ocr_settings",
        emoji="🔍",
        row=0
    )
    async def ocr_settings_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.show_ocr_settings(interaction)

    @discord.ui.button(
        label="List Gift Codes",
        style=discord.ButtonStyle.blurple,
        custom_id="list_gift",
        emoji="📋",
        row=0
    )
    async def list_gift(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.list_gift_codes(interaction)

    @discord.ui.button(
        label="Auto Gift Settings",
        style=discord.ButtonStyle.grey,
        custom_id="auto_gift_settings",
        emoji="⚙️",
        row=1
    )
    async def auto_gift_settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.setup_giftcode_auto(interaction)

    @discord.ui.button(
        label="Delete Gift Code",
        emoji="❌",
        style=discord.ButtonStyle.danger,
        custom_id="delete_gift"
    )
    async def delete_gift_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await self.cog.delete_gift_code(interaction)
        except Exception as e:
            self.logger.exception(f"Delete gift button error: {e}")
            await interaction.response.send_message(
                "❌ An error occurred while processing delete request.",
                ephemeral=True
            )

    @discord.ui.button(
        label="Gift Code Channel",
        emoji="📢",
        style=discord.ButtonStyle.primary,
        custom_id="gift_channel"
    )
    async def gift_channel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await self.cog.setup_gift_channel(interaction)
        except Exception as e:
            self.logger.exception(f"Gift channel button error: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ An error occurred while setting up gift channel.",
                    ephemeral=True
                )

    @discord.ui.button(
        label="Delete Gift Channel",
        emoji="🗑️",
        style=discord.ButtonStyle.danger,
        custom_id="delete_gift_channel"
    )
    async def delete_gift_channel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await self.cog.delete_gift_channel(interaction)
        except Exception as e:
            self.logger.exception(f"Delete gift channel button error: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ An error occurred while deleting gift channel.",
                    ephemeral=True
                )

    @discord.ui.button(
        label="Use Gift Code for Alliance",
        emoji="🎯",
        style=discord.ButtonStyle.primary,
        custom_id="use_gift_alliance"
    )
    async def use_gift_alliance_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            admin_info = await self.cog.get_admin_info(interaction.user.id)
            if not admin_info:
                await interaction.response.send_message(
                    "❌ You are not authorized to perform this action.",
                    ephemeral=True
                )
                return

            available_alliances = await self.cog.get_available_alliances(interaction)
            if not available_alliances:
                await interaction.response.send_message(
                    embed=discord.Embed(
                        title="❌ No Available Alliances",
                        description="You don't have access to any alliances.",
                        color=discord.Color.red()
                    ),
                    ephemeral=True
                )
                return

            alliances_with_counts = []
            for alliance_id, name in available_alliances:
                with sqlite3.connect('db/users.sqlite') as users_db:
                    cursor = users_db.cursor()
                    cursor.execute("SELECT COUNT(*) FROM users WHERE alliance = ?", (alliance_id,))
                    member_count = cursor.fetchone()[0]
                    alliances_with_counts.append((alliance_id, name, member_count))

            alliance_embed = discord.Embed(
                title="🎯 Use Gift Code for Alliance",
                description=(
                    "Select an alliance to use gift code:\n\n"
                    "**Alliance List**\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    "Select an alliance from the list below:\n"
                ),
                color=discord.Color.blue()
            )

            view = AllianceSelectView(alliances_with_counts, self.cog)
            
            view.current_select.options.insert(0, discord.SelectOption(
                label="ALL ALLIANCES",
                value="all",
                description=f"Apply to all {len(alliances_with_counts)} alliances",
                emoji="🌐"
            ))

            async def alliance_callback(select_interaction: discord.Interaction):
                try:
                    selected_value = view.current_select.values[0]
                    
                    if selected_value == "all":
                        all_alliances = [aid for aid, name, _ in alliances_with_counts]
                    else:
                        alliance_id = int(selected_value)
                        all_alliances = [alliance_id]
                    
                    self.cog.cursor.execute("""
                        SELECT giftcode, date FROM gift_codes
                        ORDER BY date DESC
                    """)
                    gift_codes = self.cog.cursor.fetchall()

                    if not gift_codes:
                        await select_interaction.response.edit_message(
                            content="No gift codes available.",
                            view=None
                        )
                        return

                    giftcode_embed = discord.Embed(
                        title="🎁 Select Gift Code",
                        description=(
                            "Select a gift code to use:\n\n"
                            "**Gift Code List**\n"
                            "━━━━━━━━━━━━━━━━━━━━━━\n"
                            "Select a gift code from the list below:\n"
                        ),
                        color=discord.Color.blue()
                    )

                    select_giftcode = discord.ui.Select(
                        placeholder="Select a gift code",
                        options=[
                            discord.SelectOption(
                                label=f"Code: {code}",
                                value=code,
                                description=f"Created: {date}",
                                emoji="🎁"
                            ) for code, date in gift_codes
                        ]
                    )

                    async def giftcode_callback(giftcode_interaction: discord.Interaction):
                        try:
                            selected_code = giftcode_interaction.data["values"][0]
                            
                            confirm_embed = discord.Embed(
                                title="⚠️ Confirm Gift Code Usage",
                                description=(
                                    f"Are you sure you want to use this gift code?\n\n"
                                    f"**Details**\n"
                                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                    f"🎁 **Gift Code:** `{selected_code}`\n"
                                    f"🏰 **Alliances:** {'ALL' if selected_value == 'all' else next((name for aid, name, _ in alliances_with_counts if aid == alliance_id), 'Unknown')}\n"
                                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                ),
                                color=discord.Color.yellow()
                            )

                            confirm_view = discord.ui.View()
                            
                            async def confirm_callback(button_interaction: discord.Interaction):
                                try:
                                    progress_embed = discord.Embed(
                                        title="🎁 Gift Code Distribution Progress",
                                        description=(
                                            f"**Overall Progress**\n"
                                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                            f"🎁 **Gift Code:** `{selected_code}`\n"
                                            f"🏰 **Total Alliances:** `{len(all_alliances)}`\n"
                                            f"⏳ **Current Alliance:** `Starting...`\n"
                                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                        ),
                                        color=discord.Color.blue()
                                    )
                                    
                                    await button_interaction.response.edit_message(
                                        content=None,
                                        embed=progress_embed,
                                        view=None
                                    )
                                    
                                    completed = 0
                                    for aid in all_alliances:
                                        alliance_name = next((name for a_id, name, _ in alliances_with_counts if a_id == aid), 'Unknown')
                                        
                                        progress_embed.description = (
                                            f"**Overall Progress**\n"
                                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                            f"🎁 **Gift Code:** `{selected_code}`\n"
                                            f"🏰 **Total Alliances:** `{len(all_alliances)}`\n"
                                            f"⏳ **Current Alliance:** `{alliance_name}`\n"
                                            f"📊 **Progress:** `{completed}/{len(all_alliances)}`\n"
                                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                        )
                                        await button_interaction.edit_original_response(embed=progress_embed)
                                        
                                        result = await self.cog.use_giftcode_for_alliance(aid, selected_code)
                                        if result:
                                            completed += 1
                                        
                                        await asyncio.sleep(5)
                                    
                                    final_embed = discord.Embed(
                                        title="✅ Gift Code Distribution Complete",
                                        description=(
                                            f"**Final Status**\n"
                                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                            f"🎁 **Gift Code:** `{selected_code}`\n"
                                            f"🏰 **Total Alliances:** `{len(all_alliances)}`\n"
                                            f"✅ **Completed:** `{completed}/{len(all_alliances)}`\n"
                                            f"⏰ **Time:** <t:{int(datetime.now().timestamp())}:R>\n"
                                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                        ),
                                        color=discord.Color.green()
                                    )
                                    
                                    await button_interaction.edit_original_response(embed=final_embed)

                                except Exception as e:
                                    self.logger.exception(f"Error using gift code: {e}")
                                    await button_interaction.followup.send(
                                        "❌ An error occurred while using the gift code.",
                                        ephemeral=True
                                    )

                            async def cancel_callback(button_interaction: discord.Interaction):
                                cancel_embed = discord.Embed(
                                    title="❌ Operation Cancelled",
                                    description="The gift code usage has been cancelled.",
                                    color=discord.Color.red()
                                )
                                await button_interaction.response.edit_message(
                                    embed=cancel_embed,
                                    view=None
                                )

                            confirm_button = discord.ui.Button(
                                label="Confirm",
                                emoji="✅",
                                style=discord.ButtonStyle.success,
                                custom_id="confirm"
                            )
                            confirm_button.callback = confirm_callback

                            cancel_button = discord.ui.Button(
                                label="Cancel",
                                emoji="❌",
                                style=discord.ButtonStyle.danger,
                                custom_id="cancel"
                            )
                            cancel_button.callback = cancel_callback

                            confirm_view.add_item(confirm_button)
                            confirm_view.add_item(cancel_button)

                            await giftcode_interaction.response.edit_message(
                                embed=confirm_embed,
                                view=confirm_view
                            )

                        except Exception as e:
                            self.logger.exception(f"Error in gift code selection: {e}")
                            if not giftcode_interaction.response.is_done():
                                await giftcode_interaction.response.send_message(
                                    "❌ An error occurred while processing your selection.",
                                    ephemeral=True
                                )
                            else:
                                await giftcode_interaction.followup.send(
                                    "❌ An error occurred while processing your selection.",
                                    ephemeral=True
                                )

                    select_giftcode.callback = giftcode_callback
                    giftcode_view = discord.ui.View()
                    giftcode_view.add_item(select_giftcode)

                    if not select_interaction.response.is_done():
                        await select_interaction.response.edit_message(
                            embed=giftcode_embed,
                            view=giftcode_view
                        )
                    else:
                        await select_interaction.message.edit(
                            embed=giftcode_embed,
                            view=giftcode_view
                        )

                except Exception as e:
                    self.logger.exception(f"Error in alliance selection: {e}")
                    if not select_interaction.response.is_done():
                        await select_interaction.response.send_message(
                            "❌ An error occurred while processing your selection.",
                            ephemeral=True
                        )
                    else:
                        await select_interaction.followup.send(
                            "❌ An error occurred while processing your selection.",
                            ephemeral=True
                        )

            view.callback = alliance_callback

            await interaction.response.send_message(
                embed=alliance_embed,
                view=view,
                ephemeral=True
            )

        except Exception as e:
            self.logger.exception(f"Error in use_gift_alliance_button: {str(e)}")
            await interaction.response.send_message(
                "❌ An error occurred while processing the request.",
                ephemeral=True
            )

    @discord.ui.button(
        label="Main Menu",
        emoji="🏠",
        style=discord.ButtonStyle.secondary,
        custom_id="main_menu"
    )
    async def main_menu_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            alliance_cog = self.cog.bot.get_cog("Alliance")
            if alliance_cog:
                try:
                    await interaction.message.edit(content=None, embed=None, view=None)
                except:
                    pass
                await alliance_cog.show_main_menu(interaction)
        except:
            pass

class GPUDeviceModal(discord.ui.Modal, title="GPU Device Selection"):
    def __init__(self, cog, current_device_id, original_interaction):
        super().__init__()
        self.cog = cog
        self.original_interaction = original_interaction
        self.device_id = discord.ui.TextInput(
            label="GPU Device ID (0, 1, etc.)",
            placeholder="Enter GPU device ID (usually 0)",
            default=str(current_device_id),
            required=True
        )
        self.add_item(self.device_id)

    async def on_submit(self, modal_interaction: discord.Interaction):
        await modal_interaction.response.defer(ephemeral=True)
        try:
            device_id = int(self.device_id.value)
            success, message = await self.cog.update_ocr_settings(
                modal_interaction,
                gpu_device=device_id
            )
            await modal_interaction.followup.send(f"{'✅' if success else '❌'} {message}", ephemeral=True)
            await self.cog.show_ocr_settings(self.original_interaction)
        except ValueError:
            await modal_interaction.followup.send("❌ Please enter a valid device ID (integer).", ephemeral=True)
        except Exception as e:
            logger = getattr(self.cog, 'logger', None)
            if logger:
                logger.exception(f"Error in GPUDeviceModal on_submit: {e}")
            else:
                print(f"Error in GPUDeviceModal on_submit: {e}")
                traceback.print_exc()
            await modal_interaction.followup.send("❌ An error occurred while setting the GPU device.", ephemeral=True)

    async def on_submit(self, modal_interaction: discord.Interaction):
        await modal_interaction.response.defer(ephemeral=True)
        try:
            device_id = int(self.device_id.value)

            success, message = await self.cog.update_ocr_settings(
                modal_interaction,
                gpu_device=device_id
            )

            await modal_interaction.followup.send(
                f"{'✅' if success else '❌'} {message}",
                ephemeral=True
            )

            await self.cog.show_ocr_settings(self.original_interaction)

        except ValueError:
            await modal_interaction.followup.send(
                "❌ Please enter a valid device ID (integer).",
                ephemeral=True
            )
        except Exception as e:
            self.logger.exception(f"Error in GPUDeviceModal on_submit: {e}")
            traceback.print_exc()
            await modal_interaction.followup.send(
                "❌ An error occurred while setting the GPU device.",
                ephemeral=True
            )

class OCRSettingsView(discord.ui.View):
    def __init__(self, cog, ocr_settings, libraries_installed):
        super().__init__(timeout=None)
        self.cog = cog
        self.enabled, self.use_gpu, self.gpu_device, self.save_images_setting = ocr_settings
        self.libraries_installed = libraries_installed
        self.disable_controls = not libraries_installed

        # Button Updates
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = self.disable_controls
                if item.custom_id == "enable_ocr":
                    item.label = "Disable CAPTCHA Solver" if self.enabled == 1 else "Enable CAPTCHA Solver"
                    item.style = discord.ButtonStyle.danger if self.enabled == 1 else discord.ButtonStyle.success
                    item.emoji = "🚫" if self.enabled == 1 else "✅"
                elif item.custom_id == "toggle_gpu":
                    item.label = "Use CPU" if self.use_gpu == 1 else "Use GPU"
                    item.style = discord.ButtonStyle.danger if self.use_gpu == 1 else discord.ButtonStyle.success

            # Select Menu for Image Saving
            elif isinstance(item, discord.ui.Select) and item.custom_id == "image_save_select":
                 item.disabled = self.disable_controls
                 for option in item.options:
                     option.default = (str(self.save_images_setting) == option.value)


    # Row 0: Enable/Disable OCR, Toggle GPU
    @discord.ui.button(emoji="✅", custom_id="enable_ocr", row=0)
    async def enable_ocr_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.libraries_installed:
            await interaction.response.send_message("❌ Required libraries are not installed.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        new_enabled = 1 if self.enabled == 0 else 0
        success, message = await self.cog.update_ocr_settings(interaction, enabled=new_enabled)
        await interaction.followup.send(f"{'✅' if success else '❌'} {message}", ephemeral=True)
        await self.cog.show_ocr_settings(interaction)

    @discord.ui.button(emoji="🖥️", custom_id="toggle_gpu", row=1)
    async def toggle_gpu_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.libraries_installed:
            await interaction.response.send_message("❌ Required libraries are not installed.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        new_use_gpu = 1 if self.use_gpu == 0 else 0
        success, message = await self.cog.update_ocr_settings(interaction, use_gpu=new_use_gpu)
        await interaction.followup.send(f"{'✅' if success else '❌'} {message}", ephemeral=True)
        await self.cog.show_ocr_settings(interaction)

    # Row 1: Set GPU Device
    @discord.ui.button(label="Set GPU Device", style=discord.ButtonStyle.primary, emoji="🎯", custom_id="set_gpu_device", row=1)
    async def set_gpu_device_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        button.disabled = self.disable_controls
        if not self.libraries_installed:
            await interaction.response.send_message("❌ Required libraries are not installed.", ephemeral=True)
            return
        modal = GPUDeviceModal(self.cog, self.gpu_device, interaction)
        await interaction.response.send_modal(modal)

    # Row 2: Image Saving Select
    @discord.ui.select(
        placeholder="Select Captcha Image Saving Option",
        min_values=1, max_values=1, row=2, custom_id="image_save_select",
        options=[
            discord.SelectOption(label="Don't Save Any Images", value="0", description=None),
            discord.SelectOption(label="Save Only Failed Captchas", value="1", description=None),
            discord.SelectOption(label="Save Only Successful Captchas", value="2", description=None),
            discord.SelectOption(label="Save All Captchas (High Disk Usage!)", value="3", description=None)
        ]
    )
    async def image_save_select_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        select.disabled = self.disable_controls
        if not self.libraries_installed:
             await interaction.response.send_message("❌ Required libraries are not installed.", ephemeral=True)
             return

        await interaction.response.defer(ephemeral=True)
        try:
            selected_value = int(select.values[0])
            success, message = await self.cog.update_ocr_settings(interaction, save_images=selected_value)
            await interaction.followup.send(f"{'✅' if success else '❌'} {message}", ephemeral=True)
            await self.cog.show_ocr_settings(interaction)
        except ValueError:
             await interaction.followup.send("❌ Invalid selection value.", ephemeral=True)
        except Exception as e:
            self.cog.logger.exception("Error processing image save selection.")
            await interaction.followup.send("❌ An error occurred while updating image saving settings.", ephemeral=True)

    # Test CAPTCHA Solver Button for testing - uncomment when needed
    # @discord.ui.button(label="Test CAPTCHA Solver", style=discord.ButtonStyle.success, emoji="🧪", custom_id="test_ocr", row=0)
    # async def test_ocr_button(self, interaction: discord.Interaction, button: discord.ui.Button):
    #     button.disabled = self.disable_controls
    #     user_id = interaction.user.id
    #     current_time = time.time()
    #     logger = self.cog.logger

    #     last_test_time = self.cog.test_captcha_cooldowns.get(user_id, 0)
    #     if current_time - last_test_time < self.cog.test_captcha_delay:
    #         remaining_time = int(self.cog.test_captcha_delay - (current_time - last_test_time))
    #         await interaction.response.send_message(f"❌ Please wait {remaining_time} more seconds before testing again.", ephemeral=True)
    #         return

    #     if not self.libraries_installed:
    #         await interaction.response.send_message("❌ Required libraries (like easyocr, opencv) are not installed on the bot server.", ephemeral=True)
    #         return
    #     if not self.cog.captcha_solver:
    #         await interaction.response.send_message("❌ CAPTCHA solver is not initialized. Please ensure it's enabled and libraries are installed.", ephemeral=True)
    #         return

    #     await interaction.response.defer(ephemeral=True)
    #     logger.info(f"[Test Button] User {user_id} triggered test. Interaction deferred.")

    #     captcha_image_base64, error = None, None
    #     captcha_code, success, method, confidence = None, False, None, None
    #     lock_acquired = False
    #     test_fid = "244886619"

    #     try:
    #         self.cog.test_captcha_cooldowns[user_id] = current_time
    #         logger.info(f"[Test Button] Attempting to acquire validation lock for test FID {test_fid}...")

    #         try:
    #             async with asyncio.timeout(30.0):
    #                 async with self.cog._validation_lock:
    #                     lock_acquired = True
    #                     logger.info("[Test Button] Validation lock acquired.")
    #                     logger.info("[Test Button] Fetching captcha...")
    #                     captcha_image_base64, error = await self.cog.fetch_captcha(test_fid)
    #                     logger.info(f"[Test Button] Captcha fetch result: Error='{error}', HasImage={captcha_image_base64 is not None}")
    #             logger.info("[Test Button] Validation lock released.")
    #             lock_acquired = False

    #         except asyncio.TimeoutError:
    #             logger.warning(f"[Test Button] Timeout ({30.0}s) waiting to acquire validation lock for user {user_id}.")
    #             await interaction.followup.send("❌ Timed out waiting for internal resources. The system might be busy. Please try again shortly.", ephemeral=True)
    #             return
    #         except Exception as lock_err:
    #              logger.exception(f"[Test Button] Unexpected error during lock acquisition/release: {lock_err}")
    #              await interaction.followup.send("❌ An internal error occurred while managing resources. Please try again.", ephemeral=True)
    #              return

    #         if error:
    #             await interaction.followup.send(f"❌ Error fetching test captcha from the API: `{error}`", ephemeral=True)
    #             return

    #         if captcha_image_base64:
    #              logger.info("[Test Button] Solving fetched captcha...")
    #              start_solve_time = time.time()
    #              captcha_code, success, method, confidence = await self.cog.captcha_solver.solve_captcha(
    #                  captcha_image_base64, fid=f"test-{user_id}")
    #              solve_duration = time.time() - start_solve_time
    #              log_confidence_str = f'{confidence:.2f}' if confidence is not None else 'N/A'
    #              logger.info(f"[Test Button] Solve result: Success={success}, Code={captcha_code}, Method={method}, Conf={log_confidence_str}. Duration: {solve_duration:.2f}s")
    #         else:
    #              logger.error("[Test Button] ERROR - Captcha image base64 was empty after fetch.")
    #              await interaction.followup.send("❌ Internal error: Failed to retrieve captcha image data from the API.", ephemeral=True)
    #              return

    #         if success and isinstance(confidence, (float, int)):
    #             confidence_str = f'{confidence:.2f}'
    #         else:
    #             confidence_str = 'N/A'

    #         embed = discord.Embed(
    #             title="🧪 CAPTCHA Solver Test Results",
    #             description=(
    #                 f"**Test Summary**\n━━━━━━━━━━━━━━━━━━━━━━\n"
    #                 f"🤖 **OCR Success:** {'✅ Yes' if success else '❌ No'}\n"
    #                 f"🔍 **Recognized Code:** `{captcha_code if success else 'N/A'}`\n"
    #                 f"⚙️ **Method Used:** `{method if success else 'N/A'}`\n"
    #                 f"📊 **Confidence:** `{confidence_str}`\n"
    #                 f"⏱️ **Solve Time:** `{solve_duration:.2f}s`\n"
    #                 f"━━━━━━━━━━━━━━━━━━━━━━\n"
    #             ), color=discord.Color.green() if success else discord.Color.red()
    #         )

    #         self.cog.settings_cursor.execute("SELECT save_images FROM ocr_settings ORDER BY id DESC LIMIT 1")
    #         save_setting_row = self.cog.settings_cursor.fetchone()
    #         current_save_mode = save_setting_row[0] if save_setting_row else 0
    #         should_save_img = False
    #         save_tag = "UNKNOWN"

    #         if success and (current_save_mode == 2 or current_save_mode == 3): should_save_img = True; save_tag = captcha_code if captcha_code else "SUCCESS_NOCDE"
    #         elif not success and (current_save_mode == 1 or current_save_mode == 3): should_save_img = True; save_tag = "FAILED"

    #         if should_save_img and self.cog.captcha_solver and captcha_image_base64:
    #             logger.info(f"[Test Button] Attempting to save image based on mode {current_save_mode}. Status success={success}, tag={save_tag}")
    #             try:
    #                  if isinstance(captcha_image_base64, str) and captcha_image_base64.startswith("data:image"): img_base64_data = captcha_image_base64.split(",", 1)[1]
    #                  elif isinstance(captcha_image_base64, str): img_base64_data = captcha_image_base64
    #                  else: img_base64_data = None

    #                  if img_base64_data:
    #                      import base64, numpy as np, cv2, os
    #                      img_bytes = base64.b64decode(img_base64_data)
    #                      nparr = np.frombuffer(img_bytes, np.uint8)
    #                      img_np = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

    #                      if img_np is not None:
    #                          captcha_dir = self.cog.captcha_solver.captcha_dir
    #                          safe_tag = re.sub(r'[\\/*?:"<>|]', '_', save_tag)
    #                          test_filename = f"test_fid{user_id}_try0_OCR_{safe_tag}_{int(time.time())}.png"
    #                          test_path = os.path.join(captcha_dir, test_filename)
    #                          os.makedirs(captcha_dir, exist_ok=True)

    #                          if cv2.imwrite(test_path, img_np):
    #                               logger.info(f"[Test Button] Saved test captcha image to {test_path}")
    #                               embed.add_field(name="📸 Captcha Image", value=f"Saved to `{test_path}`", inline=False)
    #                          else:
    #                               logger.error(f"[Test Button] Failed to write test captcha image to {test_path} (cv2.imwrite returned False)")
    #                               embed.add_field(name="⚠️ Image Save Error", value="Failed to write image file (check permissions/path).", inline=False)
    #                      else:
    #                          logger.error(f"[Test Button] Failed to decode image with cv2 for saving.")
    #                          embed.add_field(name="⚠️ Image Save Error", value="Failed to decode image data for saving.", inline=False)
    #                  else:
    #                      logger.error(f"[Test Button] Could not extract base64 data for saving.")
    #                      embed.add_field(name="⚠️ Image Save Error", value="Could not decode image data string.", inline=False)
    #             except ImportError as lib_err:
    #                   logger.exception(f"[Test Button] Missing library for image saving: {lib_err}")
    #                   embed.add_field(name="⚠️ Image Save Error", value=f"Missing library required for saving: {lib_err}", inline=False)
    #             except Exception as img_save_err:
    #                   logger.exception(f"[Test Button] Error saving test image: {img_save_err}")
    #                   embed.add_field(name="⚠️ Image Save Error", value=f"Unexpected error during saving: {img_save_err}", inline=False)

    #         await interaction.followup.send(embed=embed, ephemeral=True)
    #         logger.info(f"[Test Button] Test completed for user {user_id}.")

    #     except Exception as e:
    #         logger.exception(f"[Test Button] UNEXPECTED Error during test for user {user_id}: {e}")
    #         if lock_acquired:
    #              try:
    #                  self.cog._validation_lock.release()
    #                  logger.info("[Test Button] Force-released validation lock due to unexpected error.")
    #              except RuntimeError:
    #                   pass
    #              except Exception as release_err:
    #                   logger.error(f"[Test Button] Error force-releasing lock: {release_err}")

    #         try:
    #              await interaction.followup.send(f"❌ An unexpected error occurred during the test: `{e}`. Please check the bot logs.", ephemeral=True)
    #         except Exception as followup_err:
    #              logger.error(f"[Test Button] Failed to send final error followup to user {user_id}: {followup_err}")

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, emoji="◀️", custom_id="back", row=1)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.show_gift_menu(interaction)

class ImageSaveSelect(discord.ui.Select):
    def __init__(self, cog, options):
        super().__init__(placeholder="Select Captcha Image Saving Option", min_values=1, max_values=1, options=options, row=1)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            selected_value = int(self.values[0])
            success, message = await self.cog.update_ocr_settings(
                interaction,
                save_images=selected_value
            )
            await interaction.followup.send(
                f"{'✅' if success else '❌'} {message}",
                ephemeral=True
            )
            await self.cog.show_ocr_settings(interaction)
        except ValueError:
             await interaction.followup.send("❌ Invalid selection value.", ephemeral=True)
        except Exception as e:
            self.cog.logger.exception("Error processing image save selection.")
            await interaction.followup.send("❌ An error occurred while updating image saving settings.", ephemeral=True)

async def setup(bot):
    await bot.add_cog(GiftOperations(bot)) 
