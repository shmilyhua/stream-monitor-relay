import subprocess
import time
import threading
import os
import signal
import sys
import html
import json
import requests
import re
import atexit
import shlex
from datetime import datetime
from flask import Flask, request, render_template_string, redirect
from dotenv import load_dotenv

# --- LOGGING SETUP ---
LOG_FILE = "debug.log"

class Logger(object):
    def __init__(self):
        self.terminal = sys.stdout
        self.log = open(LOG_FILE, "a", encoding="utf-8")

    def write(self, message):
        self.terminal.write(message)
        self.log.write(message)
        self.log.flush()

    def flush(self):
        self.terminal.flush()
        self.log.flush()

sys.stdout = Logger()
sys.stderr = sys.stdout

app = Flask(__name__)
http_session = requests.Session()

# Load Secrets
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")

# Load Configuration
with open('config.json', 'r', encoding='utf-8') as config_file:
    config = json.load(config_file)

NORMAL_TWITCH = config.get("NORMAL_TWITCH")
NORMAL_YOUTUBE = config.get("NORMAL_YOUTUBE")
TARGET = config.get("TARGET")
OVERRIDE_FILE = config.get("OVERRIDE_FILE")
TWITCH_OPTS = config.get("TWITCH_OPTS")
TWITCH_QUALITY = config.get("TWITCH_QUALITY")
YOUTUBE_QUALITY = config.get("YOUTUBE_QUALITY")
FREE_CHAT_ID = config.get("FREE_CHAT_ID")

# New variables
COOKIES_FILE = config.get("COOKIES_FILE")
TWITCH_CONFIG_FILE = config.get("TWITCH_CONFIG_FILE")

last_youtube_id = FREE_CHAT_ID
notified_ids = set()

twitch_access_token = None
twitch_token_expiry = 0

# State Management
active_process = None
process_lock = threading.Lock()

# --- HELPER FUNCTIONS ---

def cleanup_process():
    """Ensure subprocesses are killed on exit."""
    global active_process
    with process_lock:
        if active_process:
            try:
                os.killpg(os.getpgid(active_process.pid), signal.SIGINT)
            except:
                pass

atexit.register(cleanup_process)

def get_twitch_game_name(username):
    global twitch_access_token, twitch_token_expiry
    
    current_time = time.time()
    
    if not twitch_access_token or current_time >= twitch_token_expiry:
        auth_url = "https://id.twitch.tv/oauth2/token"
        payload = {
            "client_id": TWITCH_CLIENT_ID,
            "client_secret": TWITCH_CLIENT_SECRET,
            "grant_type": "client_credentials"
        }
        try:
            auth_res = requests.post(auth_url, data=payload, timeout=10).json()
            twitch_access_token = auth_res.get("access_token")
            if not twitch_access_token:
                return ""
            twitch_token_expiry = current_time + auth_res.get("expires_in", 0) - 60
        except Exception as e:
            print(f"Twitch Auth Error: {e}")
            return ""

    headers = {
        "Client-ID": TWITCH_CLIENT_ID,
        "Authorization": f"Bearer {twitch_access_token}"
    }
    
    try:
        stream_url = f"https://api.twitch.tv/helix/streams?user_login={username}"
        stream_res = requests.get(stream_url, headers=headers, timeout=10).json()
        data = stream_res.get("data", [])
        if data:
            return data[0].get("game_name", "")
    except Exception as e:
        print(f"Twitch API Error: {e}")
        
    return ""

def send_telegram_notification(url, prefix=""):
    """Sends notification in a non-blocking thread, using local RTMP frame only for Twitch."""
    def _send():
        try:
            cmd = ['yt-dlp', '--cookies', COOKIES_FILE, '-j', '--no-warnings', '--playlist-items', '1', '--ignore-no-formats-error', url]
            result = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode('utf-8')
            data = json.loads(result)
            
            start_time_str = ""
            
            if "twitch.tv" in url.lower():
                final_title = data.get('description') or data.get('title') or "Twitch Stream"
                final_link = url
                
                match = re.search(r'twitch\.tv/([^/?]+)', url.lower())
                if match:
                    internal_username = match.group(1)
                    game_name = get_twitch_game_name(internal_username)
                    if game_name:
                        final_title = f"{final_title} \u2014 {game_name}"
            else:
                raw_title = data.get('title') or "YouTube Stream"
                video_id = data.get('id')
                sched_ts = data.get('scheduled_timestamp') or data.get('release_timestamp')
                if sched_ts:
                    start_time_str = f" | Start: {datetime.fromtimestamp(float(sched_ts)).strftime('%Y-%m-%d %H:%M')}"
                
                final_title = re.split(r'\s\d{4}-\d{2}-\d{2}', raw_title)[0].strip()
                final_link = f"https://www.youtube.com/watch?v={video_id}" if video_id else url

            full_prefix = f"{prefix}{start_time_str}"
            caption = f"<b>{full_prefix}</b>\n{html.escape(final_title)}\n{final_link}"
            
            photo_file = None
            frame_path = "live_frame.jpg"
            
            # --- MODIFIED BLOCK: Restrict to Twitch URLs only ---
            if "twitch.tv" in url.lower() and "LIVE" in prefix:
                try:
                    cmd_frame = [
                        'ffmpeg', '-y', '-i', TARGET, 
                        '-vframes', '1', '-q:v', '2', 
                        '-f', 'image2', frame_path
                    ]
                    # Timeout prevents the thread from hanging if the RTMP server is unresponsive
                    subprocess.run(cmd_frame, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                    
                    if os.path.exists(frame_path) and os.path.getsize(frame_path) > 0:
                        photo_file = open(frame_path, 'rb')
                except Exception as e:
                    print(f"Frame extraction skipped/failed: {e}")

            api_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
            
            if photo_file:
                # Send the extracted local file via multipart form-data
                payload = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"}
                files = {"photo": photo_file}
                http_session.post(api_url, data=payload, files=files, timeout=15)
                photo_file.close()
                os.remove(frame_path) # Cleanup
            else:
                # Fallback to the CDN URL for YouTube/Overrides or if extraction failed
                thumb = data.get('thumbnail', '')
                if thumb:
                    # Append timestamp to bypass CDN and Telegram caching
                    separator = '&' if '?' in thumb else '?'
                    thumb = f"{thumb}{separator}t={int(time.time())}"
                payload = {"chat_id": TELEGRAM_CHAT_ID, "photo": thumb, "caption": caption, "parse_mode": "HTML"}
                http_session.post(api_url, json=payload, timeout=15)
                
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Notification sent: {final_title}")
        except Exception as e:
            print(f"Notification Error: {e}")

    threading.Thread(target=_send, daemon=True).start()

def start_stream_process(cmd, log_f):
    """Starts the subprocess safely with locking."""
    global active_process
    with process_lock:
        active_process = subprocess.Popen(cmd, shell=True, preexec_fn=os.setsid, stdout=log_f, stderr=log_f)
    return active_process

def streaming_logic():
    global active_process, last_youtube_id
    if not os.path.exists(OVERRIDE_FILE):
        open(OVERRIDE_FILE, 'w').close()

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Monitoring started...")

    with open(LOG_FILE, "a") as log_f:
        while True:
            try:
                # --- 1. TWITCH CHECK ---
                safe_twitch_url = shlex.quote(NORMAL_TWITCH)
                cmd_twitch = f'streamlink --config {TWITCH_CONFIG_FILE} {TWITCH_OPTS} {safe_twitch_url} {TWITCH_QUALITY} -O | ffmpeg -i pipe:0 -c copy -f flv {TARGET}'
                
                # Check if streamlink can even find the stream first (saves ffmpeg overhead)
                # This prevents the "start -> crash -> silent" loop if stream is just offline
                # But for now, we use your logic:
                
                proc = start_stream_process(cmd_twitch, log_f)
                time.sleep(10) # Wait for stability
                
                if proc.poll() is None:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] Twitch is LIVE. Relaying...")
                    send_telegram_notification(NORMAL_TWITCH, prefix="[LIVE - Twitch]")
                    proc.wait()
                    with process_lock: active_process = None
                    print("Twitch stream ended.")
                    time.sleep(5); continue
                else:
                    # DEBUGGING: If it died instantly, was it because it's offline or because of an error?
                    # Streamlink returns 1 if stream not found. FFmpeg returns 1 if connection refused.
                    if proc.returncode != 1: 
                        print(f"Twitch Process exited with code {proc.returncode}. Check debug.log.")
                    with process_lock: active_process = None

                # --- 2. YOUTUBE CHECK ---
                is_actually_live = False
                current_id = None
                
                try:
                    check_cmd = ['yt-dlp', '--cookies', COOKIES_FILE, '-j', '--no-warnings', '--playlist-items', '1', '--ignore-no-formats-error', NORMAL_YOUTUBE]
                    # Removed DEVNULL so you can see if yt-dlp itself is crashing in the console
                    yt_output = subprocess.check_output(check_cmd).decode('utf-8') 
                    yt_meta = json.loads(yt_output)
                    
                    current_id = yt_meta.get('id')
                    is_actually_live = yt_meta.get('live_status') == 'is_live' or yt_meta.get('is_live')
                    
                    if current_id and current_id != last_youtube_id and current_id != FREE_CHAT_ID:
                        if current_id not in notified_ids:
                            send_telegram_notification(NORMAL_YOUTUBE, prefix="[SCHEDULED]")
                            notified_ids.add(current_id)
                            last_youtube_id = current_id
                except subprocess.CalledProcessError as e:
                    print(f"yt-dlp check failed: {e}")
                except Exception as e:
                    pass # JSON parse error usually means offline/no data

                if is_actually_live:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] YouTube is LIVE. Starting relay...")
                    safe_yt_url = shlex.quote(NORMAL_YOUTUBE)
                    cmd_yt = f'streamlink --http-cookies-file {COOKIES_FILE} {TWITCH_OPTS} {safe_yt_url} {TWITCH_QUALITY} -O | ffmpeg -i pipe:0 -c copy -f flv {TARGET}'

                    proc = start_stream_process(cmd_yt, log_f)
                    
                    time.sleep(10)
                    if proc.poll() is None:
                        if current_id != FREE_CHAT_ID:
                            send_telegram_notification(NORMAL_YOUTUBE, prefix="[LIVE - YouTube]")
                        proc.wait()
                        last_youtube_id = FREE_CHAT_ID
                        with process_lock: active_process = None
                        time.sleep(5); continue
                    else:
                        print(f"YouTube relay failed immediately. Exit code: {proc.returncode}. Check debug.log for RTMP/FFmpeg errors.")
                        with process_lock: active_process = None

                # --- 3. OVERRIDE (SMART) ---
                if os.path.exists(OVERRIDE_FILE):
                    with open(OVERRIDE_FILE, 'r') as f:
                        url_to_stream = f.read().strip()
                
                    if url_to_stream:
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] Override found: {url_to_stream}")
                        
                        # Start the stream
                        safe_ov_url = shlex.quote(url_to_stream)
                        cmd_ov = f'streamlink --http-cookies-file {COOKIES_FILE} --config {TWITCH_CONFIG_FILE} {TWITCH_OPTS} {safe_ov_url} {TWITCH_QUALITY} -O | ffmpeg -i pipe:0 -c copy -f flv {TARGET}'
                        proc = start_stream_process(cmd_ov, log_f)
                        
                        time.sleep(10)
                        
                        # If it is running, send notification and wait
                        if proc.poll() is None:
                            send_telegram_notification(url_to_stream, prefix="[LIVE - Collab]")
                            proc.wait() # Wait until stream ends or crashes
                            
                            # --- SMART CLEAR LOGIC ---
                            # Stream stopped. Was it a crash or did it actually end?
                            print("Override process stopped. Checking status...")
                            try:
                                # Ask yt-dlp if the stream is still live
                                check_cmd = ['yt-dlp', '--cookies', COOKIES_FILE, '-j', '--playlist-items', '1', url_to_stream]
                                info = json.loads(subprocess.check_output(check_cmd, stderr=subprocess.DEVNULL).decode('utf-8'))
                                is_still_live = info.get('live_status') == 'is_live' or info.get('is_live')
                                
                                if not is_still_live:
                                    print("Stream is confirmed OFFLINE. clearing override.")
                                    open(OVERRIDE_FILE, 'w').close()
                                else:
                                    print("Stream is still LIVE. Retrying in next loop...")
                            except:
                                # If checking fails (e.g. video deleted), assume it's done.
                                open(OVERRIDE_FILE, 'w').close()
                                
                            with process_lock: active_process = None
                            time.sleep(5); continue
                        
                        else:
                            # It died instantly (invalid URL or connection error)
                            print(f"Override failed immediately (Code {proc.returncode}). Retrying in 30s...")
                            with process_lock: active_process = None

                # Reset and Sleep
                with process_lock: active_process = None
                time.sleep(30)
            except Exception as e:
                print(f"Critical Logic Error: {e}")
                time.sleep(10)

# --- WEB UI & ROUTES ---
@app.route('/')
def index():
    url = "None"
    if os.path.exists(OVERRIDE_FILE):
        with open(OVERRIDE_FILE, 'r') as f: url = f.read().strip() or "None"
    return render_template_string('''
        <!DOCTYPE html><html><head><title>Stream Control</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>body{font-family:sans-serif;background:#f4f4f4;padding:20px;}
        .container{max-width:400px;background:white;padding:20px;border-radius:8px;margin:auto;box-shadow:0 2px 5px rgba(0,0,0,0.1);}
        input{width:100%;padding:10px;margin:10px 0;box-sizing:border-box;}
        button{padding:10px;cursor:pointer;border:none;border-radius:4px;width:100%;color:white;}
        .btn-set{background:#007bff;} .btn-clear{background:#dc3545;margin-top:10px;}
        .status{background:#e9ecef;padding:10px;border-radius:4px;word-break:break-all;font-size:0.9em;}</style></head>
        <body><div class="container"><h2>Mea Monitor</h2>
        <div class="status"><strong>Current Override:</strong><br><code>{{url}}</code></div>
        <form method="POST" action="set"><input name="url" placeholder="Collab URL" required><button type="submit" class="btn-set">Set Override</button></form>
        <form method="POST" action="clear"><button type="submit" class="btn-clear">Stop & Clear</button></form>
        </div></body></html>
    ''', url=url)

@app.route('/set', methods=['POST'])
def set_url():
    new_url = request.form.get('url', '').strip()
    if new_url:
        with open(OVERRIDE_FILE, 'w') as f: f.write(new_url)
    return redirect("./")

@app.route('/clear', methods=['POST'])
def clear():
    global active_process
    open(OVERRIDE_FILE, 'w').close()
    
    with process_lock:
        if active_process:
            try: os.killpg(os.getpgid(active_process.pid), signal.SIGINT)
            except: pass
            active_process = None
            
    return redirect("./")

if __name__ == '__main__':
    threading.Thread(target=streaming_logic, daemon=True).start()
    app.run(host='127.0.0.1', port=12450)
