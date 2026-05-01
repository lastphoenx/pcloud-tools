#!/usr/bin/env bash
#
# pcloud_file_history.sh (Smart Version)
# --------------------------------------
# Rekonstruiert die Historie einer Datei über lokale Manifeste.
# Nutzt Pfad, Inode und Hash-Tracing um Umbenennungen und Verschiebungen zu folgen.

set -euo pipefail

MANIFEST_DIR="${PCLOUD_ARCHIVE_DIR:-/srv/pcloud-archive}/manifests"

if [[ $# -lt 1 ]]; then
    echo "Nutzung: $0 <relativer_dateipfad>"
    exit 1
fi

# Start-Parameter
CUR_PATH="$1"
CUR_INODE_INO=""
CUR_INODE_DEV=""
CUR_HASH=""

echo "========================================================================================="
echo "Smart History Trace für: ${CUR_PATH}"
echo "Strategie: Path -> Inode -> Hash (Rückwärts-Suche)"
echo "========================================================================================="
printf "%-25s | %-12s | %-10s | %-7s | %s\n" "Snapshot" "Status" "Größe" "Match" "Pfad / Info"
echo "--------------------------|--------------|------------|---------|------------------------"

# Manifeste absteigend sortieren (vom Neuesten zum Ältesten für Tracing)
mapfile -t manifests < <(ls "${MANIFEST_DIR}"/*.json 2>/dev/null | sort -r)

found_any=0

for manifest in "${manifests[@]}"; do
    snap_name=$(basename "${manifest}" .json)
    
    # 1. Suche nach Pfad, Inode oder Hash
    # Wir nutzen jq um das beste Match im Manifest zu finden
    # Priorität: 1. Pfad, 2. Inode (falls bekannt), 3. Hash (falls bekannt)
    res=$(jq -r --arg path "${CUR_PATH}" --arg ino "${CUR_INODE_INO}" --arg dev "${CUR_INODE_DEV}" --arg hash "${CUR_HASH}" '
        # Hilfsfunktion für Match-Bewertung
        def score:
            if .relpath == $path then 10
            elif (.inode.ino | tostring) == $ino and (.inode.dev | tostring) == $dev then 5
            elif .sha256 == $hash and .sha256 != null then 2
            else 0 end;

        [.items[]? | select(.type == "file") | {item: ., s: score} | select(.s > 0)]
        | sort_by(-.s) | .[0] | if . then "\(.s)|\(.item.relpath)|\(.item.sha256)|\(.item.size)|\(.item.inode.ino)|\(.item.inode.dev)" else empty end
    ' "${manifest}" 2>/dev/null || true)

    if [[ -n "${res}" ]]; then
        found_any=1
        IFS='|' read -r score match_path match_hash match_size match_ino match_dev <<< "${res}"
        
        # Match-Typ bestimmen
        case $score in
            10) m_type="PATH" ;;
            5)  m_type="INODE" ;;
            2)  m_type="HASH" ;;
            *)  m_type="???" ;;
        esac

        # Größe formatieren
        if [[ ${match_size} -ge 1048576 ]]; then size_h=$(printf "%.1f MB" "$(echo "${match_size}/1048576" | bc -l)"); else size_h="$((match_size/1024)) KB"; fi

        printf "%-25s | %-12s | %-10s | [%-5s] | %s\n" \
            "${snap_name}" "FOUND" "${size_h}" "${m_type}" "${match_path}"

        # Update Tracing-Parameter für den nächsten (älteren) Snapshot
        CUR_PATH="${match_path}"
        CUR_INODE_INO="${match_ino}"
        CUR_INODE_DEV="${match_dev}"
        CUR_HASH="${match_hash}"
    else
        # Wenn wir schon mal was gefunden hatten und jetzt nichts mehr, ist die Kette evtl. zu Ende
        if [[ $found_any -eq 1 ]]; then
            printf "%-25s | %-12s | %-10s | %-7s | %s\n" \
                "${snap_name}" "GAP/END" "---" "---" "Kette gerissen"
            # Wir machen trotzdem weiter, evtl. taucht die Inode/Hash später (älter) wieder auf
        fi
    fi
done

echo "========================================================================================="
