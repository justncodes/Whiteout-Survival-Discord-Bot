# Applying the CAPTCHA Solver Patch (Manual Installation)

This guide explains how to manually update your existing bot installation to include the new automatic CAPTCHA solving feature for gift code redemption.

Please note that until Relo is able to pull the code into the main repository, or provides a fix of his own, this patch will require manual installation.

**IMPORTANT:** Before proceeding, it is crucial to back up your bot's data to prevent loss in case anything goes wrong.

---

## Prerequisites

*   An existing, functional installation of the [Whiteout Survival Discord Bot](https://github.com/Reloisback/Whiteout-Survival-Discord-Bot).
*   Access to the server or machine where the bot is running.
*   Ability to download files and copy them into the bot's directory.
*   Python and pip installed on the system running the bot (should be already if the bot is working).

---

## Installation Steps

1.  **🛑 STOP THE BOT:** Ensure your Discord bot `main.py` script is not currently running.

2.  **🛡️ Backup Your Database:**
    *   Navigate to your bot's main directory.
    *   Locate the `db` folder.
    *   **COPY** (do **NOT** move) the entire `db` folder to a safe location outside of the bot's directory. This backup contains your user data, alliance settings, gift codes, etc. and can be restored in case you ever need to revert to the original code.

3.  **💾 Backup Existing Code (Recommended):**
    *   It's also wise to back up the specific Python files you are about to replace. Copy the following files from your current bot installation to your backup location:
        *   `main.py`
        *   `cogs/gift_operations.py`

4.  **⬇️ Download Patch Files:**
    *   Download the updated Python files for the CAPTCHA patch. You will need these specific files:
        *   `main.py`
        *   `cogs/gift_operations.py`
        *   `cogs/gift_captchasolver.py` (This is a new file)
    *   *(Download link for the patch files will be provided here shortly `[Download Patch Files]`)*

5.  **🔄 Replace/Add Files:**
    *   Go to your bot's main directory.
    *   Replace the existing `main.py` with the downloaded `main.py`.
    *   Go into the `cogs` sub-directory.
    *   Replace the existing `gift_operations.py` with the downloaded one.
    *   Add the new `gift_captchasolver.py` file into the `cogs` directory.

6.  **⚙️ Install Dependencies:**
    *   Open a terminal or command prompt **in your bot's main directory**.
    *   Run the bot's startup command as you normally would (e.g., `python main.py`).
    *   Observe the console output. The script should detect missing libraries (especially PyTorch and EasyOCR) and attempt to install them. This step might take several minutes, especially for PyTorch, depending on your internet connection.
    *   **If the automatic installation fails:** You may need to install them manually. The most common command would be:
        ```bash
        pip install easyocr torch torchvision torchaudio opencv-python pillow numpy PyYAML scipy
        ```
        *(Note: For CPU-only systems, PyTorch should ideally be installed using the specific CPU index URL as handled by the updated `main.py`. If manual install is needed, refer to the [PyTorch website](https://pytorch.org/get-started/locally/) for the correct CPU-only install command for your OS.)*

7.  **▶️ Restart the Bot:**
    *   If the dependency installation required a manual step, run the bot startup command again (e.g., `python main.py`).
    *   If the automatic installation in step 6 completed successfully, the bot should continue starting up.

8.  **✅ Verify:**
    *   Check the bot's console output for any errors during startup, especially related to `GiftOperations` or `GiftCaptchaSolver`. Look for messages indicating whether the solver initialized successfully (and if it's using CPU or GPU).
    *   Once the bot is online, try using the `/settings` command (or equivalent) and navigate to the Gift Code Operations menu to ensure it loads correctly.
    *   Test redeeming some gift codes. It should be working, although it may take longer than before.
---

## Post-Installation

*   The CAPTCHA solver should now be active (using CPU by default). It has approximately 70% accuracy at the moment. Improvements are being worked on, but it works for now.
*   In case any IDs end up with an Error result, you can always re-run the redemption for the same gift code and alliance again to hopefully redeem it successfully this time.
*   You can configure GPU usage (if available and desired) and other OCR settings via the bot's `/settings` -> Gift Code Operations -> CAPTCHA Settings menu.
*   You can monitor the bot's logs (`log/giftlog.txt` and `log/gift_ops.log`) for CAPTCHA-related messages during gift code processing.

If you encounter issues with this patch, please contact Yolo - Discord id: `yoloblaster`.

You can find the original readme [here](https://github.com/Reloisback/Whiteout-Survival-Discord-Bot/blob/main/README.md).