# CapyIdx diagrams

These diagrams are kept separately for design reviews and documentation sites
that want to embed only the visuals. They use Mermaid and render in GitHub,
GitLab, and Mermaid-compatible Markdown viewers.

## Component view

```mermaid
flowchart TB
    subgraph Clients
        Py[Python application]
        CX[Codex CLI]
    end
    subgraph CapyIdx
        API[Public async API]
        MCP[MCP stdio server]
        RT[Runtime]
        IDX[CodeIndexer]
        WALK[Walker + DiskOperations]
        REF[Refresh planner]
        CH[ChunkCodebaseIndex]
        PARSE[Tree-sitter chunker]
        LOOK[SymbolLookup]
        CACHE[SymbolCache]
        WATCH[AutoFileWatcher]
    end
    subgraph Persistence
        DB[(SQLite)]
        FS[(Repository files)]
    end

    Py --> API
    CX <-->|MCP stdio| MCP
    MCP --> RT
    API --> IDX
    RT --> IDX
    RT --> LOOK
    IDX --> WALK
    WALK --> FS
    IDX --> REF
    REF --> CH
    CH --> PARSE
    CH --> DB
    LOOK --> DB
    IDX --> WATCH
    WATCH --> FS
    RT --> CACHE
    CACHE --> LOOK
```

## Initial indexing

```mermaid
flowchart LR
    A[Validate repo] --> B[Resolve roots and Git tag]
    B --> C[Open SQLite and schema]
    C --> D[Walk files]
    D --> E[Compare tag_catalog + hashes]
    E --> F{Refresh operation}
    F -->|compute| G[Read and parse]
    F -->|delete| H[Delete old rows]
    F -->|add_tag| I[Add chunk tag]
    F -->|remove_tag| J[Remove chunk tag]
    G --> K[Replace symbols/chunks]
    H --> L[Mark catalog complete]
    I --> L
    J --> L
    K --> L
    L --> M[Yield progress]
```

## Incremental watch refresh

```mermaid
sequenceDiagram
    participant W as Watchdog or polling
    participant Q as Pending path set
    participant I as CodeIndexer
    participant R as Refresh planner
    participant DB as SQLite
    participant K as Symbol cache

    W->>Q: changed file batch
    Q->>K: invalidate path
    loop each flush interval
        I->>Q: take pending paths
        I->>R: plan only selected paths
        R->>DB: compare catalog/hash
        R-->>I: file operations
        I->>DB: replace/delete indexed data
        I->>I: clear in-flight paths
    end
```

## Symbol lookup and reconstruction

```mermaid
flowchart TD
    A["lookup_symbol(name)"] --> B[Scope by tags and paths]
    B --> C[Exact case-sensitive query]
    C --> D[Case-insensitive exact fallback]
    D --> E[Case-insensitive substring fallback]
    E --> F[Stable sort and SymbolMatch]
    F --> G{Unique exact?}
    G -->|yes| H[Selected SymbolCode]
    G -->|no| I[Candidate metadata]
    I --> J{get_symbol / resolve_lookup?}
    J -->|yes| K[Load symbol descendants]
    H --> K
    K --> L[Order chunks by idx/piece]
    L --> M[Assemble source]
```

## MCP request lifecycle

```mermaid
stateDiagram-v2
    [*] --> Starting
    Starting --> Indexing: build_runtime
    Indexing --> Ready: initial refresh done
    Indexing --> Reindexing: branch HEAD changed
    Ready --> Refreshing: file batch arrives
    Refreshing --> Ready: batch complete
    Ready --> Reindexing: branch HEAD changed
    Reindexing --> Ready: full refresh complete
    Ready --> Serving: tools/call
    Serving --> Ready: response returned
    Starting --> ShuttingDown: startup failure/cancel
    Ready --> ShuttingDown: server lifespan ends
    Serving --> ShuttingDown: server lifespan ends
    ShuttingDown --> [*]
```

