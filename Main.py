import os
import re
import shutil
import zipfile
import asyncio
from pathlib import Path
from typing import Optional, Tuple

import instaloader
from pyrogram.enums import ParseMode
from pyrogram import Client, filters
from pyrogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    Message, CallbackQuery
)
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("API_ID", "0") or 0)
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)

# Folders
BASE = Path(__file__).parent if "__file__" in globals() else Path.cwd()
SESS_DIR = BASE / "sessions"
DL_DIR = BASE / "downloads"
SESS_DIR.mkdir(exist_ok=True)
DL_DIR.mkdir(exist_ok=True)

if not (API_ID and API_HASH and BOT_TOKEN):
    raise SystemExit("API_ID, API_HASH, BOT_TOKEN are required (env vars)")

app = Client("ig_instaloader_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ----- State management (simple in-memory flow prompts) -----
# user_state[user_id] = (mode, extra)
user_state: dict[int, Tuple[str, Optional[str]]] = {}

# ----- UI Helpers -----

def main_menu(is_owner: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📥 Download", callback_data="menu_download")],
        [InlineKeyboardButton("🔑 Login", callback_data="menu_login")],
        [InlineKeyboardButton("🎬 Stories & Highlights", callback_data="menu_sh")],
    ]
    return InlineKeyboardMarkup(rows)


def download_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Reel", callback_data="dl_reel"),
         InlineKeyboardButton("Post", callback_data="dl_post")],
        [InlineKeyboardButton("Profile Photo", callback_data="dl_pfp")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_home")],
    ])


def login_menu(is_owner: bool) -> InlineKeyboardMarkup:
    rows = []
    if is_owner:
        rows.append([InlineKeyboardButton("Owner Login", callback_data="login_owner")])
    rows.append([InlineKeyboardButton("User Login", callback_data="login_user")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="back_home")])
    return InlineKeyboardMarkup(rows)


def sh_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Story", callback_data="sh_story"),
         InlineKeyboardButton("Highlights", callback_data="sh_highlights")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_home")],
    ])


# ----- Instaloader helpers -----

def make_loader() -> instaloader.Instaloader:
    L = instaloader.Instaloader(
        dirname_pattern=str(DL_DIR / "{target}"),
        download_videos=True,
        save_metadata=False,
        post_metadata_txt_pattern="",
    )
    # keep logs quiet
    try:
        L.context.log.setLevel("ERROR")
    except Exception:
        pass
    return L


def ig_session_file_for_user(tg_user_id: int) -> Path:
    return SESS_DIR / f"{tg_user_id}.session"


def load_user_session(L: instaloader.Instaloader, tg_user_id: int) -> bool:
    sess_file = ig_session_file_for_user(tg_user_id)
    if sess_file.exists():
        try:
            L.load_session_from_file(username=None, filename=str(sess_file))
            return True
        except Exception:
            return False
    return False


def save_user_session(L: instaloader.Instaloader, tg_user_id: int) -> None:
    sess_file = ig_session_file_for_user(tg_user_id)
    L.save_session_to_file(filename=str(sess_file))


async def run_blocking(func, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))


def extract_shortcode_from_url(url: str) -> Optional[str]:
    # Accept /p/<code>/, /reel/<code>/, /tv/<code>/ patterns
    m = re.search(r"/(p|reel|tv)/([A-Za-z0-9_-]+)/?", url)
    if m:
        return m.group(2)
    # If user just pasted a shortcode
    if re.fullmatch(r"[A-Za-z0-9_-]{5,25}", url):
        return url
    return None


async def send_folder_files(message: Message, folder: Path, caption_html: Optional[str] = None):
    files = []
    for root, _, fns in os.walk(folder):
        for fn in fns:
            p = Path(root) / fn
            # skip non-media junk
            if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".mp4", ".webp", ".mov"}:
                files.append(p)
    files.sort()

    if caption_html:
        # send caption first as a separate message (blockquote formatting)
        await message.reply_text(
            "📥 Download Complete\n────────────\n<blockquote>" + caption_html + "</blockquote>",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    if not files:
        await message.reply_text("No media files found.")
        return

    # send one by one to avoid album limits and keep it simple
    for p in files:
        try:
            await message.reply_document(str(p))
        except Exception as e:
            await message.reply_text(f"Failed sending file: {p.name} ({e})")


# ----- Command & Menu Handlers -----

@app.on_message(filters.command("start"))
async def start_cmd(_, m: Message):
    await m.reply_text(
        (
            "👋 Welcome!\n\n"
            "• Send an *Instagram URL* (post/reel/igtv) and I'll download it.\n"
            "• Send an *Instagram username* to get profile info + PFP.\n\n"
            "Use buttons for more options.\n\n"
            "❗ We do not store your password. If you login, we only save a session cookie so you don't have to login again."
        ),
        reply_markup=main_menu(is_owner=(m.from_user.id == OWNER_ID)),
        disable_web_page_preview=True,
    )


@app.on_callback_query()
async def cb_handler(_, cb: CallbackQuery):
    uid = cb.from_user.id
    data = cb.data or ""

    if data == "back_home":
        user_state.pop(uid, None)
        await cb.message.edit_text(
            "Main Menu:", reply_markup=main_menu(is_owner=(uid == OWNER_ID))
        )
        return

    if data == "menu_download":
        user_state.pop(uid, None)
        await cb.message.edit_text("Choose download type:", reply_markup=download_menu())
        return

    if data == "menu_login":
        user_state.pop(uid, None)
        await cb.message.edit_text("Login options:", reply_markup=login_menu(uid == OWNER_ID))
        return

    if data == "menu_sh":
        user_state.pop(uid, None)
        await cb.message.edit_text("Stories & Highlights:", reply_markup=sh_menu())
        return

    # --- Download submenu ---
    if data == "dl_reel":
        user_state[uid] = ("expect_link_reel", None)
        await cb.message.edit_text("Send Reel URL:")
        return
    if data == "dl_post":
        user_state[uid] = ("expect_link_post", None)
        await cb.message.edit_text("Send Post URL:")
        return
    if data == "dl_pfp":
        user_state[uid] = ("expect_username_pfp", None)
        await cb.message.edit_text("Send Username to get Profile Photo:")
        return

    # --- Login submenu ---
    if data == "login_owner":
        if uid != OWNER_ID:
            await cb.answer("Only owner can use this.", show_alert=True)
            return
        user_state[uid] = ("expect_login_owner", None)
        await cb.message.edit_text(
            "Send as: <code>username,password</code>\n\n"
            "❗ Password is NOT stored. Only a session cookie is saved.",
            parse_mode=ParseMode.HTML,
        )
        return
    if data == "login_user":
        user_state[uid] = ("expect_login_user", None)
        await cb.message.edit_text(
            "Send as: <code>username,password</code>\n\n"
            "❗ We do NOT store your password. Only a session cookie is saved for your Telegram account.",
            parse_mode=ParseMode.HTML,
        )
        return

    # --- Stories & Highlights ---
    if data == "sh_story":
        user_state[uid] = ("expect_username_story", None)
        await cb.message.edit_text("Send Username to download STORIES (login may be required for private).")
        return
    if data == "sh_highlights":
        user_state[uid] = ("expect_username_highlights", None)
        await cb.message.edit_text("Send Username to download HIGHLIGHTS (login may be required for private).")
        return


# ----- Text input handler (routes by state or auto-detect) -----

@app.on_message(filters.text & ~filters.command(["start"]))
async def text_router(_, m: Message):
    uid = m.from_user.id
    txt = m.text.strip()
    state = user_state.get(uid)

    # 1) If user is in a prompted state, handle that first
    if state:
        mode, _ = state
        try:
            if mode in ("expect_link_reel", "expect_link_post"):
                await handle_link_download(m, txt)
                user_state.pop(uid, None)
                return
            if mode == "expect_username_pfp":
                await handle_profile_photo(m, txt)
                user_state.pop(uid, None)
                return
            if mode == "expect_login_owner":
                await handle_login(m, txt, owner_only=True)
                user_state.pop(uid, None)
                return
            if mode == "expect_login_user":
                await handle_login(m, txt, owner_only=False)
                user_state.pop(uid, None)
                return
            if mode == "expect_username_story":
                await handle_stories(m, txt)
                user_state.pop(uid, None)
                return
            if mode == "expect_username_highlights":
                await handle_highlights(m, txt)
                user_state.pop(uid, None)
                return
        except Exception as e:
            await m.reply_text(f"Error: {e}")
            return

    # 2) No state → auto-detect
    if "instagram.com" in txt:
        await handle_link_download(m, txt)
        return

    # If looks like a username (letters, dots, underscores)
    if re.fullmatch(r"[A-Za-z0-9._]+", txt):
        await handle_profile_info(m, txt)
        return

    await m.reply_text("Send a valid Instagram link or a username.")


# ----- Handlers Implementation -----

async def handle_login(m: Message, creds: str, owner_only: bool):
    uid = m.from_user.id
    if owner_only and uid != OWNER_ID:
        await m.reply_text("Only owner can login here.")
        return

    if "," not in creds:
        await m.reply_text("Send as: username,password")
        return

    username, password = [x.strip() for x in creds.split(",", 1)]
    L = make_loader()

    await m.reply_text("Attempting login...")
    try:
        # NOTE: 2FA interactive is not supported via chat. Use non-2FA accounts.
        await run_blocking(L.login, username, password)
        save_user_session(L, uid)
        await m.reply_text("✅ Login successful. Session saved. We do NOT store your password.")
    except Exception as e:
        await m.reply_text(f"❌ Login failed: {e}\nIf 2FA is enabled, disable it or provide a pre-saved session.")


async def handle_link_download(m: Message, url: str):
    uid = m.from_user.id
    L = make_loader()
    load_user_session(L, uid)  # use session if available (helps with rate limits/private)

    sc = extract_shortcode_from_url(url)
    if not sc:
        await m.reply_text("Could not parse shortcode from URL. Send full post/reel/igtv link.")
        return

    await m.reply_text("Downloading... This may take a moment.")

    def do_post(shortcode: str, tgt: str):
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        L.download_post(post, target=tgt)
        return post

    # use unique target per task
    tgt_name = f"{uid}_{sc}"
    tgt_folder = DL_DIR / tgt_name
    if tgt_folder.exists():
        shutil.rmtree(tgt_folder)

    try:
        post = await run_blocking(do_post, sc, tgt_name)
        # Build caption
        caption = (post.caption or "").strip()
        await send_folder_files(m, tgt_folder, caption_html=caption if caption else None)
    except instaloader.exceptions.LoginRequiredException:
        await m.reply_text("Private content requires login. Use the Login menu first.")
    except Exception as e:
        await m.reply_text(f"Download failed: {e}")


async def handle_profile_photo(m: Message, username: str):
    uid = m.from_user.id
    L = make_loader()
    load_user_session(L, uid)

    tgt_name = f"pfp_{uid}_{username}"
    tgt_folder = DL_DIR / tgt_name
    if tgt_folder.exists():
        shutil.rmtree(tgt_folder)

    def do_pfp(uname: str, tgt: str):
        profile = instaloader.Profile.from_username(L.context, uname)
        # download_profilepic saves inside target path named after profile
        L.download_profilepic(profile, target=tgt)
        return profile

    try:
        profile = await run_blocking(do_pfp, username, tgt_name)
        await m.reply_text(f"👤 @{profile.username}\nName: {profile.full_name}")
        await send_folder_files(m, DL_DIR / tgt_name, caption_html=None)
    except instaloader.exceptions.LoginRequiredException:
        await m.reply_text("Private profile photo requires login. Use the Login menu first.")
    except Exception as e:
        await m.reply_text(f"Failed: {e}")


async def handle_profile_info(m: Message, username: str):
    uid = m.from_user.id
    L = make_loader()
    load_user_session(L, uid)

    def do_info(uname: str):
        profile = instaloader.Profile.from_username(L.context, uname)
        return {
            "username": profile.username,
            "full_name": profile.full_name or "",
            "bio": profile.biography or "",
            "posts": profile.mediacount,
            "followers": profile.followers,
            "following": profile.followees,
            "is_private": profile.is_private,
            "user_id": profile.userid,
            "profile": profile,
        }

    try:
        info = await run_blocking(do_info, username)
        bio_html = (info["bio"].strip() or "-")
        priv = "🔒 Private" if info["is_private"] else "🔓 Public"
        text = (
            f"👤 Profile Info for: @{info['username']}\n"
            f"📝 Name: {info['full_name']}\n"
            f"📸 Posts: {info['posts']}\n"
            f"👥 Followers: {info['followers']}\n"
            f"➡️ Following: {info['following']}\n"
            f"{priv}\n\n"
            f"<blockquote>{bio_html}</blockquote>"
        )
        await m.reply_text(text, parse_mode=ParseMode.HTML)
        # send profile photo too
        await handle_profile_photo(m, info["username"])
    except instaloader.exceptions.LoginRequiredException:
        await m.reply_text("This profile requires login to view. Use the Login menu first.")
    except Exception as e:
        await m.reply_text(f"Failed to fetch profile info: {e}")


async def handle_stories(m: Message, username: str):
    uid = m.from_user.id
    L = make_loader()
    if not load_user_session(L, uid):
        await m.reply_text("Stories for private accounts require login. Use the Login menu first.")
        return

    tgt_name = f"stories_{uid}_{username}"
    tgt_folder = DL_DIR / tgt_name
    if tgt_folder.exists():
        shutil.rmtree(tgt_folder)

    def do_stories(uname: str, tgt: Path):
        prof = instaloader.Profile.from_username(L.context, uname)
        count = 0
        for story in L.get_stories(userids=[prof.userid]):
            for item in story.get_items():
                L.download_storyitem(item, target=str(tgt))
                count += 1
        return count

    try:
        cnt = await run_blocking(do_stories, username, tgt_folder)
        if cnt == 0:
            await m.reply_text("No stories found (or not visible).")
            return
        await send_folder_files(m, tgt_folder, caption_html=None)
    except Exception as e:
        await m.reply_text(f"Failed to download stories: {e}")


async def handle_highlights(m: Message, username: str):
    uid = m.from_user.id
    L = make_loader()
    if not load_user_session(L, uid):
        await m.reply_text("Highlights for private accounts require login. Use the Login menu first.")
        return

    tgt_root = DL_DIR / f"highlights_{uid}_{username}"
    if tgt_root.exists():
        shutil.rmtree(tgt_root)
    tgt_root.mkdir(parents=True, exist_ok=True)

    def do_highlights(uname: str, tgt: Path):
        prof = instaloader.Profile.from_username(L.context, uname)
        total = 0
        for hl in L.get_highlights(prof.userid):
            hdir = tgt / f"highlight_{hl.unique_id}"
            hdir.mkdir(exist_ok=True)
            for item in hl.get_items():
                L.download_storyitem(item, target=str(hdir))
                total += 1
        return total

    try:
        total = await run_blocking(do_highlights, username, tgt_root)
        if total == 0:
            await m.reply_text("No highlights found (or not visible).")
            return
        # Zip each highlight folder for convenience
        for folder in sorted(tgt_root.iterdir()):
            if folder.is_dir():
                zip_path = folder.with_suffix('.zip')
                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for root, _, files in os.walk(folder):
                        for f in files:
                            fp = Path(root) / f
                            zf.write(fp, arcname=fp.relative_to(folder))
                await m.reply_document(str(zip_path), caption=f"{folder.name}")
    except Exception as e:
        await m.reply_text(f"Failed to download highlights: {e}")


# ----- Run bot -----

if __name__ == "__main__":
    print("Starting Instagram Downloader Bot (Instaloader)...")
    app.run()
  
