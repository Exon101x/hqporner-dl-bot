# HQPorner Telegram Bot (Multi-User Pro Edition)

An asynchronous, feature-rich Telegram bot with **native 2 GB video upload support**, built on top of [`unofficial-api-for-hqporner`](https://github.com/EchterAlsFake/unofficial-api-for-hqporner) by **EchterAlsFake** and powered by [`Telethon`](https://github.com/LonamiWebs/Telethon) (MTProto).

## Credits & Upstream Attribution

This bot is a Telegram interface built using the open-source Python API wrapper created and maintained by [**EchterAlsFake**](https://github.com/EchterAlsFake):

* **Upstream API Repository:** [`EchterAlsFake/unofficial-api-for-hqporner`](https://github.com/EchterAlsFake/unofficial-api-for-hqporner)

* **Official API Documentation:** [`EchterAlsFake/API_Docs`](https://github.com/EchterAlsFake/API_Docs)

* **PyPI Package:** `unofficial-api-for-hqporner` (`hqporner_api`)

* **Support the Original API Creator:** [Ko-fi](https://ko-fi.com/EchterAlsFake) | [PayPal](https://paypal.me/EchterAlsFake)

All scraping, metadata parsing, and video download functionality is provided by EchterAlsFake's `hqporner_api` library.

## Features

* **Instant 2 GB Uploads (MTProto):** Uses [`Telethon`](https://github.com/LonamiWebs/Telethon) + `cryptg` to upload videos up to 2,000 MB directly over Telegram's MTProto layer—no C++ `telegram-bot-api` compilation required.

* **Refined Telegram UI:** Modern HTML `<blockquote>` cards, in-place menu navigation (`🏠 Home` / `⬅️ Back`), and compact numbered inline button grids (`🔎 1..5` preview and `⚡ 1..5` quick download).

* **Preview Thumbnails & Full Metadata:** Displays preview images (with `og:image` / `poster` fallback and `Referer` handling), duration, views, publish date, rating, cast, tags, and available qualities.

* **Animated Live Progress Cards:** Real-time download and upload progress with `▰▱` progress bars, spinner animation, transferred size, live speed (`MB/s`), ETA, and a `✖ Cancel` button.

* **Smart URL Handling:** Automatically normalizes desktop (`hqporner.com`), mobile (`m.hqporner.com`), `www.`, and schemeless links, supports multi-URL batch paste (up to 5 links per message), and routes `/category/` and `/actress/` URLs directly.

* **Pagination & Personal Library:** 25-item paginated results (`⬅️ Prev` / `Next ➡️`), persistent `⭐ Favorites`, and `🕒 History` (last 20 downloads).

* **Per-User Settings (`/settings`):** Default quality (`BEST`, `1080`, `720`, `480`, `360`), Upload Mode (`Streamable Video` vs `Document`), Auto-Download on Paste, Preview Thumbnails toggle, Spoiler Blur cover toggle, Results Per Page (`3`/`5`/`8`/`10`), and Clear History/Favorites.

* **Multi-User Rate Limiting:** Configurable command cooldown, per-user download cooldown, per-user concurrency limit, global concurrency slots, and daily UTC download quotas (admins automatically bypass limits).

* **Admin Controls (`/admin`):** Manage users with `/users`, `/ban`, `/unban`, `/broadcast`, `/cancelall`, `/queue`, and `/stats` (uptime, RAM, disk space, active jobs).

## Commands

| Command | Description | 
| ----- | ----- | 
| `/start`, `/help` | Open the interactive main dashboard | 
| `/video <url>` | Fetch preview thumbnail, metadata, and quality buttons | 
| `/search <query>` | Search videos with paginated results | 
| `/top [week|month|all_time]` | Browse top videos by timeframe | 
| `/random` | Fetch a random video preview | 
| `/brazzers` | Browse Brazzers videos | 
| `/category <name>` | Browse videos in a specific category | 
| `/actress <name>` | Browse videos by actress | 
| `/favorites` | View your saved favorites | 
| `/history` | View your recent downloads | 
| `/settings` | Customize quality, format, spoiler blur, auto-download, and page size | 
| `/queue` | View currently active download/upload jobs | 
| `/profile` | Check your User ID, role, daily quota, and download stats | 
| `/stats` | View bot uptime, RAM usage, disk free space, and active limits | 
| `/admin` | Show admin-only moderation commands (`/users`, `/ban`, `/unban`, `/broadcast`, `/cancelall`) | 

You can also paste 1–5 `hqporner.com` or `m.hqporner.com` links directly in chat without any command.

## Configuration (Environment Variables)

| Variable | Required | Default | Description | 
| ----- | ----- | ----- | ----- | 
| `TELEGRAM_BOT_TOKEN` | Yes | — | Bot token from [@BotFather](https://t.me/BotFather) | 
| `TELEGRAM_API_ID` | Yes | — | Telegram API ID from [my.telegram.org/apps](https://my.telegram.org/apps) | 
| `TELEGRAM_API_HASH` | Yes | — | Telegram API Hash from [my.telegram.org/apps](https://my.telegram.org/apps) | 
| `ADMIN_USER_IDS` | No | *(empty)* | Comma-separated admin Telegram user IDs (bypass rate limits & access `/admin`) | 
| `ALLOWED_USER_IDS` | No | *(empty)* | Comma-separated allowlist of user IDs (leave blank to allow all users) | 
| `MAX_UPLOAD_MB` | No | `2000` | Maximum video upload size in MB | 
| `MAX_CONCURRENT_DOWNLOADS` | No | `3` | Global parallel download slots | 
| `MAX_USER_CONCURRENT_DOWNLOADS` | No | `1` | Maximum simultaneous downloads per standard user | 
| `COMMAND_COOLDOWN_SEC` | No | `3` | Cooldown in seconds between commands/button taps | 
| `DOWNLOAD_COOLDOWN_SEC` | No | `30` | Cooldown in seconds between downloads per user | 
| `DAILY_DOWNLOAD_LIMIT` | No | `15` | Daily downloads allowed per user (`0` = unlimited, resets at `00:00 UTC`) | 
| `PAGE_SIZE` | No | `5` | Default number of results per page | 
| `DOWNLOAD_DIR` | No | System temp | Directory for temporary downloads (auto-cleaned after each upload) | 
| `BOT_DATA_FILE` | No | `bot_data.json` | JSON file storing favorites, history, settings, bans, and stats | 

## Quick Deployment Guides

### 1. Kaggle Notebook (Instant Run, \~15 Seconds)

1. In Kaggle, open **Notebook options** → set **Internet** to **On**.

2. Install dependencies:

   ```
   pip install -q unofficial-api-for-hqporner telethon cryptg
   
   ```

3. Export `TELEGRAM_BOT_TOKEN`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, and `ADMIN_USER_IDS` via `os.environ`, then launch with unbuffered output:

   ```
   python -u bot_kaggle.py
   
   ```

### 2. Linux VPS (Ubuntu / Debian, No Docker)

```
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install unofficial-api-for-hqporner telethon cryptg
export TELEGRAM_BOT_TOKEN="your_token"
export TELEGRAM_API_ID="your_api_id"
export TELEGRAM_API_HASH="your_api_hash"
export ADMIN_USER_IDS="your_telegram_id"
python -u bot_kaggle.py

```

For 24/7 operation on a VPS, run `bot_kaggle.py` under a `systemd` service with `Restart=on-failure` and `EnvironmentFile=/path/to/.env`.

### 3. Termux (Android `aarch64`)

```
pkg update && pkg upgrade -y
pkg install -y python git clang libffi openssl libxml2 libxslt python-lxml
python -m venv --system-site-packages .venv
source .venv/bin/activate
pip install unofficial-api-for-hqporner telethon cryptg
termux-wake-lock
python -u bot_kaggle.py

```

## License & Disclaimer

* **AGPLv3 License:** Because this bot integrates [`EchterAlsFake/unofficial-api-for-hqporner`](https://github.com/EchterAlsFake/unofficial-api-for-hqporner), which is licensed under **GNU AGPLv3**, hosting or distributing this bot (including over a network) requires complying with the AGPLv3 license—making your complete source code available under AGPLv3 unless you obtain a commercial license from EchterAlsFake (`EchterAlsFakeBS@proton.me`).

* **Disclaimer:** This is an unofficial, unaffiliated project. Users are responsible for complying with the target website's rules, copyright/licensing requirements, and applicable local laws. Do not use this bot to bypass access controls or scrape at disruptive rates.