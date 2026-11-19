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
import hashlib
import ipaddress
from contextlib import closing, contextmanager
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_from_directory, send_file, make_response, g, abort
from flask_socketio import SocketIO, emit
from werkzeug.utils import secure_filename
from datetime import datetime
from plugin_manager import register_plugin_manager
from server_files import register_file_manager
from addon_updates import AddonUpdateService, AddonUpdateError, AddonUpdateBusy
from log_reader import read_tail, StartupLogStatus
from panel_security import PanelSecurity
from panel_accounts import grants, session_grants
from minecraft_releases import MinecraftReleases, DOWNLOAD_HEADERS, java_path
from server_backups import ServerBackups, BackupBusy, BackupCompletedError, backup_error, backup_server_key
from backup_lifecycle import BackupLifecycle
from server_automation import ServerAutomation
from server_icon import server_icon_path, save_server_icon, ServerIconError
from bluemap import register_bluemap, checked_port
from guard_runtime import GuardRuntime, redact_auth_commands

release_service = MinecraftReleases()
startup_log_status = StartupLogStatus()

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
# Socket events only read Flask's signed session; do not create a stale session fork.
socketio = SocketIO(app, manage_session=False)

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
DATA_DIR = os.getenv('PANEL_DATA_DIR', '/data')
BRANDING_DIR = os.path.join(DATA_DIR, "branding")
SERVER_DIR = os.path.join(DATA_DIR, "server")
os.makedirs(BRANDING_DIR, exist_ok=True)
os.makedirs(SERVER_DIR, exist_ok=True)
os.makedirs(os.path.join(DATA_DIR, "logs"), exist_ok=True)

CONFIG_FILE = os.path.join(DATA_DIR, "panel_config.json")
CONFIG_LOCK = threading.RLock()
PLAYER_DB_FILE = os.path.join(DATA_DIR, "players.db")

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

init_db()



# --- Global State Variables ---
MINECRAFT_PROCESS = None
MINECRAFT_LOG_THREAD = None
CONSOLE_HISTORY = []
PLAYER_STATE_LOCK = threading.RLock()
GUARD_PLAYER_SESSIONS = {}

# --- Core System Configuration Fallbacks ---
DEFAULT_CONFIG = {
    "admin_user": "admin",
    "is_setup": False,
    "mc_version": "",
    "server_type": "",
    "java_path": "",
    "forge_version": "",
    "forge_full_version": "",
    "launch_type": "",
    "launch_target": "",
    "theme": "dark",
    "panel_name": "Byte Panel",
    "accent": "lime",
    "bluemap_enabled": False,
    "bluemap_port": 8100,
    "backup_shutdown": False,
    "auto_start": False,
    "auto_restart": False,
    "bg_blur": 5,
    "bg_tint": 50,
    "timezone": "UTC",
    "time_format": "12"
}

# --- Helper Functions ---
def load_config():
    with CONFIG_LOCK:
        if not os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "w") as f:
                json.dump(DEFAULT_CONFIG, f, indent=4)
            return DEFAULT_CONFIG.copy()
        with open(CONFIG_FILE, "r") as f:
            conf = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                if k not in conf:
                    conf[k] = v
            # Product branding is fixed; the server keeps its independent display name.
            conf["panel_name"] = "Byte Panel"
            if conf.get("accent") not in ("lime", "yellow", "blue"):
                conf["accent"] = "lime"
            return conf

def save_config(config):
    with CONFIG_LOCK:
        config.pop('admin_pass', None)
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=4)


def mark_minecraft_offline(expected_pid=None):
    # Update only runtime fields from a fresh snapshot, never stale settings.
    with CONFIG_LOCK:
        config = load_config()
        if expected_pid is not None and config.get('server_pid') != expected_pid:
            return
        config.update(is_running=False, server_pid=None, target_state='Offline')
        save_config(config)

security = PanelSecurity(app, DATA_DIR, load_config, save_config, lambda: get_custom_assets_state())
backup_lifecycle = BackupLifecycle(lambda: MINECRAFT_PROCESS, lambda: MINECRAFT_LOG_THREAD,
                                   load_config, save_config, lambda: start_minecraft_server(), app.logger,
                                   before_stop=lambda: server_automation.stop())
server_backups = ServerBackups(DATA_DIR, SERVER_DIR, load_config, app.logger, backup_lifecycle.offline)
server_automation = ServerAutomation(load_config, lambda: start_minecraft_server(automatic=True),
                                     lambda: MINECRAFT_PROCESS, lambda: MINECRAFT_LOG_THREAD,
                                     server_backups.exclusive, app.logger)
register_bluemap(app, load_config)


def exclusive_server_files(function):
    """Do not replace server data while a backup is reading it (or vice versa)."""
    @wraps(function)
    def guarded(*args, **kwargs):
        if request.method in ('GET', 'HEAD'):
            return function(*args, **kwargs)
        try:
            with server_backups.exclusive():
                return function(*args, **kwargs)
        except BackupBusy as error:
            return jsonify(status='error', error=str(error), message=str(error)), 409
    return guarded

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

def clear_player_data():
    """Reset server-scoped player records without deleting an active SQLite/WAL file."""
    with PLAYER_STATE_LOCK, closing(sqlite3.connect(PLAYER_DB_FILE)) as conn:
        with conn:
            GUARD_PLAYER_SESSIONS.clear()
            conn.execute("DELETE FROM login_events")
            conn.execute("DELETE FROM ban_events")
            conn.execute("DELETE FROM users")
            conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('login_events', 'ban_events')")
            try:
                os.remove(os.path.join(DATA_DIR, "player_history.json"))
            except FileNotFoundError:
                pass

def record_player_join(player_uuid, username, ip, country, timestamp=None):
    """Display history only. Log observations never authorize Guard admission."""
    if not player_uuid or player_uuid == 'unknown':
        return
    timestamp = timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with PLAYER_STATE_LOCK, closing(sqlite3.connect(PLAYER_DB_FILE)) as conn:
        with conn:
            conn.execute('''INSERT INTO users (uuid,username,first_seen,last_seen,online_status)
                VALUES (?,?,?,?,1) ON CONFLICT(uuid) DO UPDATE SET
                username=excluded.username,last_seen=excluded.last_seen,online_status=1''',
                (player_uuid, username, timestamp, timestamp))
            conn.execute('INSERT INTO login_events(uuid,timestamp,ip,country) VALUES (?,?,?,?)',
                         (player_uuid, timestamp, ip, country))


def record_guard_outcome(result, payload):
    # Only the authenticated companion's completed session can update this view.
    with PLAYER_STATE_LOCK:
        if result.get('scope') != security.store.scope():
            return
        name = str(result.get('username', '')).casefold()
        attempt = result.get('attempt_id')
        if result.get('committed'):
            player_uuid = result.get('uuid') or result['identity']
            record_player_join(player_uuid, result['username'], result['ip'], result['country'])
            GUARD_PLAYER_SESSIONS[name] = (attempt, player_uuid)
        if result.get('stage') == 'disconnected' and GUARD_PLAYER_SESSIONS.get(name, (None,))[0] == attempt:
            _, player_uuid = GUARD_PLAYER_SESSIONS.pop(name)
            with closing(sqlite3.connect(PLAYER_DB_FILE)) as conn:
                with conn:
                    conn.execute('UPDATE users SET online_status=0 WHERE uuid=?', (player_uuid,))


def player_country(ip):
    try:
        address = ipaddress.ip_address(ip)
        if not address.is_global:
            return 'Local Network' if address.is_private else 'Unknown'
        location = guard_runtime.guard.geoip.lookup(str(address))
        return location.get('country_name') or location.get('country') or 'Unknown'
    except (ValueError, OSError):
        return 'Unknown'

def _log_to_console(msg):
    config = load_config()
    if not config.get('debug_mode', False):
        return
    try:
        with open(os.path.join(SERVER_DIR, "console.log"), "a", encoding='utf-8') as lf:
            lf.write(f"\n[System Debug] {msg}\n")
    except:
        pass

def read_properties(file_path):
    props = {}
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    if '=' in line:
                        k, v = line.split('=', 1)
                        props[k] = v
                        
    # Apply overrides
    override_path = os.path.join(SERVER_DIR, "properties_override.json")
    if os.path.exists(override_path):
        try:
            with open(override_path, "r") as f:
                overrides = json.load(f)
            props.update(overrides)
        except:
            pass
            
    _log_to_console("Server properties read from file.")
    return props

def write_properties(file_path, new_props):
    lines = []
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    
    out_lines = []
    written_keys = set()
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith('#') and '=' in stripped:
            k, v = stripped.split('=', 1)
            if k in new_props:
                out_lines.append(f"{k}={new_props[k]}\n")
                written_keys.add(k)
            else:
                out_lines.append(line)
        else:
            out_lines.append(line)
            
    for k, v in new_props.items():
        if k not in written_keys:
            out_lines.append(f"{k}={v}\n")
            
    with open(file_path, "w", encoding="utf-8") as f:
        f.writelines(out_lines)
        
    _log_to_console("Server properties written to file.")

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
    # Serialize the snapshot and history writes with a concurrent player purge.
    conn.execute("BEGIN IMMEDIATE")
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
        return java_path(mc_version)
    except (ValueError, TypeError):
        return "/usr/bin/java"

def get_configured_java_path(config):
    """Honor a configured Java binary, otherwise select one for the MC version."""
    configured_path = str(config.get('java_path') or '').strip()
    if configured_path:
        return configured_path
    return get_java_version_path(config.get('mc_version', ''))

def resolve_forge_version(mc_version):
    """Resolve an MC version to Forge's recommended build, or latest as fallback."""
    return release_service.forge_version(mc_version)

def _server_relative_path(path):
    """Return a safe path under SERVER_DIR for persisted launch metadata."""
    absolute_path = os.path.abspath(path)
    server_root = os.path.abspath(SERVER_DIR)
    if os.path.commonpath([absolute_path, server_root]) != server_root:
        raise RuntimeError("Launch target must be inside the Minecraft server directory.")
    return os.path.relpath(absolute_path, server_root)

def discover_forge_launch_target(config):
    """Prefer modern Forge's unix_args file, with legacy jar fallbacks."""
    configured_type = config.get('launch_type')
    configured_target = config.get('launch_target')
    if configured_type in ('forge_args', 'jar') and configured_target:
        configured_path = os.path.join(SERVER_DIR, configured_target)
        if os.path.isfile(configured_path):
            return configured_type, _server_relative_path(configured_path)

    full_version = str(config.get('forge_full_version') or '').strip()
    if full_version:
        expected_args = os.path.join(
            SERVER_DIR, 'libraries', 'net', 'minecraftforge', 'forge',
            full_version, 'unix_args.txt'
        )
        if os.path.isfile(expected_args):
            return 'forge_args', _server_relative_path(expected_args)

    forge_library_root = os.path.join(
        SERVER_DIR, 'libraries', 'net', 'minecraftforge', 'forge'
    )
    discovered_args = []
    if os.path.isdir(forge_library_root):
        for root, _, files in os.walk(forge_library_root):
            if 'unix_args.txt' in files:
                discovered_args.append(os.path.join(root, 'unix_args.txt'))
    if discovered_args:
        discovered_args.sort(key=os.path.getmtime, reverse=True)
        return 'forge_args', _server_relative_path(discovered_args[0])

    legacy_jars = []
    for filename in os.listdir(SERVER_DIR):
        lower_name = filename.lower()
        if (lower_name.startswith('forge-') and lower_name.endswith('.jar')
                and 'installer' not in lower_name):
            legacy_jars.append(filename)
    if legacy_jars:
        legacy_jars.sort(key=lambda name: ('universal' in name.lower(), name))
        return 'jar', legacy_jars[0]

    server_jar = os.path.join(SERVER_DIR, 'server.jar')
    if os.path.isfile(server_jar):
        return 'jar', 'server.jar'

    raise FileNotFoundError(
        "Forge installation is incomplete: no unix_args.txt or runnable Forge jar was found."
    )

def build_server_command(config, ram_gb=None):
    """Build an engine-aware command for setup boot and normal lifecycle actions."""
    memory_gb = ram_gb if ram_gb is not None else config.get('ram_gb', 2)
    java_bin = get_configured_java_path(config)
    memory_args = [f"-Xmx{memory_gb}G", f"-Xms{memory_gb}G"]

    if config.get('server_type') == 'Forge':
        launch_type, launch_target = discover_forge_launch_target(config)
        if launch_type == 'forge_args':
            return [java_bin, *memory_args, f"@{launch_target}", 'nogui']
        return [java_bin, *memory_args, '-jar', launch_target, 'nogui']

    server_jar = os.path.join(SERVER_DIR, 'server.jar')
    if not os.path.isfile(server_jar):
        raise FileNotFoundError("server.jar is missing from the server directory.")
    return [java_bin, *memory_args, '-jar', server_jar, 'nogui']

def start_minecraft_server(automatic=False):
    global MINECRAFT_PROCESS, MINECRAFT_LOG_THREAD
    config = load_config()

    configured_pid = config.get('server_pid')
    process_is_live = MINECRAFT_PROCESS and MINECRAFT_PROCESS.poll() is None
    configured_pid_is_live = configured_pid and psutil.pid_exists(configured_pid)
    if process_is_live or configured_pid_is_live:
        print("[WATCHDOG] ERROR: Cannot start. Server is already running.", flush=True)
        return False

    if MINECRAFT_LOG_THREAD and MINECRAFT_LOG_THREAD.is_alive():
        app.logger.warning('Cannot start Minecraft while its previous log reader is finishing.')
        return False

    # Clear stale state left behind by a process that exited between status polls.
    config['is_running'] = False
    config['server_pid'] = None
    config['target_state'] = 'Offline'
    save_config(config)

    version = config.get('mc_version')
    ram_gb = config.get('ram_gb', 2)

    if not version:
        print("[WATCHDOG] ERROR: Cannot start. No Minecraft version found in config.", flush=True)
        return False

    # 1. Bypass the EULA Crash
    eula_path = os.path.join(SERVER_DIR, "eula.txt")
    with open(eula_path, "w") as f:
        f.write("eula=true\n")

    try:
        # 2. Build the engine-specific Java execution command.
        cmd = build_server_command(config, ram_gb)
        # All manual/automatic starts cross the same effective-properties gate.
        # It also considers queued overrides before any file is applied.
        cmd[1:1] = guard_runtime.prepare_start()
        guard_active = guard_runtime.enabled()

        # 3. Launch in the background and pipe output to a log file
        log_path = os.path.join(SERVER_DIR, "console.log")
        history_path = os.path.join(DATA_DIR, "logs", "full_console_history.log")
        
        # Append existing console log to full history before clearing
        if os.path.exists(log_path):
            with open(log_path, "r", encoding='utf-8', errors='replace') as src, open(history_path, "a", encoding='utf-8') as dst:
                shutil.copyfileobj(src, dst, length=128 * 1024)
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
        startup_log_status.reset()
            
        log_file = open(log_path, "a", encoding='utf-8')
        
        # Apply properties overrides
        override_path = os.path.join(SERVER_DIR, "properties_override.json")
        if os.path.exists(override_path):
            try:
                with open(override_path, "r") as f:
                    overrides = json.load(f)
                write_properties(os.path.join(SERVER_DIR, "server.properties"), overrides)
                os.remove(override_path)
            except Exception as e:
                print(f"[WATCHDOG] Failed to apply property overrides: {e}")
                
        # Also, the user requested: "During all server start ups read the propterties file"
        # Reading it here will trigger the log message
        _ = read_properties(os.path.join(SERVER_DIR, "server.properties"))

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
        server_automation.track(process, automatic=automatic)

        # Catch invalid launch targets and immediate JVM failures before claiming success.
        time.sleep(0.75)
        if process.poll() is not None:
            startup_output = redact_auth_commands(process.stdout.read()) if process.stdout else ''
            if startup_output:
                log_file.write(startup_output)
                log_file.flush()
            log_file.close()
            raise RuntimeError(
                f"Minecraft exited during startup with code {process.returncode}. "
                f"{startup_output[-500:].strip()}"
            )
        
        def stream_logger(proc, log_f):
            global MINECRAFT_PROCESS
            pending_uuids = {} # username -> uuid map
            
            for line in proc.stdout:
                line = redact_auth_commands(line)
                log_f.write(line)
                log_f.flush()
                
                # Active Log Parsing for Security Monitor
                try:
                    # Guard uses session-bound auth/join outcomes, never console text.
                    if guard_active:
                        continue
                    # 1. Capture UUID mappings
                    uuid_match = re.search(r'UUID of player ([a-zA-Z0-9_]+) is ([a-zA-Z0-9\-]+)', line)
                    if uuid_match:
                        pending_uuids[uuid_match.group(1)] = uuid_match.group(2)
                        
                    # 2. Capture Logins with IPs
                    login_match = re.search(r'([a-zA-Z0-9_]+)\[/(\[?[0-9a-fA-F:.]+\]?):[0-9]+\] logged in', line)
                    if login_match:
                        username = login_match.group(1)
                        ip = str(ipaddress.ip_address(login_match.group(2).strip('[]')))
                        player_uuid = pending_uuids.get(username)
                        # Preserve an observed but ambiguous name separately, never
                        # coalesce multiple people into the shared "unknown" UUID.
                        if not player_uuid:
                            player_uuid = 'unverified:' + username.casefold()
                        record_player_join(player_uuid, username, ip, player_country(ip))
                        
                    # 3. Capture Disconnects
                    disconnect_match = re.search(r'([a-zA-Z0-9_]+) lost connection:', line)
                    if disconnect_match:
                        username = disconnect_match.group(1)
                        with PLAYER_STATE_LOCK, closing(sqlite3.connect(PLAYER_DB_FILE)) as conn:
                            with conn:
                                conn.execute("UPDATE users SET online_status = 0 WHERE username = ?", (username,))
                        
                except Exception as e:
                    print(f"Log parsing error: {e}", flush=True)

            # EOF alone does not prove Java has exited. Never clear the live PID
            # or let recovery start another server while the old process remains.
            proc.wait()
            log_f.close()
            # On crash/stop, mark everyone offline
            try:
                conn = sqlite3.connect(PLAYER_DB_FILE)
                conn.execute("UPDATE users SET online_status = 0")
                conn.commit()
                conn.close()
            except: pass

            mark_minecraft_offline(expected_pid=proc.pid)
            if MINECRAFT_PROCESS is proc:
                MINECRAFT_PROCESS = None
            
        # 4. Save the Process ID (PID) so the Stop button knows what to kill later
        config['server_pid'] = process.pid
        config['is_running'] = True
        config['target_state'] = 'Online'
        save_config(config)

        # Start log consumption only after state is persisted so a fast process exit
        # cannot race with this function and leave a stale online flag behind.
        MINECRAFT_LOG_THREAD = threading.Thread(target=stream_logger, args=(process, log_file), daemon=True)
        MINECRAFT_LOG_THREAD.start()
        
        print(f"[WATCHDOG] SERVER LAUNCH: '{version}' started successfully on PID {process.pid}.", flush=True)
        return True

    except Exception as e:
        config['is_running'] = False
        config['server_pid'] = None
        config['target_state'] = 'Offline'
        save_config(config)
        app.logger.error('Minecraft startup refused: %s', redact_auth_commands(str(e)))
        return False

def stop_minecraft_server():
    global MINECRAFT_PROCESS
    # Cancel a pending crash restart even if Minecraft has already exited.
    server_automation.stop()
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
            # Keep its PID until exit is confirmed by the reader/status monitor.
            
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
            
            # Keep the PID until the terminated process has actually exited.
            
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
def wait_for_minecraft_shutdown(process, reader):
    """Confirm process exit and drain its logs before replacing files or restarting."""
    global MINECRAFT_PROCESS, MINECRAFT_LOG_THREAD
    try:
        if process is not None:
            process.wait(timeout=30)
            if process.poll() is None:
                raise RuntimeError('Minecraft is still stopping. Wait until it is offline and retry.')
        pid = load_config().get('server_pid')
        if pid and (process is None or pid != process.pid) and psutil.pid_exists(pid):
            psutil.Process(pid).wait(timeout=30)
    except (subprocess.TimeoutExpired, psutil.TimeoutExpired) as error:
        raise RuntimeError('Minecraft is still stopping. No files were replaced and no new server was started. Wait and retry.') from error
    except psutil.NoSuchProcess:
        pass
    if reader is not None:
        reader.join(timeout=10)
        if reader.is_alive():
            raise RuntimeError('Minecraft log activity is still finishing. Wait and retry.')
    if MINECRAFT_PROCESS is process:
        MINECRAFT_PROCESS = None
    if MINECRAFT_LOG_THREAD is reader:
        MINECRAFT_LOG_THREAD = None
    config = load_config()
    config.update(is_running=False, server_pid=None, target_state='Offline')
    save_config(config)



def checked_backup_path(server_name, filename):
    for value in (server_name, filename):
        if not isinstance(value, str) or not value or value in ('.', '..') or any(c in value for c in '/\\:'):
            abort(400, description='Invalid backup path.')
    root = os.path.realpath(os.path.join(DATA_DIR, 'backups'))
    path = os.path.realpath(os.path.join(root, server_name, filename))
    if os.path.commonpath([root, path]) != root or not filename.lower().endswith('.zip'):
        abort(400, description='Invalid backup path.')
    return path

def validate_server_zip(path):
    """Validate before deleting any current server files."""
    try:
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                name = member.filename.replace('\\', '/')
                if name.startswith('/') or ':' in name or '..' in name.split('/') or (member.external_attr >> 16) & 0o170000 == 0o120000:
                    abort(400, description='Unsafe path in server ZIP.')
            if archive.testzip():
                abort(400, description='The ZIP contains a damaged file.')
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError):
        abort(400, description='Upload a valid, unencrypted server ZIP.')

# Wire it to a Flask Route
@app.route('/stop', methods=['POST'])
@exclusive_server_files
def stop_server():
    stop_minecraft_server()
    return redirect(url_for('overview'))

@app.route('/api/backup/download/<server_name>/<filename>')
def api_download_backup(server_name, filename):
    safe_name = os.path.basename(server_name)
    safe_file = os.path.basename(filename)
    path = checked_backup_path(server_name, filename)
    if os.path.exists(path):
        return send_file(path, as_attachment=True, download_name=safe_file)
    return "Not Found", 404

@app.route('/api/backup/delete', methods=['POST'])
def api_delete_backup():
    payload = request.get_json() or {}
    server_name = payload.get('server_name')
    filename = payload.get('filename')
    
    if server_name and filename:
        safe_name = os.path.basename(server_name)
        safe_file = os.path.basename(filename)
        path = checked_backup_path(server_name, filename)
        if os.path.exists(path):
            os.remove(path)
            return jsonify({"status": "success"})
    return jsonify({"error": "Failed to delete"}), 400

@app.route('/api/backup/restore', methods=['POST'])
@exclusive_server_files
def api_restore_backup():
    import zipfile
    import time
    
    payload = request.get_json() or {}
    server_name = payload.get('server_name')
    filename = payload.get('filename')
    
    if not server_name or not filename:
        return jsonify({"error": "Missing params"}), 400
        
    safe_name = os.path.basename(server_name)
    safe_file = os.path.basename(filename)
    path = checked_backup_path(server_name, filename)
    
    if not os.path.exists(path):
        return jsonify({"error": "File not found"}), 404
    validate_server_zip(path)
        
    global MINECRAFT_PROCESS
    config = load_config()
    if ((MINECRAFT_PROCESS and MINECRAFT_PROCESS.poll() is None) or
            (config.get('server_pid') and psutil.pid_exists(config['server_pid'])) or
            (MINECRAFT_LOG_THREAD and MINECRAFT_LOG_THREAD.is_alive())):
        return jsonify(error='Stop Minecraft and wait until shutdown finishes before restoring a backup.'), 409
        
    server_automation.stop()
    # Wipe current server directory
    addon_updates.reset()
    clear_player_data()
    for f in os.listdir(SERVER_DIR):
        f_path = os.path.join(SERVER_DIR, f)
        try:
            if os.path.isdir(f_path): shutil.rmtree(f_path)
            else: os.unlink(f_path)
        except: pass
        
    with zipfile.ZipFile(path, 'r') as zip_ref:
        zip_ref.extractall(SERVER_DIR)
        
    # Extract player_history.json if present
    imported_history = os.path.join(SERVER_DIR, "player_history.json")
    if os.path.exists(imported_history):
        history_file = os.path.join(DATA_DIR, "player_history.json")
        shutil.move(imported_history, history_file)
        
    imported_config = os.path.join(SERVER_DIR, "panel_config.json")
    if os.path.exists(imported_config):
        try:
            with open(imported_config, 'r') as f:
                b_conf = json.load(f)

            curr_conf = load_config()
            for key in [
                'server_name', 'mc_version', 'server_type', 'ram_gb', 'java_path',
                'forge_version', 'forge_full_version', 'launch_type', 'launch_target'
            ]:
                if key in b_conf:
                    curr_conf[key] = b_conf[key]

            save_config(curr_conf)
        except Exception as e:
            pass
        finally:
            try: os.remove(imported_config)
            except: pass

    # A restored world must be explicitly opted back into public map hosting.
    restored_config = load_config()
    restored_config['bluemap_enabled'] = False
    restored_config['auto_start'] = False
    restored_config['auto_restart'] = False
    save_config(restored_config)
    actor = security.store.reset(keep_actor=g.panel_user)
    guard_runtime.reset()
    security.store.sign_in(session, actor)
    return jsonify({"status": "restored"})

@app.route('/api/backups/list')
def api_list_backups():
    backups_dir = os.path.join(DATA_DIR, "backups")
    res = {}
    if os.path.exists(backups_dir):
        for srv in os.listdir(backups_dir):
            srv_path = os.path.join(backups_dir, srv)
            if os.path.isdir(srv_path):
                files = []
                for f in os.listdir(srv_path):
                    if f.endswith(".zip"):
                        # Parse date from name: server_name_YYYYMMDDHHMMSS_manual.zip
                        stat = os.stat(os.path.join(srv_path, f))
                        date_str = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                        
                        btype = "manual"
                        if "_auto.zip" in f: btype = "auto"
                        
                        files.append({
                            "filename": f,
                            "date": date_str,
                            "type": btype,
                            "timestamp": stat.st_mtime,
                            "size": stat.st_size
                        })
                files.sort(key=lambda x: x['timestamp'], reverse=True)
                if files:
                    res[srv] = files
    return jsonify({"backups": res})

@app.route('/api/backup/rename', methods=['POST'])
def rename_backup():
    if not session.get('logged_in'): return jsonify({"status": "error", "message": "Unauthorized"}), 403
    
    server_name = request.form.get('server_name')
    old_name = request.form.get('old_name')
    new_name = request.form.get('new_name')
    
    if not server_name or not old_name or not new_name:
        return jsonify({"status": "error", "message": "Missing parameters"})
        
    if not new_name.endswith(".zip"):
        new_name += ".zip"
        
    if "_auto.zip" in old_name:
        return jsonify({"status": "error", "message": "Cannot rename automatic backups"})
        
    if "/" in new_name or "\\" in new_name or ".." in new_name:
        return jsonify({"status": "error", "message": "Invalid backup name"})
        
    backups_dir = os.path.join(DATA_DIR, "backups")
    srv_path = os.path.join(backups_dir, server_name)
    old_path = checked_backup_path(server_name, old_name)
    new_path = checked_backup_path(server_name, new_name)
    
    if not os.path.exists(old_path):
        return jsonify({"status": "error", "message": "Backup not found"})
        
    if os.path.exists(new_path):
        return jsonify({"status": "error", "message": "A backup with that name already exists"})
        
    try:
        os.rename(old_path, new_path)
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route('/backups')
def backups_page():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    config = load_config()
    return render_template('backups.html', config=config, has_custom=get_custom_assets_state(),
                           current_backup_key=backup_server_key(config.get('server_name', 'My Server')))

@app.route('/branding-assets/<filename>')
def serve_custom_branding(filename):
    if filename == 'server-icon.png':
        icon = server_icon_path(SERVER_DIR)
        if icon is None:
            abort(404)
        response = send_file(icon, conditional=True, max_age=0)
        response.headers['Cache-Control'] = 'no-store'
        return response
    return send_from_directory(BRANDING_DIR, filename)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        # Retrieve input safely
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        client_ip = get_client_ip()
        
        config = load_config()
        
        # Authentication Validation
        user, login_error = security.login(username, password)
        if user:
            
            # SUCCESS LOG: flush=True forces the log straight to Docker/Supervisord stdout
            print(f"[WATCHDOG] AUTH SUCCESS: User '{username}' authenticated from IP: {client_ip}", flush=True)
            
            return redirect(url_for('accounts.my_account' if user['must_change_password'] else 'overview'))
            
        else:
            # FAILURE LOG: Crucial for brute-force watchdog tracking
            print(f"[WATCHDOG] AUTH FAILURE: Invalid credentials for '{username}' from IP: {client_ip}", flush=True)
            
            # (Optional) flash('Invalid credentials', 'error')
            return render_template('login.html', error=login_error, config=config, has_custom=get_custom_assets_state()), 401
            
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

SETUP_PROGRESS = {"percent": 0, "msg": "Ready", "status": "idle"}

@app.route('/api/minecraft/versions')
def api_minecraft_versions():
    try:
        return jsonify(release_service.catalog())
    except RuntimeError as error:
        return jsonify(error=str(error)), 503

@app.route('/setup', methods=['GET', 'POST'])
def setup():
    global SETUP_PROGRESS
    config = load_config()
    max_ram = get_system_ram_gb() # Fetch the max RAM
    if request.method == 'POST':

        if SETUP_PROGRESS.get('status') == 'running':
            return jsonify(error='Server setup is already running.'), 409

        server_type = request.form.get('server_type')
        version = request.form.get('version')
        
        # Capture the RAM slider value (default to 2 if missing)
        ram_allocation = request.form.get('ram_allocation', '2')

        try:
            catalog = release_service.catalog()
            selected = next((item for item in catalog['versions'] if item['version'] == version), None)
            if not selected or server_type not in selected['engines']:
                return jsonify(error='That Minecraft release/engine combination is unavailable. Refresh the version list.'), 400
        except RuntimeError as error:
            return jsonify(error=str(error)), 503
        
        server_name = request.form.get('server_name', 'My Server')

        # Save to Config
        config['server_name'] = server_name
        config['server_type'] = server_type
        config['mc_version'] = version
        config['ram_gb'] = int(ram_allocation)
        config['admin_user'] = g.panel_user['username']
        
        save_config(config)
        
        # Write ServerEngineInfo for future imports
        engine_info_path = os.path.join(SERVER_DIR, "ServerEngineInfo")
        with open(engine_info_path, "w") as f:
            json.dump({
                "server_name": server_name,
                "mc_version": version,
                "server_type": server_type,
                "ram_gb": int(ram_allocation)
            }, f)

        # Launch background setup process
        import threading
        SETUP_PROGRESS["percent"] = 5
        SETUP_PROGRESS["msg"] = "Downloading Minecraft Server Files"
        SETUP_PROGRESS["status"] = "running"
        t = threading.Thread(target=setup_server_bg, args=(server_type, version, config))
        t.daemon = True
        t.start()
        
        return jsonify({"status": "started"})
        
    return render_template('setup.html', config=config, max_ram=max_ram, has_custom=get_custom_assets_state())

def setup_server_bg(server_type, version, config):
    global SETUP_PROGRESS
    try:

        # Resolve published builds and required Java, never hard-code loader versions or filenames.
        download = release_service.resolve_download(server_type, version)
        jar_url = download['url']
        installer_mode = server_type == 'Forge'
        config['forge_version'] = download['forge_version']
        config['forge_full_version'] = download['forge_full_version']
        config['java_path'] = download['java_path']

        # 2. File Acquisition
        target_filename = "installer.jar" if installer_mode else "server.jar"
        target_path = os.path.join(SERVER_DIR, target_filename)
        
        if jar_url:
            res = requests.get(jar_url, stream=True, headers=DOWNLOAD_HEADERS, timeout=(10, 180))
            res.raise_for_status()
            checksum = download.get('checksum')
            digest = hashlib.new(checksum[0]) if checksum else None
            with open(target_path, 'wb') as f:
                for chunk in res.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        if digest:
                            digest.update(chunk)
            if digest and digest.hexdigest() != checksum[1]:
                os.remove(target_path)
                raise RuntimeError('The server download failed its checksum verification. Retry installation.')
            if not zipfile.is_zipfile(target_path):
                raise RuntimeError(f"Downloaded {target_filename} is not a valid Java archive.")

        # 3. Forge Installation Subprocess
        if installer_mode and server_type == "Forge":
            SETUP_PROGRESS["percent"] = 30
            SETUP_PROGRESS["msg"] = "Applying Correct Java Environment (Forge Installer)"
            java_bin = download['java_path']
            installer_result = subprocess.run(
                [java_bin, "-jar", target_filename, "--installServer"],
                cwd=SERVER_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                timeout=900
            )
            if installer_result.returncode != 0:
                installer_tail = (installer_result.stdout or '')[-1500:].strip()
                raise RuntimeError(
                    f"Forge installer exited with code {installer_result.returncode}. {installer_tail}"
                )

            launch_type, launch_target = discover_forge_launch_target(config)
            config['launch_type'] = launch_type
            config['launch_target'] = launch_target
            os.remove(target_path)
        else:
            config['forge_version'] = ""
            config['forge_full_version'] = ""
            config['launch_type'] = 'jar'
            config['launch_target'] = 'server.jar'

        SETUP_PROGRESS["percent"] = 50
        SETUP_PROGRESS["msg"] = "Applying Correct Java Environment"
        
        # 4. Finalize Configuration & First Boot
        config['java_path'] = download['java_path']
        save_config(config)

        # Refresh import/backup metadata now that Forge build and launch data are known.
        engine_info_path = os.path.join(SERVER_DIR, "ServerEngineInfo")
        with open(engine_info_path, "w") as f:
            json.dump({
                "server_name": config.get('server_name'),
                "mc_version": version,
                "server_type": server_type,
                "ram_gb": config.get('ram_gb', 2),
                "java_path": config.get('java_path'),
                "forge_version": config.get('forge_version', ''),
                "forge_full_version": config.get('forge_full_version', ''),
                "launch_type": config.get('launch_type', ''),
                "launch_target": config.get('launch_target', '')
            }, f, indent=2)
        
        SETUP_PROGRESS["percent"] = 60
        SETUP_PROGRESS["msg"] = "Building Server Files"
        
        # EULA
        with open(os.path.join(SERVER_DIR, "eula.txt"), "w") as f:
            f.write("eula=true\n")
            
        cmd = build_server_command(config, 1)
        
        process = subprocess.Popen(
            cmd, cwd=SERVER_DIR, stdin=subprocess.PIPE, stdout=subprocess.PIPE, 
            stderr=subprocess.STDOUT, universal_newlines=True
        )
        
        boot_output = []
        boot_ready = False
        for line in iter(process.stdout.readline, ''):
            boot_output.append(line)
            if "Done (" in line or "For help, type" in line:
                boot_ready = True
                break

        if not boot_ready:
            raise RuntimeError(
                f"Initial server boot exited with code {process.poll()}. "
                f"{''.join(boot_output)[-1500:].strip()}"
            )
                
        # Wait a few seconds for world save, then stop
        import time
        time.sleep(3)
        try:
            process.stdin.write("stop\n")
            process.stdin.flush()
            process.wait(timeout=30)
        except:
            process.kill()
            
        SETUP_PROGRESS["percent"] = 90
        SETUP_PROGRESS["msg"] = "Creating Initial Backup/Restore Point"
        
        # Create initial backup
        create_backup(config.get('server_name'), is_manual=True, is_initial=True)
        
        config['is_setup'] = True
        save_config(config)
        
        SETUP_PROGRESS["percent"] = 100
        SETUP_PROGRESS["msg"] = "Done"
        SETUP_PROGRESS["status"] = "done"
        
    except Exception as e:
        config['is_setup'] = False
        config['is_running'] = False
        config['server_pid'] = None
        config['target_state'] = 'Offline'
        save_config(config)
        SETUP_PROGRESS["msg"] = f"Error: {e}"
        SETUP_PROGRESS["status"] = "error"

@app.route('/api/setup/progress')
def api_setup_progress():
    # Persisted completion wins over the in-memory snapshot, including the
    # save-before-progress-update window and a panel restart after completion.
    if load_config().get('is_setup'):
        return jsonify(percent=100, msg='Done', status='done')
    return jsonify(SETUP_PROGRESS)

@app.route('/')
@app.route('/overview')
def overview():
    activity = []
    activity_message = "Player connections will appear here when they are recorded."
    if security.can('players.view'):
        try:
            with closing(sqlite3.connect(PLAYER_DB_FILE, timeout=2)) as conn:
                conn.row_factory = sqlite3.Row
                activity = [dict(row) for row in conn.execute("""
                    SELECT u.username, e.timestamp FROM login_events e
                    JOIN users u ON u.uuid = e.uuid
                    ORDER BY e.event_id DESC LIMIT 6
                """)]
        except sqlite3.Error:
            app.logger.warning("Recent player activity could not be read.")
            activity_message = "Activity history is temporarily unavailable."
    else:
        activity_message = "Player-history permission is required to view connection activity."
    return render_template('overview.html', config=load_config(), has_custom=get_custom_assets_state(),
                           recent_activity=activity, activity_message=activity_message)

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

def player_roster():
    conn = sqlite3.connect(PLAYER_DB_FILE)
    conn.row_factory = sqlite3.Row
    users = conn.execute('''
        SELECT u.uuid, u.username, u.first_seen, u.last_seen, u.online_status, u.ban_status, u.ban_type,
               (SELECT ip FROM login_events WHERE uuid = u.uuid ORDER BY timestamp DESC LIMIT 1) as last_ip,
               (SELECT country FROM login_events WHERE uuid = u.uuid ORDER BY timestamp DESC LIMIT 1) as country
        FROM users u
    ''').fetchall()
    conn.close()
    roster = [dict(row) for row in users]
    if not roster:
        return roster
    try:
        links = guard_runtime.guard.player_profile_links(roster)
    except (OSError, sqlite3.Error, ValueError):
        return roster
    for player in roster:
        if player['uuid'] in links:
            player['guard_identity'] = links[player['uuid']]
    return roster

@app.route('/api/players')
def api_players():
    if not request.args.get('skipSync'):
        sync_bans_from_server()
    return jsonify(player_roster())

@app.route('/players')
def players():
    sync_bans_from_server()
    return render_template('players.html', config=load_config(), players=player_roster(),
                           has_custom=get_custom_assets_state())

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
    if action not in ('kick', 'ban', 'unban') or ban_type not in ('username', 'ip', 'both'):
        return jsonify(error='Invalid moderation action.'), 400
    if not isinstance(player_name, str) or not re.fullmatch(r'[A-Za-z0-9_]{1,16}', player_name):
        return jsonify(error='Invalid player name.'), 400
    if not isinstance(reason, str) or any(ord(c) < 32 for c in reason) or len(reason) > 500:
        return jsonify(error='Invalid moderation reason.'), 400
    if ip:
        import ipaddress
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            return jsonify(error='Invalid player IP address.'), 400
    
    reason_str = f" {reason}" if reason else ""
    
    if not MINECRAFT_PROCESS or MINECRAFT_PROCESS.poll() is not None:
        return jsonify({"status": "error", "message": "Server offline"})
        
    admin_user = g.panel_user['username']
    web_panel_source = f"Web Panel: {admin_user}"
    now_ts = datetime.utcnow().isoformat() + "Z"
    
    conn = sqlite3.connect(PLAYER_DB_FILE)
    conn.execute("BEGIN IMMEDIATE")
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
        "server_icon": server_icon_path(SERVER_DIR) is not None,
        "favicon": os.path.exists(os.path.join(BRANDING_DIR, 'favicon.ico')),
        "bg": os.path.exists(os.path.join(BRANDING_DIR, 'bg.jpg'))
    }

def server_process_active():
    """Fail closed while a managed/unmanaged server or its old log reader is alive."""
    try:
        process = MINECRAFT_PROCESS
        if process is not None and process.poll() is None:
            return True
        if MINECRAFT_LOG_THREAD is not None and MINECRAFT_LOG_THREAD.is_alive():
            return True
        pid = load_config().get('server_pid')
        return bool(pid and psutil.pid_exists(pid))
    except (OSError, ValueError, TypeError):
        return True


@contextmanager
def server_file_mutation_guard():
    try:
        with server_backups.exclusive():
            if server_process_active():
                abort(409, description='Stop Minecraft and wait for shutdown to finish before changing server files.')
            server_automation.stop()
            yield
    except BackupBusy as error:
        abort(409, description=str(error))


@contextmanager
def addon_file_mutation_guard():
    # Preserve existing online plugin editing, but serialize it with backups.
    # Loader mods must be changed while stopped; they have no safe hot reload.
    try:
        with server_backups.exclusive():
            if str(load_config().get('server_type', '')).lower() in ('forge', 'fabric'):
                if server_process_active():
                    abort(409, description='Stop Minecraft before changing mods.')
                server_automation.stop()
            yield
    except BackupBusy as error:
        abort(409, description=str(error))


guard_runtime = GuardRuntime(app, security, DATA_DIR, SERVER_DIR, load_config, read_properties,
                             server_file_mutation_guard, server_process_active, record_guard_outcome)

register_plugin_manager(app, SERVER_DIR, DATA_DIR, load_config, get_custom_assets_state,
                        mutation_guard=addon_file_mutation_guard, path_guard=guard_runtime.protect_path,
                        content_guard=guard_runtime.protect_content)
register_file_manager(app, SERVER_DIR, DATA_DIR, load_config, get_custom_assets_state,
                      mutation_guard=server_file_mutation_guard, path_guard=guard_runtime.protect_path,
                      content_guard=guard_runtime.protect_content)

addon_updates = AddonUpdateService(SERVER_DIR, os.path.join(DATA_DIR, 'addon_updates.json'),
                                  load_config, server_process_active, server_file_mutation_guard)


@app.route('/api/addon-updates', methods=['GET', 'PUT'])
def addon_update_settings():
    if request.method == 'GET':
        return jsonify(addon_updates.snapshot())
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(error='Expected JSON update settings.'), 400
    try:
        return jsonify(addon_updates.configure(payload.get('enabled'), payload.get('projects')))
    except AddonUpdateError as error:
        return jsonify(error=str(error)), 400
    except OSError:
        app.logger.exception('Unable to save add-on update settings')
        return jsonify(error='Unable to save update settings. Check data permissions and free space.'), 500


@app.route('/api/addon-updates/check', methods=['POST'])
def check_addon_updates():
    try:
        return jsonify(addon_updates.start('check')), 202
    except AddonUpdateBusy as error:
        return jsonify(error=str(error)), 409


@app.route('/api/addon-updates/apply', methods=['POST'])
def apply_addon_updates():
    if server_process_active():
        return jsonify(error='Stop Minecraft and wait for shutdown before applying add-on updates.'), 409
    try:
        return jsonify(addon_updates.start('apply')), 202
    except AddonUpdateBusy as error:
        return jsonify(error=str(error)), 409


def addon_update_scheduler(stop_event, interval=3600):
    """Opt-in hourly checks while stopped; imports/tests never start a worker."""
    next_check = 0
    next_cleanup = 0
    while not stop_event.is_set():
        now = time.monotonic()
        try:
            # Private rollback retention is independent of automatic installation.
            if now >= next_cleanup:
                if addon_updates.cleanup_backups() is not None:
                    next_cleanup = now + 3600
            if (now >= next_check and addon_updates.snapshot()['enabled']
                    and not server_process_active()):
                addon_updates.start('apply')
                next_check = now + interval
        except AddonUpdateBusy:
            pass
        except Exception:
            app.logger.exception('Unable to schedule add-on updates')
            next_check = now + interval
            next_cleanup = now + 3600
        stop_event.wait(60)

@app.route('/properties', methods=['GET', 'POST'])
def properties():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
        
    props_path = os.path.join(SERVER_DIR, "server.properties")
    
    if request.method == 'POST':
        old_props = read_properties(props_path)
        new_props = {}
        for key in request.form:
            new_props[key] = request.form[key]
            
        changed = {}
        for k, v in new_props.items():
            if old_props.get(k) != v:
                changed[k] = v
                
        if changed:
            override_path = os.path.join(SERVER_DIR, "properties_override.json")
            if os.path.exists(override_path):
                try:
                    with open(override_path, "r") as f:
                        pending = json.load(f)
                except:
                    pending = {}
            else:
                pending = {}
                
            pending.update(changed)
            with open(override_path, "w") as f:
                json.dump(pending, f)
                
            global MINECRAFT_PROCESS
            if MINECRAFT_PROCESS and MINECRAFT_PROCESS.poll() is None:
                for k, v in changed.items():
                    if k == 'difficulty':
                        MINECRAFT_PROCESS.stdin.write(f"difficulty {v}\n")
                    elif k == 'gamemode':
                        MINECRAFT_PROCESS.stdin.write(f"defaultgamemode {v}\n")
                    elif k == 'white-list':
                        cmd = "whitelist on" if str(v).lower() in ('true', 'yes', '1') else "whitelist off"
                        MINECRAFT_PROCESS.stdin.write(f"{cmd}\n")
                try:
                    MINECRAFT_PROCESS.stdin.flush()
                except: pass
                
            return jsonify({"status": "success", "message": "Properties updated. A server restart is required to apply all changes.", "requires_restart": True})
        return jsonify({"status": "success", "message": "No changes made."})
        
    props = read_properties(props_path)
    
    categories_def = {
        "Server Settings": ['server-port', 'server-ip', 'max-players', 'motd', 'online-mode', 'enable-rcon', 'rcon.password', 'rcon.port', 'enable-query', 'query.port', 'network-compression-threshold', 'max-tick-time', 'rate-limit', 'enable-status'],
        "World Settings": ['level-name', 'level-seed', 'level-type', 'generator-settings', 'allow-nether', 'generate-structures', 'max-world-size', 'spawn-protection', 'sync-chunk-writes'],
        "Gameplay Settings": ['gamemode', 'force-gamemode', 'difficulty', 'pvp', 'hardcore', 'enable-command-block', 'op-permission-level', 'function-permission-level', 'broadcast-console-to-ops', 'broadcast-rcon-to-ops', 'spawn-monsters', 'spawn-animals', 'spawn-npcs', 'allow-flight', 'enforce-whitelist', 'white-list', 'player-idle-timeout', 'entity-broadcast-range-percentage'],
        "Resource Settings": ['resource-pack', 'resource-pack-sha1', 'require-resource-pack', 'resource-pack-prompt']
    }
    
    categorized_props = {
        "Server Settings": {},
        "World Settings": {},
        "Gameplay Settings": {},
        "Resource Settings": {},
        "Advanced": {}
    }
    
    for k, v in props.items():
        placed = False
        for cat_name, cat_keys in categories_def.items():
            if k in cat_keys:
                categorized_props[cat_name][k] = v
                placed = True
                break
        if not placed:
            categorized_props["Advanced"][k] = v
            
    # Remove empty categories
    categorized_props = {k: v for k, v in categorized_props.items() if v}
            
    return render_template('properties.html', 
                           categorized_props=categorized_props, 
                           config=load_config(), 
                           has_custom=get_custom_assets_state())

@app.route('/settings', methods=['GET', 'POST'])
@exclusive_server_files
def settings():
    config = load_config()
    settings_message = None
    if request.method == 'POST':
        config['theme'] = request.form.get('theme', config.get('theme', 'dark'))
        config['java_path'] = request.form.get('java_path', config.get('java_path'))
        accent = request.form.get('accent', config.get('accent', 'lime'))
        server_name = request.form.get('server_name', config.get('server_name', '')).strip()
        if accent not in ('lime', 'yellow', 'blue'):
            return render_template('settings.html', config=config, has_custom=get_custom_assets_state(),
                                   settings_error='Choose a supported accent color.'), 400
        if 'server_name' in request.form and (not server_name or len(server_name) > 80 or
                                              re.search(r'[\x00-\x1f\x7f]', server_name)):
            return render_template('settings.html', config=config, has_custom=get_custom_assets_state(),
                                   settings_error='Server name must be 1–80 characters without control characters.'), 400
        # Validate map settings before saving any fields or uploaded images.
        try:
            map_port = checked_port(request.form.get('bluemap_port', config.get('bluemap_port', 8100)))
        except ValueError as error:
            return render_template('settings.html', config=config, has_custom=get_custom_assets_state(),
                                   settings_error=str(error)), 400
        config['bluemap_port'] = map_port
        if 'bluemap_settings_present' in request.form:
            config['bluemap_enabled'] = request.form.get('bluemap_enabled') == 'true'
        config['accent'] = accent
        config['server_name'] = server_name
        config['panel_name'] = 'Byte Panel'
        config['bg_blur'] = request.form.get('bg_blur', config.get('bg_blur', 25))
        config['bg_tint'] = request.form.get('bg_tint', config.get('bg_tint', 50))
        config['timezone'] = request.form.get('timezone', config.get('timezone', 'UTC'))
        config['time_format'] = request.form.get('time_format', config.get('time_format', '12'))
        
        config['debug_mode'] = request.form.get('debug_mode') == 'true'
        config['auto_backup_enabled'] = request.form.get('auto_backup_enabled') == 'true'
        config['auto_backup_freq'] = request.form.get('auto_backup_freq', config.get('auto_backup_freq', 'weekly'))
        config['auto_backup_keep'] = int(request.form.get('auto_backup_keep', config.get('auto_backup_keep', 5)))
        if 'backup_settings_present' in request.form:
            config['backup_shutdown'] = request.form.get('backup_shutdown') == 'true'
        if 'automation_settings_present' in request.form:
            config['auto_start'] = request.form.get('auto_start') == 'true'
            config['auto_restart'] = request.form.get('auto_restart') == 'true'
        # Accept the legacy field for an already-open Settings page, but use the
        # same validation and canonical server file for both upload names.
        icon_upload = request.files.get('server_icon') or request.files.get('custom_logo')
        if icon_upload and icon_upload.filename:
            try:
                save_server_icon(icon_upload.stream, SERVER_DIR)
            except (ServerIconError, BackupBusy) as error:
                return render_template('settings.html', config=config, has_custom=get_custom_assets_state(),
                                       settings_error=str(error)), 409 if isinstance(error, BackupBusy) else 400
            except OSError:
                app.logger.exception('Could not save server icon')
                return render_template('settings.html', config=config, has_custom=get_custom_assets_state(),
                                       settings_error='Could not save the server icon. Check disk space and server-folder permissions.'), 500
            settings_message = 'Settings saved. Server icon updated on the sign-in screen and in the server badge. Restart Minecraft, then refresh the multiplayer list to see it there. The Byte Panel navigation logo is unchanged.'
        if 'custom_favicon' in request.files and request.files['custom_favicon'].filename:
            request.files['custom_favicon'].save(os.path.join(BRANDING_DIR, 'favicon.ico'))
        if 'custom_bg' in request.files and request.files['custom_bg'].filename:
            request.files['custom_bg'].save(os.path.join(BRANDING_DIR, 'bg.jpg'))
            
        with CONFIG_LOCK:
            current = load_config()
            for key in ('is_running', 'server_pid', 'target_state'):
                if key in current:
                    config[key] = current[key]
            save_config(config)
        if 'automation_settings_present' in request.form and not config['auto_restart']:
            server_automation.cancel_pending_restart()
    
    return render_template('settings.html', config=config, has_custom=get_custom_assets_state(),
                           settings_message=settings_message)

@app.route('/start', methods=['POST'])
@exclusive_server_files
def start_server():
    # Only try to start if it isn't already running
    config = load_config()
    if not config.get('is_running', False):
        start_minecraft_server()
    return redirect(url_for('overview'))

# --- Process Lifecycle Infrastructure API Rules ---
@app.route('/api/server/<action>', methods=['POST'])
@exclusive_server_files
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
        process, reader = MINECRAFT_PROCESS, MINECRAFT_LOG_THREAD
        stop_minecraft_server()
        try:
            wait_for_minecraft_shutdown(process, reader)
        except (RuntimeError, OSError, psutil.Error) as error:
            return jsonify(status='error', message=str(error)), 409
        if not start_minecraft_server():
            return jsonify(status='error', message='Minecraft could not restart. Check the console.'), 409
        return jsonify({"status": "restarted"})

def create_backup(server_name, is_manual, is_initial=False, is_restore_backup=False):
    return server_backups.create(server_name, is_manual, is_initial, is_restore_backup)

def backup_scheduler():
    import time
    from datetime import datetime, timedelta
    
    while True:
        try:
            config = load_config()
            if config.get('is_setup') and config.get('auto_backup_enabled') and config.get('server_name'):
                freq = config.get('auto_backup_freq', 'weekly')
                last_run = config.get('last_auto_backup', 0)
                now = time.time()
                
                should_run = False
                if freq == 'hourly' and (now - last_run) >= 3600:
                    should_run = True
                elif freq == '6_hours' and (now - last_run) >= 21600:
                    should_run = True
                elif freq in ['12_hours', 'twice_daily'] and (now - last_run) >= 43200:
                    should_run = True
                elif freq == 'daily' and (now - last_run) >= 86400:
                    should_run = True
                elif freq == 'weekly' and (now - last_run) >= 604800:
                    should_run = True
                elif freq == 'monthly' and (now - last_run) >= 2592000:
                    should_run = True
                    
                # A persistent failure must not bounce an online server every minute.
                cooldown = (config.get('backup_shutdown') is True and
                            now - config.get('last_auto_backup_attempt', 0) < 3600)
                if should_run and not cooldown:
                    try:
                        create_backup(config.get('server_name'), is_manual=False)
                    except BackupBusy:
                        raise
                    except BackupCompletedError:
                        # The archive is valid even if the server did not restart.
                        config = load_config()
                        config['last_auto_backup'] = now
                        config['last_auto_backup_attempt'] = now
                        save_config(config)
                        raise
                    except Exception:
                        config = load_config()
                        config['last_auto_backup_attempt'] = now
                        save_config(config)
                        raise
                    config = load_config()
                    config['last_auto_backup'] = now
                    config['last_auto_backup_attempt'] = now
                    save_config(config)
        except BackupBusy:
            pass  # Retry next minute; do not change the last successful backup time.
        except Exception as e:
            app.logger.exception('Automatic server backup failed: %s', backup_error(e))
            
        time.sleep(60)

@app.route('/api/backup/create', methods=['POST'])
def api_create_backup():
    config = load_config()
    server_name = config.get('server_name', 'UnknownServer')
    
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify(status='error', message='Invalid backup request.'), 400
    is_restore = payload.get('is_restore', False)

    try:
        if payload.get('background') is True:
            return jsonify(server_backups.start(security.store.scope(), is_restore)), 202
        zip_path = create_backup(server_name, is_manual=True, is_restore_backup=is_restore)
        return jsonify({"status": "success", "file": os.path.basename(zip_path),
                        "server_key": os.path.basename(os.path.dirname(zip_path))})
    except BackupBusy as e:
        return jsonify(status='error', message=str(e)), 409
    except BackupCompletedError as e:
        app.logger.exception('Backup created, but server recovery failed')
        return jsonify(status='error', message=str(e), backup_created=True,
                       file=os.path.basename(e.path), server_key=os.path.basename(os.path.dirname(e.path))), 500
    except Exception as e:
        app.logger.exception('Manual server backup failed')
        return jsonify(status='error', message=backup_error(e)), 500


@app.route('/api/backup/status/<job_id>')
def api_backup_status(job_id):
    job = server_backups.status(job_id, security.store.scope())
    if job is None:
        return jsonify(status='error', message='Backup progress is no longer available. The panel may have restarted. Check the backup list before retrying.'), 404
    return jsonify(job)

@app.route('/api/purge', methods=['POST'])
@exclusive_server_files
def purge_server():
    global MINECRAFT_PROCESS, MINECRAFT_LOG_THREAD
    server_automation.stop()
    # stop_minecraft_server sends a command; it does not wait for process exit.
    # Drain the old log reader before clearing records, or buffered logins can
    # recreate players after the purge has reported success.
    process = MINECRAFT_PROCESS
    pid = load_config().get('server_pid')
    try:
        if process and process.poll() is None:
            if not stop_minecraft_server():
                return jsonify(error='Unable to stop the server. Stop it before purging.'), 409
            process.wait(timeout=30)
        elif pid and psutil.pid_exists(pid):
            detached = psutil.Process(pid)
            if not stop_minecraft_server():
                return jsonify(error='Unable to stop the server. Stop it before purging.'), 409
            detached.wait(timeout=30)
    except (subprocess.TimeoutExpired, psutil.TimeoutExpired):
        return jsonify(error='The server is still stopping. Wait until it is offline, then purge again.'), 409
    except psutil.NoSuchProcess:
        pass
    except (OSError, psutil.Error):
        app.logger.exception('Unable to stop server before purge')
        return jsonify(error='Unable to verify server shutdown. Stop it before purging.'), 409
    if MINECRAFT_LOG_THREAD:
        MINECRAFT_LOG_THREAD.join(timeout=10)
        if MINECRAFT_LOG_THREAD.is_alive():
            return jsonify(error='Player tracking is still finishing. Wait a moment, then purge again.'), 409
    MINECRAFT_PROCESS = None
    MINECRAFT_LOG_THREAD = None

    try:
        clear_player_data()
    except (sqlite3.Error, OSError):
        app.logger.exception('Unable to clear player data during purge')
        return jsonify(error='Unable to clear player history. Check data permissions and disk space, then retry purge.'), 500
            
    # Wipe core server files
    addon_updates.reset()
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
    config['bluemap_enabled'] = False
    config['auto_start'] = False
    config['auto_restart'] = False
    config['log_session_counter'] = 0
    config['mc_version'] = None
    config['server_type'] = None
    config['forge_version'] = ''
    config['forge_full_version'] = ''
    config['launch_type'] = ''
    config['launch_target'] = ''
    config['target_state'] = 'Offline'
    config['is_running'] = False
    config['server_pid'] = None
    config['log_session_counter'] = 0
    config.pop('admin_pass', None)
    config['admin_user'] = 'admin'
    config['welcome_seen'] = False
    save_config(config)
    security.store.reset()
    guard_runtime.reset()
    session.clear()
    
    return jsonify({"status": "purged"})

@app.route('/api/import', methods=['POST'])
@exclusive_server_files
def import_server():
    global MINECRAFT_PROCESS
    temp_zip = os.path.join(DATA_DIR, "temp_import.zip")
    
    if 'import_backup_path' in request.form:
        backup_path = request.form['import_backup_path']
        # backup_path is passed as 'serverName/filename.zip'
        backup_root = os.path.realpath(os.path.join(DATA_DIR, 'backups'))
        full_backup_path = os.path.realpath(os.path.join(backup_root, backup_path))
        if os.path.commonpath([backup_root, full_backup_path]) != backup_root or not full_backup_path.lower().endswith('.zip'):
            return jsonify(error='Invalid backup path.'), 400
        if not os.path.exists(full_backup_path):
            return jsonify({"error": "Backup file not found"}), 404
        shutil.copy2(full_backup_path, temp_zip)
    else:
        if 'import_zip' not in request.files:
            return jsonify({"error": "No file uploaded"}), 400
            
        file = request.files['import_zip']
        if file.filename == '':
            return jsonify({"error": "No file selected"}), 400
            
        file.save(temp_zip)
    
    validate_server_zip(temp_zip)
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
        
    server_automation.stop()
    process, reader = MINECRAFT_PROCESS, MINECRAFT_LOG_THREAD
    pid = load_config().get('server_pid')
    try:
        if ((process and process.poll() is None) or (pid and psutil.pid_exists(pid))):
            if not stop_minecraft_server():
                return jsonify(error='Unable to stop Minecraft. No server files were replaced.'), 409
        wait_for_minecraft_shutdown(process, reader)
    except (RuntimeError, OSError, psutil.Error) as error:
        return jsonify(error=str(error)), 409

    # Wipe current server files only after shutdown and log activity are complete.
    addon_updates.reset()
    clear_player_data()
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
                for key in [
                    'server_type', 'ram_gb', 'server_name', 'java_path',
                    'forge_version', 'forge_full_version', 'launch_type', 'launch_target'
                ]:
                    if key in engine_data:
                        config[key] = engine_data[key]
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
        
    # Extract player_history.json if present
    imported_history = os.path.join(SERVER_DIR, "player_history.json")
    if os.path.exists(imported_history):
        history_file = os.path.join(DATA_DIR, "player_history.json")
        shutil.move(imported_history, history_file)
    else:
        # If no history was imported, make sure we wipe the current history!
        history_file = os.path.join(DATA_DIR, "player_history.json")
        if os.path.exists(history_file):
            os.remove(history_file)
        
    config['is_setup'] = True
    config['target_state'] = 'Offline'
    config['is_running'] = False
    config['server_pid'] = None
    
    config['bluemap_enabled'] = False
    # A replacement is a new server scope, never an import of old accounts.
    config['auto_start'] = False
    config['auto_restart'] = False
    actor = security.store.reset(keep_actor=g.panel_user)
    security.store.sign_in(session, actor)
        
    save_config(config)
    guard_runtime.reset()
    
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
        is_done = startup_log_status.ready(log_path, MINECRAFT_PROCESS.pid)
        
        if target_state == "Shutting Down":
            status = "Shutting Down"
        elif not is_done:
            status = "Starting"
            
        try:
            p = psutil.Process(MINECRAFT_PROCESS.pid)
            cpu = p.cpu_percent(interval=0.05)
            ram = p.memory_info().rss / (1024 * 1024)
        except psutil.NoSuchProcess: pass
    elif config.get('server_pid') and psutil.pid_exists(config['server_pid']):
        # Never erase a live unmanaged PID: offline backups must fail safely for it.
        status = 'Online'
    elif not server_backups.lock.locked():
        # Recheck while holding the operation lock: a start/stop or Settings save
        # may have begun since the status snapshot above.
        try:
            with server_backups.exclusive():
                current = load_config()
                live = MINECRAFT_PROCESS and MINECRAFT_PROCESS.poll() is None
                saved_live = current.get('server_pid') and psutil.pid_exists(current['server_pid'])
                if not live and not saved_live and (current.get('target_state') != 'Offline'
                                                    or current.get('is_running') or current.get('server_pid')):
                    mark_minecraft_offline()
        except BackupBusy:
            pass
            
    return jsonify({"status": status, "cpu": f"{cpu:.1f}%", "ram": f"{ram:.1f} MB", "tps": "20.0" if status == "Online" else "0.0"})

@app.route('/api/console')
def get_console():
    log_path = os.path.join(SERVER_DIR, "console.log")
    try:
        if os.path.exists(log_path):
            return read_tail(log_path)
        else:
            return "Server is offline or initializing. Waiting for log output..."
    except Exception as e:
        return f"Error reading console: {str(e)}"


@socketio.on('connect')
def socket_connect(auth=None):
    user = security.current_user()
    if 'console.view' not in session_grants(user):
        return False
    with security.lock:
        security.socket_clients[request.sid] = dict(session)

@socketio.on('disconnect')
def socket_disconnect(reason=None):
    with security.lock:
        security.socket_clients.pop(request.sid, None)

def normalize_console_command(payload):
    """Accept an optional leading slash without rewriting plugin prefixes or arguments."""
    if not isinstance(payload, dict) or not isinstance(payload.get('command'), str):
        raise ValueError('Enter a server command.')
    command = payload['command']
    if any(character in command for character in ('\r', '\n', '\x00')):
        raise ValueError('Enter one command at a time, without line breaks or null characters.')
    command = command.strip()
    if command in ('', '/'):
        raise ValueError('Enter a server command.')
    # Preserve explicit prefixes (including // plugin commands) exactly as entered.
    if command.startswith('/'):
        return command
    config = load_config()
    version = re.fullmatch(r'(\d+)\.(\d+)(?:\.(\d+))?', str(config.get('mc_version', '')))
    # Modern Paper/Purpur use performPrefixedCommand. Older releases dispatch
    # raw command names and can reject /help; keep their native console format.
    if config.get('server_type') in ('Paper', 'Purpur') and version:
        parts = tuple(int(part or 0) for part in version.groups())
        if parts >= (1, 21, 8):
            return '/' + command
    return command


def mark_intentional_console_shutdown(command):
    name = command[1:] if command.startswith('/') else command
    name = name.split(None, 1)[0].lower()
    if name in ('stop', 'minecraft:stop', 'bukkit:stop', 'restart', 'bukkit:restart', 'spigot:restart'):
        server_automation.stop()
        if MINECRAFT_PROCESS and MINECRAFT_PROCESS.poll() is None:
            # A shutdown backup must not mistake this intentional console stop
            # for a server it should stop and then bring back online.
            with CONFIG_LOCK:
                config = load_config()
                config['target_state'] = 'Shutting Down'
                save_config(config)


@socketio.on('send_command')
def handle_command(payload=None):
    global MINECRAFT_PROCESS
    if 'console.command' not in session_grants(security.current_user()):
        return {'error': 'You do not have permission to run console commands.'}
    try:
        command = normalize_console_command(payload)
    except ValueError as error:
        return {'error': str(error)}
    try:
        with server_backups.exclusive():
            mark_intentional_console_shutdown(command)
            if MINECRAFT_PROCESS and MINECRAFT_PROCESS.poll() is None:
                MINECRAFT_PROCESS.stdin.write(command + "\n")
                MINECRAFT_PROCESS.stdin.flush()
                return {'status': 'success'}
            return {'error': 'Server offline'}
    except BackupBusy as error:
        return {'error': str(error)}
    except (OSError, ValueError):
        return {'error': 'Could not send the command. The server connection is closed.'}

@app.route('/api/command', methods=['POST'])
@exclusive_server_files
def handle_api_command():
    global MINECRAFT_PROCESS
    try:
        command = normalize_console_command(request.get_json(silent=True))
    except ValueError as error:
        return jsonify({'status': 'error', 'message': str(error)}), 400
    mark_intentional_console_shutdown(command)
    if MINECRAFT_PROCESS and MINECRAFT_PROCESS.poll() is None:
        try:
            MINECRAFT_PROCESS.stdin.write(command + "\n")
            MINECRAFT_PROCESS.stdin.flush()
        except (OSError, ValueError):
            return jsonify({'status': 'error', 'message': 'Could not send the command. The server connection is closed.'}), 409
        return jsonify({"status": "success"})
    return jsonify({"status": "error", "message": "Server offline"})

if __name__ == '__main__':
    # Only the actual panel process owns the automation thread; imports/tests
    # must never start Minecraft or a duplicate monitor.
    guard_runtime.start_bridge()
    threading.Thread(target=guard_runtime.maintenance, name='connection-guard-maintenance', daemon=True).start()
    automation_stop = threading.Event()
    automation_thread = threading.Thread(target=server_automation.run, args=(automation_stop,),
                                         name='minecraft-automation', daemon=True)
    automation_thread.start()
    updates_thread = threading.Thread(target=addon_update_scheduler, args=(automation_stop,),
                                      name='addon-update-scheduler', daemon=True)
    updates_thread.start()
    # Start backup scheduler thread
    import threading
    scheduler_thread = threading.Thread(target=backup_scheduler, daemon=True)
    scheduler_thread.start()
    
    socketio.run(app, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)
