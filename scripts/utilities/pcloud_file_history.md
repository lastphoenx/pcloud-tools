# PCloud File History Utility (Smart Version)

Dieses Utility ermöglicht es, die Lebensgeschichte einer einzelnen Datei über alle im System archivierten Manifeste hinweg zu verfolgen.

## 📋 Zweck

Im Gegensatz zu einer einfachen Pfadsuche nutzt diese "Smarte Version" eine **Beweiskette (Chain of Custody)**, um Dateien auch dann zu finden, wenn sie umbenannt, verschoben oder durch Applikationen (wie Microsoft Word) mittels "Atomic Saves" neu geschrieben wurden.

Das Skript löst das Problem der Identität über drei Ebenen:
1.  **PATH**: Direkte Suche nach dem Dateinamen.
2.  **INODE**: Suche nach der physischen Dateinummer (bleibt bei `mv` innerhalb eines Datenträgers gleich).
3.  **HASH**: Suche nach dem identischen Inhalt (findet Kopien oder Dateien nach Atomic-Saves).

## 🚀 Nutzung

Das Skript wird mit dem aktuellsten bekannten Pfad der Datei aufgerufen. Es arbeitet sich dann rückwärts durch die Zeit.

```bash
./pcloud_file_history.sh "pfad/zu/deiner/datei.txt"
```

### Die Spalte "Match" verstehen

In der Ausgabe siehst du in Klammern, wie die Datei im jeweiligen Snapshot gefunden wurde:

-   **[PATH]**: Die Datei existiert unter demselben Namen wie im neueren Snapshot.
-   **[INODE]**: Der Name hat sich geändert, aber die Inode ist identisch (Beweis für Verschiebung/Umbenennung).
-   **[HASH]**: Name und Inode haben sich geändert, aber der Inhalt ist identisch (Beweis für Kopie oder Neuerstellung mit gleichem Inhalt).

## 🧠 Smart Tracing Logik

Das Skript führt einen internen Status für den "aktuell gesuchten Vorfahren" (Current Ancestor). Bei jedem Schritt rückwärts (von neu nach alt) passiert folgendes:

1.  Suche im Manifest nach dem aktuellen Pfad.
2.  Falls nicht gefunden: Suche nach der letzten bekannten Inode.
3.  Falls immer noch nicht gefunden: Suche nach dem letzten bekannten SHA256-Hash.
4.  Sobald ein Match gefunden wird, werden Pfad, Inode und Hash für den *nächsten* (älteren) Schritt aktualisiert.

Dies erlaubt es, auch komplexen Ketten wie dieser zu folgen:
`dokument_final_v2.docx` (Snapshot 20) 
  -> `dokument_final.docx` (Snapshot 15, via Inode-Match)
  -> `entwurf.docx` (Snapshot 10, via Hash-Match)

## 🛠️ Voraussetzungen

-   **jq**: Für das Parsing der JSON-Manifeste.
-   **bc**: Für die Berechnung der menschenlesbaren Dateigrößen.
-   **Manifeste**: Das Skript benötigt die lokalen Manifeste in `/srv/pcloud-archive/manifests/`.
