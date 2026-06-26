#!/usr/bin/env bash
# Pin each /srv/nas top-level share to one SSD branch; merge split trees; fix mergerfs policy.
# Run on pi-nas as root. See docs/NAS_SSD_PIN.md
set -euo pipefail

SSD1="${SSD1:-/mnt/ssd1}"
SSD2="${SSD2:-/mnt/ssd2}"
MERGED="${MERGED:-/srv/nas}"
STATE_DIR="${STATE_DIR:-/var/lib/nas-ssd-pin}"
LOG="${STATE_DIR}/nas-ssd-pin.log"
FSTAB="${FSTAB:-/etc/fstab}"

# Soll-Zuordnung (ein Ordner = genau eine SSD). mergerfs bleibt ein Mount über beide Branches.
SSD1_DIRS=(
  Thomas
  Eltern
  Gemeinsam
  Monika
  Giulia
  restore
  admin
  av-quarantine
  Paperless
)

SSD2_DIRS=(
  Fotos
  Videos
  Backup
)

# Papierkorb-Unterordner folgen dem Share (nur diese auf SSD1)
RECYCLE_SSD1_SUBDIRS=(
  Thomas
  Eltern
  Monika
  Giulia
  nas
)

LEGACY_MERGERFS_DUPES=(
  pcloud-archive
  pcloud-temp
)

usage() {
  cat <<'EOF'
Usage: nas-ssd-pin.sh <command> [options]

Pin map (see docs/NAS_SSD_PIN.md):
  SSD1: Thomas, Eltern, Gemeinsam, Monika, Giulia, restore, admin, av-quarantine, Paperless (media)
  SSD2: Fotos, Videos, Backup (inkl. Backup/Paperless)

Commands:
  show-pin         Print pin map
  analyze          Counts/sizes per dir; highlight wrong-branch copies
  conflicts        Same relative path on both SSDs, different md5
  migrate          rsync toward canonical SSD per dir (+ .recycle subdirs)
  verify           All files on wrong branch exist on canonical branch (md5)
  purge            Remove wrong-branch trees after verify
  cleanup-dupes    mergerfs pcloud-* dupes under /mnt/ssd1|ssd2 (not pipeline binds)
  show-fstab       Recommended mergerfs options (epmfs after pin)
  apply-fstab      Patch /etc/fstab (backup .bak-YYYYMMDD); does not remount

Options:
  --dry-run        rsync -n / print rm only
  --checksum       rsync -c
  --yes            Skip confirmation for purge / cleanup-dupes --delete
EOF
}

log() { echo "[$(date -Iseconds)] $*" | tee -a "$LOG"; }

require_root() {
  [[ "$(id -u)" -eq 0 ]] || { echo "run as root on pi-nas" >&2; exit 1; }
}

mkdir -p "$STATE_DIR"

all_pin_dirs() {
  printf '%s\n' "${SSD1_DIRS[@]}" "${SSD2_DIRS[@]}"
}

pin_ssd_for_dir() {
  local d="$1" x
  for x in "${SSD1_DIRS[@]}"; do [[ "$x" == "$d" ]] && { echo "$SSD1"; return 0; }; done
  for x in "${SSD2_DIRS[@]}"; do [[ "$x" == "$d" ]] && { echo "$SSD2"; return 0; }; done
  return 1
}

wrong_ssd_for_dir() {
  local d="$1" canon
  canon=$(pin_ssd_for_dir "$d")
  [[ "$canon" == "$SSD1" ]] && echo "$SSD2" || echo "$SSD1"
}

count_files() {
  local root="$1" sub="${2:-}"
  local p="$root"
  [[ -n "$sub" ]] && p="$root/$sub"
  if [[ -d "$p" ]]; then
    find "$p" -type f 2>/dev/null | wc -l
  else
    echo 0
  fi
}

dir_size() {
  local p="$1"
  if [[ -d "$p" ]]; then
    du -sh "$p" 2>/dev/null | awk '{print $1}'
  else
    echo "-"
  fi
}

cmd_show_pin() {
  echo "SSD1 ($(basename "$SSD1")):"
  printf '  %s\n' "${SSD1_DIRS[@]}"
  echo "SSD2 ($(basename "$SSD2")):"
  printf '  %s\n' "${SSD2_DIRS[@]}"
  echo "Recycle subdirs -> SSD1: ${RECYCLE_SSD1_SUBDIRS[*]}"
}

cmd_analyze() {
  require_root
  cmd_show_pin
  echo
  echo "=== Disk space ==="
  df -h "$SSD1" "$SSD2" "$MERGED"
  echo
  printf "%-16s %6s %10s %10s %10s %8s %8s %8s\n" "DIR" "PIN" "ssd1_files" "ssd2_files" "merged" "ssd1_sz" "ssd2_sz" "wrong?"
  local d pin c1 c2 cm s1 s2 wrong w1 w2
  while IFS= read -r d; do
    pin="?"
    if pin_ssd_for_dir "$d" &>/dev/null; then
      pin=$(basename "$(pin_ssd_for_dir "$d")")
    fi
    c1=$(count_files "$SSD1" "$d")
    c2=$(count_files "$SSD2" "$d")
    cm=$(count_files "$MERGED" "$d")
    s1=$(dir_size "$SSD1/$d")
    s2=$(dir_size "$SSD2/$d")
    wrong="-"
    if pin_ssd_for_dir "$d" &>/dev/null; then
      w1=0 w2=0
      [[ "$pin" == "ssd1" && "$c2" -gt 0 ]] && wrong="ssd2:$c2"
      [[ "$pin" == "ssd2" && "$c1" -gt 0 ]] && wrong="ssd1:$c1"
    fi
    printf "%-16s %6s %10s %10s %10s %8s %8s %8s\n" "$d" "$pin" "$c1" "$c2" "$cm" "$s1" "$s2" "$wrong"
  done < <(all_pin_dirs; printf '%s\n' "${LEGACY_MERGERFS_DUPES[@]}")
  echo
  echo "=== .recycle subdirs ==="
  for sub in "${RECYCLE_SSD1_SUBDIRS[@]}"; do
    c1=$(count_files "$SSD1/.recycle" "$sub")
    c2=$(count_files "$SSD2/.recycle" "$sub")
    printf "  .recycle/%-10s ssd1=%s ssd2=%s\n" "$sub" "$c1" "$c2"
  done
  echo
  grep -E 'mergerfs|/srv/nas' "$FSTAB" 2>/dev/null || true
  findmnt -n -o OPTIONS "$MERGED" 2>/dev/null || true
}

cmd_conflicts() {
  require_root
  local out="${STATE_DIR}/conflicts-$(date +%Y%m%d-%H%M%S).tsv"
  : >"$out"
  local d
  while IFS= read -r d; do
    [[ -d "$SSD1/$d" || -d "$SSD2/$d" ]] || continue
    while IFS= read -r rel; do
      [[ -z "$rel" ]] && continue
      local f1="$SSD1/$d/$rel" f2="$SSD2/$d/$rel"
      [[ -f "$f1" && -f "$f2" ]] || continue
      local m1 m2
      m1=$(md5sum "$f1" | awk '{print $1}')
      m2=$(md5sum "$f2" | awk '{print $1}')
      [[ "$m1" != "$m2" ]] && printf '%s\t%s\t%s\n' "$d/$rel" "$m1" "$m2" >>"$out"
    done < <(
      comm -12 \
        <(find "$SSD1/$d" -type f 2>/dev/null | sed "s|^$SSD1/$d/||" | sort) \
        <(find "$SSD2/$d" -type f 2>/dev/null | sed "s|^$SSD2/$d/||" | sort)
    )
  done < <(all_pin_dirs)
  local n
  n=$(wc -l <"$out")
  echo "Conflicts: $n -> $out"
  head -20 "$out"
}

rsync_toward() {
  local src="$1" dst="$2" dry="$3" checksum="$4"
  local opts=(-aHAX --human-readable --info=progress2 --partial)
  [[ "$dry" -eq 1 ]] && opts+=(-n -v)
  [[ "$checksum" -eq 1 ]] && opts+=(-c)
  mkdir -p "$dst"
  rsync "${opts[@]}" "$src" "$dst"
}

cmd_migrate() {
  require_root
  local dry=0 checksum=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run) dry=1 ;;
      --checksum) checksum=1 ;;
      *) echo "unknown: $1" >&2; exit 1 ;;
    esac
    shift
  done
  local d canon wrong src dst
  log "migrate start"
  while IFS= read -r d; do
    canon=$(pin_ssd_for_dir "$d")
    wrong=$(wrong_ssd_for_dir "$d")
    src="$wrong/$d/"
    dst="$canon/$d/"
    [[ -d "$src" ]] || { log "skip $d (nothing on wrong branch $wrong)"; continue; }
    log "PIN $d -> $canon: rsync $src -> $dst"
    rsync_toward "$src" "$dst" "$dry" "$checksum"
  done < <(all_pin_dirs)
  for sub in "${RECYCLE_SSD1_SUBDIRS[@]}"; do
    src="$SSD2/.recycle/$sub/"
    dst="$SSD1/.recycle/$sub/"
    [[ -d "$src" ]] || continue
    log "recycle $sub -> ssd1: rsync $src -> $dst"
    rsync_toward "$src" "$dst" "$dry" "$checksum"
  done
  log "migrate done"
}

cmd_verify() {
  require_root
  local missing=0 mismatch=0 d canon wrong
  while IFS= read -r d; do
    canon=$(pin_ssd_for_dir "$d")
    wrong=$(wrong_ssd_for_dir "$d")
    [[ -d "$wrong/$d" ]] || continue
    while IFS= read -r f; do
      local rel="${f#"$wrong/$d/"}"
      local t="$canon/$d/$rel"
      if [[ ! -f "$t" ]]; then
        echo "MISSING on $(basename "$canon"): $d/$rel"
        missing=$((missing + 1))
      else
        local m1 m2
        m1=$(md5sum "$f" | awk '{print $1}')
        m2=$(md5sum "$t" | awk '{print $1}')
        if [[ "$m1" != "$m2" ]]; then
          echo "MISMATCH: $d/$rel"
          mismatch=$((mismatch + 1))
        fi
      fi
    done < <(find "$wrong/$d" -type f 2>/dev/null)
  done < <(all_pin_dirs)
  for sub in "${RECYCLE_SSD1_SUBDIRS[@]}"; do
    [[ -d "$SSD2/.recycle/$sub" ]] || continue
    while IFS= read -r f; do
      local rel="${f#"$SSD2/.recycle/$sub/"}"
      local t="$SSD1/.recycle/$sub/$rel"
      if [[ ! -f "$t" ]]; then
        echo "MISSING recycle/$sub/$rel"
        missing=$((missing + 1))
      fi
    done < <(find "$SSD2/.recycle/$sub" -type f 2>/dev/null)
  done
  echo "verify: missing=$missing mismatch=$mismatch"
  [[ "$missing" -eq 0 && "$mismatch" -eq 0 ]]
}

cmd_purge() {
  require_root
  local yes=0 dry=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --yes) yes=1 ;;
      --dry-run) dry=1 ;;
      *) echo "unknown: $1" >&2; exit 1 ;;
    esac
    shift
  done
  cmd_verify || { echo "verify failed — fix before purge" >&2; exit 1; }
  echo "Will remove wrong-branch copies:"
  local d wrong
  while IFS= read -r d; do
    wrong=$(wrong_ssd_for_dir "$d")
    [[ -d "$wrong/$d" ]] && echo "  $wrong/$d ($(count_files "$wrong" "$d") files)"
  done < <(all_pin_dirs)
  for sub in "${RECYCLE_SSD1_SUBDIRS[@]}"; do
    [[ -d "$SSD2/.recycle/$sub" ]] && echo "  $SSD2/.recycle/$sub"
  done
  if [[ "$yes" -ne 1 ]]; then
    read -r -p "Type YES to delete wrong-branch trees: " confirm
    [[ "$confirm" == "YES" ]] || { echo "aborted"; exit 1; }
  fi
  while IFS= read -r d; do
    wrong=$(wrong_ssd_for_dir "$d")
    [[ -d "$wrong/$d" ]] || continue
    if [[ "$dry" -eq 1 ]]; then
      echo "rm -rf $wrong/$d"
    else
      log "rm -rf $wrong/$d"
      rm -rf "$wrong/$d"
    fi
  done < <(all_pin_dirs)
  for sub in "${RECYCLE_SSD1_SUBDIRS[@]}"; do
    [[ -d "$SSD2/.recycle/$sub" ]] || continue
    if [[ "$dry" -eq 1 ]]; then
      echo "rm -rf $SSD2/.recycle/$sub"
    else
      log "rm -rf $SSD2/.recycle/$sub"
      rm -rf "$SSD2/.recycle/$sub"
    fi
  done
  log "purge done"
}

cmd_cleanup_dupes() {
  require_root
  local delete=0 yes=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --delete) delete=1 ;;
      --yes) yes=1 ;;
      *) echo "unknown: $1" >&2; exit 1 ;;
    esac
    shift
  done
  local dup ssd p canon_archive="/srv/pcloud-archive"
  for dup in "${LEGACY_MERGERFS_DUPES[@]}"; do
    for ssd in "$SSD1" "$SSD2"; do
      p="$ssd/$dup"
      [[ -d "$p" ]] || continue
      echo "=== $p ($(dir_size "$p")) ==="
      if [[ "$dup" == "pcloud-archive" && -d "$canon_archive/manifests" ]]; then
        diff -rq "$p/manifests" "$canon_archive/manifests" 2>/dev/null | head -5 || true
      fi
      if [[ "$delete" -eq 1 ]]; then
        if [[ "$yes" -ne 1 ]]; then
          read -r -p "Delete $p? Type YES: " confirm
          [[ "$confirm" == "YES" ]] || continue
        fi
        log "rm -rf $p"
        rm -rf "$p"
      fi
    done
  done
}

recommended_mergerfs_opts() {
  # Nach Pin: jeder Top-Level-Ordner nur auf einem Branch -> epmfs legt Creates in bestehenden Pfad
  # (nicht mfs = global freieste Platte, nicht ff = alles SSD1)
  echo "defaults,allow_other,use_ino,cache.files=partial,category.create=epmfs,category.search=epmfs,category.action=epmfs,minfreespace=4G,fsname=mergerfs"
}

cmd_show_fstab() {
  echo "Recommended (after per-dir pin + purge):"
  echo "/mnt/ssd1:/mnt/ssd2  /srv/nas  fuse.mergerfs  $(recommended_mergerfs_opts)  0  0"
}

cmd_apply_fstab() {
  require_root
  local bak opts
  bak="${FSTAB}.bak-$(date +%Y%m%d)"
  cp -a "$FSTAB" "$bak"
  log "fstab backup $bak"
  if ! grep -qE 'fuse\.mergerfs' "$FSTAB"; then
    cmd_show_fstab
    exit 1
  fi
  opts=$(recommended_mergerfs_opts)
  while IFS= read -r line; do
    if [[ "$line" =~ fuse\.mergerfs ]] && [[ "$line" =~ /srv/nas ]]; then
      awk -v opts="$opts" '
        /fuse\.mergerfs/ && /\/srv\/nas/ {
          for (i = 1; i <= NF; i++) {
            if ($i ~ /^fuse\.mergerfs$/) { $(i+1) = opts; break }
          }
        }
        { print }
      ' <<<"$line"
    else
      echo "$line"
    fi
  done <"$FSTAB" >"${FSTAB}.new"
  if ! grep -q 'category.create=epmfs' "${FSTAB}.new"; then
    cmd_show_fstab
    rm -f "${FSTAB}.new"
    exit 1
  fi
  mv "${FSTAB}.new" "$FSTAB"
  echo "Patched. Remount: umount $MERGED && mount $MERGED"
  grep -E 'mergerfs|/srv/nas' "$FSTAB"
}

main() {
  local cmd="${1:-}"
  shift || true
  case "$cmd" in
    show-pin) cmd_show_pin "$@" ;;
    analyze) cmd_analyze "$@" ;;
    conflicts) cmd_conflicts "$@" ;;
    migrate) cmd_migrate "$@" ;;
    verify) cmd_verify "$@" ;;
    purge|purge-ssd2) cmd_purge "$@" ;;
    cleanup-dupes) cmd_cleanup_dupes "$@" ;;
    show-fstab) cmd_show_fstab "$@" ;;
    apply-fstab) cmd_apply_fstab "$@" ;;
    -h|--help|help|"") usage ;;
    *) echo "unknown: $cmd" >&2; usage; exit 1 ;;
  esac
}

main "$@"
