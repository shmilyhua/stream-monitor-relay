import subprocess
import time
import threading
import os
import signal
import sys
import json
import atexit
import shlex
from datetime import datetime

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

with open('config.json', 'r', encoding='utf-8') as config_file:
    config = json.load(config_file)

ACTIVE_LIVES_FILE = config.get("ACTIVE_LIVES_FILE")
TARGET = config.get("TARGET")
TWITCH_OPTS = config.get("TWITCH_OPTS", "")
TWITCH_QUALITY = config.get("TWITCH_QUALITY", "best")
COOKIES_FILE = config.get("COOKIES_FILE", "")
TWITCH_CONFIG_FILE = config.get("TWITCH_CONFIG_FILE", "")
MAIN_CHANNELS = config.get("MAIN_CHANNELS", [])

active_process = None
process_lock = threading.Lock()
dead_streams = {}  # Local cache for recently terminated streams

def cleanup_process():
    global active_process
    with process_lock:
        if active_process:
            try:
                os.killpg(os.getpgid(active_process.pid), signal.SIGINT)
            except Exception:
                pass
            active_process = None

atexit.register(cleanup_process)

def start_stream_process(cmd, log_f):
    global active_process
    with process_lock:
        active_process = subprocess.Popen(cmd, shell=True, preexec_fn=os.setsid, stdout=log_f, stderr=log_f)
    return active_process

def get_best_stream(active_lives, dead_cache):
    if not active_lives:
        return None, -1
        
    best_stream = None
    best_priority = -1
    best_timestamp = 0
    current_time = time.time()
    
    for stream in active_lives:
        url = stream.get('url')
        
        # Skip streams that recently terminated to bypass the active_lives.json cleanup delay
        if url in dead_cache and current_time < dead_cache[url]:
            continue
            
        name = stream.get('channel_name', '')
        priority = 10 if name in MAIN_CHANNELS else 1
        ts = stream.get('timestamp', 0)
        
        # Priority rules: Higher tier wins. If tied, the newer stream wins.
        if priority > best_priority or (priority == best_priority and ts > best_timestamp):
            best_priority = priority
            best_timestamp = ts
            best_stream = stream
            
    return best_stream, best_priority

def streaming_logic():
    global active_process
    current_url = None
    current_priority = -1

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Dynamic Relay Engine Active.")

    with open(LOG_FILE, "a") as log_f:
        while True:
            try:
                best_stream = None
                best_prio = -1

                if ACTIVE_LIVES_FILE and os.path.exists(ACTIVE_LIVES_FILE):
                    try:
                        with open(ACTIVE_LIVES_FILE, 'r', encoding='utf-8') as f:
                            active_lives = json.load(f)
                        if active_lives and isinstance(active_lives, list):
                            best_stream, best_prio = get_best_stream(active_lives, dead_streams)
                    except json.JSONDecodeError:
                        pass 

                target_url = best_stream['url'] if best_stream else None

                # 1. Handle Native Termination (Stream ended naturally or crashed)
                if active_process and active_process.poll() is not None:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] Relay process ended (Code {active_process.returncode}). Applying 150s cooldown.")
                    if current_url:
                        dead_streams[current_url] = time.time() + 150
                    
                    with process_lock: 
                        active_process = None
                    current_url = None
                    current_priority = -1
                    time.sleep(5)
                    continue

                # 2. Handle Running State Interruption (Higher priority stream started)
                if active_process and active_process.poll() is None:
                    if not target_url or target_url == current_url:
                        time.sleep(10)
                        continue
                    
                    is_current_main = (current_priority == 10)
                    is_new_main = (best_prio == 10)
                    
                    should_switch = False
                    if is_current_main:
                        if is_new_main:
                            should_switch = True
                    else:
                        should_switch = True
                        
                    if should_switch:
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] Priority switch triggered. Terminating current process.")
                        cleanup_process()
                        
                        # Do not blacklist during an intentional switch, so it can be resumed if needed
                        current_url = None
                        current_priority = -1
                        time.sleep(2) 
                    else:
                        time.sleep(10)
                        continue

                # 3. Spawning Logic
                if target_url and not active_process:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] Relaying: {target_url}")
                    safe_url = shlex.quote(target_url)
                    
                    if "twitch.tv" in target_url.lower():
                        cmd = f'streamlink --config {TWITCH_CONFIG_FILE} {TWITCH_OPTS} {safe_url} {TWITCH_QUALITY} -O | ffmpeg -i pipe:0 -c copy -f flv {TARGET}'
                    else:
                        cmd = f'streamlink --http-cookies-file {COOKIES_FILE} {TWITCH_OPTS} {safe_url} {TWITCH_QUALITY} -O | ffmpeg -i pipe:0 -c copy -f flv {TARGET}'
                    
                    current_url = target_url
                    current_priority = best_prio
                    active_process = start_stream_process(cmd, log_f)
                    
                    # Validate startup success
                    time.sleep(5)
                    if active_process.poll() is not None:
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] Relay failed instantly. Applying 150s cooldown.")
                        dead_streams[target_url] = time.time() + 150
                        with process_lock: 
                            active_process = None
                        current_url = None
                        current_priority = -1

                time.sleep(10)

            except Exception as e:
                print(f"Critical Loop Error: {e}")
                time.sleep(10)

if __name__ == '__main__':
    streaming_logic()
