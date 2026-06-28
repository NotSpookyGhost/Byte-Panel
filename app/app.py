import os
import re
import sys
import json
import subprocess
import threading
import time
import requests
import psutil
import signal
import shutil
import zipfile
import sqlite3
import urllib.request
import geoip2.database
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_from_directory, send_file, make_response
from flask_socketio import SocketIO, emit
from werkzeug.utils import secure_filename
from datetime import datetime

def get_client_ip():
    """Extracts the real IP address, bypassing reverse proxy obfuscation."""
    if request.headers.get('X-Forwarded-For'):
        # Get the first IP in the comma-separated list (the original client)
        return request.headers.get('X-Forwarded-For').split(',')[0].strip()
    return request.remote_addr

def get_system_ram_gb():
    """Reads /proc/meminfo to determine total system RAM in GB."""
    try:
        with open('/proc/meminfo', 'r') as f:
            for line in f:
                if 'MemTotal' in line:
                    # MemTotal is in kilobytes
                    kb_val = int(line.split()[1])
                    gb_val = int(kb_val / 1024 / 1024)
                    return max(2, gb_val) # Ensure it at least returns 2GB
    except Exception:
        return 8 # Safe fallback if reading fails

# --- VERSION CHECKPOINT ---
print("\n" + "="*50, flush=True)
print("🚀 MC-PANEL BACKEND BOOTING - VERSION 2.0 (CLEAN)", flush=True)
print("="*50 + "\n", flush=True)
try:
    version_file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'version.txt')
    with open(version_file_path, 'r') as f:
        print(f"v{f.read().strip()}")
except Exception:
    print("vUnknown")

app = Flask(__name__)
app.secret_key = os.getenv("PANEL_SECRET_KEY", "mc-panel-secure-key-2026")
socketio = SocketIO(app, cors_allowed_origins="*")

@app.context_processor
def inject_version():
    try:
        version_file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'version.txt')
        with open(version_file_path, 'r') as f:
            v = f.read().strip()
    except Exception:
        v = "Unknown"
    return dict(app_version=f"v{v}")

# --- Predefined Storage Path Architecture ---
DATA_DIR = "/data"
BRANDING_DIR = os.path.join(DATA_DIR, "branding")
SERVER_DIR = os.path.join(DATA_DIR, "server")
os.makedirs(BRANDING_DIR, exist_ok=True)
os.makedirs(SERVER_DIR, exist_ok=True)
os.makedirs(os.path.join(DATA_DIR, "logs"), exist_ok=True)

CONFIG_FILE = os.path.join(DATA_DIR, "panel_config.json")
PLAYER_DB_FILE = os.path.join(DATA_DIR, "players.db")
GEOIP_DB_FILE = os.path.join(DATA_DIR, "GeoLite2-Country.mmdb")

def init_db():
    conn = sqlite3.connect(PLAYER_DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            uuid TEXT PRIMARY KEY,
            username TEXT,
            first_seen TEXT,
            last_seen TEXT,
            ban_status BOOLEAN DEFAULT 0,
            online_status BOOLEAN DEFAULT 0
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS login_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid TEXT,
            timestamp TEXT,
            ip TEXT,
            country TEXT,
            FOREIGN KEY(uuid) REFERENCES users(uuid)
        )
    ''')
    conn.execute("UPDATE users SET online_status = 0")
    
    conn.execute('''
        CREATE TABLE IF NOT EXISTS ban_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid TEXT,
            action TEXT,
            ban_type TEXT,
            reason TEXT,
            admin TEXT,
            timestamp TEXT,
            FOREIGN KEY(uuid) REFERENCES users(uuid)
        )
    ''')
    
    try:
        conn.execute("ALTER TABLE users ADD COLUMN ban_type TEXT DEFAULT 'both'")
    except sqlite3.OperationalError:
        pass
        
    conn.commit()
    conn.close()

def ensure_geoip_db():
    if not os.path.exists(GEOIP_DB_FILE):
        print("Downloading GeoLite2 Country database...")
        try:
            url = "https://github.com/P3TERX/GeoLite.mmdb/raw/download/GeoLite2-Country.mmdb"
            urllib.request.urlretrieve(url, GEOIP_DB_FILE)
            print("Download complete.")
        except Exception as e:
            print(f"Error downloading GeoIP DB: {e}")

init_db()
ensure_geoip_db()



# --- Global State Variables ---
MINECRAFT_PROCESS = None
CONSOLE_HISTORY = []

# --- Core System Configuration Fallbacks ---
DEFAULT_CONFIG = {
    "admin_user": "admin",
    "admin_pass": "admin",
    "is_setup": False,
    "mc_version": "",
    "server_type": "",
    "java_path": "",
    "theme": "dark",
    "panel_name": "Portal Node",
    "bg_blur": 5,
    "bg_tint": 50,
    "timezone": "UTC",
    "time_format": "12"
}

# --- Helper Functions ---
def load_config():
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=4)
        return DEFAULT_CONFIG.copy()
    with open(CONFIG_FILE, "r") as f:
        conf = json.load(f)
        for k, v in DEFAULT_CONFIG.items():
            if k not in conf:
                conf[k] = v
        return conf

def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)

def load_player_history():
    file_path = os.path.join(DATA_DIR, "player_history.json")
    if not os.path.exists(file_path):
        return {}
    try:
        with open(file_path, "r") as f:
            return json.load(f)
    except:
        return {}

def save_player_history(data):
    file_path = os.path.join(DATA_DIR, "player_history.json")
    with open(file_path, "w") as f:
        json.dump(data, f, indent=4)

def sync_bans_from_server():
    banned_uuids = {}
    banned_ips = {}
    
    bp_path = os.path.join(SERVER_DIR, "banned-players.json")
    if os.path.exists(bp_path):
        try:
            with open(bp_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                for item in data:
                    if "uuid" in item:
                        banned_uuids[item["uuid"].lower()] = item
        except:
            pass
            
    bi_path = os.path.join(SERVER_DIR, "banned-ips.json")
    if os.path.exists(bi_path):
        try:
            with open(bi_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                for item in data:
                    if "ip" in item:
                        banned_ips[item["ip"].lower()] = item
        except:
            pass
            
    conn = sqlite3.connect(PLAYER_DB_FILE)
    users = conn.execute('''
        SELECT u.uuid, u.ban_status,
               (SELECT ip FROM login_events WHERE uuid = u.uuid ORDER BY timestamp DESC LIMIT 1) as last_ip
        FROM users u
    ''').fetchall()
    
    updates = []
    events = []
    now_ts = datetime.utcnow().isoformat() + "Z"
    
    for row in users:
        uuid = row[0]
        current_status = row[1]
        last_ip = row[2]
        
        uuid_ban_info = banned_uuids.get(uuid.lower())
        ip_ban_info = banned_ips.get(last_ip.lower()) if last_ip else None
        
        is_uuid_banned = uuid_ban_info is not None
        is_ip_banned = ip_ban_info is not None
        
        new_status = 1 if (is_uuid_banned or is_ip_banned) else 0
        
        if is_uuid_banned and is_ip_banned:
            new_type = 'both'
        elif is_uuid_banned:
            new_type = 'uuid'
        elif is_ip_banned:
            new_type = 'ip'
        else:
            new_type = 'none'
            
        updates.append((new_status, new_type, uuid))
        
        if current_status == 0 and new_status == 1:
            source = "Server"
            reason = "Banned by an operator."
            if uuid_ban_info:
                source = uuid_ban_info.get("source", source)
                reason = uuid_ban_info.get("reason", reason)
            elif ip_ban_info:
                source = ip_ban_info.get("source", source)
                reason = ip_ban_info.get("reason", reason)
            events.append((uuid, 'ban', new_type, reason, source, now_ts))
            
        elif current_status == 1 and new_status == 0:
            events.append((uuid, 'unban', 'none', 'Unbanned', 'Server', now_ts))
            
    if updates:
        conn.executemany("UPDATE users SET ban_status = ?, ban_type = ? WHERE uuid = ?", updates)
    if events:
        conn.executemany("INSERT INTO ban_events (uuid, action, ban_type, reason, admin, timestamp) VALUES (?, ?, ?, ?, ?, ?)", events)
        
    conn.commit()
    conn.close()

def get_java_version_path(mc_version):
    try:
        # Handle 26.x series (Java 25)
        if mc_version.startswith("26."):
            return "/usr/lib/jvm/java-25-openjdk/bin/java"
            
        parts = mc_version.split(".")
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
        
        # Handle the mid-cycle Java 21 split
        if mc_version in ["1.20.5", "1.20.6"] or (major == 1 and minor >= 21):
            return "/usr/lib/jvm/java-21-openjdk/bin/java"
        elif major == 1 and minor >= 17:
            return "/usr/lib/jvm/java-17-openjdk/bin/java"
        elif major == 1 and minor == 16:
            return "/usr/lib/jvm/java-11-openjdk/bin/java"
        else:
            return "/usr/lib/jvm/java-1.8-openjdk/jre/bin/java"
            
    except (ValueError, IndexError):
        return "/usr/bin/java"

def parse_log_line(line):
    join_match = re.search(r"UUID of player (.+?) is (.+)", line)
    ip_match = re.search(r"(.+?)\[/(.+?):\d+\] logged in", line)
    
    if join_match:
        username = join_match.group(1)
        uuid = join_match.group(2).strip()
        history = load_player_history()
        
        if username not in history:
            history[username] = {
                "username": username,
                "uuid": uuid,
                "ip": "Unknown",
                "country": "Unknown",
                "last_seen": time.strftime("%Y-%m-%d %H:%M:%S")
            }
        else:
            history[username]["uuid"] = uuid
            history[username]["last_seen"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_player_history(history)
        
    if ip_match:
        username = ip_match.group(1)
        ip = ip_match.group(2)
        history = load_player_history()
        if username in history:
            history[username]["ip"] = ip
            if ip.startswith("192.168.") or ip.startswith("10.") or ip.startswith("172.") or ip.startswith("127."):
                history[username]["country"] = "Local Network"
            else:
                try:
                    geo = requests.get(f"https://ipapi.co/{ip}/json/", timeout=3).json()
                    history[username]["country"] = geo.get("country_name", "Unknown")
                except Exception:
                    history[username]["country"] = "Unknown"
            save_player_history(history)

def monitor_minecraft_stream(process):
    global CONSOLE_HISTORY
    for line in iter(process.stdout.readline, ''):
        if not line:
            break
        decoded_line = line.strip()
        CONSOLE_HISTORY.append(decoded_line)
        if len(CONSOLE_HISTORY) > 1200:
            CONSOLE_HISTORY.pop(0)
            
        parse_log_line(decoded_line)
        socketio.emit('console_output', {'data': decoded_line})
    process.stdout.close()

def start_minecraft_server():
    global MINECRAFT_PROCESS
    config = load_config()

    if config.get('is_running', False):
        print("[WATCHDOG] ERROR: Cannot start. Server is already running.", flush=True)
        return False

    version = config.get('mc_version')
    ram_gb = config.get('ram_gb', 2)

    if not version:
        print("[WATCHDOG] ERROR: Cannot start. No Minecraft version found in config.", flush=True)
        return False

    java_bin = get_java_version_path(version)
    server_jar = os.path.join(SERVER_DIR, "server.jar")

    if not os.path.exists(server_jar):
        print("[WATCHDOG] ERROR: server.jar is missing from the server directory!", flush=True)
        return False

    # 1. Bypass the EULA Crash
    eula_path = os.path.join(SERVER_DIR, "eula.txt")
    with open(eula_path, "w") as f:
        f.write("eula=true\n")

    # 2. Build the Java Execution Command
# Build the Java Execution Command dynamically
    cmd = [
        java_bin, 
        f"-Xmx{ram_gb}G", 
        f"-Xms{ram_gb}G", 
        "-jar", server_jar, "nogui"
    ]

    try:
        # 3. Launch in the background and pipe output to a log file
        log_path = os.path.join(SERVER_DIR, "console.log")
        history_path = os.path.join(DATA_DIR, "logs", "full_console_history.log")
        
        # Append existing console log to full history before clearing
        if os.path.exists(log_path):
            with open(log_path, "r", encoding='utf-8', errors='replace') as src, open(history_path, "a", encoding='utf-8') as dst:
                dst.write(src.read())
        else:
            open(log_path, 'a').close()
                
        # Track session count and generate header
        session_num = config.get('log_session_counter', 0) + 1
        config['log_session_counter'] = session_num
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        header = f"\n{'='*55}\n[ LOG SESSION: #{session_num} | STARTED: {timestamp} ]\n{'='*55}\n\n"
        
        # Open in write mode to clear the live console for this session and write header
        with open(log_path, "w", encoding='utf-8') as log_file:
            log_file.write(header)
            
        log_file = open(log_path, "a", encoding='utf-8')
        
        process = subprocess.Popen(
            cmd, 
            cwd=SERVER_DIR, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            universal_newlines=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1
        )
        MINECRAFT_PROCESS = process
        
        def stream_logger(proc, log_f):
            pending_uuids = {} # username -> uuid map
            
            for line in proc.stdout:
                log_f.write(line)
                log_f.flush()
                
                # Active Log Parsing for Security Monitor
                try:
                    # 1. Capture UUID mappings
                    uuid_match = re.search(r'UUID of player ([a-zA-Z0-9_]+) is ([a-zA-Z0-9\-]+)', line)
                    if uuid_match:
                        pending_uuids[uuid_match.group(1)] = uuid_match.group(2)
                        
                    # 2. Capture Logins with IPs
                    login_match = re.search(r'([a-zA-Z0-9_]+)\[/([0-9\.]+):[0-9]+\] logged in', line)
                    if login_match:
                        username = login_match.group(1)
                        ip = login_match.group(2)
                        uuid = pending_uuids.get(username, "unknown")
                        
                        country = "Unknown"
                        if ip.startswith("192.168.") or ip.startswith("10.") or ip.startswith("172.") or ip.startswith("127."):
                            country = "Local Network"
                        elif os.path.exists(GEOIP_DB_FILE):
                            try:
                                with geoip2.database.Reader(GEOIP_DB_FILE) as reader:
                                    country = reader.country(ip).country.name or "Unknown"
                            except Exception:
                                pass
                                
                        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        
                        conn = sqlite3.connect(PLAYER_DB_FILE)
                        # Upsert user
                        conn.execute('''
                            INSERT INTO users (uuid, username, first_seen, last_seen, online_status) 
                            VALUES (?, ?, ?, ?, 1)
                            ON CONFLICT(uuid) DO UPDATE SET 
                                username=excluded.username,
                                last_seen=excluded.last_seen,
                                online_status=1
                        ''', (uuid, username, timestamp, timestamp))
                        
                        # Insert login event
                        conn.execute('''
                            INSERT INTO login_events (uuid, timestamp, ip, country)
                            VALUES (?, ?, ?, ?)
                        ''', (uuid, timestamp, ip, country))
                        
                        conn.commit()
                        conn.close()
                        
                    # 3. Capture Disconnects
                    disconnect_match = re.search(r'([a-zA-Z0-9_]+) lost connection:', line)
                    if disconnect_match:
                        username = disconnect_match.group(1)
                        conn = sqlite3.connect(PLAYER_DB_FILE)
                        conn.execute("UPDATE users SET online_status = 0 WHERE username = ?", (username,))
                        conn.commit()
                        conn.close()
                        
                except Exception as e:
                    print(f"Log parsing error: {e}", flush=True)

            log_f.close()
            # On crash/stop, mark everyone offline
            try:
                conn = sqlite3.connect(PLAYER_DB_FILE)
                conn.execute("UPDATE users SET online_status = 0")
                conn.commit()
                conn.close()
            except: pass
            
        threading.Thread(target=stream_logger, args=(process, log_file), daemon=True).start()
        
        # 4. Save the Process ID (PID) so the Stop button knows what to kill later
        config['server_pid'] = process.pid
        config['is_running'] = True
        save_config(config)
        
        print(f"[WATCHDOG] SERVER LAUNCH: '{version}' started successfully on PID {process.pid}.", flush=True)
        return True

    except Exception as e:
        print(f"[WATCHDOG] LAUNCH FAILURE: {str(e)}", flush=True)
        return False

def stop_minecraft_server():
    global MINECRAFT_PROCESS
    config = load_config()
    pid = config.get('server_pid')
    
    config['target_state'] = 'Shutting Down'
    save_config(config)
    
    if MINECRAFT_PROCESS and MINECRAFT_PROCESS.poll() is None:
        try:
            # Issue save-all before stopping
            MINECRAFT_PROCESS.stdin.write("save-all\n")
            MINECRAFT_PROCESS.stdin.flush()
            time.sleep(1.5) # Give it a brief moment to start saving
            
            MINECRAFT_PROCESS.stdin.write("stop\n")
            MINECRAFT_PROCESS.stdin.flush()
            print("[WATCHDOG] SERVER STOP: Sent save-all then stop command via stdin.", flush=True)
            # Give it a second before updating config
            config['is_running'] = False
            config['server_pid'] = None
            save_config(config)
            
            # Write to server console so the UI shows it stopped
            try:
                with open(os.path.join(SERVER_DIR, "console.log"), "a", encoding='utf-8') as lf:
                    lf.write("\n[System] Server shut down by user.\n")
            except: pass
            
            return True
        except Exception as e:
            print(f"[WATCHDOG] STOP ERROR (stdin): {str(e)}", flush=True)
            # fallback to os.kill
            
    if pid:
        try:
            # Send a gentle termination signal to the process
            os.kill(pid, signal.SIGTERM)
            print(f"[WATCHDOG] SERVER STOP: Sent termination signal to PID {pid}.", flush=True)
            
            # Update the config state
            config['is_running'] = False
            config['server_pid'] = None
            save_config(config)
            
            # Write to server console so the UI shows it stopped
            try:
                with open(os.path.join(SERVER_DIR, "console.log"), "a", encoding='utf-8') as lf:
                    lf.write("\n[System] Server shut down by user.\n")
            except: pass
            
            return True
            
        except ProcessLookupError:
            print("[WATCHDOG] SERVER STOP: Process already dead. Clearing state.", flush=True)
            config['is_running'] = False
            config['server_pid'] = None
            save_config(config)
            return True
        except Exception as e:
            print(f"[WATCHDOG] STOP ERROR: {str(e)}", flush=True)
            return False
    else:
        print("[WATCHDOG] STOP ERROR: No PID found in config.", flush=True)
        return False

# --- HTTP Routing & Middleware ---

# Wire it to a Flask Route
@app.route('/stop', methods=['POST'])
def stop_server():
    stop_minecraft_server()
    return redirect(url_for('overview'))

@app.before_request
def check_auth_and_setup():
    config = load_config()
    public_routes = ['login', 'static', 'serve_custom_branding']
    
    if not config.get('is_setup'):
        public_routes.extend(['setup', 'import_server', 'welcome'])

    if request.endpoint and request.endpoint not in public_routes:
        if not session.get('logged_in'):
            return redirect(url_for('login'))
            
        if not config.get('is_setup'):
            if not config.get('welcome_seen') and request.endpoint != 'welcome':
                return redirect(url_for('welcome'))
            elif config.get('welcome_seen') and request.endpoint not in ['setup', 'import_server', 'welcome']:
                return redirect(url_for('setup'))

@app.route('/branding-assets/<filename>')
def serve_custom_branding(filename):
    return send_from_directory(BRANDING_DIR, filename)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        # Retrieve input safely
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        client_ip = get_client_ip()
        
        config = load_config()
        
        # Authentication Validation
        if username == config.get('admin_user') and password == config.get('admin_pass'):
            session['logged_in'] = True
            
            # SUCCESS LOG: flush=True forces the log straight to Docker/Supervisord stdout
            print(f"[WATCHDOG] AUTH SUCCESS: User '{username}' authenticated from IP: {client_ip}", flush=True)
            
            return redirect(url_for('overview'))
            
        else:
            # FAILURE LOG: Crucial for brute-force watchdog tracking
            print(f"[WATCHDOG] AUTH FAILURE: Invalid credentials for '{username}' from IP: {client_ip}", flush=True)
            
            # (Optional) flash('Invalid credentials', 'error')
            return render_template('login.html', error="Invalid username or password.", config=config, has_custom=get_custom_assets_state())
            
    return render_template('login.html', config=load_config(), has_custom=get_custom_assets_state())

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/welcome', methods=['GET', 'POST'])
def welcome():
    config = load_config()
    if config.get('welcome_seen') and config.get('is_setup'):
        return redirect(url_for('overview'))
        
    if request.method == 'POST':
        config['welcome_seen'] = True
        save_config(config)
        return redirect(url_for('setup'))
        
    return render_template('welcome.html')

@app.route('/setup', methods=['GET', 'POST'])
def setup():
    config = load_config()
    max_ram = get_system_ram_gb() # Fetch the max RAM
    if request.method == 'POST':

        print(f"[WATCHDOG] INCOMING SETUP DATA: {request.form}", flush=True)

        server_type = request.form.get('server_type')
        version = request.form.get('version')
        
        # Capture the RAM slider value (default to 2 if missing)
        ram_allocation = request.form.get('ram_allocation', '2')

        admin_user = request.form.get('admin_username') 
        admin_pass = request.form.get('admin_password')
        
        if not all([server_type, version, admin_user, admin_pass]):
            print("[WATCHDOG] ERROR: Missing required form fields!", flush=True)
            # You can flash an error here or redirect back to setup
            return redirect(url_for('setup'))
        
        #Save to Config
        config['server_type'] = server_type
        config['mc_version'] = version
        config['ram_gb'] = int(ram_allocation)
        config['admin_user'] = admin_user
        config['admin_pass'] = admin_pass
        config['is_setup'] = True
        
        save_config(config)
        
        # Write ServerEngineInfo for future imports
        engine_info_path = os.path.join(SERVER_DIR, "ServerEngineInfo")
        with open(engine_info_path, "w") as f:
            json.dump({
                "mc_version": version,
                "server_type": server_type,
                "ram_gb": int(ram_allocation)
            }, f)

        # 1. Download Resolution Logic
        jar_url = ""
        installer_mode = False
        
        if server_type == "Vanilla":
            # Traverse Mojang's version manifest
            manifest = requests.get("https://launchermeta.mojang.com/mc/game/version_manifest.json").json()
            for v in manifest['versions']:
                if v['id'] == version:
                    meta = requests.get(v['url']).json()
                    jar_url = meta['downloads']['server']['url']
                    break
                    
        elif server_type == "Paper":
            builds = requests.get(f"https://api.papermc.io/v2/projects/paper/versions/{version}/builds").json()
            latest_build = builds['builds'][-1]['build']
            jar_url = f"https://api.papermc.io/v2/projects/paper/versions/{version}/builds/{latest_build}/downloads/paper-{version}-{latest_build}.jar"
            
        elif server_type == "Purpur":
            jar_url = f"https://api.purpurmc.org/v2/purpur/{version}/latest/download"
            
        elif server_type == "Fabric":
            # Fabric provides a pre-stitched server jar endpoint
            jar_url = f"https://meta.fabricmc.net/v2/versions/loader/{version}/0.15.11/1.0.1/server/jar"
            
        elif server_type == "Forge":
            # Forge requires an installer utility
            jar_url = f"https://maven.minecraftforge.net/net/minecraftforge/forge/{version}/forge-{version}-installer.jar"
            installer_mode = True

        # 2. File Acquisition
        target_filename = "installer.jar" if installer_mode else "server.jar"
        target_path = os.path.join(SERVER_DIR, target_filename)
        
        if jar_url:
            res = requests.get(jar_url, stream=True)
            with open(target_path, 'wb') as f:
                for chunk in res.iter_content(chunk_size=8192):
                    f.write(chunk)

        # 3. Forge Installation Subprocess
        if installer_mode and server_type == "Forge":
            java_bin = get_java_version_path(version)
            # Run the Forge installer headlessly
            subprocess.run([java_bin, "-jar", target_filename, "--installServer"], cwd=SERVER_DIR)
            # Clean up the installer
            os.remove(target_path)
            # Forge uses custom run scripts now, but for legacy compatibility we rename the generated forge jar
            for file in os.listdir(SERVER_DIR):
                if file.startswith("forge-") and file.endswith(".jar"):
                    os.rename(os.path.join(SERVER_DIR, file), os.path.join(SERVER_DIR, "server.jar"))

        # 4. Finalize Configuration
        config['mc_version'] = version
        config['server_type'] = server_type
        config['java_path'] = "" 
        config['is_setup'] = True
        save_config(config)
        
        start_minecraft_server()
        return redirect(url_for('overview'))
        
    return render_template('setup.html', config=config, max_ram=max_ram, has_custom=get_custom_assets_state())

@app.route('/')
@app.route('/overview')
def overview(): 
    return render_template('overview.html', config=load_config(), has_custom=get_custom_assets_state())

@app.route('/console')
def console(): 
    return render_template('console.html', config=load_config(), history=CONSOLE_HISTORY, has_custom=get_custom_assets_state())

@app.route('/logs')
def logs():
    log_content = ""
    history_path = os.path.join(DATA_DIR, "logs", "full_console_history.log")
    live_path = os.path.join(SERVER_DIR, "console.log")
    
    if os.path.exists(history_path):
        with open(history_path, "r", encoding='utf-8', errors='replace') as f:
            log_content += f.read()
    if os.path.exists(live_path):
        with open(live_path, "r", encoding='utf-8', errors='replace') as f:
            log_content += f.read()
            
    if not log_content:
        log_content = "No log history verified yet. Start the engine to write logs."
    return render_template('logs.html', config=load_config(), log_content=log_content, has_custom=get_custom_assets_state())

@app.route('/api/logs/clear', methods=['POST'])
def clear_logs():
    history_path = os.path.join(DATA_DIR, "logs", "full_console_history.log")
    if os.path.exists(history_path):
        os.remove(history_path)
    
    config = load_config()
    config['log_session_counter'] = 0
    save_config(config)
    
    return jsonify({"status": "cleared"})

@app.route('/api/players')
def api_players():
    if not request.args.get('skipSync'):
        sync_bans_from_server()
    conn = sqlite3.connect(PLAYER_DB_FILE)
    conn.row_factory = sqlite3.Row
    users = conn.execute('''
        SELECT u.uuid, u.username, u.first_seen, u.last_seen, u.online_status, u.ban_status, u.ban_type,
               (SELECT ip FROM login_events WHERE uuid = u.uuid ORDER BY timestamp DESC LIMIT 1) as last_ip,
               (SELECT country FROM login_events WHERE uuid = u.uuid ORDER BY timestamp DESC LIMIT 1) as country
        FROM users u
    ''').fetchall()
    conn.close()
    return jsonify([dict(row) for row in users])

@app.route('/players')
def players():
    sync_bans_from_server()
    conn = sqlite3.connect(PLAYER_DB_FILE)
    conn.row_factory = sqlite3.Row
    users = conn.execute('''
        SELECT u.uuid, u.username, u.first_seen, u.last_seen, u.online_status, u.ban_status, u.ban_type,
               (SELECT ip FROM login_events WHERE uuid = u.uuid ORDER BY timestamp DESC LIMIT 1) as last_ip,
               (SELECT country FROM login_events WHERE uuid = u.uuid ORDER BY timestamp DESC LIMIT 1) as country
        FROM users u
    ''').fetchall()
    conn.close()
    return render_template('players.html', config=load_config(), players=users, has_custom=get_custom_assets_state())

@app.route('/api/player/<uuid>/history')
def get_player_history(uuid):
    conn = sqlite3.connect(PLAYER_DB_FILE)
    conn.row_factory = sqlite3.Row
    events = conn.execute('''
        SELECT timestamp, ip, country 
        FROM login_events 
        WHERE uuid = ? 
        ORDER BY timestamp DESC
    ''', (uuid,)).fetchall()
    conn.close()
    return jsonify([dict(row) for row in events])

@app.route('/api/player/<uuid>/ban_history')
def get_player_ban_history(uuid):
    conn = sqlite3.connect(PLAYER_DB_FILE)
    conn.row_factory = sqlite3.Row
    events = conn.execute('''
        SELECT action, ban_type, reason, admin, timestamp 
        FROM ban_events 
        WHERE uuid = ? 
        ORDER BY timestamp DESC
    ''', (uuid,)).fetchall()
    conn.close()
    return jsonify([dict(row) for row in events])

@app.route('/api/player/<action>', methods=['POST'])
def moderate_player(action):
    global MINECRAFT_PROCESS
    payload = request.get_json()
    player_name = payload.get('username')
    ip = payload.get('ip')
    reason = payload.get('reason', '')
    ban_type = payload.get('ban_type', 'both')
    
    reason_str = f" {reason}" if reason else ""
    
    if not MINECRAFT_PROCESS or MINECRAFT_PROCESS.poll() is not None:
        return jsonify({"status": "error", "message": "Server offline"})
        
    admin_user = load_config().get("admin_user", "Admin")
    web_panel_source = f"Web Panel: {admin_user}"
    now_ts = datetime.utcnow().isoformat() + "Z"
    
    conn = sqlite3.connect(PLAYER_DB_FILE)
    user = conn.execute("SELECT uuid, ban_type FROM users WHERE username = ?", (player_name,)).fetchone()
    uuid = user[0] if user else None
    stored_type = user[1] if user and user[1] else 'both'
        
    if action == "kick":
        MINECRAFT_PROCESS.stdin.write(f"kick {player_name}{reason_str}\n")
    elif action == "ban":
        if ban_type in ['username', 'both']:
            MINECRAFT_PROCESS.stdin.write(f"ban {player_name}{reason_str}\n")
        if ban_type in ['ip', 'both'] and ip:
            MINECRAFT_PROCESS.stdin.write(f"ban-ip {ip}{reason_str}\n")
            
        if uuid:
            conn.execute("UPDATE users SET ban_status = 1, ban_type = ? WHERE uuid = ?", (ban_type, uuid))
            conn.execute("INSERT INTO ban_events (uuid, action, ban_type, reason, admin, timestamp) VALUES (?, ?, ?, ?, ?, ?)", (uuid, 'ban', ban_type, reason, web_panel_source, now_ts))
    elif action == "unban":
        if stored_type in ['username', 'uuid', 'both', 'none']:
            MINECRAFT_PROCESS.stdin.write(f"pardon {player_name}\n")
        if stored_type in ['ip', 'both', 'none'] and ip:
            MINECRAFT_PROCESS.stdin.write(f"pardon-ip {ip}\n")
            
        if uuid:
            conn.execute("UPDATE users SET ban_status = 0, ban_type = NULL WHERE uuid = ?", (uuid,))
            conn.execute("INSERT INTO ban_events (uuid, action, ban_type, reason, admin, timestamp) VALUES (?, ?, ?, ?, ?, ?)", (uuid, 'unban', 'none', 'Unbanned', web_panel_source, now_ts))
            
    conn.commit()
    conn.close()
        
    MINECRAFT_PROCESS.stdin.flush()
    return jsonify({"status": "success"})

def get_custom_assets_state():
    return {
        "logo": os.path.exists(os.path.join(BRANDING_DIR, 'logo.png')),
        "favicon": os.path.exists(os.path.join(BRANDING_DIR, 'favicon.ico')),
        "bg": os.path.exists(os.path.join(BRANDING_DIR, 'bg.jpg'))
    }

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    config = load_config()
    if request.method == 'POST':
        config['theme'] = request.form.get('theme', config.get('theme', 'dark'))
        config['java_path'] = request.form.get('java_path', config.get('java_path'))
        config['panel_name'] = request.form.get('panel_name', config.get('panel_name'))
        config['bg_blur'] = request.form.get('bg_blur', config.get('bg_blur', 25))
        config['bg_tint'] = request.form.get('bg_tint', config.get('bg_tint', 50))
        config['timezone'] = request.form.get('timezone', config.get('timezone', 'UTC'))
        config['time_format'] = request.form.get('time_format', config.get('time_format', '12'))
        
        if 'custom_logo' in request.files and request.files['custom_logo'].filename:
            request.files['custom_logo'].save(os.path.join(BRANDING_DIR, 'logo.png'))
        if 'custom_favicon' in request.files and request.files['custom_favicon'].filename:
            request.files['custom_favicon'].save(os.path.join(BRANDING_DIR, 'favicon.ico'))
        if 'custom_bg' in request.files and request.files['custom_bg'].filename:
            request.files['custom_bg'].save(os.path.join(BRANDING_DIR, 'bg.jpg'))
            
        save_config(config)
    
    return render_template('settings.html', config=config, has_custom=get_custom_assets_state())

@app.route('/start', methods=['POST'])
def start_server():
    # Only try to start if it isn't already running
    config = load_config()
    if not config.get('is_running', False):
        start_minecraft_server()
    return redirect(url_for('overview'))

# --- Process Lifecycle Infrastructure API Rules ---
@app.route('/api/server/<action>', methods=['POST'])
def server_action(action):
    global MINECRAFT_PROCESS
    if action == "start":
        success = start_minecraft_server()
        return jsonify({"status": "started" if success else "failed/already running"})
    elif action == "stop":
        log_path = os.path.join(SERVER_DIR, "console.log")
        with open(log_path, "a", encoding='utf-8') as f:
            f.write("[Action] Server stopped.\n")
            f.flush()
        success = stop_minecraft_server()
        return jsonify({"status": "stopping" if success else "already offline or error"})
    elif action == "restart":
        log_path = os.path.join(SERVER_DIR, "console.log")
        with open(log_path, "a", encoding='utf-8') as f:
            f.write("[Action] Server restarting.\n")
            f.flush()
        stop_minecraft_server()
        time.sleep(5) 
        start_minecraft_server()
        return jsonify({"status": "restarted"})

@app.route('/api/backup', methods=['POST'])
def backup_server():
    # Inject config into the server dir temporarily
    temp_config = os.path.join(SERVER_DIR, "panel_config.json")
    if os.path.exists(CONFIG_FILE):
        shutil.copy2(CONFIG_FILE, temp_config)
        
    backup_path = os.path.join(DATA_DIR, "server_backup")
    shutil.make_archive(backup_path, 'zip', SERVER_DIR)
    
    if os.path.exists(temp_config):
        os.remove(temp_config)
        
    response = make_response(send_file(f"{backup_path}.zip", as_attachment=True, download_name=f"server_backup_{int(time.time())}.zip"))
    response.set_cookie('backup_complete', '1', max_age=30, path='/')
    return response

@app.route('/api/purge', methods=['POST'])
def purge_server():
    global MINECRAFT_PROCESS
    # Enforce safe stop
    if MINECRAFT_PROCESS and MINECRAFT_PROCESS.poll() is None:
        stop_minecraft_server()
        time.sleep(2)
        config = load_config()
        if config.get('server_pid'):
            try: os.kill(config.get('server_pid'), signal.SIGKILL)
            except: pass
            
    # Wipe core server files
    for filename in os.listdir(SERVER_DIR):
        file_path = os.path.join(SERVER_DIR, filename)
        try:
            if os.path.isdir(file_path): shutil.rmtree(file_path)
            else: os.unlink(file_path)
        except: pass
        
    # Wipe logs
    logs_dir = os.path.join(DATA_DIR, "logs")
    if os.path.exists(logs_dir):
        for filename in os.listdir(logs_dir):
            file_path = os.path.join(logs_dir, filename)
            try:
                if os.path.isfile(file_path): os.unlink(file_path)
            except: pass
            
    # Reset internal panel ecosystem configuration
    config = load_config()
    config['is_setup'] = False
    config['log_session_counter'] = 0
    config['mc_version'] = None
    config['server_type'] = None
    config['target_state'] = 'Offline'
    config['is_running'] = False
    config['server_pid'] = None
    config['log_session_counter'] = 0
    save_config(config)
    
    return jsonify({"status": "purged"})

@app.route('/api/import', methods=['POST'])
def import_server():
    if 'import_zip' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
        
    file = request.files['import_zip']
    if file.filename == '':
        return jsonify({"error": "No file selected"}), 400
        
    global MINECRAFT_PROCESS
    
    # Pre-flight check: store zip temporarily and inspect for ServerEngineInfo
    temp_zip = os.path.join(DATA_DIR, "temp_import.zip")
    file.save(temp_zip)
    
    needs_manual = False
    try:
        with zipfile.ZipFile(temp_zip, 'r') as zip_ref:
            if "ServerEngineInfo" not in zip_ref.namelist():
                if not request.form.get('manual_mc_version'):
                    needs_manual = True
    except:
        pass
        
    if needs_manual:
        os.remove(temp_zip)
        return jsonify({"missing_engine_info": True})
        
    if MINECRAFT_PROCESS and MINECRAFT_PROCESS.poll() is None:
        stop_minecraft_server()
        time.sleep(2)
        
    # Wipe current server files to prepare for clean extraction
    for filename in os.listdir(SERVER_DIR):
        file_path = os.path.join(SERVER_DIR, filename)
        try:
            if os.path.isdir(file_path): shutil.rmtree(file_path)
            else: os.unlink(file_path)
        except: pass
        
    # Extract ZIP safely
    try:
        with zipfile.ZipFile(temp_zip, 'r') as zip_ref:
            zip_ref.extractall(SERVER_DIR)
    finally:
        if os.path.exists(temp_zip):
            os.remove(temp_zip)
            
    # Auto-detect ecosystem config from zip
    imported_info = os.path.join(SERVER_DIR, "ServerEngineInfo")
    config = load_config()
    
    if os.path.exists(imported_info):
        try:
            with open(imported_info, "r") as f:
                engine_data = json.load(f)
                if 'mc_version' in engine_data: config['mc_version'] = engine_data['mc_version']
                if 'server_type' in engine_data: config['server_type'] = engine_data['server_type']
                if 'ram_gb' in engine_data: config['ram_gb'] = engine_data['ram_gb']
        except: pass
    else:
        # Fallback to manual payload
        config['mc_version'] = request.form.get('manual_mc_version')
        config['server_type'] = request.form.get('manual_server_type')
        config['ram_gb'] = int(request.form.get('manual_ram_gb', 2))
        
        # Synthesize the file for future backups
        with open(imported_info, "w") as f:
            json.dump({
                "mc_version": config['mc_version'],
                "server_type": config['server_type'],
                "ram_gb": config['ram_gb']
            }, f)
            
    # Fallback to panel_config.json if it exists from an older backup, then clean it up
    legacy_config = os.path.join(SERVER_DIR, "panel_config.json")
    if os.path.exists(legacy_config):
        os.remove(legacy_config)
        
    config['is_setup'] = True
    config['target_state'] = 'Offline'
    config['is_running'] = False
    config['server_pid'] = None
    
    if request.form.get('admin_user') and request.form.get('admin_pass'):
        config['admin_user'] = request.form.get('admin_user')
        config['admin_pass'] = request.form.get('admin_pass')
        session['logged_in'] = True
        
    save_config(config)
    
    return jsonify({"status": "imported"})

@app.route('/api/stats')
def api_stats():
    global MINECRAFT_PROCESS
    status = "Offline"
    cpu, ram = 0, 0
    config = load_config()
    target_state = config.get('target_state', 'Offline')
    
    if MINECRAFT_PROCESS and MINECRAFT_PROCESS.poll() is None:
        status = "Online"
        
        # State Parsing
        log_path = os.path.join(SERVER_DIR, "console.log")
        is_done = False
        if os.path.exists(log_path):
            with open(log_path, "r") as f:
                content = f.read()
                if "Done (" in content or "For help, type" in content:
                    is_done = True
        
        if target_state == "Shutting Down":
            status = "Shutting Down"
        elif not is_done:
            status = "Starting"
            
        try:
            p = psutil.Process(MINECRAFT_PROCESS.pid)
            cpu = p.cpu_percent(interval=0.05)
            ram = p.memory_info().rss / (1024 * 1024)
        except psutil.NoSuchProcess: pass
    else:
        if target_state != "Offline":
            config['target_state'] = "Offline"
            save_config(config)
            
    return jsonify({"status": status, "cpu": f"{cpu:.1f}%", "ram": f"{ram:.1f} MB", "tps": "20.0" if status == "Online" else "0.0"})

@app.route('/api/console')
def get_console():
    log_path = os.path.join(SERVER_DIR, "console.log")
    try:
        if os.path.exists(log_path):
            # Read the last 100 lines of the log file so we don't overload the browser
            with open(log_path, "r", encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
                return "".join(lines[-100:])
        else:
            return "Server is offline or initializing. Waiting for log output..."
    except Exception as e:
        return f"Error reading console: {str(e)}"


@socketio.on('send_command')
def handle_command(payload):
    global MINECRAFT_PROCESS
    if MINECRAFT_PROCESS and MINECRAFT_PROCESS.poll() is None:
        cmd_str = payload.get('command', '') + "\n"
        MINECRAFT_PROCESS.stdin.write(cmd_str)
        MINECRAFT_PROCESS.stdin.flush()

@app.route('/api/command', methods=['POST'])
def handle_api_command():
    global MINECRAFT_PROCESS
    payload = request.get_json() or {}
    if MINECRAFT_PROCESS and MINECRAFT_PROCESS.poll() is None:
        cmd_str = payload.get('command', '') + "\n"
        MINECRAFT_PROCESS.stdin.write(cmd_str)
        MINECRAFT_PROCESS.stdin.flush()
        return jsonify({"status": "success"})
    return jsonify({"status": "error", "message": "Server offline"})

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)