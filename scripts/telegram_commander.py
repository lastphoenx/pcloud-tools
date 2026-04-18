#!/usr/bin/env python3
"""
Telegram Commander - Backup Trigger via Bot
=====================================================
Listens for commands from a whitelisted Telegram chat and
triggers backup operations on the local system.

Security model:
  - Only responds to chat IDs listed in ALLOWED_CHAT_IDS
  - All other messages are silently ignored
  - Bot token read from env / config file (never hardcoded)
  - Commands run as the user this script runs as (configure systemd User=)
  - Outbound-only connection to Telegram API (no open ports)

Commands:
  /status   - Return current status.json summary
  /backup   - Trigger backup-pipeline.service (systemctl start)
  /help     - List available commands

Setup:
  1. Create bot via @BotFather, get token
  2. Get your chat_id: send /start to bot, then:
       curl https://api.telegram.org/bot<TOKEN>/getUpdates
  3. Copy config:
       cp /opt/apps/pcloud-tools/main/scripts/telegram_commander.conf.example \
          /etc/pcloud-tools/telegram_commander.conf
  4. Edit conf, set BOT_TOKEN and ALLOWED_CHAT_IDS
  5. systemctl enable --now telegram-commander

Dependencies:
  pip install requests
  (no python-telegram-bot needed - uses raw Bot API long-polling)
=====================================================
"""

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: 'requests' not installed. Run: pip install requests", file=sys.stderr)
    sys.exit(1)

# ── Configuration ─────────────────────────────────────────────────────────────

def load_config() -> dict:
    """Load config from env or config file. Env vars override file."""
    conf = {}

    # Config file locations (first match wins)
    config_paths = [
        Path("/etc/pcloud-tools/telegram_commander.conf"),
        Path("/opt/apps/pcloud-tools/main/scripts/telegram_commander.conf"),
        Path(Path(__file__).parent / "telegram_commander.conf"),
    ]
    for path in config_paths:
        if path.exists():
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, _, v = line.partition("=")
                        conf[k.strip()] = v.strip().strip('"').strip("'")
            break

    # Environment overrides
    for key in ("BOT_TOKEN", "ALLOWED_CHAT_IDS", "STATUS_JSON", "BACKUP_WRAPPER",
                "LOG_LEVEL", "POLL_TIMEOUT"):
        if key in os.environ:
            conf[key] = os.environ[key]

    return conf


CONFIG = load_config()

BOT_TOKEN        = CONFIG.get("BOT_TOKEN", "")
ALLOWED_CHAT_IDS = set(
    int(x.strip()) for x in CONFIG.get("ALLOWED_CHAT_IDS", "").split(",") if x.strip()
)
STATUS_JSON      = CONFIG.get("STATUS_JSON",   "/opt/apps/monitoring/status.json")
BACKUP_WRAPPER   = CONFIG.get("BACKUP_WRAPPER",
                               "/opt/apps/pcloud-tools/main/rtb/rtb_wrapper.sh")
LOG_LEVEL        = CONFIG.get("LOG_LEVEL", "INFO").upper()
POLL_TIMEOUT     = int(CONFIG.get("POLL_TIMEOUT", "30"))

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("telegram-commander")

# ── Validate config ───────────────────────────────────────────────────────────

def validate_config() -> None:
    if not BOT_TOKEN:
        log.error("BOT_TOKEN not set. Configure /etc/pcloud-tools/telegram_commander.conf")
        sys.exit(1)
    if not ALLOWED_CHAT_IDS:
        log.error("ALLOWED_CHAT_IDS not set. At least one chat_id required.")
        sys.exit(1)
    log.info("Config loaded. Allowed chat IDs: %s", ALLOWED_CHAT_IDS)

# ── Telegram API helpers ──────────────────────────────────────────────────────

API_BASE = "https://api.telegram.org/bot{token}/{method}"

def api(method: str, **params) -> dict:
    """Call Telegram Bot API. Returns parsed JSON response."""
    url = API_BASE.format(token=BOT_TOKEN, method=method)
    try:
        r = requests.post(url, json=params, timeout=35)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        log.warning("API call %s failed: %s", method, e)
        return {}


def send(chat_id: int, text: str, parse_mode: str = "HTML") -> None:
    """Send a message to a chat."""
    api("sendMessage", chat_id=chat_id, text=text, parse_mode=parse_mode)


def get_updates(offset: int) -> list:
    """Long-poll for new updates."""
    data = api("getUpdates", offset=offset, timeout=POLL_TIMEOUT,
               allowed_updates=["message"])
    return data.get("result", [])

# ── Command handlers ──────────────────────────────────────────────────────────

def cmd_help(chat_id: int) -> None:
    send(chat_id, (
        "<b>Backup Commander</b>\n\n"
        "/status  — Aktuellen System-Status anzeigen\n"
        "/backup  — Backup manuell anstoßen (backup-pipeline)\n"
        "/help    — Befehle anzeigen"
    ))


def cmd_status(chat_id: int) -> None:
    try:
        with open(STATUS_JSON) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        send(chat_id, f"⚠️ status.json nicht lesbar: {e}")
        return

    overall = data.get("overall_status", "UNKNOWN")
    ts      = data.get("timestamp", "—")
    host    = data.get("hostname", "—")

    emoji = {"OK": "✅", "WARNING": "⚠️", "CRITICAL": "🚨", "RUNNING": "🔄"}.get(overall, "❓")

    lines = [f"{emoji} <b>{overall}</b> — {host}  <i>{ts}</i>\n"]

    # RTB
    rtb = (data.get("scripts") or {}).get("rtb_wrapper") or {}
    if rtb:
        sg = rtb.get("live_safety_gate") or rtb.get("safety_gate") or "—"
        lines.append(f"🔄 RTB: <b>{rtb.get('status','?')}</b>  last: {rtb.get('last_run','—')}  SG: {sg}")

    # pCloud
    pc = (data.get("scripts") or {}).get("pcloud_backup") or {}
    if pc:
        lines.append(f"☁️ pCloud: <b>{pc.get('status_text','?')}</b>")

    # Failed / blocked services
    for name, svc in (data.get("services") or {}).items():
        st = svc.get("status", "")
        ec = svc.get("exit_code", "")
        if st == "failed":
            lines.append(f"❌ {name}: failed")
        elif "blocked" in str(ec):
            lines.append(f"⛔ {name}: blocked (Safety-Gate)")

    send(chat_id, "\n".join(lines))


def cmd_backup(chat_id: int) -> None:
    """Trigger backup-pipeline via systemctl start (non-blocking)."""
    # Safety check: refuse if Safety-Gate is RED
    try:
        with open(STATUS_JSON) as f:
            data = json.load(f)
        rtb = (data.get("scripts") or {}).get("rtb_wrapper") or {}
        sg  = rtb.get("live_safety_gate", "")
        if sg == "RED":
            send(chat_id,
                 "🚨 <b>Backup verweigert</b>\n"
                 "Safety-Gate ist aktuell <b>RED</b> (Ransomware-Verdacht).\n"
                 "Backup nicht möglich. Bitte System prüfen!")
            return
        if sg == "YELLOW":
            send(chat_id,
                 "⚠️ <b>Achtung: Safety-Gate YELLOW</b>\n"
                 "Starte trotzdem — backup-pipeline entscheidet selbst ob es läuft.")
    except Exception:
        pass  # If status.json unreadable, proceed anyway

    send(chat_id, "🔄 Starte backup-pipeline … (systemctl start backup-pipeline.service)")
    log.info("User %s triggered backup-pipeline", chat_id)

    try:
        result = subprocess.run(
            ["systemctl", "start", "backup-pipeline.service"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            send(chat_id,
                 "✅ <b>Backup gestartet</b>\n"
                 "backup-pipeline.service wurde gestartet.\n"
                 "Verwende /status in ein paar Minuten für das Ergebnis.")
        else:
            err = (result.stderr or result.stdout or "kein Output").strip()[:300]
            send(chat_id, f"❌ <b>Fehler beim Start</b>\n<code>{err}</code>")
            log.error("systemctl start failed: %s", err)
    except subprocess.TimeoutExpired:
        send(chat_id, "⚠️ Timeout beim Starten des Services. Bitte /status prüfen.")
    except PermissionError:
        send(chat_id,
             "❌ <b>Berechtigung fehlt</b>\n"
             "telegram_commander.service braucht sudo-Recht für systemctl start.\n"
             "Siehe README: /etc/sudoers.d/telegram-commander")
    except FileNotFoundError:
        send(chat_id, "❌ systemctl nicht gefunden. Läuft dieser Dienst auf systemd?")

# ── Main dispatch ─────────────────────────────────────────────────────────────

COMMANDS = {
    "/start":  cmd_help,
    "/help":   cmd_help,
    "/status": cmd_status,
    "/backup": cmd_backup,
}


def handle_update(update: dict) -> None:
    msg     = update.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    text    = (msg.get("text") or "").strip()
    user    = (msg.get("from") or {}).get("username", "unknown")

    if not chat_id or not text:
        return

    # Security: hard-reject unknown senders — silent drop
    if chat_id not in ALLOWED_CHAT_IDS:
        log.warning("Rejected message from chat_id=%s user=%s: %r", chat_id, user, text)
        return

    # Strip bot-name suffix (e.g. /backup@MyBot → /backup)
    cmd = text.split()[0].split("@")[0].lower()

    log.info("Command from chat_id=%s user=%s: %s", chat_id, user, cmd)

    handler = COMMANDS.get(cmd)
    if handler:
        try:
            handler(chat_id)
        except Exception as e:
            log.exception("Handler %s failed: %s", cmd, e)
            send(chat_id, f"⚠️ Interner Fehler: {e}")
    else:
        send(chat_id,
             f"Unbekannter Befehl: <code>{cmd}</code>\n/help für eine Liste.")

# ── Long-polling loop ─────────────────────────────────────────────────────────

def run() -> None:
    validate_config()

    # Verify token works
    me = api("getMe")
    bot_name = (me.get("result") or {}).get("username", "?")
    log.info("Connected as @%s", bot_name)

    # Skip old updates on startup (offset = -1 trick)
    updates = get_updates(offset=-1)
    offset  = max((u["update_id"] for u in updates), default=0) + 1 if updates else 0
    log.info("Starting poll loop (offset=%d)", offset)

    while True:
        try:
            updates = get_updates(offset=offset)
            for upd in updates:
                handle_update(upd)
                offset = upd["update_id"] + 1
        except KeyboardInterrupt:
            log.info("Shutting down.")
            break
        except Exception as e:
            log.error("Poll loop error: %s — retrying in 5s", e)
            time.sleep(5)


if __name__ == "__main__":
    run()
