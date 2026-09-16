# GraphDB Representation Options for `raw_data/`

## Objective

Represent the complete `raw_data/` corpus in a way that improves research,
tracing, and reasoning while keeping the implementation practical.

The options below are filtered and ordered by **ease of implementation**.
They assume that GraphDB stores relationships and metadata, while exact source
content remains available through provenance references.

## 1. Source-file inventory graph — easiest

Create one typed node for every file and directory under `raw_data/`.

### Nodes

- `Directory`
- `SourceFile`

### Required properties

- deterministic `uid`
- relative path
- file name and extension
- size
- SHA-256 hash
- detected content type
- line count when the file is text
- repository-relative source path

### Relationships

- `Directory`-`CONTAINS`->`Directory`
- `Directory`-`CONTAINS`->`SourceFile`
- `SourceFile`-`IMPORTS`->`SourceFile` where imports can be resolved

### Why choose it

- Very easy to implement.
- Guarantees complete file-level coverage.
- Enables exact file lookup and change detection.
- Provides a reliable foundation for all later semantic extraction.

### Limitation

It does not provide detailed symbol-level or semantic reasoning by itself.

## 2. File inventory plus source chunks — recommended first upgrade

Extend the file inventory by splitting every text file into deterministic
`SourceChunk` nodes. Keep the original file as the authoritative source.

### Nodes

- `Directory`
- `SourceFile`
- `SourceChunk`

### Relationships

- `Directory`-`CONTAINS`->`SourceFile`
- `SourceFile`-`CONTAINS`->`SourceChunk`

### Chunk properties

- deterministic `uid`
- source file uid
- line start and line end
- byte or character offsets
- exact text or a content-store reference
- chunk hash
- chunk order

### Why choose it

- Still straightforward to implement.
- Preserves all text needed for evidence and context.
- Supports retrieval at file and local-context granularity.
- Can be added without replacing the current semantic extractor.

### Limitation

Fixed-size chunks may split logical code or documentation sections. Logical
chunking can be added later.

## 3. Current semantic graph plus provenance — easiest compatible refinement

Keep the existing labels such as `Function`, `Class`, `Module`, `Document`,
`Requirement`, `Concept`, and `API`, but attach every extracted item and
relationship to exact source evidence.

### Additions

- Add `SourceFile` and `SourceChunk` nodes.
- Add `source_file_uid`, `chunk_uid`, line range, and excerpt metadata.
- Add explicit provenance relationships:
  - `SourceFile`-`CONTAINS`->semantic entity
  - `SourceChunk`-`SUPPORTS`->semantic entity
  - `SourceChunk`-`EVIDENCES`->relationship

### Why choose it

- Reuses the current working method and schema.
- Requires fewer changes than replacing the extractor.
- Makes inferred facts auditable.
- Preserves graph-native typed labels and relationship types.

### Required quality controls

- Reject unresolved endpoints instead of guessing.
- Keep confidence on relationships, not only nodes.
- Store exact evidence for every inferred relationship.
- Separate explicit facts from inferred facts.
- Report false-positive candidates and ambiguous matches.

## 4. AST and documentation structure graph — moderate effort

Replace broad regex extraction for supported source languages with parser-backed
structural extraction where practical.

### Code entities

- modules and namespaces
- classes and structs
- functions and methods
- variables and constants
- types
- imports and includes
- call sites

### Documentation entities

- documents
- sections
- requirements
- APIs
- concepts

### Why choose it

- Produces more accurate tracing than token or regex matching.
- Improves calls, declarations, inheritance, and ownership relationships.
- Works well with the provenance layer from option 3.

### Limitation

Parser support is language-specific. Unsupported files still need the
source-file and chunk fallback.

## 5. Hybrid GraphDB plus full-text index — moderate effort, strongest practical design

Use GraphDB for typed entities and relationships, and a full-text index for
the complete original content. Link index documents or chunks to GraphDB uids.

### GraphDB responsibilities

- typed entities
- structural and semantic relationships
- provenance metadata
- traversal and dependency queries

### Full-text index responsibilities

- exact phrase search
- symbol search
- complete raw file content
- ranking and context retrieval

### Why choose it

- Best balance of fidelity, retrieval quality, and graph reasoning.
- Avoids forcing large raw text blobs into graph properties.
- Lets downstream agents validate graph claims against source text.

### Limitation

Requires operating and synchronizing a second storage/search component.

## Options not recommended as the first implementation

### Evidence ledger as a separate graph model

Useful for auditability, but it introduces extra evidence nodes and relation
management before basic corpus coverage is guaranteed. Add it after options 2
or 3 are working.

### Full multi-layer reasoning graph

Combines physical, structural, semantic, provenance, and inferred reasoning
layers. It is the most complete long-term architecture, but it has the highest
implementation and maintenance cost. Build it incrementally from the earlier
options.

## Recommended implementation path

1. Implement the complete `Directory`/`SourceFile` inventory.
2. Add deterministic `SourceChunk` nodes for every readable text file.
3. Attach the current semantic entities and relations to source files and
   chunks with exact line ranges and evidence snippets.
4. Replace the most error-prone regex extraction with parser-backed extraction
   for the languages that matter most.
5. Add a full-text index if GraphDB text lookup is insufficient.
6. Add higher-level inferred relations only when they remain evidence-backed
   and clearly marked by confidence.

## Final selection by ease and value

| Rank | Option | Ease | Value | Recommendation |
|---|---|---:|---:|---|
| 1 | Source-file inventory graph | Very high | Medium | Required foundation |
| 2 | File inventory plus source chunks | High | High | Implement immediately after inventory |
| 3 | Current semantic graph plus provenance | High | Very high | Best refinement of the current method |
| 4 | AST and documentation structure graph | Medium | Very high | Add incrementally |
| 5 | Hybrid GraphDB plus full-text index | Medium | Very high | Strongest practical target |
| 6 | Separate evidence ledger | Medium-low | High | Add after provenance basics |
| 7 | Full multi-layer reasoning graph | Low | Highest | Long-term architecture |

## Recommended target

The most implementable high-quality target is:

> **Source-file inventory + source chunks + the current semantic graph with
> mandatory provenance, followed by a full-text index when needed.**

This preserves the complete corpus, keeps the current graph-native model, and
allows every research or reasoning result to be traced back to exact source
content.
