# setup_venv.ps1 — Erstelle und konfiguriere Python Virtual Environment für pcloud-tools
#
# Erstellt eine .venv im Root-Verzeichnis des Projekts und installiert alle Dependencies.
# 
# Verwendung:
#   .\scripts\setup_venv.ps1
#   .\scripts\setup_venv.ps1 -Force    # Überschreibt existierende venv
#
# Nach dem Setup:
#   .\.venv\Scripts\Activate.ps1       # venv aktivieren
#   python scripts\analyze_manifest_duplicates.py --help

param(
    [switch]$Force,
    [switch]$SkipTest
)

$ErrorActionPreference = "Stop"

# Farben für Output
function Write-Success { param($msg) Write-Host "✓ $msg" -ForegroundColor Green }
function Write-Info { param($msg) Write-Host "ℹ $msg" -ForegroundColor Cyan }
function Write-Warning { param($msg) Write-Host "⚠ $msg" -ForegroundColor Yellow }
function Write-Error { param($msg) Write-Host "✗ $msg" -ForegroundColor Red }

# === Projektverzeichnis ermitteln (ein Level über scripts/) ===
$ScriptDir = Split-Path -Parent $PSCommandPath
$ProjectRoot = Split-Path -Parent $ScriptDir

Write-Info "Projekt-Root: $ProjectRoot"
Set-Location $ProjectRoot

# === Python-Version prüfen ===
Write-Info "Prüfe Python-Installation..."
try {
    $pythonVersion = python --version 2>&1
    Write-Success "Python gefunden: $pythonVersion"
} catch {
    Write-Error "Python nicht gefunden. Bitte Python 3.8+ installieren."
    Write-Info "Download: https://www.python.org/downloads/"
    exit 1
}

# === Venv-Pfad ===
$VenvPath = Join-Path $ProjectRoot ".venv"

# === Venv existiert bereits? ===
if (Test-Path $VenvPath) {
    if ($Force) {
        Write-Warning "Lösche existierende venv..."
        Remove-Item -Recurse -Force $VenvPath
    } else {
        Write-Warning "Venv existiert bereits: $VenvPath"
        Write-Info "Verwende -Force zum Überschreiben"
        
        # Nur Dependencies aktualisieren
        Write-Info "Aktualisiere Dependencies..."
        & "$VenvPath\Scripts\python.exe" -m pip install --upgrade pip -q
        & "$VenvPath\Scripts\python.exe" -m pip install -r requirements.txt -q
        Write-Success "Dependencies aktualisiert"
        
        Write-Info ""
        Write-Info "Aktiviere venv mit:"
        Write-Host "  .\.venv\Scripts\Activate.ps1" -ForegroundColor Yellow
        exit 0
    }
}

# === Venv erstellen ===
Write-Info "Erstelle Virtual Environment..."
python -m venv $VenvPath

if (-not (Test-Path $VenvPath)) {
    Write-Error "Venv-Erstellung fehlgeschlagen"
    exit 1
}

Write-Success "Virtual Environment erstellt: $VenvPath"

# === pip upgraden ===
Write-Info "Upgrade pip..."
& "$VenvPath\Scripts\python.exe" -m pip install --upgrade pip -q

# === Dependencies installieren ===
$RequirementsFile = Join-Path $ProjectRoot "requirements.txt"

if (Test-Path $RequirementsFile) {
    Write-Info "Installiere Dependencies aus requirements.txt..."
    & "$VenvPath\Scripts\python.exe" -m pip install -r $RequirementsFile -q
    Write-Success "Dependencies installiert"
} else {
    Write-Warning "Keine requirements.txt gefunden - überspringe Dependencies"
}

# === Test: Skripte ausführbar? ===
if (-not $SkipTest) {
    Write-Info "Teste Installation..."
    
    # Test 1: analyze_manifest_duplicates.py --help
    $TestScript = Join-Path $ProjectRoot "scripts\analyze_manifest_duplicates.py"
    if (Test-Path $TestScript) {
        try {
            $helpOutput = & "$VenvPath\Scripts\python.exe" $TestScript --help 2>&1
            if ($LASTEXITCODE -eq 0) {
                Write-Success "analyze_manifest_duplicates.py funktioniert"
            } else {
                Write-Warning "analyze_manifest_duplicates.py --help hat Fehler zurückgegeben"
            }
        } catch {
            Write-Warning "Konnte analyze_manifest_duplicates.py nicht testen"
        }
    }
    
    # Test 2: Pandas/Openpyxl importierbar?
    try {
        & "$VenvPath\Scripts\python.exe" -c "import pandas, openpyxl; print('OK')" 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Success "pandas & openpyxl erfolgreich importiert"
        }
    } catch {
        Write-Warning "Import-Test fehlgeschlagen"
    }
}

# === Fertig ===
Write-Success "Setup abgeschlossen!"
Write-Info ""
Write-Info "Nächste Schritte:"
Write-Host "  1. Aktiviere venv:  " -NoNewline -ForegroundColor White
Write-Host ".\.venv\Scripts\Activate.ps1" -ForegroundColor Yellow
Write-Host "  2. Teste Skript:    " -NoNewline -ForegroundColor White
Write-Host "python scripts\analyze_manifest_duplicates.py --help" -ForegroundColor Yellow
Write-Info ""
Write-Info "Tipp: VS Code erkennt die venv automatisch (Statusleiste unten links)"
