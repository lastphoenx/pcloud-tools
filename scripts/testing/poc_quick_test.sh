#!/usr/bin/env bash
# Quick-Test-Runner für PoC Chunked Resume

set -euo pipefail

TESTFILE="/tmp/poc_test_$(date +%s).bin"
POC_SCRIPT="$(dirname "$0")/poc_chunked_resume.py"
PYTHON="${PYTHON:-python3}"

# Farben
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log() {
    echo -e "${BLUE}[$(date +%H:%M:%S)]${NC} $*"
}

success() {
    echo -e "${GREEN}[✓]${NC} $*"
}

warn() {
    echo -e "${YELLOW}[!]${NC} $*"
}

error() {
    echo -e "${RED}[✗]${NC} $*"
}

cleanup() {
    if [[ -f "$TESTFILE" ]]; then
        log "Lösche Test-Datei: $TESTFILE"
        rm -f "$TESTFILE"
    fi
}

trap cleanup EXIT

# Test 1: Normaler Upload
test_normal_upload() {
    log "=== Test 1: Normaler Upload (ohne Abbruch) ==="
    
    log "Erstelle 20 MB Test-Datei..."
    dd if=/dev/urandom of="$TESTFILE" bs=1M count=20 2>/dev/null
    
    log "Starte Upload..."
    if $PYTHON "$POC_SCRIPT" --file "$TESTFILE" --mode normal; then
        success "Upload erfolgreich abgeschlossen"
        return 0
    else
        error "Upload fehlgeschlagen"
        return 1
    fi
}

# Test 2: Abbruch + Resume (kurze Pause)
test_abort_and_resume() {
    log "=== Test 2: Abbruch + Resume (10s Pause) ==="
    
    log "Erstelle 50 MB Test-Datei..."
    dd if=/dev/urandom of="$TESTFILE" bs=1M count=50 2>/dev/null
    
    log "Starte Upload mit Abbruch nach 3 Chunks..."
    if $PYTHON "$POC_SCRIPT" --file "$TESTFILE" --mode abort-after --abort-after-chunks 3; then
        success "Abbruch wie erwartet"
    else
        error "Abbruch fehlgeschlagen"
        return 1
    fi
    
    log "State-File prüfen..."
    STATE_FILE="$HOME/.pcloud_poc_state/$(basename "$TESTFILE" | sed 's/\./_/g').state.json"
    if [[ -f "$STATE_FILE" ]]; then
        success "State-File gefunden: $STATE_FILE"
        log "Inhalt:"
        cat "$STATE_FILE" | jq '.' 2>/dev/null || cat "$STATE_FILE"
    else
        error "State-File NICHT gefunden!"
        return 1
    fi
    
    log "Warte 10 Sekunden..."
    sleep 10
    
    log "Resume Upload..."
    if $PYTHON "$POC_SCRIPT" --file "$TESTFILE" --mode resume; then
        success "Resume erfolgreich abgeschlossen"
        return 0
    else
        error "Resume fehlgeschlagen"
        return 1
    fi
}

# Test 3: Datei-Änderung während Pause (soll fehlschlagen)
test_file_modification() {
    log "=== Test 3: Datei-Änderung während Pause (Hash-Mismatch) ==="
    
    log "Erstelle 20 MB Test-Datei..."
    dd if=/dev/urandom of="$TESTFILE" bs=1M count=20 2>/dev/null
    
    log "Starte Upload mit Abbruch nach 2 Chunks..."
    $PYTHON "$POC_SCRIPT" --file "$TESTFILE" --mode abort-after --abort-after-chunks 2 || true
    
    log "Ändere Datei (füge 1 Byte hinzu)..."
    echo "X" >> "$TESTFILE"
    
    log "Versuche Resume (sollte mit Hash-Mismatch fehlschlagen)..."
    if $PYTHON "$POC_SCRIPT" --file "$TESTFILE" --mode resume 2>&1 | grep -q "Hash-Mismatch"; then
        success "Hash-Validierung funktioniert (Resume korrekt abgelehnt)"
        return 0
    else
        error "Hash-Validierung fehlgeschlagen (Resume sollte abgelehnt werden!)"
        return 1
    fi
}

# Test 4: Response-Log-Analyse
test_response_log() {
    log "=== Test 4: Response-Log-Analyse ==="
    
    log "Erstelle 30 MB Test-Datei..."
    dd if=/dev/urandom of="$TESTFILE" bs=1M count=30 2>/dev/null
    
    log "Starte Upload..."
    $PYTHON "$POC_SCRIPT" --file "$TESTFILE" --mode normal || true
    
    RESPONSE_LOG="$HOME/.pcloud_poc_state/$(basename "$TESTFILE" | sed 's/\./_/g').responses.jsonl"
    
    if [[ -f "$RESPONSE_LOG" ]]; then
        success "Response-Log gefunden"
        
        log "Analysiere Responses..."
        
        # Chunk-Upload-Zeiten
        if command -v jq &>/dev/null; then
            CHUNKS=$(cat "$RESPONSE_LOG" | jq -r 'select(.response.step == "upload_write") | .response.chunk_number' | wc -l)
            AVG_TIME=$(cat "$RESPONSE_LOG" | jq -r 'select(.response.step == "upload_write") | .response.duration_s' | awk '{sum+=$1; n++} END {if(n>0) print sum/n; else print 0}')
            
            log "Chunks hochgeladen: $CHUNKS"
            log "Durchschnittliche Chunk-Zeit: ${AVG_TIME}s"
            
            # Alle result-Codes
            log "API Result-Codes:"
            cat "$RESPONSE_LOG" | jq -r '.response.response.result' | sort | uniq -c
        else
            warn "jq nicht installiert, überspringe detaillierte Analyse"
            log "Zeige rohe Logs:"
            cat "$RESPONSE_LOG"
        fi
        
        return 0
    else
        warn "Response-Log nicht gefunden (normales Verhalten wenn State gelöscht wurde)"
        return 0
    fi
}

# Hauptmenü
show_menu() {
    echo ""
    echo "========================================="
    echo "  PoC Chunked Resume - Quick Tests"
    echo "========================================="
    echo ""
    echo "1) Test 1: Normaler Upload"
    echo "2) Test 2: Abbruch + Resume"
    echo "3) Test 3: Hash-Mismatch-Validierung"
    echo "4) Test 4: Response-Log-Analyse"
    echo "5) Alle Tests nacheinander"
    echo "6) State-Files anzeigen"
    echo "7) State-Files löschen"
    echo "q) Beenden"
    echo ""
    echo -n "Auswahl: "
}

show_state_files() {
    STATE_DIR="$HOME/.pcloud_poc_state"
    if [[ -d "$STATE_DIR" ]]; then
        log "State-Files in $STATE_DIR:"
        ls -lh "$STATE_DIR/" 2>/dev/null || echo "  (leer)"
        
        if command -v jq &>/dev/null; then
            for f in "$STATE_DIR"/*.state.json 2>/dev/null; do
                if [[ -f "$f" ]]; then
                    echo ""
                    log "Inhalt: $(basename "$f")"
                    cat "$f" | jq '.'
                fi
            done
        fi
    else
        warn "State-Verzeichnis existiert noch nicht: $STATE_DIR"
    fi
}

cleanup_state_files() {
    STATE_DIR="$HOME/.pcloud_poc_state"
    if [[ -d "$STATE_DIR" ]]; then
        log "Lösche alle State-Files..."
        rm -rf "$STATE_DIR"/*
        success "State-Files gelöscht"
    else
        warn "Nichts zu löschen"
    fi
}

run_all_tests() {
    PASSED=0
    FAILED=0
    
    for test_func in test_normal_upload test_abort_and_resume test_file_modification test_response_log; do
        echo ""
        echo "========================================"
        if $test_func; then
            ((PASSED++))
        else
            ((FAILED++))
        fi
        
        # Cleanup zwischen Tests
        rm -rf "$HOME/.pcloud_poc_state"/*
        if [[ -f "$TESTFILE" ]]; then
            rm -f "$TESTFILE"
        fi
        
        sleep 2
    done
    
    echo ""
    echo "========================================"
    echo "  Test-Zusammenfassung"
    echo "========================================"
    echo -e "${GREEN}Erfolgreich: $PASSED${NC}"
    echo -e "${RED}Fehlgeschlagen: $FAILED${NC}"
    echo "========================================"
}

# Main Loop
if [[ $# -gt 0 ]]; then
    # Kommandozeilen-Modus
    case "$1" in
        1|normal) test_normal_upload ;;
        2|resume) test_abort_and_resume ;;
        3|hash) test_file_modification ;;
        4|log) test_response_log ;;
        5|all) run_all_tests ;;
        *) echo "Unbekannter Test: $1"; exit 1 ;;
    esac
else
    # Interaktiver Modus
    while true; do
        show_menu
        read -r choice
        
        case "$choice" in
            1) test_normal_upload ;;
            2) test_abort_and_resume ;;
            3) test_file_modification ;;
            4) test_response_log ;;
            5) run_all_tests ;;
            6) show_state_files ;;
            7) cleanup_state_files ;;
            q|Q) log "Beende..."; exit 0 ;;
            *) warn "Ungültige Auswahl" ;;
        esac
        
        echo ""
        echo -n "Weiter mit Enter..."
        read -r
    done
fi
