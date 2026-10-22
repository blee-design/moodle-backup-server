#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                         MOODLE NINJA BACKUP SYSTEM v2.0                      ║
║                     [ primary + exam server ]                               ║
║                  "We don't back up – we extract digital souls"              ║
╚══════════════════════════════════════════════════════════════════════════════╝

Moodle Backup and Restore System for primary and exam servers.
Credentials are never passed on the command line – the script tries
to connect without a password first and prompts interactively only if needed.
Default backup location is the home directory of the user who invoked sudo.

During backup, the exam server's web root is omitted (to save space) because
it is a clone of the primary server's web root. During restore, the exam server's
web root is cloned from the restored primary web root.

Configuration files:
  - For each server, the backup contains config-<server>.php (e.g., config-exam.php).
  - During restore, that file is copied to its original location.
  - It is then also copied to config.php in the same directory, so that the live
    configuration file is a copy of the backed‑up config. The original config-*.php
    remains as a manual backup.

If no restore location is provided, the script automatically finds the most recent
backup in the default backup directory (the invoking user's home) and asks for
confirmation before proceeding.

During restore, if any destination file or directory already exists, you are asked
ONCE how to handle conflicts:
    [A]sk each time, [R]emove all, [B]ackup all, [S]kip all

Symlinks are created pointing to the 'public' subdirectory inside each server's
web root (e.g., psc -> psc-Server/public).

Database commands: The script first attempts to use 'mariadb' and 'mariadb-dump'.
If those are not found, it falls back to 'mysql' and 'mysqldump'.

NEW in this version:
  - The script automatically extracts the database name, user, and password
    from the restored Moodle config and grants all privileges on the restored
    database to that user (on localhost).
  - All databases are created with utf8mb4 character set and utf8mb4_unicode_ci
    collation (Moodle's recommended settings).
  - The --default-character-set=utf8mb4 flag is used for both dump and restore.
  - After restoring files, the script fixes ownership and permissions only when
    they are not already correct:
      * Web root: owned by root:root, directories 755, files 644.
      * Writable subdirectories (temp, cache, sessions, etc.) inside web root: owned by www-data:www-data.
      * Moodle data directory: owned by www-data:www-data, directories 755, files 644.
  - For the exam server, its own config file is restored from its backup after cloning.
  - Improved regex for parsing Moodle config variables.

SUBCOMMANDS:
  backup             Create a backup archive.
  restore            Restore from a backup archive.
  restore-previous   Restore from a previous backup directory created during a restore.
  rp                 Alias for restore-previous.
"""

import argparse
import subprocess
import sys
import os
import tempfile
import shutil
import datetime
import getpass
import pwd
import glob
import random
import time
import threading
import itertools
import re
import json

PHP_BIN = 'php'

# ----------------------------------------------------------------------
# CONFIGURATION – loaded from ~/moodle_ninja_config.json
# Auto-created on first run. Nothing is hardcoded any more.
# DB credentials are NEVER stored here – they are read from Moodle's
# config.php at runtime (or prompted once).
# ----------------------------------------------------------------------
CONFIG_FILE_NAME = '.moodle_ninja_config.json'

DEFAULT_CONFIG = {
    "version": 1,
    "php_bin": "php",
    "web_parent": "/var/www/html",
    "db_charset": "utf8mb4",
    "db_collation": "utf8mb4_unicode_ci",
    "writable_dirs": [
        "temp", "cache", "sessions", "localcache", "repository",
        "theme", "environment", "filter", "calendar", "muc", "lang", "rss", "search"
    ],
    "primary_server": "psc",
    "secondary_server": "exam",
    "servers": {
        "psc": {
            "config_file": "/var/www/html/psc-Server/config.php",
            "symlink_name": "psc",
            "symlink_target": "psc-Server/public"
        },
        "exam": {
            "config_file": "/var/www/html/exam-Server/config.php",
            "symlink_name": "exam",
            "symlink_target": "exam-Server/public"
        }
    }
}

def get_config_path():
    """Return the path to config.json in the *invoking* user's home (sudo-aware)."""
    original_user = os.environ.get('SUDO_USER')
    if original_user:
        try:
            home = pwd.getpwnam(original_user).pw_dir
        except KeyError:
            home = os.path.expanduser(f"~{original_user}")
    else:
        home = os.path.expanduser("~")
    return os.path.join(home, CONFIG_FILE_NAME)

def save_config(cfg):
    """Atomically write config.json and chown back to SUDO_USER if present."""
    path = get_config_path()
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, path)
    if os.environ.get('SUDO_USER'):
        try:
            u = pwd.getpwnam(os.environ['SUDO_USER'])
            os.chown(path, u.pw_uid, u.pw_gid)
        except (KeyError, PermissionError):
            pass
    return path

def _deep_merge(base, override):
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v

def load_config():
    """Load config.json, creating a default if missing. Unknown keys are merged."""
    path = get_config_path()

    # One-time migration: old non-hidden file -> new hidden file
    if not os.path.exists(path):
        legacy = os.path.join(os.path.dirname(path), 'moodle_ninja_config.json')
        if os.path.exists(legacy):
            try:
                os.replace(legacy, path)
                print(f"[CONFIG] Migrated legacy config -> {path}")
            except OSError as e:
                print_warning(f"Could not migrate {legacy}: {e}")

    if not os.path.exists(path):
        print(f"[CONFIG] First run – creating default config at {path}")
        save_config(DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(path, 'r') as f:
            user_cfg = json.load(f)
    except json.JSONDecodeError as e:
        print(f"[CONFIG] ERROR: {path} is not valid JSON: {e}", file=sys.stderr)
        sys.exit(1)
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    _deep_merge(merged, user_cfg)
    return merged

# ---- Load the config and expose module-level names (so the rest of the file
# ---- keeps working unchanged) ----
CONFIG            = load_config()
PHP_BIN           = CONFIG.get('php_bin', 'php')
PRIMARY_SERVER    = CONFIG['primary_server']
SECONDARY_SERVER  = CONFIG['secondary_server']
WEB_PARENT        = CONFIG['web_parent']
DB_CHARSET        = CONFIG['db_charset']
DB_COLLATION      = CONFIG['db_collation']
WRITABLE_DIRS     = CONFIG['writable_dirs']
SERVERS           = CONFIG['servers']
CONFIG_PATHS      = {k: v['config_file'] for k, v in SERVERS.items()}

# ----------------------------------------------------------------------
# HACKER-STYLE AESTHETICS
# ----------------------------------------------------------------------
_USE_COLOR = True

MATRIX_GREEN = '\033[92m'
BRIGHT_RED = '\033[91m'
BRIGHT_YELLOW = '\033[93m'
BRIGHT_BLUE = '\033[94m'
BRIGHT_MAGENTA = '\033[95m'
BRIGHT_CYAN = '\033[96m'
WHITE = '\033[97m'
RESET = '\033[0m'
DIM = '\033[90m'
BOLD = '\033[1m'
# Extended color palette
COLOR_RESET = '\033[0m'
COLOR_GRAY = '\033[90m'
COLOR_RED = '\033[91m'
COLOR_GREEN = '\033[92m'
COLOR_YELLOW = '\033[93m'
COLOR_BLUE = '\033[94m'
COLOR_MAGENTA = '\033[95m'
COLOR_CYAN = '\033[96m'
COLOR_WHITE = '\033[97m'
COLOR_BOLD = '\033[1m'
COLOR_DIM = '\033[2m'

BLINK = '\033[5m' if _USE_COLOR else ''

HACKER_PHRASES = [
    "Bypassing firewalls...", "Decrypting payload...", "Injecting code...",
    "Slicing through the matrix...", "Evading IDS...", "Compiling exploit...",
    "Rootkit deployed.", "Tracing packets...", "Cracking hashes...",
    "Elevating privileges...", "Spoofing MAC address...", "Hijacking session...",
    "Loading kernel module...", "Reverse engineering...", "Exploiting buffer overflow..."
]

ICONS = ['⟳', '⚡', '⧩', '⨀', '⨂', '↺', '➤', '⎈', '⌘', '⛭', '🔪', '💀', '👾', '🤖', '⚙️']

# Rich icons per component
ICON_MAP = {
    'db': '🗄️',
    'web': '🌐',
    'data': '📁',
    'config': '⚙️',
    'tar': '📦',
    'symlink': '🔗',
    'permissions': '🔐',
    'cron': '⏰',
    'maintenance': '🔧',
    'upgrade': '⬆️',
}

def matrix_intro():
    if not _USE_COLOR:
        print("Moodle Ninja Backup System v2.0")
        return
    os.system('clear' if os.name == 'posix' else 'cls')
    for _ in range(8):
        line = ''.join(random.choice(['0', '1', '█', '▓', '▒', '░']) for _ in range(70))
        print(f"{MATRIX_GREEN}{line}{RESET}")
        time.sleep(0.03)
    time.sleep(0.2)
    banner = f"""
    {BRIGHT_RED}   ▄▄▄▄▄▄▄▄▄▄▄  ▄▄▄▄▄▄▄▄▄▄▄  ▄▄▄▄▄▄▄▄▄▄▄  ▄▄▄▄▄▄▄▄▄▄▄  ▄▄▄▄▄▄▄▄▄▄▄
    {BRIGHT_RED}  ▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌
    {BRIGHT_RED}  ▐░█▀▀▀▀▀▀▀█░▌▐░█▀▀▀▀▀▀▀█░▌▐░█▀▀▀▀▀▀▀█░▌▐░█▀▀▀▀▀▀▀▀▀ ▐░█▀▀▀▀▀▀▀█░▌
    {BRIGHT_RED}  ▐░▌       ▐░▌▐░▌       ▐░▌▐░▌       ▐░▌▐░▌          ▐░▌       ▐░▌
    {BRIGHT_RED}  ▐░█▄▄▄▄▄▄▄█░▌▐░█▄▄▄▄▄▄▄█░▌▐░█▄▄▄▄▄▄▄█░▌▐░▌          ▐░█▄▄▄▄▄▄▄█░▌
    {BRIGHT_RED}  ▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌▐░▌          ▐░░░░░░░░░░░▌
    {BRIGHT_RED}  ▐░█▀▀▀▀▀▀▀█░▌▐░█▀▀▀▀▀▀▀█░▌▐░█▀▀▀▀▀▀▀█░▌▐░▌          ▐░█▀▀▀▀▀▀▀█░▌
    {BRIGHT_RED}  ▐░▌       ▐░▌▐░▌       ▐░▌▐░▌       ▐░▌▐░▌          ▐░▌       ▐░▌
    {BRIGHT_RED}  ▐░█▄▄▄▄▄▄▄█░▌▐░█▄▄▄▄▄▄▄█░▌▐░█▄▄▄▄▄▄▄█░▌▐░█▄▄▄▄▄▄▄▄▄ ▐░█▄▄▄▄▄▄▄█░▌
    {BRIGHT_RED}  ▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌
    {BRIGHT_RED}   ▀▀▀▀▀▀▀▀▀▀▀  ▀▀▀▀▀▀▀▀▀▀▀  ▀▀▀▀▀▀▀▀▀▀▀  ▀▀▀▀▀▀▀▀▀▀▀  ▀▀▀▀▀▀▀▀▀▀▀
    {WHITE}{BOLD}           MOODLE NINJA BACKUP SYSTEM v2.0 – "We extract digital souls"{RESET}
    {BRIGHT_YELLOW}                      ▄▄▄▄▄   ▄▄▄   ▄▄▄▄▄   ▄▄▄▄▄   ▄▄▄   ▄▄▄▄▄
    {BRIGHT_YELLOW}                      █   █ █   █ █   █ █   █ █   █   █
    {BRIGHT_YELLOW}                      █   █ █   █ █   █ █   █ █   █   █
    {BRIGHT_YELLOW}                      █   █ █   █ █   █ █   █ █   █   █
    {BRIGHT_YELLOW}                      █   █ █   █ █   █ █   █ █   █   █
    {BRIGHT_YELLOW}                      █   █ █   █ █   █ █   █ █   █   █
    {BRIGHT_YELLOW}                      █▄▄▄█ █▄▄▄█ █▄▄▄█ █▄▄▄█ █▄▄▄█   █
    {RESET}
        """
    print(banner)
    time.sleep(1)
    for _ in range(3):
        sys.stdout.write(f"{MATRIX_GREEN}>> INITIALIZING NEURAL INTERFACE... {RESET}\n")
        time.sleep(0.2)
    # Updated line – list all subcommands
    print(f"{DIM}[ System ready. Available: backup, restore, restore-previous, upgrade, maintenance, cron, status ]{RESET}\n")

def spinner(message):
    if not _USE_COLOR:
        print(message)
        return
    done = [False]   # use a mutable list so we can update it
    def spin():
        for c in itertools.cycle(['|', '/', '-', '\\']):
            if done[0]:
                break
            sys.stdout.write(f'\r{BRIGHT_CYAN}{c} {message}{RESET}')
            sys.stdout.flush()
            time.sleep(0.1)
    t = threading.Thread(target=spin)
    t.daemon = True   # optional: allows thread to exit if main dies
    t.start()

    def stop():
        done[0] = True
        t.join()
        # clear the spinner line
        sys.stdout.write('\r' + ' ' * (len(message) + 2) + '\r')
        sys.stdout.flush()

    return stop

def glitch_effect(text):
    if not _USE_COLOR:
        return text
    glitched = []
    for ch in text:
        if random.random() < 0.05:
            glitched.append(random.choice(['█', '▓', '▒', '░', '?', '!']))
        else:
            glitched.append(ch)
    return ''.join(glitched)

def print_hacker_msg(msg, delay=0.02):
    if not _USE_COLOR:
        print(msg)
        return
    for ch in msg:
        sys.stdout.write(ch)
        sys.stdout.flush()
        time.sleep(delay)
    print()

def print_info(msg):
    icon = random.choice(['🔍', '📡', '💾', '🕵️', '🔓', '⚡', '🖥️'])
    print(f"{BRIGHT_CYAN}{icon} {msg}{RESET}")

def print_success(msg):
    print(f"{MATRIX_GREEN}✔ {msg}{RESET}")

def print_warning(msg, file=sys.stderr):
    print(f"{BRIGHT_YELLOW}⚠ {msg}{RESET}", file=file)

def print_error(msg, file=sys.stderr):
    print(f"{BRIGHT_RED}✘ {msg}{RESET}", file=file)

def print_prompt(msg):
    return input(f"{BRIGHT_MAGENTA}{BOLD}❯ {msg}{RESET} ")

def print_verbose(msg, component=None, color=None, animated=False):
    if not _USE_COLOR:
        print(f"[VERBOSE] {msg}")
        return
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    icon = ICON_MAP.get(component, random.choice(ICONS)) if component else random.choice(ICONS)
    color = color or random.choice([COLOR_BLUE, COLOR_CYAN, COLOR_MAGENTA, COLOR_GREEN])
    # Add a fun prefix
    prefix = f"{COLOR_DIM}[{timestamp}]{COLOR_RESET} {color}{icon}{COLOR_RESET}"
    if animated:
        # Show a bouncing dot effect (simple)
        dots = '.' * (random.randint(1, 3))
        sys.stdout.write(f"\r{prefix} {msg}{dots}")
    else:
        sys.stdout.write(f"{prefix} {msg}\n")
    sys.stdout.flush()

def print_progress_bar(iteration, total, prefix='', suffix='', decimals=1, length=50, fill='█'):
    """
    Call in a loop to create terminal progress bar.
    """
    if not _USE_COLOR:
        return
    percent = ("{0:." + str(decimals) + "f}").format(100 * (iteration / float(total)))
    filled_length = int(length * iteration // total)
    bar = fill * filled_length + '-' * (length - filled_length)
    sys.stdout.write(f'\r{COLOR_CYAN}{prefix}{COLOR_RESET} |{COLOR_GREEN}{bar}{COLOR_RESET}| {COLOR_WHITE}{percent}%{COLOR_RESET} {suffix}')
    sys.stdout.flush()
    if iteration == total:
        print()  # newline at end

def run_cmd_stream(cmd, verbose=False, **kwargs):
    if verbose:
        print_verbose(f"Executing: {' '.join(cmd)}", component='tar')
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, **kwargs)
    for line in proc.stdout:
        # Colorize based on line content
        if 'adding:' in line or 'restoring:' in line:
            sys.stdout.write(f"{COLOR_GREEN}{line}{COLOR_RESET}")
        elif 'skipping:' in line or 'warning:' in line:
            sys.stdout.write(f"{COLOR_YELLOW}{line}{COLOR_RESET}")
        elif 'error:' in line:
            sys.stdout.write(f"{COLOR_RED}{line}{COLOR_RESET}")
        else:
            sys.stdout.write(f"{COLOR_WHITE}{line}{COLOR_RESET}")
        sys.stdout.flush()
    proc.wait()
    if proc.returncode != 0:
        print_error(f"Command failed: {' '.join(cmd)}")
        sys.exit(1)
    return proc

# ----------------------------------------------------------------------
# Helper functions
# ----------------------------------------------------------------------
def run_cmd(cmd, verbose=False, check=True, **kwargs):
    if verbose:
        print_verbose(f"Executing: {' '.join(cmd)}")
    if 'stdout' in kwargs or 'stderr' in kwargs:
        result = subprocess.run(cmd, **kwargs)
    else:
        result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if check and result.returncode != 0:
        print_error(f"Command failed: {' '.join(cmd)}")
        if hasattr(result, 'stderr') and result.stderr:
            print_error(result.stderr)
        sys.exit(1)
    if verbose and hasattr(result, 'stdout') and result.stdout:
        print(result.stdout)
    return result

def check_required_commands(commands, verbose):
    for cmd in commands:
        if not shutil.which(cmd):
            print_error(f"Missing weapon: '{cmd}' not found in PATH.")
            sys.exit(1)
    if verbose:
        print_verbose("All required binaries located.")

def get_default_backup_dir():
    original_user = os.environ.get('SUDO_USER')
    if original_user:
        try:
            return pwd.getpwnam(original_user).pw_dir
        except KeyError:
            return os.path.expanduser(f"~{original_user}")
    else:
        return os.path.expanduser("~")

def get_default_backup_path():
    home = get_default_backup_dir()
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(home, f"moodle_backup_{timestamp}.tar.xz")

def find_latest_backup():
    home = get_default_backup_dir()
    pattern = os.path.join(home, "moodle_backup_*.tar.xz")
    files = glob.glob(pattern)
    if not files:
        return None
    files.sort(key=os.path.getmtime, reverse=True)
    return files[0]

# ----------------------------------------------------------------------
# Database client selection
# ----------------------------------------------------------------------
def get_db_client():
    if shutil.which('mariadb'):
        return 'mariadb'
    elif shutil.which('mysql'):
        return 'mysql'
    else:
        print_error("Neither 'mariadb' nor 'mysql' found in PATH.")
        sys.exit(1)

def get_db_dump():
    if shutil.which('mariadb-dump'):
        return 'mariadb-dump'
    elif shutil.which('mysqldump'):
        return 'mysqldump'
    else:
        print_error("Neither 'mariadb-dump' nor 'mysqldump' found in PATH.")
        sys.exit(1)

# ----------------------------------------------------------------------
# Database authentication helpers
# ----------------------------------------------------------------------
def test_db_connection(auth_args, db_name=None):
    client = get_db_client()
    cmd = [client] + auth_args + ['-e', 'SELECT 1']
    if db_name:
        cmd.append(db_name)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        return True, None
    else:
        return False, result.stderr

def obtain_db_credentials(verbose):
    print_info("Database authentication required. Please enter credentials:")
    user = input("Username (default root): ") or "root"
    password = getpass.getpass("Password: ")

    fd, path = tempfile.mkstemp(prefix='db_', suffix='.cnf', text=True)
    with os.fdopen(fd, 'w') as f:
        f.write(f"[client]\nuser={user}\npassword={password}\n")
    os.chmod(path, 0o600)
    if verbose:
        print_verbose(f"Credentials stored temporarily: {path}")
    return ['--defaults-extra-file=' + path], path

# ----------------------------------------------------------------------
# Database existence and manipulation helpers (with charset support)
# ----------------------------------------------------------------------
def database_exists(db_name, auth_args, verbose=False):
    client = get_db_client()
    cmd = [client] + auth_args + ['-e', f"USE `{db_name}`"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0

def create_database_if_not_exists(db_name, auth_args, verbose=False):
    """
    Create the database if it does not exist, using utf8mb4 charset and
    utf8mb4_unicode_ci collation (Moodle recommended).
    """
    client = get_db_client()
    create_cmd = [
        client
    ] + auth_args + [
        '-e',
        f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
        f"CHARACTER SET {DB_CHARSET} COLLATE {DB_COLLATION}"
    ]
    run_cmd(create_cmd, verbose)

def drop_database(db_name, auth_args, verbose=False):
    client = get_db_client()
    cmd = [client] + auth_args + ['-e', f"DROP DATABASE IF EXISTS `{db_name}`"]
    run_cmd(cmd, verbose)

def backup_database(db_name, auth_args, backup_dir, server_name, verbose=False):
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    db_backup_dir = os.path.join(backup_dir, server_name, 'db')
    os.makedirs(db_backup_dir, exist_ok=True)
    backup_file = os.path.join(db_backup_dir, f"{db_name}_backup_{timestamp}.sql")
    # Use --default-character-set=utf8mb4 to ensure correct charset in the dump
    dump_cmd = [get_db_dump()] + auth_args + [f'--default-character-set={DB_CHARSET}', db_name]
    with open(backup_file, 'wb') as f:
        run_cmd(dump_cmd, verbose, stdout=f)

def handle_existing_database(db_name, auth_args, backup_dir, server_name, policy, verbose):
    if policy == 'skip':
        if verbose:
            print_verbose(f"Skipping database '{db_name}' (keep existing).")
        return False
    elif policy == 'remove':
        if verbose:
            print_verbose(f"Removing database '{db_name}' without backup.")
        drop_database(db_name, auth_args, verbose)
        return True
    elif policy == 'backup':
        if verbose:
            print_verbose(f"Backing up database '{db_name}' before removal.")
        backup_database(db_name, auth_args, backup_dir, server_name, verbose)
        drop_database(db_name, auth_args, verbose)
        return True
    elif policy == 'ask':
        while True:
            choice = print_prompt(f"Database '{db_name}' already exists. What to do? [R]eplace (drop and restore), [B]ackup and replace, [S]kip (default: S): ").strip().lower()
            if choice in ('', 's', 'skip'):
                print("Skipping database restore.")
                return False
            elif choice in ('r', 'replace'):
                drop_database(db_name, auth_args, verbose)
                return True
            elif choice in ('b', 'backup'):
                backup_database(db_name, auth_args, backup_dir, server_name, verbose)
                drop_database(db_name, auth_args, verbose)
                return True
            else:
                print_warning("Invalid choice. Please enter R, B, or S.")
    else:
        return True

def get_table_prefix(server_name):
    cfg = load_server_config(server_name)
    return cfg['table_prefix']
    if not os.path.isfile(config_file):
        return 'mdl_'  # fallback
    with open(config_file, 'r') as f:
        content = f.read()
    match = re.search(r"\$CFG->prefix\s*=\s*['\"]([^'\"]+)['\"]\s*;", content)
    if match:
        return match.group(1)
    return 'mdl_'

def set_config_value(server_name, config_name, value, auth_args, verbose=False):
    prefix = get_table_prefix(server_name)
    cfg = load_server_config(server_name)
    db_name = cfg['db_name']
    prefix = cfg['table_prefix']
    client = get_db_client()
    # Convert value to appropriate SQL type
    if isinstance(value, bool):
        sql_value = '1' if value else '0'
    elif isinstance(value, str):
        sql_value = f"'{value}'"
    else:
        sql_value = str(value)
    # Use INSERT ... ON DUPLICATE KEY UPDATE
    sql = f"""
        INSERT INTO {prefix}config (name, value)
        VALUES ('{config_name}', {sql_value})
        ON DUPLICATE KEY UPDATE value = {sql_value}
    """
    cmd = [client] + auth_args + ['-e', sql, db_name]
    run_cmd(cmd, verbose)

def get_maintenance_settings(server_name, auth_args, verbose=False):
    cfg = load_server_config(server_name)
    prefix = cfg['table_prefix']
    db_name = cfg['db_name']
    client = get_db_client()
    # Query relevant config values
    sql = f"SELECT name, value FROM {prefix}config WHERE name IN ('maintenance_enabled', 'maintenance_message', 'maintenance_allow_admins')"
    cmd = [client] + auth_args + ['-e', sql, db_name]
    result = run_cmd(cmd, verbose, capture_output=True, text=True)
    lines = result.stdout.strip().splitlines()
    settings = {}
    for line in lines[1:]:  # skip header
        parts = line.split('\t')
        if len(parts) == 2:
            settings[parts[0]] = parts[1]
    return settings

# ----------------------------------------------------------------------
# Extract database credentials from Moodle config file - IMPROVED REGEX
# ----------------------------------------------------------------------
def _parse_config_var(content, var_name):
    """
    Helper to parse a variable like $CFG->dbname from PHP config content.
    Returns the value or None.
    """
    # Build regex: allow spaces, single/double quotes, and optional semicolon
    pattern = r'\$CFG->' + re.escape(var_name) + r'\s*=\s*([\'"])([^\'"]+)\1\s*;'
    match = re.search(pattern, content)
    if match:
        return match.group(2)
    return None

def get_dbname_from_config(config_path):
    try:
        with open(config_path, 'r') as f:
            content = f.read()
        return _parse_config_var(content, 'dbname')
    except Exception as e:
        print_warning(f"Could not read config file {config_path}: {e}")
        return None

def get_dbuser_pass_from_config(config_path):
    """Extract dbuser and dbpass from Moodle config. Returns (user, password) or (None, None)."""
    try:
        with open(config_path, 'r') as f:
            content = f.read()
        user = _parse_config_var(content, 'dbuser')
        pwd = _parse_config_var(content, 'dbpass')
        return user, pwd
    except Exception as e:
        print_warning(f"Could not read db credentials from {config_path}: {e}")
        return None, None

def grant_database_privileges(db_name, db_user, db_pass, auth_args, verbose=False):
    """
    Ensure the database user exists (on localhost) and grant all privileges on db_name.
    Uses the administrative connection (auth_args) to execute GRANT.
    """
    if not db_user or not db_pass:
        if verbose:
            print_verbose("No database user/password found in config, skipping privilege grant.")
        return

    client = get_db_client()
    host = 'localhost'

    # Check if user already exists
    check_user_cmd = [client] + auth_args + ['-e', f"SELECT User FROM mysql.user WHERE User='{db_user}' AND Host='{host}'"]
    result = subprocess.run(check_user_cmd, capture_output=True, text=True)
    user_exists = (result.returncode == 0 and result.stdout.strip() and db_user in result.stdout)

    if not user_exists:
        if verbose:
            print_verbose(f"Creating database user '{db_user}'@'{host}'.")
        create_user_cmd = [client] + auth_args + ['-e', f"CREATE USER '{db_user}'@'{host}' IDENTIFIED BY '{db_pass}'"]
        try:
            run_cmd(create_user_cmd, verbose, check=True)
        except subprocess.CalledProcessError as e:
            print_warning(f"Failed to create user '{db_user}': {e}. Attempting to grant anyway.")
    else:
        if verbose:
            print_verbose(f"User '{db_user}'@'{host}' already exists. Updating password (if changed).")
        alter_cmd = [client] + auth_args + ['-e', f"ALTER USER '{db_user}'@'{host}' IDENTIFIED BY '{db_pass}'"]
        subprocess.run(alter_cmd, capture_output=True)  # ignore failure

    # Grant all privileges on the database
    grant_cmd = [client] + auth_args + ['-e', f"GRANT ALL PRIVILEGES ON `{db_name}`.* TO '{db_user}'@'{host}'"]
    run_cmd(grant_cmd, verbose, check=True)

    flush_cmd = [client] + auth_args + ['-e', "FLUSH PRIVILEGES"]
    run_cmd(flush_cmd, verbose, check=True)
    print_success(f"Database privileges granted: {db_user}@localhost on database {db_name}")

def get_moodle_root(server_name):
    """Return the full path to the Moodle web root for a given server."""
    cfg = load_server_config(server_name)
    return cfg['web_root']

def run_moodle_cli(server_name, script, args=None, verbose=False):
    """
    Execute a Moodle CLI script with optional arguments.
    script: e.g., 'maintenance.php', 'upgrade.php', 'cron.php'
    args: list of strings (e.g., ['--enable'])
    Returns subprocess result.
    """
    web_root = get_moodle_root(server_name)
    if not os.path.isdir(web_root):
        print_error(f"Web root {web_root} does not exist for {server_name}")
        sys.exit(1)
    cli_script = os.path.join(web_root, 'admin', 'cli', script)
    if not os.path.isfile(cli_script):
        print_error(f"CLI script {cli_script} not found")
        sys.exit(1)
    cmd = ['php', cli_script]
    if args:
        cmd.extend(args)
    # Run with real-time output if verbose
    if verbose:
        return run_cmd_stream(cmd, verbose)
    else:
        return run_cmd(cmd, verbose, check=False)  # we'll handle errors

def get_moodle_version(server_name):
    version_file = os.path.join(get_moodle_root(server_name), 'version.php')
    if not os.path.isfile(version_file):
        return None, None
    with open(version_file, 'r') as f:
        content = f.read()
    # Look for $branch = '...'; and $release = '...';
    branch_match = re.search(r'\$branch\s*=\s*[\'"]([^\'"]+)[\'"]\s*;', content)
    release_match = re.search(r'\$release\s*=\s*[\'"]([^\'"]+)[\'"]\s*;', content)
    branch = branch_match.group(1) if branch_match else None
    release = release_match.group(1) if release_match else None
    return branch, release

def get_current_branch(web_root):
    """Return the currently checked-out branch, or None."""
    result = subprocess.run(
        ['git', '-C', web_root, 'rev-parse', '--abbrev-ref', 'HEAD'],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return None if branch in ('', 'HEAD') else branch

def is_shallow_clone(web_root):
    return os.path.exists(os.path.join(web_root, '.git', 'shallow'))

def update_moodle_code(server_name, branch=None, verbose=False):
    """
    Update Moodle code from git.

    - Shallow clone  -> depth-1 fetch + reset --hard FETCH_HEAD (stays shallow, tiny download)
    - Full clone     -> git pull --ff-only
    Untracked files (config.php, local/, plugins, moodledata) are preserved by git itself.
    Modified *tracked* files will be lost – we warn about that.
    """
    web_root = get_moodle_root(server_name)
    if not os.path.isdir(os.path.join(web_root, '.git')):
        print_error(f"{web_root} is not a git repository.")
        sys.exit(1)

    current_branch = branch or get_current_branch(web_root)
    if not current_branch:
        print_warning(f"Could not determine current branch in {web_root}.")
        current_branch = 'MOODLE_502_STABLE'
        print_info(f"Falling back to branch: {current_branch}")

    print_info(f"Updating {server_name} at {web_root} (branch: {current_branch})")

    # Warn about modified tracked files that will be lost
    status = subprocess.run(
        ['git', '-C', web_root, 'status', '--porcelain'],
        capture_output=True, text=True
    ).stdout
    modified = [l for l in status.splitlines() if l[:2].strip() and not l.startswith('??')]
    if modified:
        print_warning(f"{len(modified)} tracked file(s) have local modifications. "
                      "These will be overwritten by the update:")
        for line in modified[:10]:
            print(f"    {line}")
        if len(modified) > 10:
            print(f"    ... and {len(modified) - 10} more")
        if not print_prompt("Continue and discard these changes? (y/N): ").lower().startswith('y'):
            print_warning("Update aborted by user.")
            sys.exit(0)

    # Record current HEAD
    before = subprocess.run(
        ['git', '-C', web_root, 'rev-parse', '--short', 'HEAD'],
        capture_output=True, text=True
    ).stdout.strip()

    shallow = is_shallow_clone(web_root)

    if shallow:
        print_info("Shallow clone detected – using depth-1 fetch + reset (stays shallow).")
        run_cmd(['git', '-C', web_root, 'fetch', '--depth', '1',
                 'origin', current_branch], verbose)
        run_cmd(['git', '-C', web_root, 'reset', '--hard', 'FETCH_HEAD'], verbose)
    else:
        print_info("Full clone detected – using fast-forward pull.")
        result = subprocess.run(
            ['git', '-C', web_root, 'pull', '--ff-only', 'origin', current_branch],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print_warning(f"Fast-forward pull failed: {result.stderr.strip()}")
            print_info("Falling back to depth-1 fetch + reset.")
            run_cmd(['git', '-C', web_root, 'fetch', '--depth', '1',
                     'origin', current_branch], verbose)
            run_cmd(['git', '-C', web_root, 'reset', '--hard', 'FETCH_HEAD'], verbose)
        elif verbose and result.stdout:
            print(result.stdout)

    after = subprocess.run(
        ['git', '-C', web_root, 'rev-parse', '--short', 'HEAD'],
        capture_output=True, text=True
    ).stdout.strip()

    if before == after:
        print_success(f"No new changes – {server_name} already up to date ({after}).")
        return False

    print_success(f"Updated {server_name}: {before} -> {after}")
    return True

def upgrade_moodle(server_name, check_only=False, verbose=False):
    """
    Run Moodle upgrade.
    If check_only=True, just check if upgrade is needed (exit code 0 = no upgrade, 1 = upgrade needed).
    """
    args = ['--non-interactive']
    if check_only:
        args.append('--check')
    result = run_moodle_cli(server_name, 'upgrade.php', args, verbose)
    return result.returncode

def update_moodle(server_name, branch=None, enable_maintenance=True, verbose=False):
    """
    Full Moodle update flow:
      1. Enable maintenance mode (optional)
      2. Update code via git (shallow-aware)
      3. Run PHP DB upgrade if code changed
      4. Purge caches
      5. Disable maintenance mode (always, in finally)
    """
    web_root = get_moodle_root(server_name)
    if not os.path.isdir(web_root):
        print_error(f"Web root {web_root} not found for {server_name}")
        sys.exit(1)

    if enable_maintenance:
        print_info("Enabling maintenance mode...")
        try:
            set_maintenance(server_name, enable=True, verbose=verbose)
        except SystemExit:
            print_warning("Could not enable maintenance mode; continuing anyway.")

    try:
        # 1. Code update
        changed = update_moodle_code(server_name, branch=branch, verbose=verbose)

        # 2. Database upgrade (only needed when code actually changed)
        if changed:
            print_info("Running Moodle database upgrade...")
            rc = upgrade_moodle(server_name, check_only=False, verbose=verbose)
            if rc != 0:
                print_error(f"Moodle upgrade returned exit code {rc}. "
                            "Check the logs – maintenance mode will still be disabled.")
        else:
            print_info("No code changes; skipping database upgrade.")

        # 3. Purge caches (always – harmless and a good idea)
        print_info("Purging caches...")
        run_moodle_cli(server_name, 'purge_caches.php', verbose=verbose)

    finally:
        if enable_maintenance:
            print_info("Disabling maintenance mode...")
            try:
                set_maintenance(server_name, enable=False, verbose=verbose)
            except SystemExit:
                print_warning("Could not disable maintenance mode. Disable it manually!")

    print_success(f"Update of {server_name} complete.")

def set_maintenance(server_name, enable=True, verbose=False):
    mode = '--enable' if enable else '--disable'
    return run_moodle_cli(server_name, 'maintenance.php', [mode], verbose)

def run_cron(server_name, verbose=False):
    return run_moodle_cli(server_name, 'cron.php', verbose=verbose)

def setup_cron(server_name, schedule='* * * * *', user='www-data', verbose=False):
    """Add a cron job for the given server to the system crontab."""
    cmd = ['php', os.path.join(get_moodle_root(server_name), 'admin', 'cli', 'cron.php')]
    cron_line = f"{schedule} {user} {' '.join(cmd)}"
    # Use crontab -u root -l to check and add
    # We'll implement a helper to manage crontab entries.

def setup_cron_job(server_name, schedule='* * * * *', verbose=False):
    """Add a cron job for the given server to root's crontab."""
    cron_cmd = f"{PHP_BIN} {os.path.join(get_moodle_root(server_name), 'admin', 'cli', 'cron.php')}"
    cron_line = f"{schedule} root {cron_cmd} > /dev/null 2>&1"
    # Get current crontab
    crontab = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
    if crontab.returncode == 0 and cron_cmd in crontab.stdout:
        print_info("Cron job already exists; updating.")
    # Remove any existing line for this server to avoid duplicates
    lines = crontab.stdout.splitlines() if crontab.returncode == 0 else []
    filtered = [line for line in lines if cron_cmd not in line]
    filtered.append(cron_line)
    final = '\n'.join(filtered) + '\n'
    proc = subprocess.run(['crontab', '-'], input=final, text=True)
    if proc.returncode == 0:
        print_success(f"Cron job installed with schedule: {schedule}")
    else:
        print_error("Failed to install cron job.")
    return proc.returncode

def show_status(server_name, verbose=False):
    branch, release = get_moodle_version(server_name)
    cfg = load_server_config(server_name)
    print_info(f"Server: {server_name}")
    print(f"  Web root: {cfg['web_root']}")
    print(f"  Data dir: {cfg['data_dir']}")
    print(f"  Database: {cfg['db_name']}")
    print(f"  Branch: {branch or 'unknown'}")
    print(f"  Release: {release or 'unknown'}")
    # Check maintenance mode via CLI (optional)
    result = run_moodle_cli(server_name, 'maintenance.php', ['--status'], verbose=False)
    if result.returncode == 0 and result.stdout:
        print(f"  Maintenance: {result.stdout.strip()}")
    else:
        print("  Maintenance: unknown (try 'maintenance --status')")

# ----------------------------------------------------------------------
# File permission management after restore – only fix if needed
# ----------------------------------------------------------------------
def permissions_need_fixing(cfg, verbose):
    """
    Check if the web root and data directory permissions deviate from the desired state.
    Returns True if any fix is needed, else False.
    """
    web_root = cfg['web_root']
    data_dir = cfg['data_dir']

    # Get uid/gid for www-data
    try:
        www_uid = pwd.getpwnam('www-data').pw_uid
        www_gid = pwd.getpwnam('www-data').pw_gid
    except KeyError:
        print_warning("User 'www-data' not found; cannot check permissions.")
        return True  # assume we need to fix

    # Check web root ownership
    if os.path.exists(web_root):
        st = os.stat(web_root)
        if st.st_uid != 0 or st.st_gid != 0:
            if verbose:
                print_verbose(f"Web root {web_root} not owned by root:root (uid={st.st_uid}, gid={st.st_gid})")
            return True
    else:
        if verbose:
            print_verbose(f"Web root {web_root} does not exist, will fix if created.")
        return True

    # Check data directory ownership
    if os.path.exists(data_dir):
        st = os.stat(data_dir)
        if st.st_uid != www_uid or st.st_gid != www_gid:
            if verbose:
                print_verbose(f"Data dir {data_dir} not owned by www-data:www-data (uid={st.st_uid}, gid={st.st_gid})")
            return True
    else:
        if verbose:
            print_verbose(f"Data dir {data_dir} does not exist, will fix if created.")
        return True

    # Check writable subdirectories inside web root
    for sub in WRITABLE_DIRS:
        target = os.path.join(web_root, sub)
        if os.path.exists(target):
            st = os.stat(target)
            if st.st_uid != www_uid or st.st_gid != www_gid:
                if verbose:
                    print_verbose(f"Writable dir {target} not owned by www-data:www-data (uid={st.st_uid}, gid={st.st_gid})")
                return True
    return False

def fix_permissions(server_name, cfg, verbose):
    """
    Set secure permissions for a restored Moodle instance:
      - Web root: owned by root:root, directories 755, files 644.
      - Writable subdirectories (e.g., temp, cache) inside web root: owned by www-data:www-data.
      - Data directory (moodledata): owned by www-data:www-data, directories 755, files 644.
    Only runs if permissions are not already correct.
    """
    # Check if we need to fix
    if not permissions_need_fixing(cfg, verbose):
        if verbose:
            print_verbose(f"Permissions for {server_name} are already correct. Skipping.")
        return

    web_root = cfg['web_root']
    data_dir = cfg['data_dir']

    print_info(f"Fixing permissions for {server_name}...")

    # 1. Web root ownership and base permissions
    if os.path.exists(web_root):
        run_cmd(['chown', '-R', 'root:root', web_root], verbose)
        run_cmd(['find', web_root, '-type', 'd', '-exec', 'chmod', '755', '{}', '+'], verbose)
        run_cmd(['find', web_root, '-type', 'f', '-exec', 'chmod', '644', '{}', '+'], verbose)
        if verbose:
            print_verbose(f"Set base ownership/permissions for {web_root}")
    else:
        print_warning(f"Web root {web_root} does not exist, cannot set permissions.")

    # 2. Writable subdirectories (owned by www-data)
    for sub in WRITABLE_DIRS:
        target = os.path.join(web_root, sub)
        if os.path.exists(target):
            run_cmd(['chown', '-R', 'www-data:www-data', target], verbose)
            run_cmd(['chmod', '-R', '755', target], verbose)
            if verbose:
                print_verbose(f"Set writable permissions for {target}")

    # 3. Data directory (moodledata)
    if os.path.exists(data_dir):
        run_cmd(['chown', '-R', 'www-data:www-data', data_dir], verbose)
        run_cmd(['find', data_dir, '-type', 'd', '-exec', 'chmod', '755', '{}', '+'], verbose)
        run_cmd(['find', data_dir, '-type', 'f', '-exec', 'chmod', '644', '{}', '+'], verbose)
        if verbose:
            print_verbose(f"Set data directory permissions for {data_dir}")
    else:
        print_warning(f"Data directory {data_dir} does not exist, cannot set permissions.")

# ----------------------------------------------------------------------
# Backup
# ----------------------------------------------------------------------
def backup(backup_location, verbose):
    print_info("Initiating backup sequence...")
    required = ['tar', 'xz', 'cp', get_db_dump(), get_db_client()]
    check_required_commands(required, verbose)

    temp_dir = tempfile.mkdtemp(prefix='moodle_backup_')
    if verbose:
        print_verbose(f"Temporary staging area: {temp_dir}")
    else:
        print_info(f"Staging area created: {temp_dir}")

    auth_args = None
    temp_auth_file = None

    def ensure_db_access(db_name):
        nonlocal auth_args, temp_auth_file
        if auth_args is not None:
            return auth_args
        success, err = test_db_connection([], db_name)
        if success:
            auth_args = []
            return auth_args
        if "Access denied" not in err:
            print_error(f"Cannot access database '{db_name}': {err}")
            sys.exit(1)
        if verbose:
            print_verbose(f"Access denied for database '{db_name}'. Prompting for credentials.")
        auth_args, temp_auth_file = obtain_db_credentials(verbose)
        success, err = test_db_connection(auth_args, db_name)
        if not success:
            print_error(f"Authentication failed for database '{db_name}': {err}")
            sys.exit(1)
        return auth_args

    try:
        for server_name, cfg in SERVERS.items():
            server_cfg = load_server_config(server_name)   # <-- load dynamic config
            print_info(f"Harvesting server: {server_name}")
            server_dir = os.path.join(temp_dir, server_name)
            os.makedirs(server_dir, exist_ok=True)

            # Config file (still from SERVERS)
            cfg_src = cfg['config_file']
            cfg_dst = os.path.join(server_dir, f"config-{server_name}.php")
            if os.path.exists(cfg_src):
                run_cmd(['cp', '-a', cfg_src, cfg_dst], verbose)
                if verbose:
                    print_verbose(f"Config captured: {cfg_src} -> {cfg_dst}")
                else:
                    print_info("Config file secured.")
            else:
                print_warning(f"Config file {cfg_src} not found, skipping.")

            # Database dump – use server_cfg
            print_info("Dumping database...")
            db_dump = os.path.join(server_dir, 'db.sql')
            current_auth = ensure_db_access(server_cfg['db_name'])
            dump_cmd = [get_db_dump()] + current_auth + [f'--default-character-set={DB_CHARSET}', server_cfg['db_name']]

            stop = spinner("Dumping database...")
            try:
                with open(db_dump, 'wb') as f:
                    subprocess.run(dump_cmd, stdout=f, check=True)   # <-- fixed typo
            finally:
                stop()

            if verbose:
                print_verbose(f"Database '{server_cfg['db_name']}' dumped to {db_dump}")
            else:
                print_info("Database extracted.")

            # Web root – skip for exam server
            if server_name == SECONDARY_SERVER:
                if verbose:
                    print_verbose(f"Web root for {SECONDARY_SERVER} omitted (will be cloned during restore).")
                else:
                    print_info(f"Web root for {SECONDARY_SERVER} omitted (clone on restore).")
            else:
                print_verbose(random.choice(HACKER_PHRASES), component='web')
                print_info("Copying web root...")
                web_src = server_cfg['web_root']
                web_dst = os.path.join(server_dir, 'html')
                if os.path.exists(web_src):
                    run_cmd(['cp', '-a', '--reflink=auto', web_src, web_dst], verbose)
                    if verbose:
                        print_verbose(f"Web root copied: {web_src} -> {web_dst}")
                    else:
                        print_info("Web root archived.")
                else:
                    print_warning(f"Web root {web_src} not found, skipping.")

            # Data directory
            print_info("Copying data directory...")
            data_src = server_cfg['data_dir']
            data_dst = os.path.join(server_dir, 'data')
            if os.path.exists(data_src):
                run_cmd(['cp', '-a', '--reflink=auto', data_src, data_dst], verbose)
                if verbose:
                    print_verbose(f"Data directory copied: {data_src} -> {data_dst}")
                else:
                    print_info("Data directory archived.")
            else:
                print_warning(f"Data dir {data_src} not found, skipping.")

        # Create archive
        if backup_location:
            archive_name = backup_location
        else:
            archive_name = get_default_backup_path()
        print_info("Compressing payload...")
        if verbose:
            run_cmd_stream(['tar', '-C', temp_dir, '-cJvf', archive_name, '.'], verbose)
        else:
            run_cmd(['tar', '-C', temp_dir, '-cJf', archive_name, '.'], verbose)
        print_success(f"Backup successfully created: {archive_name}")

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        if verbose:
            print_verbose(f"Cleaned staging area: {temp_dir}")
        else:
            print_info("Staging area cleaned.")
        if temp_auth_file and os.path.exists(temp_auth_file):
            os.unlink(temp_auth_file)
            if verbose:
                print_verbose(f"Removed temporary credentials: {temp_auth_file}")

# ----------------------------------------------------------------------
# Restore helpers for handling existing files/directories
# ----------------------------------------------------------------------
def backup_existing(server_name, component, path, backup_dir, verbose):
    target_dir = os.path.join(backup_dir, server_name, component)
    os.makedirs(target_dir, exist_ok=True)
    dest = os.path.join(target_dir, os.path.basename(path))
    if os.path.exists(dest):
        counter = 1
        while True:
            alt = os.path.join(target_dir, f"{os.path.basename(path)}.{counter}")
            if not os.path.exists(alt):
                dest = alt
                break
            counter += 1
    shutil.move(path, dest)
    if verbose:
        print_verbose(f"Archived existing: {path} -> {dest}")

def handle_existing(server_name, component, path, is_dir, backup_dir, policy, verbose):
    if not os.path.lexists(path):
        return True

    if policy == 'remove':
        try:
            if is_dir and os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.unlink(path)
            if verbose:
                print_verbose(f"Removed existing: {path}")
            return True
        except Exception as e:
            print_error(f"Error removing {path}: {e}")
            sys.exit(1)

    elif policy == 'backup':
        try:
            backup_existing(server_name, component, path, backup_dir, verbose)
            return True
        except Exception as e:
            print_error(f"Error backing up {path}: {e}")
            sys.exit(1)

    elif policy == 'skip':
        if verbose:
            print_verbose(f"Skipping existing: {path}")
        return False

    else:  # policy == 'ask'
        print(f"\nExisting {'directory' if is_dir else 'file'} found: {path}")
        while True:
            choice = print_prompt("What to do? [R]emove, [B]ackup, [S]kip (default: S): ").strip().lower()
            if choice in ('', 's', 'skip'):
                print("Skipping this component.")
                return False
            elif choice in ('r', 'remove'):
                try:
                    if is_dir and os.path.isdir(path) and not os.path.islink(path):
                        shutil.rmtree(path)
                    else:
                        os.unlink(path)
                    if verbose:
                        print_verbose(f"Removed: {path}")
                    return True
                except Exception as e:
                    print_error(f"Error removing {path}: {e}")
                    sys.exit(1)
            elif choice in ('b', 'backup'):
                try:
                    backup_existing(server_name, component, path, backup_dir, verbose)
                    return True
                except Exception as e:
                    print_error(f"Error backing up {path}: {e}")
                    sys.exit(1)
            else:
                print_warning("Invalid choice. Please enter R, B, or S.")

def get_conflict_policy(verbose):
    print_info("\nConflict detection active. Choose your weapon:")
    print("  [A]sk each time (prompt for every existing item)")
    print("  [R]emove all existing items without asking")
    print("  [B]ackup all existing items without asking")
    print("  [S]kip all existing items (do not restore any that conflict)")
    choice = print_prompt("Choose (default: Ask): ").strip().lower()
    if choice in ('r', 'remove'):
        policy = 'remove'
    elif choice in ('b', 'backup'):
        policy = 'backup'
    elif choice in ('s', 'skip'):
        policy = 'skip'
    else:
        policy = 'ask'
    if verbose:
        print_verbose(f"Conflict resolution policy: {policy}")
    return policy

def load_server_config(server_name):
    """
    Merge values from config.json (server entry) with those parsed from the
    Moodle config.php. Priority:
        config.json override > config.php > built-in fallback.
    Credentials (dbuser/dbpass) always come from config.php — never stored in
    config.json, so the config file stays credential-free.
    """
    cfg = SERVERS[server_name]
    config_path = cfg['config_file']
    if not os.path.isfile(config_path):
        print_error(f"Config file {config_path} not found for {server_name}")
        sys.exit(1)
    with open(config_path, 'r') as f:
        content = f.read()

    def get_var(name):
        pattern = r'\$CFG->' + re.escape(name) + r'\s*=\s*([\'"])([^\'"]+)\1\s*;'
        m = re.search(pattern, content)
        return m.group(2) if m else None

    web_root = cfg.get('web_root') or os.path.dirname(config_path)
    dataroot = cfg.get('data_dir') or get_var('dataroot') or f'/var/serverData/{server_name}'
    dbname   = cfg.get('db_name')  or get_var('dbname')   or server_name
    dbuser   = get_var('dbuser') or ''
    dbpass   = get_var('dbpass') or ''
    prefix   = get_var('prefix') or 'mdl_'
    wwwroot  = get_var('wwwroot') or ''

    return {
        'web_root': web_root,
        'data_dir': dataroot,
        'db_name': dbname,
        'db_user': dbuser,
        'db_pass': dbpass,
        'table_prefix': prefix,
        'wwwroot': wwwroot,
        'config_file': config_path,
    }

# ----------------------------------------------------------------------
# Restore main function
# ----------------------------------------------------------------------
def restore(restore_location, verbose):
    print_info("Initiating restore sequence...")
    required = ['tar', 'xz', 'cp', 'chown', 'chmod', 'find', get_db_client()]
    check_required_commands(required, verbose)

    if not restore_location:
        latest = find_latest_backup()
        if not latest:
            print_error("No restore location provided and no existing backup found in default directory.")
            print_error(f"Default backup directory: {get_default_backup_dir()}")
            sys.exit(1)
        print_info(f"No restore location specified. Found latest backup: {latest}")
        response = print_prompt("Use this backup? (y/N): ").strip().lower()
        if response not in ('y', 'yes'):
            print_warning("Restore cancelled.")
            sys.exit(0)
        restore_location = latest

    if not os.path.isfile(restore_location):
        print_error(f"Restore location '{restore_location}' is not a valid file.")
        sys.exit(1)

    temp_dir = tempfile.mkdtemp(prefix='moodle_restore_')
    backup_root = tempfile.mkdtemp(prefix='moodle_restore_backup_')
    if verbose:
        print_verbose(f"Extracting to temporary directory: {temp_dir}")
        print_verbose(f"Existing files will be backed up to: {backup_root}")

    auth_args = None
    temp_auth_file = None

    def ensure_server_access():
        nonlocal auth_args, temp_auth_file
        if auth_args is not None:
            return auth_args
        success, err = test_db_connection([])
        if success:
            auth_args = []
            return auth_args
        if "Access denied" not in err:
            print_error(f"Cannot connect to database server: {err}")
            sys.exit(1)
        if verbose:
            print_verbose("Access denied for database server. Prompting for credentials.")
        auth_args, temp_auth_file = obtain_db_credentials(verbose)
        success, err = test_db_connection(auth_args)
        if not success:
            print_error(f"Authentication failed for database server: {err}")
            sys.exit(1)
        return auth_args

    try:
        if verbose:
            run_cmd_stream(['tar', '-C', temp_dir, '-xJvf', restore_location], verbose)
        else:
            run_cmd(['tar', '-C', temp_dir, '-xJf', restore_location], verbose)
        if verbose:
            print_verbose("Extraction complete.")

        ensure_server_access()
        policy = get_conflict_policy(verbose)

        # ========== PRIMARY SERVER RESTORE ==========
        primary_info = SERVERS[PRIMARY_SERVER]                 # for symlink name/target
        primary_cfg = load_server_config(PRIMARY_SERVER)       # full dynamic config
        server_backup = os.path.join(temp_dir, PRIMARY_SERVER)
        if not os.path.isdir(server_backup):
            print_error(f"Primary server '{PRIMARY_SERVER}' backup not found.")
            sys.exit(1)

        print_info(f"\nRestoring primary server: {PRIMARY_SERVER}")
        web_target = primary_cfg['web_root']
        web_backup = os.path.join(server_backup, 'html')
        if os.path.isdir(web_backup):
            if handle_existing(PRIMARY_SERVER, 'html', web_target, is_dir=True,
                               backup_dir=backup_root, policy=policy, verbose=verbose):
                os.makedirs(os.path.dirname(web_target), exist_ok=True)
                shutil.move(web_backup, web_target)
                if verbose:
                    print_verbose(f"Restored web root: {web_backup} -> {web_target}")
                restored_web_roots = {PRIMARY_SERVER: web_target}
            else:
                print_error("Primary web root restoration skipped – cannot proceed.")
                sys.exit(1)
        else:
            print_error(f"Web backup for primary server not found at {web_backup}")
            sys.exit(1)

        # Primary config
        cfg_backup = os.path.join(server_backup, f"config-{PRIMARY_SERVER}.php")
        cfg_target = primary_cfg['config_file']
        db_user = db_pass = None
        if os.path.isfile(cfg_backup):
            if handle_existing(PRIMARY_SERVER, 'config', cfg_target, is_dir=False,
                            backup_dir=backup_root, policy=policy, verbose=verbose):
                os.makedirs(os.path.dirname(cfg_target), exist_ok=True)
                # COPY instead of MOVE – keep the backup file in the temp directory
                shutil.copy2(cfg_backup, cfg_target)
                if verbose:
                    print_verbose(f"Restored config: {cfg_backup} -> {cfg_target}")

                # Extract credentials from the restored file (now at cfg_target)
                actual_dbname = get_dbname_from_config(cfg_target)
                if actual_dbname:
                    if verbose:
                        print_verbose(f"Extracted database name from config: {actual_dbname} (was {primary_cfg['db_name']})")
                    primary_cfg['db_name'] = actual_dbname
                else:
                    print_warning(f"Could not extract database name from {cfg_target}, using hardcoded value: {primary_cfg['db_name']}")

                db_user, db_pass = get_dbuser_pass_from_config(cfg_target)

                # Live config copy – only if it's a different location
                config_php = os.path.join(os.path.dirname(cfg_target), 'config.php')
                if config_php != cfg_target:
                    if handle_existing(PRIMARY_SERVER, 'config_live', config_php, is_dir=False,
                                    backup_dir=backup_root, policy=policy, verbose=verbose):
                        shutil.copy2(cfg_target, config_php)
                        if verbose:
                            print_verbose(f"Copied config to live file: {cfg_target} -> {config_php}")
                else:
                    if verbose:
                        print_verbose("Config file and live config.php are the same; no separate copy needed.")
        else:
            print_warning(f"Config backup for primary not found at {cfg_backup}")

        # Primary data
        data_backup = os.path.join(server_backup, 'data')
        data_target = primary_cfg['data_dir']
        if os.path.isdir(data_backup):
            if handle_existing(PRIMARY_SERVER, 'data', data_target, is_dir=True,
                               backup_dir=backup_root, policy=policy, verbose=verbose):
                os.makedirs(os.path.dirname(data_target), exist_ok=True)
                shutil.move(data_backup, data_target)
                if verbose:
                    print_verbose(f"Restored data dir: {data_backup} -> {data_target}")
        else:
            print_warning("Data backup for primary not found, skipping.")

        fix_permissions(PRIMARY_SERVER, primary_cfg, verbose)

        # Primary database
        db_backup = os.path.join(server_backup, 'db.sql')
        if os.path.isfile(db_backup):
            db_exists = database_exists(primary_cfg['db_name'], auth_args, verbose)
            proceed_with_db = True
            if db_exists:
                proceed_with_db = handle_existing_database(
                    primary_cfg['db_name'], auth_args, backup_root, PRIMARY_SERVER, policy, verbose
                )
            if proceed_with_db:
                create_database_if_not_exists(primary_cfg['db_name'], auth_args, verbose)
                client = get_db_client()
                db_cmd = [client] + auth_args + [f'--default-character-set={DB_CHARSET}', primary_cfg['db_name']]
                with open(db_backup, 'rb') as f:
                    run_cmd(db_cmd, verbose, stdin=f)
                if verbose:
                    print_verbose(f"Restored database '{primary_cfg['db_name']}' from {db_backup}")

                if db_user and db_pass:
                    grant_database_privileges(primary_cfg['db_name'], db_user, db_pass, auth_args, verbose)
                else:
                    print_warning(f"Could not extract dbuser/dbpass from config, skipping privilege grant for '{primary_cfg['db_name']}'.")
            else:
                print_info(f"Skipping database restore for '{primary_cfg['db_name']}' as per policy.")
        else:
            print_warning("Database dump for primary not found, skipping.")

        # Primary symlink
        link_name = os.path.join(WEB_PARENT, primary_info['symlink_name'])
        link_target = primary_info['symlink_target']
        if os.path.lexists(link_name):
            os.unlink(link_name)
        os.symlink(link_target, link_name)
        if verbose:
            print_verbose(f"Symlink created: {link_name} -> {link_target}")

        # ========== SECONDARY (EXAM) SERVER RESTORE ==========
        exam_info = SERVERS[SECONDARY_SERVER]
        exam_cfg = load_server_config(SECONDARY_SERVER)
        server_backup_exam = os.path.join(temp_dir, SECONDARY_SERVER)
        if not os.path.isdir(server_backup_exam):
            print_warning(f"Exam server backup not found, will try to clone from primary and restore config from backup if available.")
            os.makedirs(exam_cfg['web_root'], exist_ok=True)
            os.makedirs(exam_cfg['data_dir'], exist_ok=True)

        print_info(f"\nRestoring secondary server: {SECONDARY_SERVER}")

        # Clone exam web root from primary (if primary web root exists)
        if restored_web_roots.get(PRIMARY_SERVER) and os.path.isdir(restored_web_roots[PRIMARY_SERVER]):
            source_web = restored_web_roots[PRIMARY_SERVER]
            web_target_exam = exam_cfg['web_root']
            if handle_existing(SECONDARY_SERVER, 'html', web_target_exam, is_dir=True,
                               backup_dir=backup_root, policy=policy, verbose=verbose):
                print_info(f"Cloning {SECONDARY_SERVER} web root from {PRIMARY_SERVER}: {source_web} -> {web_target_exam}")
                os.makedirs(os.path.dirname(web_target_exam), exist_ok=True)
                if sys.version_info >= (3, 8):
                    shutil.copytree(source_web, web_target_exam, dirs_exist_ok=True, symlinks=True)
                else:
                    run_cmd(['cp', '-a', '--reflink=auto', source_web, web_target_exam], verbose)
                restored_web_roots[SECONDARY_SERVER] = web_target_exam
            else:
                print_warning("Exam web root cloning skipped – will attempt to restore from backup.")
                web_backup_exam = os.path.join(server_backup_exam, 'html')
                if os.path.isdir(web_backup_exam):
                    if handle_existing(SECONDARY_SERVER, 'html', web_target_exam, is_dir=True,
                                       backup_dir=backup_root, policy=policy, verbose=verbose):
                        os.makedirs(os.path.dirname(web_target_exam), exist_ok=True)
                        shutil.move(web_backup_exam, web_target_exam)
                        restored_web_roots[SECONDARY_SERVER] = web_target_exam
                    else:
                        print_warning("Exam web root not restored, will skip.")
                else:
                    print_warning("Exam web backup not found, cannot restore.")
        else:
            print_warning("Primary web root not available for cloning; will try to restore exam from backup.")
            web_backup_exam = os.path.join(server_backup_exam, 'html')
            if os.path.isdir(web_backup_exam):
                if handle_existing(SECONDARY_SERVER, 'html', web_target_exam, is_dir=True,
                                   backup_dir=backup_root, policy=policy, verbose=verbose):
                    os.makedirs(os.path.dirname(web_target_exam), exist_ok=True)
                    shutil.move(web_backup_exam, web_target_exam)
                    restored_web_roots[SECONDARY_SERVER] = web_target_exam
            else:
                print_warning("Exam web backup not found, skipping.")

        # Restore exam config from its own backup (if exists)
        cfg_backup_exam = os.path.join(server_backup_exam, f"config-{SECONDARY_SERVER}.php")
        exam_config_target = exam_cfg['config_file']
        os.makedirs(os.path.dirname(exam_config_target), exist_ok=True)

        if os.path.isfile(cfg_backup_exam):
            if handle_existing(SECONDARY_SERVER, 'config', exam_config_target, is_dir=False,
                            backup_dir=backup_root, policy=policy, verbose=verbose):
                os.makedirs(os.path.dirname(exam_config_target), exist_ok=True)
                # COPY instead of MOVE
                shutil.copy2(cfg_backup_exam, exam_config_target)
                if verbose:
                    print_verbose(f"Restored exam config from backup: {cfg_backup_exam} -> {exam_config_target}")

            # Extract credentials from the restored exam config
            actual_dbname = get_dbname_from_config(exam_config_target)
            if actual_dbname:
                if verbose:
                    print_verbose(f"Extracted database name from exam config: {actual_dbname} (was {exam_cfg['db_name']})")
                exam_cfg['db_name'] = actual_dbname
            else:
                print_warning(f"Could not extract database name from {exam_config_target}, using hardcoded value: {exam_cfg['db_name']}")

            db_user_exam, db_pass_exam = get_dbuser_pass_from_config(exam_config_target)

            config_php_exam = os.path.join(os.path.dirname(exam_config_target), 'config.php')
            if config_php_exam != exam_config_target:
                if handle_existing(SECONDARY_SERVER, 'config_live', config_php_exam, is_dir=False,
                                backup_dir=backup_root, policy=policy, verbose=verbose):
                    shutil.copy2(exam_config_target, config_php_exam)
                    if verbose:
                        print_verbose(f"Copied exam config to live file: {exam_config_target} -> {config_php_exam}")
            else:
                if verbose:
                    print_verbose("Exam config and live config.php are the same; no separate copy needed.")
        else:
            print_warning(f"Exam config file {exam_config_target} still missing. Database credentials will not be set automatically.")

        # Exam data directory
        data_backup_exam = os.path.join(server_backup_exam, 'data')
        data_target_exam = exam_cfg['data_dir']
        if os.path.isdir(data_backup_exam):
            if handle_existing(SECONDARY_SERVER, 'data', data_target_exam, is_dir=True,
                               backup_dir=backup_root, policy=policy, verbose=verbose):
                os.makedirs(os.path.dirname(data_target_exam), exist_ok=True)
                shutil.move(data_backup_exam, data_target_exam)
                if verbose:
                    print_verbose(f"Restored exam data dir: {data_backup_exam} -> {data_target_exam}")
        else:
            print_warning("Exam data backup not found, creating empty data directory.")
            os.makedirs(data_target_exam, exist_ok=True)

        fix_permissions(SECONDARY_SERVER, exam_cfg, verbose)

        # Exam database
        db_backup_exam = os.path.join(server_backup_exam, 'db.sql')
        if os.path.isfile(db_backup_exam):
            db_exists = database_exists(exam_cfg['db_name'], auth_args, verbose)
            proceed_with_db = True
            if db_exists:
                proceed_with_db = handle_existing_database(
                    exam_cfg['db_name'], auth_args, backup_root, SECONDARY_SERVER, policy, verbose
                )
            if proceed_with_db:
                create_database_if_not_exists(exam_cfg['db_name'], auth_args, verbose)
                client = get_db_client()
                db_cmd = [client] + auth_args + [f'--default-character-set={DB_CHARSET}', exam_cfg['db_name']]
                with open(db_backup_exam, 'rb') as f:
                    run_cmd(db_cmd, verbose, stdin=f)
                if verbose:
                    print_verbose(f"Restored exam database '{exam_cfg['db_name']}' from {db_backup_exam}")

                if db_user_exam and db_pass_exam:
                    grant_database_privileges(exam_cfg['db_name'], db_user_exam, db_pass_exam, auth_args, verbose)
                else:
                    print_warning(f"Could not extract dbuser/dbpass from exam config, skipping privilege grant for '{exam_cfg['db_name']}'.")
            else:
                print_info(f"Skipping exam database restore for '{exam_cfg['db_name']}' as per policy.")
        else:
            print_warning("Exam database dump not found, skipping.")

        # Exam symlink
        link_name_exam = os.path.join(WEB_PARENT, exam_info['symlink_name'])
        link_target_exam = exam_info['symlink_target']
        if os.path.lexists(link_name_exam):
            os.unlink(link_name_exam)
        os.symlink(link_target_exam, link_name_exam)
        if verbose:
            print_verbose(f"Symlink created: {link_name_exam} -> {link_target_exam}")

        print_success("\nRestore completed successfully.")
        if os.path.exists(backup_root) and os.listdir(backup_root):
            print_info(f"Original files that were backed up are preserved in: {backup_root}")
        else:
            shutil.rmtree(backup_root, ignore_errors=True)

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        if verbose:
            print_verbose(f"Removed temporary extraction directory: {temp_dir}")
        if temp_auth_file and os.path.exists(temp_auth_file):
            os.unlink(temp_auth_file)
            if verbose:
                print_verbose(f"Removed temporary database auth file: {temp_auth_file}")

# ----------------------------------------------------------------------
# Restore Previous (from backup directory) with charset and permissions
# ----------------------------------------------------------------------
def find_backup_dirs():
    pattern = '/tmp/moodle_restore_backup_*'
    dirs = glob.glob(pattern)
    dirs = [d for d in dirs if os.path.isdir(d)]
    dirs.sort(key=os.path.getmtime, reverse=True)
    return dirs

def restore_previous(backup_dir, verbose):
    print_info("Reverting to previous digital state...")

    if not backup_dir:
        dirs = find_backup_dirs()
        if not dirs:
            print_error("No previous backup directories found in /tmp.")
            sys.exit(1)
        if len(dirs) == 1:
            backup_dir = dirs[0]
            print_info(f"Using the only backup directory: {backup_dir}")
        else:
            print_info("Multiple previous backup directories found:")
            for i, d in enumerate(dirs, 1):
                mtime = datetime.datetime.fromtimestamp(os.path.getmtime(d)).strftime('%Y-%m-%d %H:%M:%S')
                print(f"  {i}. {d} (modified {mtime})")
            choice = print_prompt("Enter the number of the directory to use (default: 1): ").strip()
            if not choice:
                choice = '1'
            try:
                idx = int(choice) - 1
                if idx < 0 or idx >= len(dirs):
                    raise ValueError
                backup_dir = dirs[idx]
            except (ValueError, IndexError):
                print_error("Invalid selection.")
                sys.exit(1)
        print_info(f"Selected backup directory: {backup_dir}")

    if not os.path.isdir(backup_dir):
        print_error(f"Backup directory '{backup_dir}' does not exist or is not a directory.")
        sys.exit(1)

    temp_backup_root = tempfile.mkdtemp(prefix='moodle_restore_prev_backup_')
    print_info(f"Existing files will be backed up to (if policy 'backup' chosen): {temp_backup_root}")

    auth_args = None
    temp_auth_file = None

    def ensure_server_access():
        nonlocal auth_args, temp_auth_file
        if auth_args is not None:
            return auth_args
        success, err = test_db_connection([])
        if success:
            auth_args = []
            return auth_args
        if "Access denied" not in err:
            print_error(f"Cannot connect to database server: {err}")
            sys.exit(1)
        if verbose:
            print_verbose("Access denied for database server. Prompting for credentials.")
        auth_args, temp_auth_file = obtain_db_credentials(verbose)
        success, err = test_db_connection(auth_args)
        if not success:
            print_error(f"Authentication failed for database server: {err}")
            sys.exit(1)
        return auth_args

    try:
        ensure_server_access()
        policy = get_conflict_policy(verbose)

        for server_name, cfg in SERVERS.items():
            server_backup_dir = os.path.join(backup_dir, server_name)
            if not os.path.isdir(server_backup_dir):
                print_info(f"No backup found for server '{server_name}', skipping.")
                continue

            print_info(f"\nRestoring server: {server_name}")

            # Web root
            web_backup_path = os.path.join(server_backup_dir, 'html', os.path.basename(cfg['web_root']))
            if os.path.exists(web_backup_path):
                web_target = cfg['web_root']
                if handle_existing(server_name, 'html', web_target, is_dir=True,
                                   backup_dir=temp_backup_root, policy=policy, verbose=verbose):
                    os.makedirs(os.path.dirname(web_target), exist_ok=True)
                    shutil.move(web_backup_path, web_target)
                    print_info("Restored web root from backup.")
                else:
                    print_info("Skipped web root restore.")
            else:
                print("No web root backup found.")

            # Data directory
            data_backup_path = os.path.join(server_backup_dir, 'data', os.path.basename(cfg['data_dir']))
            if os.path.exists(data_backup_path):
                data_target = cfg['data_dir']
                if handle_existing(server_name, 'data', data_target, is_dir=True,
                                   backup_dir=temp_backup_root, policy=policy, verbose=verbose):
                    os.makedirs(os.path.dirname(data_target), exist_ok=True)
                    shutil.move(data_backup_path, data_target)
                    print_info("Restored data directory.")
                else:
                    print_info("Skipped data directory restore.")
            else:
                print("No data directory backup found.")

            # Config file
            config_backup = os.path.join(server_backup_dir, 'config', f"config-{server_name}.php")
            cfg_target = cfg['config_file']
            if os.path.isfile(config_backup):
                if handle_existing(server_name, 'config', cfg_target, is_dir=False,
                                   backup_dir=temp_backup_root, policy=policy, verbose=verbose):
                    os.makedirs(os.path.dirname(cfg_target), exist_ok=True)
                    shutil.move(config_backup, cfg_target)
                    print_info("Restored config file.")
                else:
                    print_info("Skipped config file restore.")
            else:
                print("No config file backup found.")

            # Live config.php
            config_live_backup = os.path.join(server_backup_dir, 'config_live', 'config.php')
            if os.path.isfile(config_live_backup):
                config_live_target = os.path.join(os.path.dirname(cfg['config_file']), 'config.php')
                if handle_existing(server_name, 'config_live', config_live_target, is_dir=False,
                                   backup_dir=temp_backup_root, policy=policy, verbose=verbose):
                    os.makedirs(os.path.dirname(config_live_target), exist_ok=True)
                    shutil.move(config_live_backup, config_live_target)
                    print_info("Restored live config.php.")
                else:
                    print_info("Skipped live config.php restore.")
            else:
                print("No live config.php backup found.")

            # Extract db credentials from the restored config file (if it exists)
            db_user = db_pass = None
            if os.path.isfile(cfg_target):
                db_user, db_pass = get_dbuser_pass_from_config(cfg_target)

            # Fix permissions (web root + data dir) only if needed
            fix_permissions(server_name, cfg, verbose)

            # Database
            db_backup_dir = os.path.join(server_backup_dir, 'db')
            if os.path.isdir(db_backup_dir):
                sql_files = glob.glob(os.path.join(db_backup_dir, f"{cfg['db_name']}_backup_*.sql"))
                if sql_files:
                    sql_files.sort(key=os.path.getmtime, reverse=True)
                    print(f"Database backups found for '{cfg['db_name']}':")
                    for i, f in enumerate(sql_files, 1):
                        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(f)).strftime('%Y-%m-%d %H:%M:%S')
                        print(f"  {i}. {f} (modified {mtime})")
                    choice = print_prompt("Enter the number to restore (or 0 to skip): ").strip()
                    if choice.isdigit() and int(choice) > 0:
                        idx = int(choice) - 1
                        if idx < len(sql_files):
                            selected = sql_files[idx]
                            db_exists = database_exists(cfg['db_name'], auth_args, verbose)
                            proceed = True
                            if db_exists:
                                proceed = handle_existing_database(
                                    cfg['db_name'], auth_args, temp_backup_root, server_name, policy, verbose
                                )
                            if proceed:
                                create_database_if_not_exists(cfg['db_name'], auth_args, verbose)
                                client = get_db_client()
                                db_cmd = [client] + auth_args + [f'--default-character-set={DB_CHARSET}', cfg['db_name']]
                                with open(selected, 'rb') as f:
                                    run_cmd(db_cmd, verbose, stdin=f)
                                print_info(f"Restored database from {selected}")

                                # Grant database privileges
                                if db_user and db_pass:
                                    grant_database_privileges(cfg['db_name'], db_user, db_pass, auth_args, verbose)
                                else:
                                    print_warning(f"Could not extract dbuser/dbpass from config, skipping privilege grant for '{cfg['db_name']}'.")
                        else:
                            print_warning("Invalid number, skipping database restore.")
                    else:
                        print("Skipping database restore.")
                else:
                    print("No SQL backup files found.")
            else:
                print("No database backup found.")

            # Symlink
            link_name = os.path.join(WEB_PARENT, cfg['symlink_name'])
            link_target = cfg['symlink_target']
            if os.path.lexists(link_name):
                os.unlink(link_name)
            os.symlink(link_target, link_name)
            if verbose:
                print_verbose(f"Symlink created: {link_name} -> {link_target}")

        print_success("\nRestore from previous backup completed.")
        if os.path.exists(temp_backup_root) and os.listdir(temp_backup_root):
            print_info(f"Original files that were backed up during this operation are preserved in: {temp_backup_root}")
        else:
            shutil.rmtree(temp_backup_root, ignore_errors=True)

    finally:
        if temp_auth_file and os.path.exists(temp_auth_file):
            os.unlink(temp_auth_file)
            if verbose:
                print_verbose(f"Removed temporary database auth file: {temp_auth_file}")

def print_summary(operation, start_time, servers, file_counts, db_sizes):
    elapsed = time.time() - start_time
    print(f"\n{COLOR_BOLD}{COLOR_CYAN}═══ {operation.upper()} SUMMARY ═══{COLOR_RESET}")
    for server in servers:
        print(f"  {COLOR_BOLD}{server}{COLOR_RESET}")
        print(f"    Web files: {file_counts[server]['web']}")
        print(f"    Data files: {file_counts[server]['data']}")
        print(f"    DB size: {db_sizes[server]}")
    print(f"{COLOR_DIM}Total time: {elapsed:.2f}s{COLOR_RESET}")

# ----------------------------------------------------------------------
# Main CLI
# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
# MENU SYSTEM (new)
# ----------------------------------------------------------------------
def _clear():
    os.system('clear' if os.name == 'posix' else 'cls')

def _choose_server():
    keys = list(SERVERS.keys())
    print("Choose a server:")
    for i, k in enumerate(keys, 1):
        marker = " (primary)" if k == PRIMARY_SERVER else (" (secondary)" if k == SECONDARY_SERVER else "")
        print(f"  {i}. {k}{marker}")
    print(f"  0. Back")
    c = print_prompt(f"Choice (1-{len(keys)}, default 1): ").strip() or "1"
    if c == "0":
        return None
    try:
        return keys[int(c) - 1]
    except (ValueError, IndexError):
        print_error("Invalid choice.")
        return None

def _menu_backup():
    _clear()
    print(f"{COLOR_BOLD}== BACKUP =={COLOR_RESET}")
    print(f"  1. Backup to default location (~/moodle_backup_<timestamp>.tar.xz)")
    print(f"  2. Backup to a custom location")
    print(f"  3. Backup with maintenance mode enabled first")
    print(f"  0. Back")
    c = print_prompt("Choice: ").strip()
    if c == "0" or not c:
        return
    loc = None
    maintenance = False
    if c == "2":
        loc = print_prompt("Full path for archive: ").strip()
        if not loc:
            print_warning("No location given, aborting.")
            return
    elif c == "3":
        maintenance = True
    try:
        if maintenance:
            set_maintenance(PRIMARY_SERVER, enable=True)
        backup(loc, verbose=False)
    finally:
        if maintenance:
            try:
                set_maintenance(PRIMARY_SERVER, enable=False)
            except SystemExit:
                print_warning("Could not disable maintenance mode — do it manually.")

def _menu_restore():
    _clear()
    print(f"{COLOR_BOLD}== RESTORE =={COLOR_RESET}")
    print(f"  1. Restore from latest backup (auto-detected in home dir)")
    print(f"  2. Restore from a specific archive path / directory")
    print(f"  0. Back")
    c = print_prompt("Choice: ").strip()
    if c == "0" or not c:
        return

    loc = None
    if c == "2":
        # Compute a sensible default from the invoking user's home directory
        latest = find_latest_backup()
        default_hint = latest if latest else os.path.join(
            get_default_backup_dir(), "moodle_backup_<timestamp>.tar.xz"
        )
        raw = print_prompt(
            f"Path to archive or directory\n"
            f"   (Enter = use default: {default_hint}\n"
            f"    type a directory to pick its newest moodle_backup_*.tar.xz)\n"
            f"❯ "
        ).strip()

        if not raw:
            # Empty -> use the auto-detected latest from home
            if not latest:
                print_warning(
                    f"No moodle_backup_*.tar.xz found in "
                    f"{get_default_backup_dir()} — aborting."
                )
                return
            loc = latest
            print_info(f"Using latest backup: {loc}")
        else:
            expanded = os.path.expanduser(raw)
            if os.path.isdir(expanded):
                # Directory given -> find the newest backup inside it
                files = sorted(
                    glob.glob(os.path.join(expanded, "moodle_backup_*.tar.xz")),
                    key=os.path.getmtime,
                    reverse=True,
                )
                if not files:
                    print_warning(
                        f"No moodle_backup_*.tar.xz found in {expanded}"
                    )
                    return
                loc = files[0]
                print_info(f"Using newest backup in directory: {loc}")
            else:
                loc = expanded

    # For choice 1, loc stays None -> restore() auto-detects + confirms
    restore(loc, verbose=False)

def _menu_update():
    _clear()
    srv = _choose_server()
    if not srv:
        return
    branch = print_prompt("Branch (leave blank for current): ").strip() or None
    maint = print_prompt("Enable maintenance mode during update? (Y/n): ").strip().lower()
    enable_maint = maint not in ("n", "no")
    update_moodle(srv, branch=branch, enable_maintenance=enable_maint, verbose=False)

def _menu_upgrade():
    _clear()
    srv = _choose_server()
    if not srv:
        return
    chk = print_prompt("Only check, do not run upgrade? (y/N): ").strip().lower()
    check_only = chk in ("y", "yes")
    upgrade_moodle(srv, check_only=check_only, verbose=False)

def _with_db_auth(fn):
    """Obtain DB admin creds once, run fn(auth_args), clean up temp file."""
    auth_args, temp_file = obtain_db_credentials(False)
    try:
        return fn(auth_args)
    finally:
        if temp_file and os.path.exists(temp_file):
            os.unlink(temp_file)

def _menu_maintenance():
    _clear()
    srv = _choose_server()
    if not srv:
        return
    print(f"  1. Enable")
    print(f"  2. Disable")
    print(f"  3. Status")
    print(f"  4. Custom message")
    print(f"  0. Back")
    c = print_prompt("Choice: ").strip()
    if c == "0" or not c:
        return

    def do(auth):
        if c == "1":
            allow = print_prompt("Allow admins to log in? (Y/n): ").strip().lower() not in ("n", "no")
            set_config_value(srv, 'maintenance_enabled', True, auth)
            set_config_value(srv, 'maintenance_allow_admins', allow, auth)
            print_success("Maintenance enabled.")
        elif c == "2":
            set_config_value(srv, 'maintenance_enabled', False, auth)
            print_success("Maintenance disabled.")
        elif c == "3":
            settings = get_maintenance_settings(srv, auth)
            print(json.dumps(settings, indent=2))
        elif c == "4":
            msg = print_prompt("Custom message: ").strip()
            if msg:
                set_config_value(srv, 'maintenance_message', msg, auth)
                print_success("Message set.")
    _with_db_auth(do)

def _menu_cron():
    _clear()
    srv = _choose_server()
    if not srv:
        return
    print(f"  1. Run cron now")
    print(f"  2. Install / update crontab entry")
    print(f"  0. Back")
    c = print_prompt("Choice: ").strip()
    if c == "0" or not c:
        return
    if c == "1":
        run_cron(srv)
    elif c == "2":
        sched = print_prompt("Schedule (default '* * * * *'): ").strip() or "* * * * *"
        setup_cron_job(srv, sched, verbose=False)

def _menu_status():
    _clear()
    srv = _choose_server()
    if not srv:
        return
    show_status(srv)

def _menu_config():
    _clear()
    print(f"{COLOR_BOLD}== CONFIG MANAGEMENT =={COLOR_RESET}")
    print(f"  File: {get_config_path()}")
    print(f"  1. View current config")
    print(f"  2. Edit a value (dot-path, e.g. servers.psc.config_file)")
    print(f"  3. Reset to defaults (destructive)")
    print(f"  4. Show file path")
    print(f"  5. Recreate file if missing")
    print(f"  0. Back")
    c = print_prompt("Choice: ").strip()
    if c == "1":
        print(json.dumps(CONFIG, indent=2))
    elif c == "2":
        print("Current config:")
        print(json.dumps(CONFIG, indent=2))
        path = print_prompt("Dot-path to edit: ").strip()
        if not path:
            return
        keys = path.split(".")
        node = CONFIG
        for k in keys[:-1]:
            if isinstance(node, dict) and k in node:
                node = node[k]
            else:
                print_error(f"Invalid path: {path}")
                return
        last = keys[-1]
        if not (isinstance(node, dict) and last in node):
            print_error(f"Key not found: {path}")
            return
        cur = node[last]
        raw = print_prompt(f"New value (current: {cur!r}): ").strip()
        try:
            new_val = json.loads(raw)
        except json.JSONDecodeError:
            new_val = raw
        node[last] = new_val
        save_config(CONFIG)
        print_success(f"Updated {path} = {new_val!r}")
        print_warning("Restart the script for module-level changes to take effect.")
    elif c == "3":
        if print_prompt("Really reset to defaults? (y/N): ").strip().lower() in ("y", "yes"):
            save_config(DEFAULT_CONFIG)
            print_success("Config reset. Restart the script.")
    elif c == "4":
        print(get_config_path())
    elif c == "5":
        if os.path.exists(get_config_path()):
            print_warning("File already exists.")
        else:
            save_config(DEFAULT_CONFIG)
            print_success(f"Created {get_config_path()}")

def run_menu():
    _clear()
    print(f"{COLOR_BOLD}{COLOR_MAGENTA} MOODLE NINJA – MAIN MENU {COLOR_RESET}")
    while True:
        print()
        print(f"  {COLOR_CYAN}1{COLOR_RESET}. Backup")
        print(f"  {COLOR_CYAN}2{COLOR_RESET}. Restore")
        print(f"  {COLOR_CYAN}3{COLOR_RESET}. Restore previous (from /tmp restore dirs)")
        print(f"  {COLOR_CYAN}4{COLOR_RESET}. Update code + DB upgrade (git)")
        print(f"  {COLOR_CYAN}5{COLOR_RESET}. Upgrade (DB only)")
        print(f"  {COLOR_CYAN}6{COLOR_RESET}. Maintenance mode")
        print(f"  {COLOR_CYAN}7{COLOR_RESET}. Cron")
        print(f"  {COLOR_CYAN}8{COLOR_RESET}. Status")
        print(f"  {COLOR_CYAN}9{COLOR_RESET}. Config management")
        print(f"  {COLOR_CYAN}0{COLOR_RESET}. Exit")
        c = print_prompt("Choice: ").strip()
        try:
            if   c == "1": _menu_backup()
            elif c == "2": _menu_restore()
            elif c == "3":
                rp_parser = argparse.Namespace(backup_dir=None, verbose=False)
                restore_previous(None, False)
            elif c == "4": _menu_update()
            elif c == "5": _menu_upgrade()
            elif c == "6": _menu_maintenance()
            elif c == "7": _menu_cron()
            elif c == "8": _menu_status()
            elif c == "9": _menu_config()
            elif c in ("0", "q", "quit", "exit"):
                print_success("Bye.")
                return
            else:
                print_warning("Invalid choice.")
        except KeyboardInterrupt:
            print("\nAborted.")
            return
        except SystemExit:
            raise
        except Exception as e:
            print_error(f"Unexpected error: {e}")

# ----------------------------------------------------------------------
# Original CLI entry point (kept for scripting / cron)
# ----------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Backup, restore, upgrade, and manage Moodle servers (primary and exam)."
    )
    subparsers = parser.add_subparsers(dest='command', required=True, help='Subcommands')

    backup_parser = subparsers.add_parser('backup', help='Create a backup')
    backup_parser.add_argument('-l', '--backup-location', help='Full path where the backup archive will be saved')
    backup_parser.add_argument('--maintenance', action='store_true', help='Enable maintenance mode before backup')
    backup_parser.add_argument('-v', '--verbose', action='store_true')

    restore_parser = subparsers.add_parser('restore', help='Restore from a backup archive')
    restore_parser.add_argument('-r', '--restore-location', help='Full path to the backup archive to restore')
    restore_parser.add_argument('--maintenance', action='store_true', help='Enable maintenance mode before restore')
    restore_parser.add_argument('-v', '--verbose', action='store_true')

    upgrade_parser = subparsers.add_parser('upgrade', help='Upgrade Moodle')
    upgrade_parser.add_argument('--server', choices=SERVERS.keys(), default=PRIMARY_SERVER)
    upgrade_parser.add_argument('--check', action='store_true')
    upgrade_parser.add_argument('-v', '--verbose', action='store_true')

    maint_parser = subparsers.add_parser('maintenance', help='Manage maintenance mode')
    maint_parser.add_argument('--enable', action='store_true')
    maint_parser.add_argument('--disable', action='store_true')
    maint_parser.add_argument('--status', action='store_true')
    maint_parser.add_argument('--allow-admins', dest='no_admins', action='store_false')
    maint_parser.add_argument('--no-admins', dest='no_admins', action='store_true')
    maint_parser.add_argument('--message')
    maint_parser.add_argument('--server', choices=SERVERS.keys(), default=PRIMARY_SERVER)
    maint_parser.add_argument('-v', '--verbose', action='store_true')

    cron_parser = subparsers.add_parser('cron', help='Run Moodle cron')
    cron_parser.add_argument('--server', choices=SERVERS.keys(), default=PRIMARY_SERVER)
    cron_parser.add_argument('--setup', nargs='?', const='* * * * *')
    cron_parser.add_argument('-v', '--verbose', action='store_true')

    status_parser = subparsers.add_parser('status', help='Show Moodle status')
    status_parser.add_argument('--server', choices=SERVERS.keys(), default=PRIMARY_SERVER)
    status_parser.add_argument('-v', '--verbose', action='store_true')

    rp_parser = subparsers.add_parser('restore-previous', aliases=['rp'])
    rp_parser.add_argument('-d', '--backup-dir')
    rp_parser.add_argument('-v', '--verbose', action='store_true')

    update_parser = subparsers.add_parser('update', help='Update Moodle code from git and run DB upgrade')
    update_parser.add_argument('--server', choices=SERVERS.keys(), default=PRIMARY_SERVER)
    update_parser.add_argument('--branch')
    update_parser.add_argument('--no-maintenance', action='store_true')
    update_parser.add_argument('-v', '--verbose', action='store_true')

    # config management for CLI too
    cfg_parser = subparsers.add_parser('config', help='Manage config.json')
    cfg_parser.add_argument('--show', action='store_true', help='Print current config')
    cfg_parser.add_argument('--path', action='store_true', help='Print config file path')
    cfg_parser.add_argument('--reset', action='store_true', help='Reset to defaults')

    args = parser.parse_args()

    if args.command == 'config':
        if args.path:
            print(get_config_path())
        elif args.reset:
            save_config(DEFAULT_CONFIG)
            print_success("Config reset.")
        else:
            print(json.dumps(CONFIG, indent=2))
        return

    if args.command == 'backup':
        backup(args.backup_location, args.verbose)
    elif args.command == 'restore':
        restore(args.restore_location, args.verbose)
    elif args.command in ('restore-previous', 'rp'):
        restore_previous(args.backup_dir, args.verbose)
    elif args.command == 'upgrade':
        upgrade_moodle(args.server, args.check, args.verbose)
    elif args.command == 'maintenance':
        auth_args, temp_auth_file = obtain_db_credentials(args.verbose)
        try:
            if args.status:
                print(json.dumps(get_maintenance_settings(args.server, auth_args, args.verbose), indent=2))
            elif args.enable:
                set_config_value(args.server, 'maintenance_enabled', 1, auth_args, args.verbose)
                allow = not getattr(args, 'no_admins', False)
                set_config_value(args.server, 'maintenance_allow_admins', allow, auth_args, args.verbose)
                if args.message:
                    set_config_value(args.server, 'maintenance_message', args.message, auth_args, args.verbose)
                print_success("Maintenance mode enabled.")
            elif args.disable:
                set_config_value(args.server, 'maintenance_enabled', 0, auth_args, args.verbose)
                print_success("Maintenance mode disabled.")
        finally:
            if temp_auth_file and os.path.exists(temp_auth_file):
                os.unlink(temp_auth_file)
    elif args.command == 'cron':
        if args.setup:
            setup_cron_job(args.server, args.setup, args.verbose)
        else:
            run_cron(args.server, args.verbose)
    elif args.command == 'status':
        show_status(args.server, args.verbose)
    elif args.command == 'update':
        update_moodle(args.server, branch=args.branch,
                      enable_maintenance=not args.no_maintenance, verbose=args.verbose)
    else:
        parser.print_help()
        sys.exit(1)

if __name__ == '__main__':
    if os.geteuid() != 0:
        print_error("This script must be run as root (sudo).")
        sys.exit(1)

    if len(sys.argv) == 1:
        # No arguments → interactive menu (the default experience)
        matrix_intro()
        run_menu()
    else:
        # Arguments given → classic CLI mode (for cron / scripting)
        matrix_intro()
        main()
