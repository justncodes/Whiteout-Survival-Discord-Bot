import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

import sys
import os
import subprocess
import platform
import time

def check_and_install_requirements():
    required_packages = {
        'discord.py': 'discord.py',
        'colorama': 'colorama',
        'requests': 'requests',
        'aiohttp': 'aiohttp',
        'python-dotenv': 'python-dotenv',
        'aiohttp-socks': 'aiohttp-socks',
        'pytz': 'pytz',
        'pyzipper': 'pyzipper'
    }
    
    ocr_packages = {
        'numpy': 'numpy',
        'Pillow': 'Pillow',
        'opencv-python': 'opencv-python',
        'easyocr': 'easyocr',
    }

    torch_packages = ['torch', 'torchvision', 'torchaudio']

    installation_happened = False

    def install_package(package_name, command_args=None):
        """Installs a package using pip. Allows custom command args."""
        nonlocal installation_happened
        if command_args is None:
            command_args = [sys.executable, "-m", "pip", "install", package_name]
        else:
            command_args = [sys.executable, "-m", "pip"] + command_args

        try:
            print(f"Installing {package_name}...")
            print(f"Running command: {' '.join(command_args)}")
            # Use a timeout for potentially long installs
            subprocess.check_call(command_args, timeout=1200)
            print(f"{package_name} installed successfully.")
            installation_happened = True
            if any(p in package_name.lower() for p in torch_packages):
                print("Pausing briefly after torch installation...")
                time.sleep(5)
            return True
        except subprocess.TimeoutExpired:
            print(f"Error: Installation of {package_name} timed out (10 minutes).")
            return False
        except subprocess.CalledProcessError as e:
            print(f"Error installing {package_name}: {e}")
            return False
        except Exception as e:
            print(f"Unexpected error installing {package_name}: {e}")
            return False

    try:
        import pkg_resources
        installed_packages = {pkg.key for pkg in pkg_resources.working_set}
    except ImportError:
        # Try installing setuptools if pkg_resources fails (common issue)
        print("pkg_resources not found, attempting to install setuptools...")
        install_package('setuptools', ["install", "setuptools"])
        try:
            import pkg_resources
            installed_packages = {pkg.key for pkg in pkg_resources.working_set}
        except ImportError:
             print("FATAL: Could not import pkg_resources even after installing setuptools. Cannot check libraries.")
             sys.exit(1)

    packages_to_install = []

    for package_key, pip_name in required_packages.items():
        if package_key.lower() not in installed_packages:
            packages_to_install.append(pip_name)

    for check_name, install_name in ocr_packages.items():
         if check_name.lower() not in installed_packages:
             packages_to_install.append(install_name)

    missing_torch = [pkg for pkg in torch_packages if pkg.lower() not in installed_packages]

    if packages_to_install or missing_torch:
        print("\nMissing libraries detected. Starting installation...")

        for package in packages_to_install:
            success = install_package(package)
            if not success:
                print(f"ERROR: Failed to install '{package}'. Please try manually: pip install {package}")
                sys.exit(1)

        if missing_torch:
            print(f"\nMissing PyTorch components: {', '.join(missing_torch)}. Installing CPU version...")
            os_name = platform.system()
            torch_install_args = None
            cpu_index_url = "https://download.pytorch.org/whl/cpu"

            torch_install_args = ["install"] + torch_packages + ["--index-url", cpu_index_url]

            print(f"Attempting PyTorch CPU install for {os_name}...")
            success = install_package("PyTorch CPU Components", command_args=torch_install_args)
            if not success:
                print(f"ERROR: Failed to install PyTorch components.")
                print(f"Please try installing manually: pip install {' '.join(torch_packages)} --index-url {cpu_index_url}")
                print("Bot may not function correctly without PyTorch.")
                sys.exit(1)

        if installation_happened:
            print("\nRequired library installation process finished!")
            if missing_torch:
                 print("NOTE: PyTorch was installed. A restart of the script might be necessary.")
            return True

    else:
        print("All required libraries seem to be installed.")
        return False

if __name__ == "__main__":
    check_and_install_requirements()
    
    import discord
    from discord.ext import commands
    import sqlite3
    from colorama import Fore, Style, init
    import requests
    import asyncio
    import pkg_resources

    VERSION_URL = "https://raw.githubusercontent.com/Reloisback/Whiteout-Survival-Discord-Bot/refs/heads/main/autoupdateinfo.txt"

    def restart_bot():
        print(Fore.YELLOW + "\nRestarting bot..." + Style.RESET_ALL)
        python = sys.executable
        os.execl(python, python, *sys.argv)

    def setup_version_table():
        try:
            with sqlite3.connect('db/settings.sqlite') as conn:
                cursor = conn.cursor()
                cursor.execute('''CREATE TABLE IF NOT EXISTS versions (
                    file_name TEXT PRIMARY KEY,
                    version TEXT,
                    is_main INTEGER DEFAULT 0
                )''')
                conn.commit()
                print(Fore.GREEN + "Version table created successfully." + Style.RESET_ALL)
        except Exception as e:
            print(Fore.RED + f"Error creating version table: {e}" + Style.RESET_ALL)

    async def check_and_update_files():
        try:
            try:
                response = requests.get(VERSION_URL)
                if response.status_code == 200:
                    source_url = "https://raw.githubusercontent.com/Reloisback/Whiteout-Survival-Discord-Bot/refs/heads/main"
                    print(Fore.GREEN + "Connected to GitHub successfully." + Style.RESET_ALL)
                else:
                    raise requests.RequestException
            except requests.RequestException:
                print(Fore.YELLOW + "Cannot connect to GitHub, trying alternative source (wosland.com)..." + Style.RESET_ALL)
                alt_version_url = "https://wosland.com/wosdc/autoupdateinfo.txt"
                response = requests.get(alt_version_url)
                if response.status_code == 200:
                    source_url = "https://wosland.com/wosdc"
                    print(Fore.GREEN + "Connected to wosland.com successfully." + Style.RESET_ALL)
                else:
                    print(Fore.RED + "Failed to connect to both GitHub and wosland.com" + Style.RESET_ALL)
                    return False

            if not os.path.exists('cogs'):
                os.makedirs('cogs')
                print(Fore.GREEN + "cogs folder created" + Style.RESET_ALL)

            content = response.text.split('\n')
            documents = {}
            main_py_updated = False

            doc_section = False
            for line in content:
                if line.startswith("Documants;"):
                    doc_section = True
                    continue
                elif doc_section and line.startswith("Updated Info;"):
                    break
                elif doc_section and '=' in line:
                    file_name, version = [x.strip() for x in line.split('=')]
                    documents[file_name] = version

            update_notes = []
            update_section = False
            for line in content:
                if line.startswith("Updated Info;"):
                    update_section = True
                    continue
                if update_section and line.strip():
                    update_notes.append(line.strip())

            updates_needed = []
            with sqlite3.connect('db/settings.sqlite') as conn:
                cursor = conn.cursor()
                
                for file_name, new_version in documents.items():
                    cursor.execute("SELECT version FROM versions WHERE file_name = ?", (file_name,))
                    current_file_version = cursor.fetchone()
                    
                    if not current_file_version:
                        updates_needed.append((file_name, new_version))
                        if file_name == 'main.py':
                            main_py_updated = True
                    elif current_file_version[0] != new_version:
                        updates_needed.append((file_name, new_version))
                        if file_name == 'main.py':
                            main_py_updated = True

                if updates_needed:
                    print(Fore.YELLOW + "\nUpdates available!" + Style.RESET_ALL)
                    print(Fore.YELLOW + "\nIf this is your first installation and you see File and No version, please update!" + Style.RESET_ALL)
                    print("\nFiles to update:")
                    for file_name, new_version in updates_needed:
                        cursor.execute("SELECT version FROM versions WHERE file_name = ?", (file_name,))
                        current = cursor.fetchone()
                        current_version = current[0] if current else "File and No Version"
                        print(f"• {file_name}: {current_version} -> {new_version}")

                    print("\nUpdate Notes:")
                    for note in update_notes:
                        print(f"• {note}")

                    if main_py_updated:
                        print(Fore.YELLOW + "\nNOTE: This update includes changes to main.py. Bot will restart after update." + Style.RESET_ALL)

                    response = input("\nDo you want to update now? (y/n): ").lower()
                    if response == 'y':
                        needs_restart = False
                        
                        for file_name, new_version in updates_needed:
                            if file_name.strip() != 'main.py':
                                file_url = f"{source_url}/{file_name}"
                                file_response = requests.get(file_url)
                                
                                if file_response.status_code == 200:
                                    os.makedirs(os.path.dirname(file_name), exist_ok=True)
                                    content = file_response.text.rstrip('\n')
                                    with open(file_name, 'w', encoding='utf-8', newline='') as f:
                                        f.write(content)
                                    
                                    cursor.execute("""
                                        INSERT OR REPLACE INTO versions (file_name, version, is_main)
                                        VALUES (?, ?, ?)
                                    """, (file_name, new_version, 0))

                        if main_py_updated:
                            main_file_url = f"{source_url}/main.py"
                            main_response = requests.get(main_file_url)
                            
                            if main_response.status_code == 200:
                                content = main_response.text.rstrip('\n')
                                with open('main.py.new', 'w', encoding='utf-8', newline='') as f:
                                    f.write(content)
                                
                                cursor.execute("""
                                    INSERT OR REPLACE INTO versions (file_name, version, is_main)
                                    VALUES (?, ?, 1)
                                """, ('main.py', documents['main.py']))
                                
                                needs_restart = True

                        conn.commit()
                        print(Fore.GREEN + "\nAll updates completed successfully!" + Style.RESET_ALL)

                        if needs_restart:
                            if os.path.exists('main.py.bak'):
                                os.remove('main.py.bak')
                            os.rename('main.py', 'main.py.bak')
                            os.rename('main.py.new', 'main.py')
                            print(Fore.YELLOW + "\nRestarting bot to apply main.py updates..." + Style.RESET_ALL)
                            restart_bot()
                    else:
                        print(Fore.YELLOW + "\nUpdate skipped. Running with existing files." + Style.RESET_ALL)

            return False

        except Exception as e:
            print(Fore.RED + f"Error during version check: {e}" + Style.RESET_ALL)
            return False

    class CustomBot(commands.Bot):
        async def on_error(self, event_name, *args, **kwargs):
            if event_name == "on_interaction":
                error = sys.exc_info()[1]
                if isinstance(error, discord.NotFound) and error.code == 10062:
                    return
            
            await super().on_error(event_name, *args, **kwargs)

        async def on_command_error(self, ctx, error):
            if isinstance(error, discord.NotFound) and error.code == 10062:
                return
            await super().on_command_error(ctx, error)

    intents = discord.Intents.default()
    intents.message_content = True

    bot = CustomBot(command_prefix='/', intents=intents)

    init(autoreset=True)

    token_file = 'bot_token.txt'
    if not os.path.exists(token_file):
        bot_token = input("Enter the bot token: ")
        with open(token_file, 'w') as f:
            f.write(bot_token)
    else:
        with open(token_file, 'r') as f:
            bot_token = f.read().strip()

    if not os.path.exists('db'):
        os.makedirs('db')
        print(Fore.GREEN + "db folder created" + Style.RESET_ALL)

    databases = {
        "conn_alliance": "db/alliance.sqlite",
        "conn_giftcode": "db/giftcode.sqlite",
        "conn_changes": "db/changes.sqlite",
        "conn_users": "db/users.sqlite",
        "conn_settings": "db/settings.sqlite",
    }

    connections = {name: sqlite3.connect(path) for name, path in databases.items()}

    print(Fore.GREEN + "Database connections have been successfully established." + Style.RESET_ALL)

    def create_tables():
        with connections["conn_changes"] as conn_changes:
            conn_changes.execute('''CREATE TABLE IF NOT EXISTS nickname_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, 
                fid INTEGER, 
                old_nickname TEXT, 
                new_nickname TEXT, 
                change_date TEXT
            )''')
            conn_changes.execute('''CREATE TABLE IF NOT EXISTS furnace_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, 
                fid INTEGER, 
                old_furnace_lv INTEGER, 
                new_furnace_lv INTEGER, 
                change_date TEXT
            )''')

        with connections["conn_settings"] as conn_settings:
            conn_settings.execute('''CREATE TABLE IF NOT EXISTS botsettings (
                id INTEGER PRIMARY KEY, 
                channelid INTEGER, 
                giftcodestatus TEXT 
            )''')
            conn_settings.execute('''CREATE TABLE IF NOT EXISTS admin (
                id INTEGER PRIMARY KEY, 
                is_initial INTEGER
            )''')

        with connections["conn_users"] as conn_users:
            conn_users.execute('''CREATE TABLE IF NOT EXISTS users (
                fid INTEGER PRIMARY KEY, 
                nickname TEXT, 
                furnace_lv INTEGER DEFAULT 0, 
                kid INTEGER, 
                stove_lv_content TEXT, 
                alliance TEXT
            )''')

        with connections["conn_giftcode"] as conn_giftcode:
            conn_giftcode.execute('''CREATE TABLE IF NOT EXISTS gift_codes (
                giftcode TEXT PRIMARY KEY, 
                date TEXT
            )''')
            conn_giftcode.execute('''CREATE TABLE IF NOT EXISTS user_giftcodes (
                fid INTEGER, 
                giftcode TEXT, 
                status TEXT, 
                PRIMARY KEY (fid, giftcode),
                FOREIGN KEY (giftcode) REFERENCES gift_codes (giftcode)
            )''')

        with connections["conn_alliance"] as conn_alliance:
            conn_alliance.execute('''CREATE TABLE IF NOT EXISTS alliancesettings (
                alliance_id INTEGER PRIMARY KEY, 
                channel_id INTEGER, 
                interval INTEGER
            )''')
            conn_alliance.execute('''CREATE TABLE IF NOT EXISTS alliance_list (
                alliance_id INTEGER PRIMARY KEY, 
                name TEXT
            )''')

        print(Fore.GREEN + "All tables checked." + Style.RESET_ALL)

    create_tables()
    setup_version_table()  

    async def load_cogs():
        await bot.load_extension("cogs.olddb")
        await bot.load_extension("cogs.control")
        await bot.load_extension("cogs.alliance")
        await bot.load_extension("cogs.alliance_member_operations")
        await bot.load_extension("cogs.bot_operations")
        await bot.load_extension("cogs.logsystem")
        await bot.load_extension("cogs.support_operations")
        await bot.load_extension("cogs.gift_operations")
        await bot.load_extension("cogs.changes")
        await bot.load_extension("cogs.w")
        await bot.load_extension("cogs.wel")
        await bot.load_extension("cogs.other_features")
        await bot.load_extension("cogs.bear_trap")
        await bot.load_extension("cogs.id_channel")
        await bot.load_extension("cogs.backup_operations")
        await bot.load_extension("cogs.bear_trap_editor")

    @bot.event
    async def on_ready():
        try:
            print(f"{Fore.GREEN}Logged in as {Fore.CYAN}{bot.user}{Style.RESET_ALL}")
            synced = await bot.tree.sync()
        except Exception as e:
            print(f"Error syncing commands: {e}")

    async def main():
        if check_and_install_requirements():
            print(f"{Fore.GREEN}Library installations completed, starting bot...{Style.RESET_ALL}")
        
        await check_and_update_files()
        await load_cogs()
        await bot.start(bot_token)

    if __name__ == "__main__":
        asyncio.run(main())
