[![AI Generated](https://img.shields.io/badge/AI_Generated-Gemini-blue.svg)](https://gemini.google.com)
## Acknowledgments

* **Code Generation:** The core logic and boilerplate for this project were generated using Gemini. All AI-generated code was subsequently reviewed and tested.
# Stream Monitor & Relay

A Python-based monitoring tool that checks specified Twitch and YouTube streams. When a stream goes live, it automatically relays the feed to a local RTMP server and sends a formatted notification to a Telegram chat, including an extracted frame for Twitch streams.

## Features
* **Multi-Platform Monitoring:** Checks Twitch via `streamlink` and YouTube via `yt-dlp`.
* **Automated RTMP Relay:** Pipes live streams directly to an RTMP target using `ffmpeg`.
* **Telegram Notifications:** Sends alerts with stream titles, links, and live frame captures.
* **Smart Override UI:** Includes a lightweight Flask web interface to manually set a temporary stream URL (e.g., for collabs).
* **Dependency Management:** Built utilizing `uv` for fast, deterministic virtual environments.

## Prerequisites
Ensure the following binaries are installed and accessible in your system's PATH:
* `python` (>= 3.10)
* `ffmpeg`
* `streamlink`
* `yt-dlp`
* `uv` (for Python dependency management)

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/shmilyhua/stream-monitor-relay.git
   cd stream-monitor-relay
   ```

2. Install dependencies using `uv`:
   ```bash
   uv venv --python 3.13
   uv sync
   ```

3. Environment Setup:
   Create a `.env` file in the root directory with your API credentials:
   ```env
   TELEGRAM_TOKEN=your_telegram_bot_token
   TELEGRAM_CHAT_ID=your_telegram_chat_id
   TWITCH_CLIENT_ID=your_twitch_client_id
   TWITCH_CLIENT_SECRET=your_twitch_client_secret
   ```

4. Configuration:
   Create a `config.json` file in the root directory. Modify the paths to match your environment:
   ```json
   {
       "NORMAL_TWITCH": "https://www.twitch.tv/target_channel",
       "NORMAL_YOUTUBE": "https://www.youtube.com/@target_channel/live",
       "TARGET": "rtmp://localhost/live/stream",
       "OVERRIDE_FILE": "override.txt",
       "TWITCH_OPTS": "--stream-sorting-excludes \">720p60\"",
       "TWITCH_QUALITY": "720p60,720p,best",
       "YOUTUBE_QUALITY": "-f \"bestvideo[height<=720]+bestaudio/best[height<=720]\" --fragment-retries 3",
       "FREE_CHAT_ID": "default_video_id_to_ignore",
       "COOKIES_FILE": "/path/to/youtube_cookies.txt",
       "TWITCH_CONFIG_FILE": "/path/to/config.twitch"
   }
   ```

## Usage

Run the script to start the monitoring loop and the Flask web UI:
```bash
uv run stream.py
```

Access the Override Web UI by navigating to `http://127.0.0.1:12450` in your browser.

## Logic Overview
1. **Twitch Check:** Attempts to start `streamlink`. If successful, it relays to RTMP and notifies Telegram.
2. **YouTube Check:** Uses `yt-dlp` to verify live status. If live, it executes `streamlink` with YouTube cookies, relays the stream, and sends an alert.
3. **Override Check:** If a URL is present in `override.txt` (set via the Web UI), it supersedes standard monitoring and attempts to relay the provided URL.
