#!/usr/bin/env python3
import os
import sys
import curses
import subprocess
import threading
import time
import random
import unicodedata
import socket
import json
import shutil
import locale
import urllib.request
import urllib.parse
import sqlite3
import re
import signal

# XDG CONFIGURATION
HOME_DIR = os.path.expanduser("~")
CONFIG_DIR = os.path.join(HOME_DIR, ".config", "arctic")
DB_FILE = os.path.join(CONFIG_DIR, "arctic.db")
STATE_FILE = os.path.join(CONFIG_DIR, "state.json")
AUDIO_EXT = ('.mp3', '.flac', '.wav', '.ogg', '.m4a', '.3gp', '.aac', '.opus', '.wma', '.alac', '.ape', '.webm')

class ArcticDB:
    def __init__(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        self.conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_db()

    def _init_db(self):
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS metadata (
                    filepath TEXT PRIMARY KEY,
                    title TEXT, artist TEXT, album TEXT, year TEXT, 
                    bitrate TEXT, duration REAL, last_seen REAL
                )
            """)

    def get(self, filepath):
        cur = self.conn.execute("SELECT title, artist, album, year, bitrate, duration FROM metadata WHERE filepath=?", (filepath,))
        row = cur.fetchone()
        if row:
            self.conn.execute("UPDATE metadata SET last_seen=? WHERE filepath=?", (time.time(), filepath))
            self.conn.commit()
            return {"Title": row[0], "Artist": row[1], "Album": row[2], "Year": row[3], "Bitrate": row[4]}, row[5]
        return None

    def save(self, filepath, meta, duration):
        with self.conn:
            self.conn.execute("""
                INSERT OR REPLACE INTO metadata 
                (filepath, title, artist, album, year, bitrate, duration, last_seen) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (filepath, meta["Title"], meta["Artist"], meta["Album"], meta["Year"], meta["Bitrate"], duration, time.time()))

    def cleanup(self):
        thirty_days_ago = time.time() - (30 * 24 * 60 * 60)
        with self.conn:
            self.conn.execute("DELETE FROM metadata WHERE last_seen < ?", (thirty_days_ago,))

class ArcticPlayer:
    def __init__(self, stdscr):
        self.stdscr = stdscr
        self.db = ArcticDB()
        self.setup_curses()
        self.detect_unicode()
        
        self.player_proc = None
        self.ipc_socket = f"/tmp/arctic_mpv_{os.getpid()}.sock"
        self.current_song = "None"
        self.current_path = ""
        self.is_playing = False
        self.is_paused = False
        self.changing_song = False
        
        self.volume = 50
        self.play_mode = 0
        self.modes = ["SEQ", "ONE", "RND"]
        
        self.duration = 0
        self.elapsed = 0
        self.sys_start_time = 0.0
        
        self.metadata = {"Title": "Unknown", "Artist": "Unknown", "Album": "Unknown", "Year": "N/A", "Bitrate": "N/A"}
        self.dir_generation = 0 
        self.clean_counter = 0
        
        self.vis_state = []
        self.vis_levels = [] 
        self.last_vis_update = 0
        
        self.lyrics = []
        self.lyrics_loading = False
        self.show_lyrics = False
        self.is_synced = False
        
        self.cwd = self.load_state()
        self.dirs, self.files = [], []
        self.active_pane = 1
        self.sel_dir = self.sel_file = 0
        self.top_dir = self.top_file = 0
        
        self.show_help = False
        self.zen_mode = False
        self.is_searching = False
        self.search_query = ""
        self._running = True

        # Setup signal handlers for clean exits
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)
        signal.signal(signal.SIGHUP, self.signal_handler)
        
        self.refresh_dir(self.cwd)
        threading.Thread(target=self.track_progress, daemon=True).start()
        self.run()

    def signal_handler(self, sig, frame):
        self.cleanup_and_quit()
        sys.exit(0)

    def setup_curses(self):
        curses.curs_set(0)
        curses.use_default_colors()
        self.stdscr.nodelay(True)
        self.stdscr.keypad(True)
        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(3, curses.COLOR_GREEN, -1)
        curses.init_pair(4, curses.COLOR_YELLOW, -1)
        curses.init_pair(5, curses.COLOR_MAGENTA, -1)

    def detect_unicode(self):
        enc = locale.getpreferredencoding().lower()
        lang = os.environ.get('LANG', '').lower()
        if "utf-8" in enc or "utf8" in lang:
            self.chars = {'full': '█', 'peaks': ['▂', '▃', '▄', '▅']}
        else:
            self.chars = {'full': '#', 'peaks': ['-', '=', '+', '*']}

    def save_state(self):
        try:
            with open(STATE_FILE, 'w') as f:
                json.dump({"cwd": self.cwd, "volume": self.volume, "play_mode": self.play_mode}, f)
        except OSError: pass

    def load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r') as f:
                    state = json.load(f)
                    self.volume = min(100, max(0, state.get("volume", 50)))
                    self.play_mode = state.get("play_mode", 0) % 3
                    path = state.get("cwd", HOME_DIR)
                    return path if os.path.isdir(path) else HOME_DIR
            except (OSError, json.JSONDecodeError): pass
        return HOME_DIR

    def refresh_dir(self, path):
        self.cwd = path
        self.save_state()
        self.dirs, self.files = [], []
        self.dir_generation += 1 
        self.clean_counter += 1
        
        if self.clean_counter > 10:
            self.db.cleanup()
            self.clean_counter = 0
        
        try:
            with os.scandir(path) as it:
                for entry in it:
                    if entry.is_dir() and not entry.name.startswith('.'):
                        self.dirs.append(entry.name)
                    elif entry.is_file() and entry.name.lower().endswith(AUDIO_EXT):
                        self.files.append(entry.name)
            self.dirs.sort()
            self.files.sort()
        except OSError:
            self.dirs = [".."]
            
        self.sel_dir = self.sel_file = 0
        self.top_dir = self.top_file = 0

        if self.files:
            threading.Thread(target=self._background_metadata_fetch, args=(path, list(self.files), self.dir_generation), daemon=True).start()

    def get_display_width(self, text):
        text = str(text)
        if not text: return 0
        if text.isascii(): return len(text)
        return sum(2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in text)

    def limit_str(self, text, max_width):
        text = str(text)
        if not text: return ""
        if text.isascii() and len(text) <= max_width: return text
            
        curr_width = 0
        res = []
        for char in text:
            w = 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
            if curr_width + w > max_width: break
            res.append(char)
            curr_width += w
        return "".join(res)

    def _background_metadata_fetch(self, folder, files, generation):
        for f in files:
            if not self._running or self.dir_generation != generation: break
            
            full_path = os.path.join(folder, f)
            if not self.db.get(full_path):
                cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", full_path]
                try:
                    res = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
                    data = json.loads(res).get("format", {})
                    tags = data.get("tags", {})
                    
                    dur = float(data.get("duration", 0))
                    meta = {
                        "Title": tags.get("title", tags.get("TITLE", os.path.splitext(f)[0])),
                        "Artist": tags.get("artist", tags.get("ARTIST", "Unknown")),
                        "Album": tags.get("album", tags.get("ALBUM", "Unknown")),
                        "Year": str(tags.get("date", tags.get("DATE", "N/A")))[:4],
                        "Bitrate": f"{int(data.get('bit_rate', 0))//1000} kbps" if str(data.get("bit_rate", "")).isdigit() else "N/A"
                    }
                    self.db.save(full_path, meta, dur)
                except Exception:
                    self.db.save(full_path, {"Title": os.path.splitext(f)[0], "Artist": "Unknown", "Album": "Unknown", "Year": "N/A", "Bitrate": "N/A"}, 0)
                time.sleep(0.05)

    def fetch_metadata_and_duration(self, path):
        cached = self.db.get(path)
        if cached:
            self.metadata, self.duration = cached
        else:
            self._background_metadata_fetch(os.path.dirname(path), [os.path.basename(path)], self.dir_generation)
            cached = self.db.get(path)
            self.metadata, self.duration = cached if cached else ({"Title": "Unknown", "Artist": "Unknown", "Album": "Unknown", "Year": "N/A", "Bitrate": "N/A"}, 0)
        self.elapsed = 0

    def fetch_lyrics(self, path, artist, title, duration):
        self.lyrics = []
        self.lyrics_loading = True
        self.is_synced = False
        
        lrc_path = os.path.splitext(path)[0] + ".lrc"
        if os.path.exists(lrc_path):
            lrc_content = None
            for encoding in ['utf-8-sig', 'utf-8', 'cp1251', 'shift_jis', 'euc-kr', 'latin-1']:
                try:
                    with open(lrc_path, 'r', encoding=encoding) as f:
                        lrc_content = f.read()
                    break
                except (OSError, UnicodeDecodeError): pass
            
            if lrc_content:
                self.lyrics = self._parse_lrc(lrc_content)
                self.is_synced = True
                self.lyrics_loading = False
                return

        if artist and title and artist != "Unknown":
            clean_title = re.sub(r'[\(\[\{].*?[\)\]\}]', '', title).strip()
            search_query = urllib.parse.urlencode({'q': f"{artist} {clean_title}"})
            url = f"https://lrclib.net/api/search?{search_query}"
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'ArcticPlayer/5.1'})
                with urllib.request.urlopen(req, timeout=3) as res:
                    results = json.loads(res.read().decode('utf-8'))
                    if results:
                        best_match = next((item for item in results if item.get('syncedLyrics')), results[0])
                        synced = best_match.get('syncedLyrics')
                        if synced:
                            self.lyrics = self._parse_lrc(synced)
                            self.is_synced = True
                        else:
                            plain = best_match.get('plainLyrics')
                            if plain:
                                lines = plain.split('\n')
                                self.lyrics = [{"time": 0, "text": l} for l in lines if l.strip()]
                                self.is_synced = False
            except Exception: pass
            
        self.lyrics_loading = False

    def _parse_lrc(self, lrc_str):
        parsed = []
        for line in lrc_str.splitlines():
            line = line.strip()
            if not line: continue
            times = []
            while line.startswith('['):
                idx = line.find(']')
                if idx == -1: break
                t_str = line[1:idx]
                line = line[idx+1:].strip()
                try:
                    t_str = t_str.replace(',', '.').replace(';', '.')
                    parts = t_str.split(':')
                    if len(parts) >= 2:
                        sec = int(parts[0]) * 60 + float(parts[1])
                        times.append(sec)
                except ValueError: pass
            for t in times:
                parsed.append({"time": t, "text": line})
        return sorted([p for p in parsed if p["text"]], key=lambda x: x["time"])

    def send_mpv_command(self, command):
        if not os.path.exists(self.ipc_socket): return
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(0.05)
                client.connect(self.ipc_socket)
                msg = json.dumps({"command": command}) + "\n"
                client.sendall(msg.encode('utf-8'))
        except (OSError, socket.timeout): pass

    def play(self, name, start_time=0):
        self.changing_song = True
        self.is_playing = False
        
        if self.player_proc:
            try:
                self.player_proc.terminate()
                self.player_proc.wait(timeout=0.5)
            except (subprocess.TimeoutExpired, ProcessLookupError):
                try: self.player_proc.kill()
                except OSError: pass
                
        if os.path.exists(self.ipc_socket):
            try: os.remove(self.ipc_socket)
            except OSError: pass
                
        self.current_path = os.path.join(self.cwd, name)
        self.current_song = name
        
        if start_time == 0:
            self.fetch_metadata_and_duration(self.current_path)
            
        self.lyrics = []
        threading.Thread(target=self.fetch_lyrics, args=(self.current_path, self.metadata.get("Artist"), self.metadata.get("Title"), self.duration), daemon=True).start()

        cmd_play = [
            "mpv", "--no-video", "--really-quiet",
            f"--input-ipc-server={self.ipc_socket}",
            f"--start={start_time}", f"--volume={self.volume}",
            "--audio-client-name=ArcticPlayer", self.current_path
        ]
        
        try:
            self.player_proc = subprocess.Popen(cmd_play, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.is_playing = True
            self.is_paused = False
            self.elapsed = start_time
            self.sys_start_time = time.time() - start_time
        except Exception:
            self.is_playing = False
            
        self.changing_song = False

    def track_progress(self):
        last_poll = time.time()
        while self._running:
            if self.is_playing and not self.is_paused and not self.changing_song:
                self.elapsed = time.time() - self.sys_start_time
                if time.time() - last_poll > 1.0:
                    if self.player_proc and self.player_proc.poll() is not None:
                        self.is_playing = False
                        self.handle_auto_next()
                    last_poll = time.time()
            time.sleep(0.05)

    def handle_auto_next(self, offset=1):
        if not self.files or self.changing_song: return
        if self.play_mode == 0:
            self.sel_file = (self.sel_file + offset) % len(self.files)
            self.play(self.files[self.sel_file])
        elif self.play_mode == 1:
            if offset != 1: 
                self.sel_file = (self.sel_file + offset) % len(self.files)
            self.play(self.files[self.sel_file])
        elif self.play_mode == 2:
            self.sel_file = random.randint(0, len(self.files) - 1)
            self.play(self.files[self.sel_file])

    def draw_box(self, y, x, h, w, title=""):
        try:
            self.stdscr.addstr(y, x, "╭" + "─" * (w-2) + "╮")
            for i in range(1, h-1):
                self.stdscr.addstr(y+i, x, "│")
                self.stdscr.addstr(y+i, x+w-1, "│")
            self.stdscr.addstr(y+h-1, x, "╰" + "─" * (w-2) + "╯")
            if title:
                self.stdscr.addstr(y, x+2, f" {title} ", curses.color_pair(1) | curses.A_BOLD)
        except curses.error: pass

    def update_visualizer(self, height, width):
        if height < 1 or width < 1: return
        if len(self.vis_levels) != width:
            self.vis_levels = [0] * width

        if self.is_playing and not self.is_paused:
            if time.time() - self.last_vis_update > 0.08:
                self.vis_state = [[' ' for _ in range(width)] for _ in range(height)]
                for x in range(width):
                    target = random.randint(0, height)
                    if self.vis_levels[x] < target:
                        self.vis_levels[x] += random.randint(1, 2)
                    elif self.vis_levels[x] > target:
                        self.vis_levels[x] -= random.randint(1, 2)
                    
                    self.vis_levels[x] = max(0, min(height, self.vis_levels[x]))
                    col_h = self.vis_levels[x]
                    
                    for y in range(height):
                        inv_y = height - 1 - y
                        if inv_y < col_h:
                            self.vis_state[y][x] = self.chars['full']
                        elif inv_y == col_h and col_h > 0:
                            self.vis_state[y][x] = random.choice(self.chars['peaks'])
                self.last_vis_update = time.time()
        else:
            self.vis_state = [[' '] * width for _ in range(height)]
            self.vis_levels = [0] * width

    def draw_lyrics(self, start_y, start_x, pane_h, pane_w):
        if self.lyrics_loading:
            msg = " Fetching Lyrics... "
            try: self.stdscr.addstr(start_y + pane_h//2, start_x + (pane_w - len(msg))//2, msg, curses.A_DIM)
            except curses.error: pass
            return
            
        if not self.lyrics:
            msg = " No Lyrics Found "
            try: self.stdscr.addstr(start_y + pane_h//2, start_x + (pane_w - len(msg))//2, msg, curses.A_DIM)
            except curses.error: pass
            return

        active_y = pane_h // 3

        if self.is_synced:
            current_idx = -1 
            sync_time = self.elapsed + 0.15 
            
            for i, line in enumerate(self.lyrics):
                if sync_time >= line["time"]:
                    current_idx = i
                else:
                    break 

            for i, line_data in enumerate(self.lyrics):
                dist = i - current_idx
                if current_idx == -1: dist = i + 1
                    
                draw_y = active_y + (dist * 2) 
                
                if 0 <= draw_y < pane_h:
                    line = line_data["text"]
                    if dist == 0 and current_idx != -1:
                        attr = curses.color_pair(1) | curses.A_BOLD
                        line = f"{line.upper()}"
                    else:
                        attr = curses.A_DIM
                    
                    lim_line = self.limit_str(line, pane_w - 4)
                    display_w = self.get_display_width(lim_line) 
                    x_pos = start_x + max(1, (pane_w - display_w) // 2)
                    
                    try: self.stdscr.addstr(start_y + draw_y, x_pos, lim_line, attr)
                    except curses.error: pass
        else:
            percent = self.elapsed / max(self.duration, 1)
            scroll_idx = int(percent * len(self.lyrics))
            
            for i, line_data in enumerate(self.lyrics):
                dist = i - scroll_idx
                draw_y = active_y + (dist * 2)
                
                if 0 <= draw_y < pane_h:
                    line = line_data["text"]
                    if dist == 0:
                        attr = curses.color_pair(1) | curses.A_BOLD
                        line = f"{line.upper()}"
                    else:
                        attr = curses.A_DIM
                        
                    lim_line = self.limit_str(line, pane_w - 4)
                    display_w = self.get_display_width(lim_line) 
                    x_pos = start_x + max(1, (pane_w - display_w) // 2)
                    
                    try: self.stdscr.addstr(start_y + draw_y, x_pos, lim_line, attr)
                    except curses.error: pass

    def draw_zen_mode(self, h, w):
        title_str = f" NOW PLAYING: {self.current_song} "
        title_lim = self.limit_str(title_str, w - 4)
        title_w = self.get_display_width(title_lim)
        try: self.stdscr.addstr(2, max(1, (w - title_w) // 2), title_lim, curses.color_pair(1) | curses.A_BOLD)
        except curses.error: pass
        
        artist_str = self.limit_str(self.metadata['Artist'], w - 4)
        artist_w = self.get_display_width(artist_str)
        try: self.stdscr.addstr(3, max(1, (w - artist_w) // 2), artist_str, curses.color_pair(4))
        except curses.error: pass
        
        if self.show_lyrics:
            self.draw_lyrics(5, 0, h - 10, w)
        else:
            vis_w = min((w - 10) // 2, 40)
            vis_h = 8
            vis_x = (w - (vis_w * 2)) // 2
            center_y = h // 2 - 4
            self.update_visualizer(vis_h, vis_w)
            for i in range(len(self.vis_state)):
                for j, char in enumerate(self.vis_state[i]):
                    attr = curses.color_pair(5) if char == self.chars['full'] else curses.color_pair(4)
                    try: self.stdscr.addstr(center_y + i, vis_x + (j*2), char * 2, attr)
                    except curses.error: pass

    def draw_help(self, h, w):
        box_w, box_h = 60, 16
        y, x = (h - box_h) // 2, (w - box_w) // 2
        for i in range(box_h):
            try: self.stdscr.addstr(y + i, x, " " * box_w)
            except curses.error: pass
            
        self.draw_box(y, x, box_h, box_w, "Help & Shortcuts")
        shortcuts = [
            ("[H/L | Left/Right]", "Change Panel"),
            ("[J/K | Up/Down]", "Navigate Files"),
            ("[Enter]", "Play / Open Folder"),
            ("[:]", "Search Audio File"),
            ("[P / Space]", "Play / Pause"),
            ("[, / .]", "Previous / Next Track"),
            ("[M]", "Toggle Mode (SEQ / ONE / RND)"),
            ("[1 / 2]", "Rewind / Fast Forward 10s"),
            ("[+ / -]", "Volume Up / Down"),
            ("[3]", "Toggle Lyrics View"),
            ("[Z / ESC]", "Toggle Zen Mode"),
            ("[?]", "Close Help"),
            ("[Q]", "Quit")
        ]
        for i, (key, desc) in enumerate(shortcuts):
            if y + 2 + i < y + box_h - 1:
                line = f"{key:<25} {desc}"
                try: self.stdscr.addstr(y + 2 + i, x + 2, line[:box_w-4], curses.color_pair(4) | curses.A_BOLD)
                except curses.error: pass

    def format_time(self, seconds):
        if seconds < 0: seconds = 0
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"

    def draw(self):
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()
        
        if h < 15 or w < 50:
            try: self.stdscr.addstr(0, 0, " Terminal too small ".center(w)[:w])
            except curses.error: pass
            self.stdscr.refresh()
            return
        
        header_text = f" ARCTIC PLAYER v4.0 | {self.cwd} "
        try: self.stdscr.addstr(0, 0, self.limit_str(header_text, w).ljust(w), curses.color_pair(2))
        except curses.error: pass

        if self.zen_mode:
            self.draw_zen_mode(h, w)
        else:
            left_w = w // 3
            pane_h = h - 5
            dir_h = pane_h // 3 + 1
            info_h = 7
            vis_h = max(3, pane_h - dir_h - info_h)

            for y in range(1, h-4): 
                try: self.stdscr.addch(y, left_w, "│", curses.A_DIM)
                except curses.error: pass

            self.draw_box(1, 0, dir_h, left_w, "Directories")
            for i in range(dir_h - 2):
                idx = i + self.top_dir
                if idx >= len(self.dirs): break
                is_sel = (self.active_pane == 0 and idx == self.sel_dir)
                attr = curses.color_pair(1) | curses.A_BOLD if is_sel else curses.color_pair(4)
                prefix = "❯ " if is_sel else "  "
                label = f"{prefix}📁 {self.dirs[idx]}"
                try: self.stdscr.addstr(i+2, 2, self.limit_str(label, left_w-4), attr)
                except curses.error: pass

            info_y = 1 + dir_h
            self.draw_box(info_y, 0, info_h, left_w, "Track Info")
            m_keys = [("Title", "Title"), ("Artist", "Artist"), ("Album", "Album"), ("Bitrate", "Bitrate")]
            y_offset = info_y + 1
            for label, key in m_keys:
                if y_offset < info_y + info_h - 1:
                    try: 
                        self.stdscr.addstr(y_offset, 2, f"{label}:", curses.color_pair(5))
                        val = self.limit_str(str(self.metadata.get(key, "N/A")), left_w-13)
                        self.stdscr.addstr(y_offset, 11, val, curses.A_BOLD)
                    except curses.error: pass
                    y_offset += 1 

            vis_y = info_y + info_h
            self.draw_box(vis_y, 0, vis_h, left_w, "Visualizer")
            self.update_visualizer(vis_h - 2, left_w - 4)
            for i in range(len(self.vis_state)):
                for j, char in enumerate(self.vis_state[i]):
                    attr = curses.color_pair(5) if char == self.chars['full'] else curses.color_pair(4)
                    try: self.stdscr.addstr(vis_y + 1 + i, 2 + j, char, attr)
                    except curses.error: pass

            if self.show_lyrics:
                try: self.stdscr.addstr(1, left_w + 2, " LYRICS ", curses.A_BOLD | curses.color_pair(1))
                except curses.error: pass
                self.draw_lyrics(2, left_w + 1, pane_h - 1, w - left_w - 2)
            else:
                try: self.stdscr.addstr(1, left_w + 2, " AUDIO FILES ", curses.A_BOLD | curses.color_pair(1))
                except curses.error: pass
                for i in range(pane_h - 1):
                    idx = i + self.top_file
                    if idx >= len(self.files): break
                    is_sel = (self.active_pane == 1 and idx == self.sel_file)
                    is_playing = (self.current_song == self.files[idx])
                    
                    if is_playing: prefix, attr = "▶ ", curses.color_pair(3) | curses.A_BOLD
                    elif is_sel: prefix, attr = "❯ ", curses.color_pair(1) | curses.A_BOLD
                    else: prefix, attr = "  ", curses.A_NORMAL
                    
                    fname = self.limit_str(f"{prefix}{self.files[idx]}", w - left_w - 4)
                    try: self.stdscr.addstr(i+2, left_w + 2, fname, attr)
                    except curses.error: pass

        if self.duration > 0:
            bar_w = w - 4
            pct = min(1.0, max(0.0, self.elapsed / self.duration))
            fill = int(bar_w * pct)
            try: self.stdscr.addstr(h-4, 2, "█" * fill + "░" * (bar_w - fill), curses.color_pair(3))
            except curses.error: pass
            
            time_str = f" {self.format_time(self.elapsed)} / {self.format_time(self.duration)} | {self.current_song} "
            try: self.stdscr.addstr(h-3, 2, self.limit_str(time_str, w-4), curses.A_BOLD)
            except curses.error: pass

        status = f" VOL: {self.volume}% | MODE: [{self.modes[self.play_mode]}] | {'PAUSED' if self.is_paused else 'PLAYING' if self.is_playing else 'IDLE'} "
        try: self.stdscr.addstr(h-2, 2, status[:w-4])
        except curses.error: pass
        
        if self.is_searching:
            search_display = f" :{self.search_query}█ "
            try: self.stdscr.addstr(h-1, 0, search_display.ljust(w)[:w-1], curses.color_pair(1) | curses.A_BOLD)
            except curses.error: pass
        else:
            hints = " [?] Help  [P/Space] Play/Pause  [,/.] Prev/Next  [3] Lyrics  [Z] Zen  [M] Mode  [Q] Quit "
            try: self.stdscr.addstr(h-1, 0, hints.center(w)[:w-1], curses.color_pair(2))
            except curses.error: pass

        if self.show_help: self.draw_help(h, w)
        self.stdscr.refresh()

    def handle_input(self, key, h, w):
        if h < 15 or w < 50:
            if key == ord('q'): self.cleanup_and_quit()
            return

        if self.show_help:
            if key in [ord('?'), 27, 10, 13, ord('q')]: 
                self.show_help = False
                if key == ord('q'): self.cleanup_and_quit()
            return
            
        if key == ord('?'):
            self.show_help = True
            return

        pane_h = h - 5
        dir_h = pane_h // 3 + 1

        if self.is_searching:
            if key in [10, 13, 27]: 
                self.is_searching = False
                self.search_query = ""
            elif key in [curses.KEY_BACKSPACE, 8, 127]:
                self.search_query = self.search_query[:-1]
            elif 32 <= key <= 126:
                self.search_query += chr(key)
            
            if self.search_query:
                q = self.search_query.lower()
                for i, f in enumerate(self.files):
                    if q in f.lower():
                        self.sel_file = i
                        if self.sel_file < self.top_file:
                            self.top_file = self.sel_file
                        elif self.sel_file >= self.top_file + (pane_h - 1):
                            self.top_file = self.sel_file - (pane_h - 2)
                        break
            return

        if key == ord('q'):
            self.cleanup_and_quit()
            return

        if key in [ord('z'), 27]:
            self.zen_mode = not self.zen_mode
            if self.zen_mode: self.active_pane = 1
            return
            
        if key == ord('3') or key == ord('l') or key == ord('L'):
            self.show_lyrics = not self.show_lyrics
            return

        if key == ord(':'):
            if not self.zen_mode and self.files and not self.show_lyrics:
                self.is_searching = True
                self.active_pane = 1
                self.search_query = ""
            return

        if not self.zen_mode and not self.show_lyrics:
            if key in [curses.KEY_LEFT, ord('h')]:
                if self.active_pane == 1 and len(self.dirs) > 0: 
                    self.active_pane = 0
                else: 
                    self.refresh_dir(os.path.abspath(os.path.join(self.cwd, "..")))
            elif key in [curses.KEY_RIGHT, ord('l'), ord('\t')]:
                if self.active_pane == 0 and self.dirs:
                    self.refresh_dir(os.path.abspath(os.path.join(self.cwd, self.dirs[self.sel_dir])))
                    self.active_pane = 1
                else: 
                    self.active_pane = 1
            elif key in [curses.KEY_UP, ord('k')]:
                if self.active_pane == 0 and self.sel_dir > 0:
                    self.sel_dir -= 1
                    if self.sel_dir < self.top_dir: self.top_dir -= 1
                elif self.active_pane == 1 and self.sel_file > 0:
                    self.sel_file -= 1
                    if self.sel_file < self.top_file: self.top_file -= 1
            elif key in [curses.KEY_DOWN, ord('j')]:
                if self.active_pane == 0 and self.sel_dir < len(self.dirs) - 1:
                    self.sel_dir += 1
                    if self.sel_dir >= self.top_dir + (dir_h - 2): self.top_dir += 1
                elif self.active_pane == 1 and self.sel_file < len(self.files) - 1:
                    self.sel_file += 1
                    if self.sel_file >= self.top_file + (pane_h - 1): self.top_file += 1

        if key in [10, 13]: 
            if self.active_pane == 1 and self.files and not self.show_lyrics:
                self.play(self.files[self.sel_file])
            elif self.active_pane == 0 and self.dirs and not self.show_lyrics:
                self.refresh_dir(os.path.abspath(os.path.join(self.cwd, self.dirs[self.sel_dir])))
                self.active_pane = 1
                
        elif key in [ord('p'), ord(' ')]:
            if self.player_proc:
                self.is_paused = not self.is_paused
                if not self.is_paused:
                    self.sys_start_time = time.time() - self.elapsed
                self.send_mpv_command(["set_property", "pause", self.is_paused])
        elif key == ord('.'): 
            self.handle_auto_next(offset=1)
        elif key == ord(','): 
            self.handle_auto_next(offset=-1)
        elif key == ord('m'):
            self.play_mode = (self.play_mode + 1) % 3
            self.save_state()
        elif key == ord('1'):
            if self.is_playing:
                self.elapsed = max(0, self.elapsed - 10)
                self.sys_start_time = time.time() - self.elapsed
                self.send_mpv_command(["seek", -10])
        elif key == ord('2'):
            if self.is_playing:
                self.elapsed = min(self.duration, self.elapsed + 10)
                self.sys_start_time = time.time() - self.elapsed
                self.send_mpv_command(["seek", 10])
        elif key in [ord('+'), ord('=')]:
            self.volume = min(100, self.volume + 5)
            self.send_mpv_command(["set_property", "volume", self.volume])
            self.save_state()
        elif key == ord('-'):
            self.volume = max(0, self.volume - 5)
            self.send_mpv_command(["set_property", "volume", self.volume])
            self.save_state()

    def cleanup_and_quit(self):
        if self.player_proc: 
            try: 
                # Intentar matar mpv de forma contundente en el cierre
                self.player_proc.kill() 
                self.player_proc.wait(timeout=0.5)
            except (ProcessLookupError, subprocess.TimeoutExpired, OSError): pass
            
        if os.path.exists(self.ipc_socket):
            try: os.remove(self.ipc_socket)
            except OSError: pass
            
        self.save_state()
        self.db.conn.close()
        self._running = False

    def run(self):
        while self._running:
            self.draw()
            try: key = self.stdscr.getch()
            except curses.error: key = -1
            if key != -1: self.handle_input(key, *self.stdscr.getmaxyx())
            time.sleep(0.06)

def check_dependencies():
    missing = []
    if not shutil.which("mpv"): missing.append("mpv")
    if not shutil.which("ffprobe"): missing.append("ffprobe (part of ffmpeg)")
    if missing:
        print(f"Error: Missing required dependencies: {', '.join(missing)}")
        print("Please install them using your system's package manager before running Arctic Player.")
        sys.exit(1)

if __name__ == "__main__":
    check_dependencies()
    try: curses.wrapper(ArcticPlayer)
    except KeyboardInterrupt: pass
