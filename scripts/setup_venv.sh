#!/bin/bash
# setup_venv.sh — Erstelle und konfiguriere Python Virtual Environment für pcloud-tools
#
# Erstellt eine .venv im Root-Verzeichnis des Projekts und installiert alle Dependencies.
# 
# Verwendung:
#   ./scripts/setup_venv.sh
#   ./scripts/setup_venv.sh --force    # Überschreibt existierende venv
#
# Nach dem Setup:
#   source .venv/bin/activate          # venv aktivieren
#   python scripts/analyze_manifest_duplicates.py --help

set -e

# === Farben & Symbole ===
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

log_success() { echo -e "${GREEN}✓${NC} $1"; }
log_info() { echo -e "${CYAN}ℹ${NC} $1"; }
log_warn() { echo -e "${YELLOW}⚠${NC} $1"; }
log_error() { echo -e "${RED}✗${NC} $1"; }

# === Argument-Parsing ===
FORCE=false
SKIP_TEST=false

while [ $# -gt 0 ]; do
    case "$1" in
        --force) FORCE=true; shift ;;
        --skip-test) SKIP_TEST=true; shift ;;
        -h|--help)
            echo "Verwendung: $0 [--force] [--skip-test]"
            echo "  --force      Überschreibt existierende venv"
            echo "  --skip-test  Überspringt Funktionstests"
            exit 0
            ;;
        *)
            log_error "Unbekannter Parameter: $1"
            exit 1
            ;;
    esac
done

# === Projektverzeichnis ermitteln ===
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

log_info "Projekt-Root: $PROJECT_ROOT"
cd "$PROJECT_ROOT"

# === Python-Version prüfen ===
log_info "Prüfe Python-Installation..."
if ! command -v python3 &> /dev/null; then
    log_error "Python3 nicht gefunden. Bitte installieren:"
    echo "  Ubuntu/Debian: sudo apt install python3 python3-pip python3-venv"
    echo "  RHEL/CentOS:   sudo yum install python3 python3-pip"
    exit 1
fi

PYTHON_VERSION=$(python3 --version)
log_success "Python gefunden: $PYTHON_VERSION"

# === Venv-Pfad ===
VENV_PATH="$PROJECT_ROOT/.venv"

# === Venv existiert bereits? ===
if [ -d "$VENV_PATH" ]; then
    if [ "$FORCE" = true ]; then
        log_warn "Lösche existierende venv..."
        rm -rf "$VENV_PATH"
    else
        log_warn "Venv existiert bereits: $VENV_PATH"
        log_info "Verwende --force zum Überschreiben"
        
        # Nur Dependencies aktualisieren
        log_info "Aktualisiere Dependencies..."
        "$VENV_PATH/bin/python" -m pip install --upgrade pip -q
        "$VENV_PATH/bin/python" -m pip install -r requirements.txt -q
        log_success "Dependencies aktualisiert"
        
        echo ""
        log_info "Aktiviere venv mit:"
        echo -e "  ${YELLOW}source .venv/bin/activate${NC}"
        exit 0
    fi
fi

# === Venv erstellen ===
log_info "Erstelle Virtual Environment..."
python3 -m venv "$VENV_PATH"

if [ ! -d "$VENV_PATH" ]; then
    log_error "Venv-Erstellung fehlgeschlagen"
    exit 1
fi

log_success "Virtual Environment erstellt: $VENV_PATH"

# === pip upgraden ===
log_info "Upgrade pip..."
"$VENV_PATH/bin/python" -m pip install --upgrade pip -q

# === Dependencies installieren ===
REQUIREMENTS_FILE="$PROJECT_ROOT/requirements.txt"

if [ -f "$REQUIREMENTS_FILE" ]; then
    log_info "Installiere Dependencies aus requirements.txt..."
    "$VENV_PATH/bin/python" -m pip install -r "$REQUIREMENTS_FILE" -q
    log_success "Dependencies installiert"
else
    log_warn "Keine requirements.txt gefunden - überspringe Dependencies"
fi

# === Test: Skripte ausführbar? ===
if [ "$SKIP_TEST" = false ]; then
    log_info "Teste Installation..."
    
    # Test 1: analyze_manifest_duplicates.py --help
    TEST_SCRIPT="$PROJECT_ROOT/scripts/analyze_manifest_duplicates.py"
    if [ -f "$TEST_SCRIPT" ]; then
        if "$VENV_PATH/bin/python" "$TEST_SCRIPT" --help &> /dev/null; then
            log_success "analyze_manifest_duplicates.py funktioniert"
        else
            log_warn "analyze_manifest_duplicates.py --help hat Fehler zurückgegeben"
        fi
    fi
    
    # Test 2: Pandas/Openpyxl importierbar?
    if "$VENV_PATH/bin/python" -c "import pandas, openpyxl" 2>/dev/null; then
        log_success "pandas & openpyxl erfolgreich importiert"
    else
        log_warn "Import-Test fehlgeschlagen"
    fi
fi

# === Fertig ===
log_success "Setup abgeschlossen!"
echo ""
log_info "Nächste Schritte:"
echo -e "  1. Aktiviere venv:  ${YELLOW}source .venv/bin/activate${NC}"
echo -e "  2. Teste Skript:    ${YELLOW}python scripts/analyze_manifest_duplicates.py --help${NC}"
echo ""
log_info "Tipp: VS Code erkennt die venv automatisch (Statusleiste unten links)"
