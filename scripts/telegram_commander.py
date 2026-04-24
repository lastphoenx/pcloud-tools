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
  /logs     - Show systemd service logs (backup-pipeline, rtb, pcloud)
  /reset_safety_gate - Reset Safety-Gate to GREEN (requires confirmation)
  /help     - List available commands

Features:
  - Interactive inline keyboard (buttons)
  - Callback query support for button clicks
  - Live log viewing without SSH
  - Safety-Gate reset with confirmation

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

    # Fallback: Read from apprise.yml if BOT_TOKEN not set
    if not conf.get("BOT_TOKEN"):
        apprise_paths = [
            Path("/opt/apps/apprise.yml"),
            Path("/etc/pcloud-tools/apprise.yml"),
        ]
        for apprise_path in apprise_paths:
            if apprise_path.exists():
                try:
                    with open(apprise_path) as f:
                        for line in f:
                            # Format: tgram://BOTTOKEN/CHATID/
                            if "tgram://" in line:
                                import re
                                match = re.search(r'tgram://([^/]+)/([0-9]+)', line)
                                if match:
                                    conf["BOT_TOKEN"] = match.group(1)
                                    if not conf.get("ALLOWED_CHAT_IDS"):
                                        conf["ALLOWED_CHAT_IDS"] = match.group(2)
                                    break
                except Exception as e:
                    pass  # Ignore apprise.yml parsing errors

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
REPORTS_JSON     = CONFIG.get("REPORTS_JSON",  "/opt/apps/monitoring/reports.json")
BACKUP_WRAPPER   = CONFIG.get("BACKUP_WRAPPER",
                               "/opt/apps/pcloud-tools/main/rtb/rtb_wrapper.sh")
SAFETY_GATE_SCRIPT = CONFIG.get("SAFETY_GATE_SCRIPT",
                                 "/opt/apps/entropywatcher/main/safety_gate.sh")
FORECAST_SCRIPT    = CONFIG.get("FORECAST_SCRIPT",
                                 "/opt/apps/entropywatcher/main/scripts/forecast_safety_gate.sh")
SAFETY_GATE_SCRIPT = CONFIG.get("SAFETY_GATE_SCRIPT",
                                 "/opt/apps/entropywatcher/main/safety_gate.sh")
FORECAST_SCRIPT    = CONFIG.get("FORECAST_SCRIPT",
                                 "/opt/apps/entropywatcher/main/scripts/forecast_safety_gate.sh")
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

# Rate-limiting & backoff state
LAST_API_CALL_TIME = 0
MIN_API_DELAY = 0.5  # Minimum 500ms between API calls
CONSECUTIVE_ERRORS = 0
MAX_CONSECUTIVE_ERRORS = 10  # After this, wait 60s before retrying

def api(method: str, max_retries: int = 3, **params) -> dict:
    """Call Telegram Bot API with rate-limiting and exponential backoff."""
    global LAST_API_CALL_TIME, CONSECUTIVE_ERRORS
    
    # Rate-limit: enforce minimum delay between API calls
    now = time.time()
    elapsed = now - LAST_API_CALL_TIME
    if elapsed < MIN_API_DELAY:
        time.sleep(MIN_API_DELAY - elapsed)
    
    url = API_BASE.format(token=BOT_TOKEN, method=method)
    
    for attempt in range(max_retries):
        try:
            LAST_API_CALL_TIME = time.time()
            r = requests.post(url, json=params, timeout=35)
            r.raise_for_status()
            CONSECUTIVE_ERRORS = 0  # Reset error counter on success
            return r.json()
        except requests.Timeout:
            log.warning("API call %s timed out (attempt %d/%d)", method, attempt+1, max_retries)
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)  # Exponential backoff: 1s, 2s, 4s
        except requests.ConnectionError as e:
            log.warning("API call %s connection error: %s (attempt %d/%d)", method, e, attempt+1, max_retries)
            CONSECUTIVE_ERRORS += 1
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
        except requests.HTTPError as e:
            # HTTP errors (4xx, 5xx) - don't retry client errors
            if e.response.status_code >= 500:
                log.warning("API call %s HTTP %d (attempt %d/%d)", method, e.response.status_code, attempt+1, max_retries)
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
            else:
                log.error("API call %s failed with HTTP %d: %s", method, e.response.status_code, e)
                CONSECUTIVE_ERRORS += 1
                return {}
        except requests.RequestException as e:
            log.warning("API call %s failed: %s (attempt %d/%d)", method, e, attempt+1, max_retries)
            CONSECUTIVE_ERRORS += 1
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
    
    # All retries exhausted
    log.error("API call %s failed after %d attempts", method, max_retries)
    CONSECUTIVE_ERRORS += 1
    return {}


def send(chat_id: int, text: str, parse_mode: str = "HTML", reply_markup: dict = None) -> None:
    """Send a message to a chat, optionally with inline keyboard."""
    params = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        params["reply_markup"] = reply_markup
    api("sendMessage", **params)


def edit_message(chat_id: int, message_id: int, text: str, parse_mode: str = "HTML", reply_markup: dict = None) -> None:
    """Edit an existing message (for callback updates)."""
    params = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        params["reply_markup"] = reply_markup
    api("editMessageText", **params)


def answer_callback(callback_query_id: str, text: str = None) -> None:
    """Answer a callback query (removes loading indicator)."""
    params = {"callback_query_id": callback_query_id}
    if text:
        params["text"] = text
    api("answerCallbackQuery", **params)


def get_updates(offset: int) -> list:
    """Long-poll for new updates (messages + callback queries)."""
    data = api("getUpdates", offset=offset, timeout=POLL_TIMEOUT,
               allowed_updates=["message", "callback_query"])
    return data.get("result", [])

# ── Command handlers ──────────────────────────────────────────────────────────

def load_data():
    """Load status.json and reports.json (like dashboard). Returns (status, reports)."""
    try:
        with open(STATUS_JSON) as f:
            status = json.load(f)
    except Exception:
        status = None
    
    try:
        with open(REPORTS_JSON) as f:
            reports = json.load(f)
    except Exception:
        reports = None
    
    return status, reports

def get_main_keyboard(dashboard_url: str = None) -> dict:
    """Return the main inline keyboard with action buttons."""
    keyboard = [
        [{"text": "📊 Status", "callback_data": "cmd_status"}],
        [{"text": "🔄 Backup starten", "callback_data": "cmd_backup"}],
        [{"text": "📜 Logs anzeigen", "callback_data": "menu_logs"}],
        [{"text": "🔓 Safety-Gate", "callback_data": "menu_safety_gate"}],
        [{"text": "🧬 Malware & Integrität", "callback_data": "cmd_malware"}],
    ]
    
    if dashboard_url:
        keyboard.append([{"text": "🌐 Dashboard öffnen", "url": dashboard_url}])
    
    keyboard.append([{"text": "❓ Hilfe", "callback_data": "cmd_help"}])
    
    return {"inline_keyboard": keyboard}


def get_logs_keyboard() -> dict:
    """Return keyboard for log selection."""
    return {
        "inline_keyboard": [
            [{"text": "📜 backup-pipeline", "callback_data": "logs_backup-pipeline"}],
            [{"text": "📜 RTB", "callback_data": "logs_rtb_wrapper"}],
            [{"text": "📜 pCloud", "callback_data": "logs_pcloud"}],
            [{"text": "📜 Telegram Commander", "callback_data": "logs_telegram-commander"}],
            [{"text": "🔙 Zurück", "callback_data": "menu_main"}]
        ]
    }


def get_safety_gate_keyboard() -> dict:
    """Return keyboard for safety gate actions."""
    return {
        "inline_keyboard": [
            [{"text": "🔍 LIVE Status prüfen", "callback_data": "sg_check_live"}],
            [{"text": "🔮 Forecast anzeigen", "callback_data": "sg_forecast"}],
            [{"text": "🔓 Reset zu GREEN", "callback_data": "sg_reset_confirm"}],
            [{"text": "🔙 Zurück", "callback_data": "menu_main"}]
        ]
    }


def cmd_help(chat_id: int, show_keyboard: bool = True) -> None:
    """Show help text with optional main menu keyboard."""
    text = (
        "<b>🤖 Backup Commander</b>\n\n"
        "<b>Befehle:</b>\n"
        "/status — System-Status anzeigen\n"
        "/backup — Backup manuell anstoßen\n"
        "/logs — Service-Logs anzeigen\n"
        "/reset_safety_gate — Safety-Gate zurücksetzen\n"
        "/menu — Haupt-Menü anzeigen\n"
        "/help — Diese Hilfe\n\n"
        "<b>💡 Tipp:</b> Nutze die Buttons unten für schnelle Aktionen!"
    )
    keyboard = get_main_keyboard() if show_keyboard else None
    send(chat_id, text, reply_markup=keyboard)


def cmd_status(chat_id: int) -> None:
    """Show comprehensive system status matching dashboard (uses status.json + reports.json)"""
    status, reports = load_data()
    
    if not status:
        send(chat_id, "⚠️ status.json nicht lesbar")
        return

    # ── Header: Overall Status ──
    overall = (status.get("overall_status", "UNKNOWN") or "UNKNOWN").upper()
    host    = status.get("hostname", "—")
    dashboard_url = status.get("dashboard_url")
    
    emoji_map = {"OK": "✅", "WARNING": "⚠️", "CRITICAL": "🚨", "RUNNING": "🔄"}
    emoji = emoji_map.get(overall, "❓")
    
    lines = [
        f"<b>{emoji} {overall}</b>",
        f"<i>{host}</i>",
        "━━━━━━━━━━━━━━━━━\n"
    ]

    # ── Backup-Status (RTB + pCloud + Safety-Gate) ──
    rtb = (status.get("scripts") or {}).get("rtb_wrapper") or {}
    pc  = (status.get("scripts") or {}).get("pcloud_backup") or {}
    
    # RTB Status
    rtb_status = rtb.get("status", "unknown")
    rtb_emoji = {"success": "✅", "blocked": "⛔", "skipped": "⏭️", "failed": "❌", "running": "🔄"}.get(rtb_status, "❓")
    
    # pCloud Status  
    pc_code = pc.get("status_code", 3)
    pc_ok = pc_code == 0
    pc_emoji = "✅" if pc_ok else "⚠️" if pc_code == 1 else "❌"
    
    # Safety-Gate (LIVE if available!)
    sg_live = rtb.get("live_safety_gate", "")
    sg_hist = rtb.get("safety_gate", "")
    sg = sg_live or sg_hist or "—"
    sg_emoji = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}.get(sg, "⚪")
    sg_label = " (live)" if sg_live else ""
    
    lines.append(f"<b>🔄 Backup-Status</b>")
    lines.append(f"RTB      {rtb_emoji}  pCloud {pc_emoji}  Safety {sg_emoji} {sg}{sg_label}\n")
    
    # RTB Details
    lines.append(f"<b>Letzter Lauf:</b> {rtb.get('last_run', '—')[:16]}")
    lines.append(f"<b>RTB Snapshots:</b> {rtb.get('snapshot_count', 0)}")
    
    # Next backup forecast (from dry_run_result)
    dry_run = rtb.get("dry_run_result", "unknown")
    dry_ts  = rtb.get("dry_run_timestamp", "")
    if dry_run == "no_changes":
        lines.append(f"<b>Nächstes Backup:</b> ✓ Keine Änderungen ({dry_ts[11:16] if len(dry_ts) > 15 else ''})")
    elif dry_run == "changes_detected":
        lines.append(f"<b>Nächstes Backup:</b> 📝 Backup nötig ({dry_ts[11:16] if len(dry_ts) > 15 else ''})")
    
    # pCloud Sync details
    if pc.get("last_sync"):
        pc_sync_short = pc["last_sync"][:16] if len(pc["last_sync"]) > 16 else pc["last_sync"]
        lines.append(f"<b>Sync-Stand:</b> {rtb.get('latest_snapshot', '—')[:19]}")
    
    lines.append("")  # Separator

    # ── Malware & Integrität (from reports.json like dashboard!) ──
    if reports:
        ew = reports.get("entropywatcher") or {}
        
        # Flagged files (from DB)
        flagged_files = ew.get("flagged_files") or {}
        total_flagged = sum(flagged_files.values())
        
        # Missing files (from last scan)
        last_scans = ew.get("last_scans") or []
        total_missing = last_scans[0].get("missing_count", 0) if last_scans else 0
        
        # Active monitors (from services)
        services = status.get("services") or {}
        monitor_names = ["entropywatcher-nas", "entropywatcher-os", "entropywatcher-nas-av", 
                         "entropywatcher-os-av", "honeyfile-monitor"]
        active_count = sum(1 for n in monitor_names if services.get(n, {}).get("status") == "active")
        
        if total_flagged > 0 or total_missing > 0:
            mal_emoji = "🚨" if total_flagged > 0 else "⚠️"
            lines.append(f"<b>{mal_emoji} Malware & Integrität</b>")
            lines.append(f"<b>🔴 {total_flagged} Flagged</b>  <b>⚠️ {total_missing} Missing</b>  ✅ {active_count} Aktiv")
            lines.append("")
    
    # ── Performance Stats (30d) ──
    if reports:
        perf = reports.get("performance_stats") or {}
        if perf.get("total_runs"):
            success_pct = round((perf.get("successful_runs", 0) / perf["total_runs"]) * 100)
            avg_dur = perf.get("avg_duration_min", 0)
            total_gb = perf.get("total_gb_uploaded", 0)
            
            lines.append(f"<b>📊 Performance (30d)</b>")
            lines.append(f"Erfolgsrate: {success_pct}%  |  Ø Dauer: {avg_dur} min")
            lines.append(f"Gesamt hochgeladen: {total_gb} GB")
            lines.append("")

    send(chat_id, "\n".join(lines), reply_markup=get_main_keyboard(dashboard_url))


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


def cmd_logs(chat_id: int, service: str = None) -> None:
    """Show systemd service logs or log files. If service is None, show selection menu."""
    if not service:
        send(chat_id, "<b>📜 Log-Auswahl</b>\n\nWähle einen Service:", reply_markup=get_logs_keyboard())
        return
    
    # Map friendly names to actual service names or log files
    service_map = {
        "backup-pipeline": {"type": "systemd", "name": "backup-pipeline.service"},
        "rtb_wrapper": {"type": "file", "path": "/var/log/backup/rtb_wrapper.log"},
        "pcloud": {"type": "file", "path": "/var/log/pcloud/pcloud_sync.log"},
        "telegram-commander": {"type": "systemd", "name": "telegram-commander.service"}
    }
    
    if service not in service_map:
        send(chat_id, f"❌ Unbekannter Service: {service}", reply_markup=get_logs_keyboard())
        return
    
    config = service_map[service]
    log.info("Fetching logs for %s", service)
    
    try:
        if config["type"] == "systemd":
            # Use journalctl for systemd services
            result = subprocess.run(
                ["journalctl", "-u", config["name"], "-n", "30", "--no-pager"],
                capture_output=True, text=True, timeout=10
            )
            
            if result.returncode != 0:
                send(chat_id, f"⚠️ <b>Service nicht gefunden:</b> {config['name']}",
                     reply_markup=get_logs_keyboard())
                return
            
            logs = result.stdout.strip() or "-- No entries --"
            display_name = config["name"]
            
        elif config["type"] == "file":
            # Read log file directly
            log_path = config["path"]
            
            if not os.path.exists(log_path):
                send(chat_id, f"⚠️ <b>Log-Datei nicht gefunden:</b>\n{log_path}",
                     reply_markup=get_logs_keyboard())
                return
            
            result = subprocess.run(
                ["tail", "-n", "30", log_path],
                capture_output=True, text=True, timeout=10
            )
            
            logs = result.stdout.strip() or "-- No entries --"
            display_name = os.path.basename(log_path)
        
        # Telegram message limit is 4096 chars
        if len(logs) > 3800:
            logs = "[...gekürzt...]\n" + logs[-3800:]
        
        send(chat_id, f"<b>📜 Logs: {display_name}</b>\n\n<code>{logs}</code>",
             reply_markup=get_logs_keyboard())
    
    except subprocess.TimeoutExpired:
        send(chat_id, "⚠️ Timeout beim Abrufen der Logs.", reply_markup=get_logs_keyboard())
    except PermissionError:
        send(chat_id, "❌ Keine Berechtigung zum Lesen der Logs. Service muss als root laufen.",
             reply_markup=get_logs_keyboard())
    except Exception as e:
        log.exception("Failed to fetch logs for %s", service)
        send(chat_id, f"❌ Fehler beim Abrufen der Logs: {e}", reply_markup=get_logs_keyboard())


def cmd_reset_safety_gate(chat_id: int, confirmed: bool = False) -> None:
    """Reset Safety-Gate to GREEN (requires confirmation with big warning)."""
    if not confirmed:
        # Show BIG warning confirmation menu
        try:
            with open(STATUS_JSON) as f:
                data = json.load(f)
            rtb = (data.get("scripts") or {}).get("rtb_wrapper") or {}
            sg  = rtb.get("live_safety_gate") or rtb.get("safety_gate", "UNKNOWN")
        except Exception:
            sg = "UNKNOWN"
        
        keyboard = {
            "inline_keyboard": [
                [{"text": "⚠️ JA, OVERRIDE ERZWINGEN", "callback_data": "sg_reset_do "}],
                [{"text": "❌ Abbrechen (empfohlen)", "callback_data": "menu_safety_gate"}]
            ]
        }
        
        send(chat_id,
             f"<b>🚨 GEFAHR - Safety-Gate Override</b>\n\n"
             f"Aktueller Status: <b>{sg}</b>\n\n"
             f"<b>⚠️ WARNUNG:</b>\n"
             f"Du bist im Begriff, die Safety-Gate-Prüfung zu umgehen!\n\n"
             f"Dies sollte <u>NUR</u> in Notfällen erfolgen:\n"
             f"  • Nach manueller Malware-Prüfung\n"
             f"  • Bei False-Positives\n"
             f"  • Nach System-Wiederherstellung\n\n"
             f"<b>❌ NICHT bei Ransomware-Verdacht!</b>\n\n"
             f"Bist du dir sicher?",
             reply_markup=keyboard)
        return
    
    # Confirmed - perform reset WITH LOG
    log.warning("User %s FORCED Safety-Gate reset to GREEN - OVERRIDE!", chat_id)
    
    try:
        with open(STATUS_JSON, "r") as f:
            data = json.load(f)
        
        # Update safety gate
        if "scripts" not in data:
            data["scripts"] = {}
        if "rtb_wrapper" not in data["scripts"]:
            data["scripts"]["rtb_wrapper"] = {}
        
        data["scripts"]["rtb_wrapper"]["live_safety_gate"] = "GREEN"
        data["scripts"]["rtb_wrapper"]["safety_gate"] = "GREEN"
        
        # Write back
        with open(STATUS_JSON, "w") as f:
            json.dump(data, f, indent=2)
        
        send(chat_id,
             "✅ <b>Safety-Gate zurückgesetzt</b>\n\n"
             "Status: <b>GREEN</b>\n\n"
             "Backups sind jetzt wieder möglich.",
             reply_markup=get_main_keyboard())
        
    except FileNotFoundError:
        send(chat_id, f"❌ status.json nicht gefunden: {STATUS_JSON}", reply_markup=get_main_keyboard())
    except json.JSONDecodeError:
        send(chat_id, "❌ status.json ist kein gültiges JSON", reply_markup=get_main_keyboard())
    except PermissionError:
        send(chat_id,
             "❌ <b>Keine Schreibberechtigung</b>\n\n"
             "Der telegram-commander.service muss als root laufen, um status.json zu ändern.",
             reply_markup=get_main_keyboard())
    except Exception as e:
        log.exception("Failed to reset safety gate")
        send(chat_id, f"❌ Fehler beim Reset: {e}", reply_markup=get_main_keyboard())


def cmd_malware(chat_id: int) -> None:
    """Show malware and integrity monitoring details (uses reports.json like dashboard)."""
    status, reports = load_data()
    
    if not status:
        send(chat_id, "⚠️ status.json nicht lesbar", reply_markup=get_main_keyboard())
        return
    
    if not reports:
        send(chat_id, "⚠️ reports.json nicht verfügbar - keine Detail-Daten", 
             reply_markup=get_main_keyboard())
        return
    
    ew = reports.get("entropywatcher") or {}
    services = status.get("services") or {}
    
    # Flagged files (from reports.json DB data)
    flagged_files = ew.get("flagged_files") or {}
    total_flagged = sum(flagged_files.values())
    
    # Missing files (from last scans)
    last_scans = ew.get("last_scans") or []
    total_missing = last_scans[0].get("missing_count", 0) if last_scans else 0
    
    # Active monitors
    monitor_names = ["entropywatcher-nas", "entropywatcher-os", "entropywatcher-nas-av", 
                     "entropywatcher-os-av", "honeyfile-monitor"]
    active_monitors = [n for n in monitor_names if services.get(n, {}).get("status") == "active"]
    failed_monitors = [n for n in monitor_names if services.get(n, {}).get("status") == "failed"]
    
    # Overall status
    if total_flagged > 0:
        emoji = "🚨"
        status_text = "CRITICAL - Verdächtige Dateien gefunden!"
    elif total_missing > 0:
        emoji = "⚠️"
        status_text = "WARNING - Fehlende Dateien erkannt"
    elif failed_monitors:
        emoji = "❌"
        status_text = "FEHLER - Monitore haben Probleme"
    else:
        emoji = "✅"
        status_text = "OK - Alle Monitore laufen sauber"
    
    lines = [
        f"<b>{emoji} Malware & Integrität</b>",
        "━━━━━━━━━━━━━━━━━",
        f"{status_text}\n",
        f"<b>Zusammenfassung:</b>",
        f"✅ Aktiv: {len(active_monitors)}  🔴 Flagged: {total_flagged}",
        f"⚠️ Missing: {total_missing}  ❌ Fehler: {len(failed_monitors)}\n"
    ]
    
    # Details: Show breakdown by service if issues found
    if total_flagged > 0 or total_missing > 0:
        lines.append("<b>Details pro Service:</b>")
        for service_name, count in flagged_files.items():
            if count > 0:
                lines.append(f"  🔴 {service_name}: {count} flagged")
        
        if total_missing > 0 and last_scans:
            scan = last_scans[0]
            lines.append(f"  ⚠️ {scan.get('service_name', 'unknown')}: {total_missing} missing")
        
        lines.append("")
    
    # Monitor status
    lines.append("<b>Monitor-Status:</b>")
    for name in monitor_names:
        svc = services.get(name)
        if not svc:
            continue
        svc_status = svc.get("status", "unknown")
        svc_emoji = "🟢" if svc_status == "active" else "🔴" if svc_status == "failed" else "⚪"
        next_run = svc.get("next_run", "")
        next_short = next_run[:16] if len(next_run) > 15 else next_run
        
        lines.append(f"{svc_emoji} {name}")
        if next_short and next_short != "N/A":
            lines.append(f"    nächster: {next_short}")
    
    dashboard_url = status.get("dashboard_url")
    send(chat_id, "\n".join(lines), reply_markup=get_main_keyboard(dashboard_url))


def cmd_sg_forecast(chat_id: int) -> None:
    """Show Safety-Gate forecast for next backup run."""
    send(chat_id, "🔮 <b>Forecast wird berechnet...</b>\n\nBitte warten...", parse_mode="HTML")
    
    try:
        if not os.path.exists(FORECAST_SCRIPT):
            send(chat_id, f"❌ Forecast Script nicht gefunden: {FORECAST_SCRIPT}",
                 reply_markup=get_safety_gate_keyboard())
            return
        
        result = subprocess.run(
            [FORECAST_SCRIPT],
            capture_output=True,
            text=True,
            timeout=15
        )
        
        output = result.stdout + result.stderr
        
        # Parse forecast output
        will_block = "blockieren" in output.lower() or "red" in output.lower()
        next_check = "Unbekannt"
        block_reason = ""
        
        for line in output.split("\n"):
            if "Nächste Prüfung" in line or "next check" in line.lower():
                next_check = line.split(":", 1)[-1].strip()
            if "Grund" in line or "reason" in line.lower():
                block_reason = line.split(":", 1)[-1].strip()
        
        emoji = "🚨" if will_block else "✅"
        status = "WIRD BLOCKIEREN" if will_block else "Backup möglich"
        
        message = (
            f"<b>🔮 Safety-Gate Forecast</b>\n\n"
            f"{emoji} <b>{status}</b>\n\n"
            f"<b>Nächste Prüfung:</b> {next_check}\n"
        )
        
        if will_block and block_reason:
            message += f"\n⚠️ <b>Grund:</b> {block_reason}\n"
        
        message += f"\n<i>Forecast: {time.strftime('%Y-%m-%d %H:%M:%S')}</i>"
        
        send(chat_id, message, reply_markup=get_safety_gate_keyboard())
        
    except subprocess.TimeoutExpired:
        send(chat_id, "❌ Forecast-Berechnung hat zu lange gedauert",
             reply_markup=get_safety_gate_keyboard())
    except Exception as e:
        log.exception("Failed to run forecast")
        send(chat_id, f"❌ Fehler beim Forecast: {e}",
             reply_markup=get_safety_gate_keyboard())


def cmd_menu(chat_id: int) -> None:
    """Show main menu with buttons."""
    try:
        with open(STATUS_JSON) as f:
            data = json.load(f)
        dashboard_url = data.get("dashboard_url")
    except:
        dashboard_url = None
    send(chat_id, "<b>🤖 Haupt-Menü</b>\n\nWähle eine Aktion:", reply_markup=get_main_keyboard(dashboard_url))


def cmd_safety_gate_check_live(chat_id: int) -> None:
    """Execute LIVE Safety-Gate check by running safety_gate.sh NOW."""
    send(chat_id, "🔍 <b>LIVE Safety-Gate Prüfung läuft...</b>\n\nBitte warten...", parse_mode="HTML")
    
    try:
        # Execute safety_gate.sh LIVE
        if not os.path.exists(SAFETY_GATE_SCRIPT):
            send(chat_id, f"❌ Safety-Gate Script nicht gefunden: {SAFETY_GATE_SCRIPT}",
                 reply_markup=get_safety_gate_keyboard())
            return
        
        result = subprocess.run(
            [SAFETY_GATE_SCRIPT],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        # Parse output (safety_gate.sh schreibt "SAFETY-GATE: GREEN/YELLOW/RED")
        output = result.stdout + result.stderr
        
        sg_status = "UNKNOWN"
        if "SAFETY-GATE: GREEN" in output:
            sg_status = "GREEN"
        elif "SAFETY-GATE: YELLOW" in output:
            sg_status = "YELLOW"
        elif "SAFETY-GATE: RED" in output:
            sg_status = "RED"
        
        emoji_map = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴", "UNKNOWN": "⚪"}
        emoji = emoji_map.get(sg_status, "⚪")
        
        status_text = {
            "GREEN": "✅ System ist sicher, Backups möglich",
            "YELLOW": "⚠️ Warnung erkannt, aber Backups noch erlaubt",
            "RED": "🚨 GEFAHR! Ransomware-Verdacht - Backups blockiert!",
            "UNKNOWN": "❓ Status konnte nicht ermittelt werden"
        }.get(sg_status, "Unbekannter Status")
        
        # Extract details from output
        details_lines = []
        for line in output.split("\n"):
            if "Honeyfiles:" in line or "nas:" in line or "nas-av:" in line:
                details_lines.append(line.strip())
        
        details = "\n".join(details_lines[:5]) if details_lines else "Keine Details verfügbar"
        
        message = (
            f"<b>🔍 LIVE Safety-Gate Status</b>\n\n"
            f"{emoji} <b>{sg_status}</b> <i>(gerade geprüft)</i>\n"
            f"{status_text}\n\n"
            f"<b>Details:</b>\n<code>{details}</code>\n\n"
            f"<i>Prüfung: {time.strftime('%Y-%m-%d %H:%M:%S')}</i>"
        )
        
        send(chat_id, message, reply_markup=get_safety_gate_keyboard())
        
    except subprocess.TimeoutExpired:
        send(chat_id, "❌ Safety-Gate Prüfung hat zu lange gedauert (Timeout)",
             reply_markup=get_safety_gate_keyboard())
    except Exception as e:
        log.exception("Failed to run live safety gate check")
        send(chat_id, f"❌ Fehler bei LIVE-Prüfung: {e}",
             reply_markup=get_safety_gate_keyboard())


def cmd_safety_gate_check(chat_id: int) -> None:
    """Check and display cached Safety-Gate status from status.json."""
    try:
        with open(STATUS_JSON) as f:
            data = json.load(f)
        
        rtb = (data.get("scripts") or {}).get("rtb_wrapper") or {}
        
        # LIVE Safety-Gate (priorisiert!) wie Dashboard
        sg_live = rtb.get("live_safety_gate", "")
        sg_hist = rtb.get("safety_gate", "")
        sg = sg_live or sg_hist or "UNKNOWN"
        sg_label = "LIVE" if sg_live else "Historisch" if sg_hist else "Unknown"
        
        # Details and timestamp
        sg_details = rtb.get("live_sg_details") or rtb.get("details", "")
        last_check = rtb.get("last_run", "—")
        
        emoji_map = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴", "UNKNOWN": "⚪"}
        emoji = emoji_map.get(sg, "⚪")
        
        status_text = {
            "GREEN": "✅ System ist sicher, Backups möglich",
            "YELLOW": "⚠️ Warnung erkannt, aber Backups noch erlaubt",
            "RED": "🚨 GEFAHR! Ransomware-Verdacht - Backups blockiert!",
            "UNKNOWN": "❓ Status konnte nicht ermittelt werden"
        }.get(sg, "Unbekannter Status")
        
        message = (
            f"<b>🔒 Safety-Gate Status</b>\n\n"
            f"{emoji} <b>{sg}</b> <i>({sg_label})</i>\n"
            f"{status_text}\n"
        )
        
        # Add details if available
        if sg_details:
            message += f"\n<b>Details:</b>\n{sg_details}\n"
        
        # Forecast if available
        forecast = data.get("safety_gate_forecast") or {}
        if forecast.get("next_check"):
            message += f"\n<b>🔮 Nächste Prüfung:</b> {forecast['next_check']}"
            if forecast.get("will_block"):
                message += f"\n⚠️ Wird blockieren: {forecast.get('block_reason', 'unknown')}"
        
        message += f"\n\n<i>Letzte Prüfung: {last_check}</i>"
        
        send(chat_id, message, reply_markup=get_safety_gate_keyboard())
    
    except Exception as e:
        send(chat_id, f"❌ Fehler beim Abrufen des Safety-Gate-Status: {e}",
             reply_markup=get_safety_gate_keyboard())

# ── Main dispatch ─────────────────────────────────────────────────────────────

COMMANDS = {
    "/start":  cmd_help,
    "/help":   cmd_help,
    "/menu":   cmd_menu,
    "/status": cmd_status,
    "/backup": cmd_backup,
    "/logs":   cmd_logs,
    "/reset_safety_gate": cmd_reset_safety_gate,
}

# Callback handlers for inline keyboard buttons
CALLBACKS = {
    "cmd_status":    lambda cid, mid: cmd_status(cid),
    "cmd_backup":    lambda cid, mid: cmd_backup(cid),
    "cmd_help":      lambda cid, mid: cmd_help(cid),
    "cmd_malware":   lambda cid, mid: cmd_malware(cid),
    "menu_main":     lambda cid, mid: cmd_menu(cid),
    "menu_logs":     lambda cid, mid: cmd_logs(cid),
    "menu_safety_gate": lambda cid, mid: edit_message(cid, mid, "<b>🔒 Safety-Gate</b>\n\nWähle eine Aktion:", reply_markup=get_safety_gate_keyboard()),
    
    # Safety-Gate actions
    "sg_check":      lambda cid, mid: cmd_safety_gate_check(cid),
    "sg_check_live": lambda cid, mid: cmd_safety_gate_check_live(cid),
    "sg_forecast":   lambda cid, mid: cmd_sg_forecast(cid),
    "sg_reset_confirm": lambda cid, mid: cmd_reset_safety_gate(cid, confirmed=False),
    "sg_reset_do":   lambda cid, mid: cmd_reset_safety_gate(cid, confirmed=True),
    
    # Log callbacks
    "logs_backup-pipeline": lambda cid, mid: cmd_logs(cid, "backup-pipeline"),
    "logs_rtb_wrapper":     lambda cid, mid: cmd_logs(cid, "rtb_wrapper"),
    "logs_pcloud":          lambda cid, mid: cmd_logs(cid, "pcloud"),
    "logs_telegram-commander": lambda cid, mid: cmd_logs(cid, "telegram-commander"),
}


def handle_message(update: dict) -> None:
    """Handle text message commands."""
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
             f"Unbekannter Befehl: <code>{cmd}</code>\n/help für eine Liste.",
             reply_markup=get_main_keyboard())


def handle_callback(update: dict) -> None:
    """Handle callback queries from inline keyboard buttons."""
    callback = update.get("callback_query") or {}
    callback_id = callback.get("id")
    data = callback.get("data", "")
    msg = callback.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    user = (callback.get("from") or {}).get("username", "unknown")

    if not chat_id or not data:
        return

    # Security: hard-reject unknown senders
    if chat_id not in ALLOWED_CHAT_IDS:
        log.warning("Rejected callback from chat_id=%s user=%s: %r", chat_id, user, data)
        answer_callback(callback_id, "❌ Nicht autorisiert")
        return

    log.info("Callback from chat_id=%s user=%s: %s", chat_id, user, data)

    # Answer callback to remove loading indicator
    answer_callback(callback_id)

    # Execute handler
    handler = CALLBACKS.get(data)
    if handler:
        try:
            handler(chat_id, message_id)
        except Exception as e:
            log.exception("Callback handler %s failed: %s", data, e)
            send(chat_id, f"⚠️ Interner Fehler: {e}")
    else:
        log.warning("Unknown callback data: %s", data)
        send(chat_id, f"⚠️ Unbekannte Aktion: {data}")


def handle_update(update: dict) -> None:
    """Route update to message or callback handler."""
    if "message" in update:
        handle_message(update)
    elif "callback_query" in update:
        handle_callback(update)

# ── Long-polling loop ─────────────────────────────────────────────────────────

def run() -> None:
    global CONSECUTIVE_ERRORS
    validate_config()

    # Verify token works
    me = api("getMe")
    if not me or not me.get("result"):
        log.error("Failed to connect to Telegram API. Check token and network.")
        sys.exit(1)
    bot_name = (me.get("result") or {}).get("username", "?")
    log.info("Connected as @%s", bot_name)

    # Skip old updates on startup (offset = -1 trick)
    updates = get_updates(offset=-1)
    offset  = max((u["update_id"] for u in updates), default=0) + 1 if updates else 0
    log.info("Starting poll loop (offset=%d)", offset)

    while True:
        try:
            # If too many consecutive errors, wait longer before retrying
            if CONSECUTIVE_ERRORS >= MAX_CONSECUTIVE_ERRORS:
                log.warning("Too many consecutive errors (%d), sleeping 60s", CONSECUTIVE_ERRORS)
                time.sleep(60)
                CONSECUTIVE_ERRORS = 0  # Reset after cooldown
            
            updates = get_updates(offset=offset)
            
            # Empty result might indicate API issue, but could also be no updates
            if updates is None:
                log.warning("get_updates returned None, waiting 10s before retry")
                time.sleep(10)
                continue
            
            for upd in updates:
                handle_update(upd)
                offset = upd["update_id"] + 1
        except KeyboardInterrupt:
            log.info("Shutting down.")
            break
        except Exception as e:
            log.error("Poll loop error: %s — retrying in 10s", e, exc_info=True)
            CONSECUTIVE_ERRORS += 1
            time.sleep(10)


if __name__ == "__main__":
    run()
