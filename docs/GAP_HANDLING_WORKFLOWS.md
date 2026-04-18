# Gap-Handling: Workflow-Diagramme

Visuelle Darstellung der Gap-Handling-Workflows mit Mermaid-Diagrammen.

---

## 📊 Hauptworkflow: Gap-Detection & Handling

```mermaid
flowchart TD
    Start([Backup-Run Start]) --> LoadSnaps[Load Local Snapshots]
    LoadSnaps --> LoadRemote[Load Remote Snapshots]
    LoadRemote --> LoopStart{For each<br/>local snapshot}
    
    LoopStart -->|Next| CheckRemote{Remote<br/>exists?}
    CheckRemote -->|YES| LoopStart
    CheckRemote -->|NO| CheckLater{Later snapshots<br/>exist remote?}
    
    CheckLater -->|NO| NewSnapshot[New Snapshot<br/>Regular Upload]
    NewSnapshot --> BuildPush[build_and_push]
    BuildPush --> Counter1[new_count++]
    Counter1 --> LoopStart
    
    CheckLater -->|YES| GapDetected[🚨 Gap Detected!]
    GapDetected --> Strategy{Gap Strategy}
    
    Strategy -->|conservative| Abort[❌ ABORT<br/>Manual Intervention]
    Abort --> End([EXIT 1])
    
    Strategy -->|optimistic| Validate[🔍 Validate Integrity<br/>All Later Snapshots]
    Validate --> ValidateLoop{For each<br/>later snapshot}
    
    ValidateLoop -->|Next| CallValidate[validate_snapshot_integrity]
    CallValidate --> Status{Status?}
    
    Status -->|OK| ValidateLoop
    Status -->|MISSING_MANIFEST<br/>or BROKEN_CHAIN| SetBroken[needs_rebuild = 1]
    SetBroken --> ScenarioA
    
    ValidateLoop -->|All OK| ScenarioB[✅ Scenario B<br/>Intact Chain]
    ScenarioB --> UploadGap[Upload Gap Only]
    UploadGap --> Counter2[gap_count++]
    Counter2 --> LoopStart
    
    ValidateLoop -->|Any Broken| ScenarioA[⚠️ Scenario A<br/>Broken Chain]
    ScenarioA --> DeleteLoop{For each<br/>later snapshot}
    DeleteLoop -->|Next| DeleteRemote[delete_remote_snapshot]
    DeleteRemote --> DeleteLoop
    DeleteLoop -->|Done| UploadGapA[Upload Gap]
    UploadGapA --> RebuildLoop{For each<br/>later snapshot}
    RebuildLoop -->|Next| RebuildUpload[build_and_push<br/>Rebuild]
    RebuildUpload --> Counter3[rebuilt_count++]
    Counter3 --> RebuildLoop
    RebuildLoop -->|Done| Counter4[gap_count++]
    Counter4 --> LoopStart
    
    Strategy -->|aggressive| AggressiveDelete[DELETE All Later<br/>No Validation]
    AggressiveDelete --> AggressiveUpload[Upload Gap + Rebuild All]
    AggressiveUpload --> Counter5[gap_count++<br/>rebuilt_count += N]
    Counter5 --> LoopStart
    
    LoopStart -->|All Done| UpdateMetrics[Update MariaDB Metrics]
    UpdateMetrics --> Success([✅ SUCCESS])
    
    style GapDetected fill:#ff9999
    style ScenarioA fill:#ffcc99
    style ScenarioB fill:#99ff99
    style Abort fill:#ff6666
    style Success fill:#66ff66
```

---

## 🔍 Integritäts-Validierung (validate_snapshot_integrity)

```mermaid
flowchart TD
    Start([validate_snapshot_integrity]) --> Input[Input: snapshot name]
    Input --> CheckManifest{Manifest<br/>exists locally?}
    
    CheckManifest -->|NO| ReturnMissing[Return:<br/>MISSING_MANIFEST]
    ReturnMissing --> End([END])
    
    CheckManifest -->|YES| LoadManifest[Load manifest.json]
    LoadManifest --> ExtractRef[jq '.ref_snapshot']
    ExtractRef --> CheckRef{ref_snapshot<br/>== null?}
    
    CheckRef -->|YES| FirstSnapshot[First Snapshot<br/>No Reference Needed]
    FirstSnapshot --> ReturnOK[Return: OK]
    ReturnOK --> End
    
    CheckRef -->|NO| CheckRemoteRef{ref_snapshot<br/>exists remote?}
    CheckRemoteRef -->|NO| ReturnBroken[Return:<br/>BROKEN_CHAIN]
    ReturnBroken --> End
    
    CheckRemoteRef -->|YES| OptionalDeep{Deep Validation<br/>enabled?}
    OptionalDeep -->|NO| ReturnOK
    OptionalDeep -->|YES| RunDelta[pcloud_quick_delta]
    RunDelta --> DeltaStatus{Delta Status?}
    DeltaStatus -->|OK| ReturnOK
    DeltaStatus -->|FAILED| ReturnBroken
    
    style ReturnMissing fill:#ff9999
    style ReturnBroken fill:#ff9999
    style ReturnOK fill:#99ff99
```

---

## 🗑️ Remote-Snapshot-Löschung (delete_remote_snapshot)

```mermaid
flowchart TD
    Start([delete_remote_snapshot]) --> Input[Input: snapshot name]
    Input --> Log[_log INFO 'Deleting...' ]
    Log --> Python[Python Inline Script]
    Python --> LoadLib[import pcloud_bin_lib]
    LoadLib --> GetConfig[effective_config]
    GetConfig --> BuildPath[Construct snap_path]
    BuildPath --> Path[/Backup/rtb_1to1/_snapshots/NAME]
    
    Path --> APICall{delete_folder API}
    APICall -->|Success| ReturnOK[print 'OK']
    ReturnOK --> ExitSuccess([EXIT 0])
    
    APICall -->|Error| ReturnErr[print ERROR to stderr]
    ReturnErr --> ExitFail([EXIT 1])
    
    style ExitSuccess fill:#99ff99
    style ExitFail fill:#ff9999
```

---

## 📋 Scenario-Vergleich (Side-by-Side)

```mermaid
flowchart LR
    subgraph ScenarioA["Scenario A: Broken Chain"]
        A1[Gap: 2026-04-15] --> A2[Later: 2026-04-16, 17]
        A2 --> A3[validate 2026-04-16]
        A3 --> A4{ref_snapshot exists?}
        A4 -->|NO| A5[BROKEN_CHAIN]
        A5 --> A6[DELETE 16, 17]
        A6 --> A7[UPLOAD 15]
        A7 --> A8[REBUILD 16, 17]
        A8 --> A9[✅ Chain Repaired]
    end
    
    subgraph ScenarioB["Scenario B: Intact Chain"]
        B1[Gap: 2026-04-15] --> B2[Later: 2026-04-16, 17]
        B2 --> B3[validate 2026-04-16]
        B3 --> B4{ref_snapshot exists?}
        B4 -->|YES, locally| B5[OK]
        B5 --> B6[validate 2026-04-17]
        B6 --> B7[OK]
        B7 --> B8[UPLOAD 15 Only]
        B8 --> B9[✅ Gap Filled<br/>3x Faster!]
    end
    
    style A5 fill:#ff9999
    style A9 fill:#99ff99
    style B5 fill:#99ff99
    style B7 fill:#99ff99
    style B9 fill:#66ff66
```

---

## 🎯 Strategie-Entscheidungsbaum

```mermaid
flowchart TD
    Start{Gap Detected} --> Strategy{PCLOUD_GAP_STRATEGY}
    
    Strategy -->|conservative| C1[❌ Abort Immediately]
    C1 --> C2[Log Error Message]
    C2 --> C3[EXIT 1]
    C3 --> C4([Manual Intervention<br/>Required])
    
    Strategy -->|optimistic| O1[🔍 Validate Later Snapshots]
    O1 --> O2{All Valid?}
    O2 -->|YES| O3[✅ Scenario B]
    O3 --> O4[Upload Gap Only]
    O4 --> O5[⚡ Fast Path]
    
    O2 -->|NO| O6[⚠️ Scenario A]
    O6 --> O7[Delete Later Snapshots]
    O7 --> O8[Upload Gap + Rebuild]
    O8 --> O9[🔒 Safe Path]
    
    Strategy -->|aggressive| A1[🔨 No Validation]
    A1 --> A2[Delete All Later]
    A2 --> A3[Upload Gap + Rebuild All]
    A3 --> A4[🛡️ Paranoid Path<br/>Always Safe]
    
    style C3 fill:#ff6666
    style O5 fill:#66ff66
    style O9 fill:#99ff99
    style A4 fill:#ffcc99
```

---

## ⏱️ Performance-Vergleich (Timeline)

```mermaid
gantt
    title Gap-Handling Performance (150 GB per Snapshot)
    dateFormat HH:mm
    axisFormat %H:%M
    
    section Scenario A (Broken)
    Validation (10s)           :a1, 00:00, 1m
    Delete Later (2min)        :a2, 00:01, 2m
    Upload Gap (7h)            :a3, 00:03, 420m
    Rebuild 2026-04-16 (7h)    :a4, 07:03, 420m
    Rebuild 2026-04-17 (7h)    :a5, 14:03, 420m
    Total: 21h                 :milestone, 21:03, 0m
    
    section Scenario B (Optimistic)
    Validation (10s)           :b1, 00:00, 1m
    Upload Gap ONLY (7h)       :b2, 00:01, 420m
    Done!                      :milestone, 07:01, 0m
    
    section Scenario B (Aggressive)
    No Validation              :c1, 00:00, 0m
    Delete Later (2min)        :c2, 00:00, 2m
    Upload Gap (7h)            :c3, 00:02, 420m
    Rebuild 16 (7h)            :c4, 07:02, 420m
    Rebuild 17 (7h)            :c5, 14:02, 420m
    Total: 21h (WASTE!)        :milestone, 21:02, 0m
```

**Legende:**
- **Scenario A (Broken):** Rebuild nötig → 21h (korrekt)
- **Scenario B (Optimistic):** Nur Gap → 7h (**3x schneller**)
- **Scenario B (Aggressive):** Unnötiger Rebuild → 21h (14h verschwendet!)

---

## 🔄 Build & Push Workflow (build_and_push)

```mermaid
sequenceDiagram
    participant W as wrapper_pcloud_sync
    participant M as pcloud_json_manifest.py
    participant P as pcloud_push_json_manifest.py
    participant D as pcloud_quick_delta.py
    participant DB as MariaDB
    participant API as pCloud API
    
    W->>M: Generate manifest
    M->>M: Scan snapshot dir
    M->>M: Calculate SHA256 hashes
    M->>M: Use ref_manifest (Smart-Mode)
    M-->>W: manifest.json
    
    W->>P: Upload snapshot
    P->>API: Create _snapshots/NAME
    P->>API: Upload new files
    P->>API: Create stubs (copyfolder)
    P-->>W: Upload complete
    
    W->>D: Delta-Verification
    D->>API: listfolder (Live state)
    D->>D: Compare vs. content_index
    D->>D: Check anchors, hashes
    D-->>W: delta_report.json (OK)
    
    W->>DB: Update metrics
    DB-->>W: ACK
    
    Note over W: Snapshot successfully synced!
```

---

## 🔍 Gap-Erkennung (Detection-Algorithmus)

```mermaid
flowchart TD
    Start([Local Snapshots]) --> L1[2026-04-13]
    L1 --> L2[2026-04-14]
    L2 --> L3[2026-04-15]
    L3 --> L4[2026-04-16]
    L4 --> L5[2026-04-17]
    
    Start2([Remote Snapshots]) --> R1[2026-04-13]
    R1 --> R2[2026-04-14]
    R2 --> R3[❌ Missing]
    R3 --> R4[2026-04-16]
    R4 --> R5[2026-04-17]
    
    L3 -.->|Compare| R3
    R3 --> Gap{is_gap?}
    Gap -->|Check later| R4
    Gap -->|Check later| R5
    R4 -.->|exists| Yes1[Later: 2026-04-16]
    R5 -.->|exists| Yes2[Later: 2026-04-17]
    
    Yes1 --> Result[🚨 GAP Detected!<br/>Missing: 2026-04-15<br/>Later: 2026-04-16, 17]
    Yes2 --> Result
    
    style R3 fill:#ff9999
    style Result fill:#ff9999
```

---

## 🎨 State-Machine (Gap-Handling-States)

```mermaid
stateDiagram-v2
    [*] --> Scanning: Start Backup Run
    
    Scanning --> NoGap: All Snapshots Present
    Scanning --> GapFound: Missing Snapshot + Later Exist
    Scanning --> NewSnapshot: Missing Snapshot, No Later
    
    NoGap --> [*]: Done
    
    NewSnapshot --> Upload: Regular Upload
    Upload --> [*]: Done
    
    GapFound --> Conservative: Strategy: conservative
    GapFound --> Optimistic: Strategy: optimistic
    GapFound --> Aggressive: Strategy: aggressive
    
    Conservative --> Abort: Exit 1
    Abort --> [*]: Manual Fix Required
    
    Optimistic --> Validate: Check Later Snapshots
    Validate --> ScenarioB: All OK
    Validate --> ScenarioA: Any Broken
    
    ScenarioB --> UploadGap: Upload Gap Only
    UploadGap --> [*]: Done (Fast)
    
    ScenarioA --> DeleteLater: Remove Broken
    DeleteLater --> RebuildChain: Upload Gap + Later
    RebuildChain --> [*]: Done (Repaired)
    
    Aggressive --> ForceRebuild: No Validation
    ForceRebuild --> RebuildAll: Delete + Upload All
    RebuildAll --> [*]: Done (Safe)
    
    note right of Optimistic
        Intelligence:
        Distinguishes
        Scenario A vs. B
    end note
    
    note right of ScenarioB
        Performance:
        3x-10x faster
    end note
```

---

## 📊 Metrics Update Flow

```mermaid
flowchart LR
    subgraph Counting["Metric Counters"]
        C1[gap_count = 0]
        C2[new_count = 0]
        C3[rebuilt_count = 0]
        C4[uploaded_count = 0]
    end
    
    subgraph Events["Events"]
        E1[Gap Filled] --> C1
        E2[New Snapshot] --> C2
        E3[Snapshot Rebuilt] --> C3
        E1 --> C4
        E2 --> C4
        E3 --> C4
    end
    
    subgraph Database["MariaDB Update"]
        C1 --> DB[gaps_synced]
        C2 --> DB2[new_snapshots]
        C3 --> DB3[rebuilt_snapshots]
        C4 --> DB4[uploaded_count]
    end
    
    subgraph Logging["JSONL Log"]
        DB --> L1[timestamp]
        DB --> L2[level: INFO]
        DB --> L3[message]
        DB --> L4[run_id]
    end
    
    style DB fill:#99ccff
    style DB2 fill:#99ccff
    style DB3 fill:#99ccff
    style DB4 fill:#99ccff
```

---

## 🧪 Testing-Workflows

### Test 1: Conservative Mode

```mermaid
sequenceDiagram
    participant Admin
    participant Web as pCloud Web-UI
    participant Script as wrapper_pcloud_sync
    participant DB as MariaDB
    
    Admin->>Web: Delete 2026-04-14
    Web-->>Admin: ✓ Deleted
    
    Admin->>Script: Run (conservative)
    Script->>Script: Detect Gap
    Script->>Script: Check Later Snapshots
    Script->>Script: Strategy: conservative
    Script-->>Admin: ❌ ABORT "Manual intervention"
    Script->>DB: run_status = 'FAILED'
    DB-->>Script: ACK
    
    Note over Admin: Expected: Script stops safely
```

---

### Test 2: Optimistic Scenario B

```mermaid
sequenceDiagram
    participant Admin
    participant Web as pCloud Web-UI
    participant Script as wrapper_pcloud_sync
    participant API as pCloud API
    
    Admin->>Web: Delete 2026-04-14
    Web-->>Admin: ✓ Deleted
    
    Admin->>Script: Run (optimistic)
    Script->>Script: Detect Gap
    Script->>Script: Validate 2026-04-15 → OK
    Script->>Script: Validate 2026-04-16 → OK
    Script->>Script: Decision: Scenario B
    
    Script->>API: Upload 2026-04-14 ONLY
    API-->>Script: ✓ Complete
    
    Script-->>Admin: ✅ SUCCESS<br/>(gaps: 1, rebuilt: 0)
    
    Note over Admin: Expected: Fast (7h), No Rebuild
```

---

### Test 3: Optimistic Scenario A

```mermaid
sequenceDiagram
    participant Admin
    participant Script as wrapper_pcloud_sync
    participant API as pCloud API
    
    Admin->>Script: Delete manifest/2026-04-13.json
    Admin->>Script: Run (optimistic)
    
    Script->>Script: Detect Gap: 2026-04-13
    Script->>Script: Validate 2026-04-14
    Script->>Script: ref_snapshot missing → BROKEN
    Script->>Script: Decision: Scenario A
    
    Script->>API: DELETE 2026-04-14
    Script->>API: DELETE 2026-04-15
    Script->>API: DELETE 2026-04-16
    
    Script->>API: UPLOAD 2026-04-13 (Gap)
    Script->>API: UPLOAD 2026-04-14 (Rebuild)
    Script->>API: UPLOAD 2026-04-15 (Rebuild)
    Script->>API: UPLOAD 2026-04-16 (Rebuild)
    
    API-->>Script: ✓ All Complete
    Script-->>Admin: ✅ SUCCESS<br/>(gaps: 1, rebuilt: 3)
    
    Note over Admin: Expected: Slow (21h+), Chain Repaired
```

---

## 🔗 Integration mit RTB-Pipeline

```mermaid
flowchart TD
    Cron([Systemd Timer]) --> RTB[rtb_wrapper.sh]
    RTB --> Safety[EntropyWatcher Safety Gate]
    Safety -->|Green| Rsync[rsync_tmbackup.sh]
    Safety -->|Red/Yellow| Abort([ABORT])
    
    Rsync --> Snapshot[Create Local Snapshot]
    Snapshot --> Wrapper[wrapper_pcloud_sync_1to1.sh]
    
    Wrapper --> Preflight[Preflight Checks]
    Preflight --> Gap[Gap Detection]
    Gap -->|No Gap| Upload[Regular Upload]
    Gap -->|Gap Found| Handler[Gap Handler]
    
    Handler --> Strategy{Strategy}
    Strategy -->|conservative| ManualFix
    Strategy -->|optimistic| SmartFix[Smart Repair]
    Strategy -->|aggressive| ForceFix[Force Rebuild]
    
    Upload --> Verify[Delta Verification]
    SmartFix --> Verify
    ForceFix --> Verify
    
    Verify --> DB[(MariaDB)]
    DB --> Done([✅ Done])
    
    style Safety fill:#99ff99
    style Abort fill:#ff9999
    style Done fill:#66ff66
```

---

**📚 Weitere Dokumentation:**
- [GAP_HANDLING.md](GAP_HANDLING.md) - Vollständige Doku
- [GAP_HANDLING_QUICKSTART.md](GAP_HANDLING_QUICKSTART.md) - Quick-Start
- [GAP_HANDLING_FAQ.md](GAP_HANDLING_FAQ.md) - FAQ

---

*Diagramme generiert mit Mermaid.js*  
*Letzte Aktualisierung: 2026-04-16*
