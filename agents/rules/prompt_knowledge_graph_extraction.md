# SYSTEM ROLE: Knowledge Graph Extraction Agent - Production-Grade Codebase Analysis
# VERSION: 5.0 - Parser-First, Incrementally-Updatable Knowledge Graph
#
# CHANGELOG vs 4.1 (v2, archiviata in `output/v2_extraction_script_nrf_example/`):
# - root cause analysis completa in
#   `output/v2_extraction_script_nrf_example/17_09_2026.md`. In sintesi: v2
#   dichiarava "AST-first, regex come fallback" ma lo script che ne è stato
#   derivato usava regex su prosa non filtrata per TUTTA l'estrazione
#   codice/documentazione, producendo entità come "Device", "Guidelines",
#   "ABC" da parole inglesi comuni, e relazioni `REFERENCES`/`SATISFIES`
#   fasulle da matching per nome non scoped (`nodes_by_name[name.casefold()]`
#   su tutta la repository).
# - v5 rende il vincolo "usare un parser" NON aggirabile: specifica
#   esattamente quale libreria usare per ogni linguaggio (sezione
#   PARSER REQUIREMENTS), vieta esplicitamente il pattern regex che ha
#   causato il problema, e introduce nodi/relazioni dedicati per Kconfig e
#   Devicetree (la superficie di implementazione più rilevante per un SDK
#   firmware, assente in v2).
# - v5 aggiunge la INCREMENTAL UPDATE SPECIFICATION: il grafo deve poter
#   essere aggiornato in base ai soli file cambiati in `/raw_data/`, non
#   richiedere una re-ingestion completa ad ogni modifica.
# - v5 revoca il divieto "no nuove dipendenze" limitatamente ai parser
#   elencati in PARSER REQUIREMENTS: il divieto stesso è la causa diretta
#   per cui v2 è ricaduta su regex (nessun parser C/Kconfig/Devicetree era
#   autorizzato). Restano vietati accesso a rete a runtime e dipendenze non
#   motivate da un parser mancante.

## MISSION
Generare uno script Python che estragga una rete semantica **evidence-backed**
di entità e relazioni dal codice sorgente, configurazione e documentazione in
`/raw_data/` per l'integrazione su GraphDB, e che possa **ri-eseguire
l'estrazione in modo incrementale** quando `/raw_data/` cambia, senza dover
ricostruire l'intero grafo ogni volta.

**CRITICAL REQUIREMENT**: Ogni entità e relazione DEVE essere tracciabile a
evidenza testuale esatta nel source, prodotta da un **parser reale del
linguaggio**, non da un pattern regex su testo libero. Le uniche eccezioni
regex ammesse sono quelle elencate esplicitamente in PARSER REQUIREMENTS per
formati talmente semplici e non ambigui (es. `CONFIG_X=y`) da non avere una
grammatica dedicata disponibile.

Priorità: **provenance accuracy > completezza file-level > qualità relazioni
semantiche > aggiornabilità incrementale > velocità**.

## INPUT SPECIFICATION
- Root directory: `/raw_data/` (ricorsivo, tutte le sottocartelle, TUTTI i file)
- Reference implementation precedente: `/output/v2_extraction_script_nrf_example/extraction_script.py`
  (SOLO come riferimento storico di cosa NON fare; vedi
  `output/v2_extraction_script_nrf_example/17_09_2026.md`)
- Real-data constraint: basarsi esclusivamente su file presenti in `/raw_data/`

## OUTPUT SPECIFICATION
- Script Python: `/output/extraction_script.py` (eseguibile, autonomo)
- Entities: JSONL con schema definito sotto (`/output/entities.jsonl`)
- Relations: JSONL con provenance obbligatoria (`/output/relations.jsonl`)
- Source chunks: `/output/source_chunks.jsonl` (testo segmentato con evidenza)
- Manifest di ingestion incrementale: `/output/ingestion_manifest.json`
  (sha256 per file, usato per calcolare added/modified/deleted tra due run)
- Logs: `/logs/agents/extraction_ops_level_{1-5}.log`
- Access log: `/logs/access_log.jsonl`
- Ambiguities report: `/output/ambiguities.json`
- Unresolved relations report: `/output/unresolved_relations.json`
- Graph schema report: `/output/graph_schema.json`

## PARSER REQUIREMENTS (MANDATORY, NON-NEGOTIABLE)

Ogni linguaggio/formato riconosciuto DEVE essere estratto con il parser
elencato qui sotto. È vietato scrivere un pattern regex per identificare
funzioni, classi/struct, opzioni Kconfig, nodi Devicetree, o riferimenti
incrociati documentazione↔codice quando il linguaggio ha un parser assegnato
in questa tabella.

| Formato | Estensioni/nomi file | Parser obbligatorio |
|---------|----------------------|----------------------|
| Python | `.py` | modulo nativo `ast` |
| C / C++ | `.c .h .cc .cpp .cxx .hh .hpp .ipp` | `tree_sitter_language_pack.get_parser("c"\|"cpp")` |
| Kconfig (dichiarazioni) | file chiamati `Kconfig` o `Kconfig.*` | `tree_sitter_language_pack.get_parser("kconfig")` |
| Kconfig (valori/fragment) | `.conf`, `*_defconfig`, `*.defconfig` | parser di linea dedicato (vedi sotto; sintassi `CONFIG_X=valore` non ha ambiguità e non richiede una grammatica) |
| Devicetree | `.dts .dtsi .overlay` | `tree_sitter_language_pack.get_parser("devicetree")` |
| Devicetree bindings | `.yaml/.yml` sotto `dts/bindings/**` | `PyYAML` (`yaml.safe_load`), non tree-sitter: servono i valori semantici (`compatible`, `properties`), non solo la sintassi |
| reStructuredText | `.rst` | `tree_sitter_language_pack.get_parser("rst")` |
| Markdown | `.md .markdown` | parser di linea dedicato per heading (`^#{1,6}\s`) e code span (`` `testo` ``); sintassi non ambigua, non richiede tree-sitter |
| CMake | `.cmake`, `CMakeLists.txt` | fuori scope v5: inventariare e segmentare (`SourceFile` + `SourceChunk`), NON estrarre entità semantiche. Documentato come lavoro futuro. |
| Altro YAML/JSON/TOML generico | non sotto `dts/bindings/` | `PyYAML`/`json`/`tomllib` per validare che sia testo strutturato leggibile, poi solo inventario + chunk. Nessuna entità semantica inventata. |
| Tutto il resto (immagini, certificati, binari, testo libero senza struttura riconosciuta) | — | solo inventario (`SourceFile`) + chunk se decodificabile come testo. Nessuna entità semantica. |

**VINCOLO DI INSTALLAZIONE**: `tree_sitter` e `tree_sitter_language_pack` sono
dipendenze autorizzate e obbligatorie (wheel precompilati, nessun bisogno di
un compilatore C o di preprocessare gli header). Vanno aggiunte a
`python_venv_requirements.txt`. Non sono ammesse altre dipendenze non
motivate da una riga di questa tabella.

**REGOLA ANTI-REGRESSIONE (deriva direttamente dal bug osservato in v2)**:
è ESPLICITAMENTE VIETATO un pattern come il seguente, che ha causato la
generazione di entità da parole inglesi comuni:

```python
# VIETATO - causa reale del bug in v2, non riproporre in nessuna forma
re.finditer(r"(?i)\b(?:api|endpoint|interface|function)\s*[:`]*\s*([A-Za-z_]\w*)", text)
```

Qualunque estrazione di riferimenti API/simboli da un documento DEVE passare
da un marcatore strutturale esplicito (ruolo Sphinx/RST, code span Markdown,
nodo AST), MAI da una parola-chiave seguita da testo libero.

## MANDATORY: FILE-LEVEL INVENTORY (INVARIATO DA v1/v2)
**Primo passo obbligatorio**: Creare un nodo per OGNI file e directory in `/raw_data/`.

### Node Types - File System Layer
| Label | Proprietà Obbligatorie |
|-------|----------------------|
| `:Folder` | uid, path, name, parent_uid (nota: `Directory` è una parola riservata nella grammatica Cypher di Memgraph — `CREATE INDEX ON :Directory(...)` fallisce — quindi la label usata è `:Folder`) |
| `:SourceFile` | uid, path, name, extension, size_bytes, sha256_hash, line_count (se testo), detected_type (code/doc/config/binary/other), language |

### Relationships - File System Layer
```cypher
(:Folder)-[:CONTAINS]->(:Folder)
(:Folder)-[:CONTAINS]->(:SourceFile)
(:SourceFile)-[:INCLUDES]->(:SourceFile)   // #include/import risolto contro l'albero reale dei file
```

**VINCOLO**: Se un file esiste in `/raw_data/`, DEVE avere un nodo `:SourceFile`.
Nessuna eccezione. L'elenco canonico dei file DEVE essere costruito con una
scansione ricorsiva del filesystem (`RAW_DATA_DIR.rglob("*")`), mai da una
lista `processable`/`supported_files` filtrata per estensione prima
dell'inventario.

Il conteggio deve essere verificato prima dell'export e dopo l'ingestion:
```python
discovered_count = len(all_files)
exported_count = len(file_inventory)
source_file_count = count_entities(label="SourceFile")
if not (discovered_count == exported_count == source_file_count):
    raise RuntimeError("File completeness failure")
```

## SOURCE CHUNK SPECIFICATION (INVARIATO)
Ogni file di testo DEVE essere segmentato in nodi `:SourceChunk` a finestra
fissa (200 righe, senza overlap) per garantire evidenza recuperabile anche
per i file senza parser semantico dedicato. Ogni entità semantica riporta in
`metadata.source_chunk_uid` il chunk che la contiene, oltre a conservare
`source_text` (lo snippet esatto prodotto dal parser) quando disponibile:
questa è la evidenza a grana fine, il chunk è l'evidenza a grana di file.

```python
{
    "uid": "SourceChunk|<source_file>|<line_start>-<line_end>|<hash12>",
    "source_file": "<relative path>",
    "line_start": int,
    "line_end": int,
    "content": "<testo esatto>",
    "chunk_type": "fixed_window",
}
```

## NODE SCHEMA (AGGIORNATO v5)

Ogni entità semantica riporta `extraction_method` (es. `"tree-sitter-c"`,
`"tree-sitter-kconfig"`, `"tree-sitter-devicetree"`, `"tree-sitter-rst"`,
`"python-ast"`, `"pyyaml"`, `"markdown-heading"`, `"kconfig-fragment"`) al
posto di un punteggio di confidence numerico: l'esistenza stessa dell'entità
è già certificata dal parser. Il confidence score resta **esclusivamente**
una proprietà delle relazioni (vedi sezione dedicata).

| Label | Proprietà chiave | Da chi è generato |
|-------|-------------------|--------------------|
| `:Function` | uid, name, file, line_start, line_end, kind (`declaration`\|`definition`), signature, return_type, parameters, source_text | tree-sitter c/cpp, python ast |
| `:Class` | uid, name, file, line_start, line_end, kind (`struct`\|`union`\|`class`), source_text | tree-sitter c/cpp, python ast |
| `:Type` | uid, name, file, line_start, line_end, kind (`typedef`\|`enum`), underlying, source_text | tree-sitter c/cpp |
| `:Macro` | uid, name, file, line_start, line_end, kind (`object`\|`function`), parameters, value_text | tree-sitter c/cpp |
| `:Variable` | uid, name, file, line_start, line_end, source_text | tree-sitter c/cpp (solo scope file/translation-unit, MAI variabili locali), python ast (assegnazioni a livello di modulo) |
| `:KconfigOption` | uid (`KconfigOption\|{NAME}`, globale, non scoped per file), name, file, type (`bool`\|`int`\|`string`\|`hex`\|`tristate`), prompt, help_text | tree-sitter kconfig |
| `:DTNode` | uid, name (label se presente, altrimenti nome@unit_address), file, node_path, label, unit_address, compatible (lista) | tree-sitter devicetree |
| `:DTBinding` | uid (`DTBinding\|{compatible}`, globale), compatible, file, description | PyYAML su `dts/bindings/**/*.yaml` |
| `:Document` | uid, path, title, doc_type (`rst`\|`md`\|`txt`) | inventario + tree-sitter rst / markdown |
| `:Concept` | uid, name, file, line_start, heading_level | tree-sitter rst (nodo `title`/`section`), markdown heading |
| `:Requirement` | uid, text, file, line_start | tree-sitter rst, SOLO su frasi con verbo modale esplicito (vedi REQUIREMENT EXTRACTION) |
| `:SourceChunk` | uid, source_file, line_start, line_end, content, chunk_type | chunking a finestra fissa |

**Nodi rimossi rispetto a v2**: `:API` generico e `:Module` generico sono
eliminati. Il primo esisteva solo per la regex vietata sopra; il secondo
duplicava `:SourceFile` senza aggiungere informazione (ogni file aveva sia un
nodo `:Module` sia, se documento, un nodo `:Document` per la stessa entità
fisica). I simboli di codice si collegano direttamente a `:SourceFile` con
`DECLARES`; i riferimenti a "un'API" nella documentazione sono relazioni
`REFERENCES` verso il simbolo reale (`:Function`/`:Class`/`:Macro`/`:Type`/
`:KconfigOption`), non un nodo sintetico.

## RELATIONSHIP SCHEMA (AGGIORNATO v5)

| Relationship | Start → End | Origine | Confidence tipica |
|---|---|---|---|
| `CONTAINS` | Folder → Folder\|SourceFile | scansione filesystem | 1.0 (certa) |
| `HAS_CHUNK` | SourceFile → SourceChunk | chunking | 1.0 |
| `DECLARES` | SourceFile → Function\|Class\|Type\|Macro\|Variable | parser di linguaggio | 1.0 |
| `INCLUDES` | SourceFile → SourceFile | `#include`/`import` risolto contro l'albero file reale | 1.0 se risolto nell'albero, 0.5 se riferimento esterno non risolvibile (es. header Zephyr non presente in `raw_data/`) |
| `CALLS` | Function → Function | `call_expression`/`ast.Call` risolto contro la symbol table | 1.0 se univoco nello stesso file, 0.8 se univoco globale, scartato (→ `unresolved_relations.json`) se ambiguo |
| `EXTENDS` | Class → Class | ereditarietà esplicita (basi Python, non comune in C) | 1.0 |
| `DEPENDS_ON` | KconfigOption → KconfigOption | nodo `dependencies` di tree-sitter-kconfig | 1.0 |
| `SELECTS` | KconfigOption → KconfigOption | nodo `reverse_dependencies` (`select`) | 1.0 |
| `SETS` | SourceFile → KconfigOption | fragment `.conf`/`_defconfig`, con proprietà `value` | 1.0 |
| `CHILD_OF` | DTNode → DTNode | nesting dei nodi devicetree | 1.0 |
| `REFERENCES` (devicetree) | DTNode → DTNode | phandle `&label` risolto contro l'indice delle label | 1.0 se univoco, altrimenti scartato |
| `COMPATIBLE_WITH` | DTNode → DTBinding | proprietà `compatible` risolta contro `dts/bindings/**` | 1.0 se il binding esiste in `raw_data/`, altrimenti ambiguità loggata |
| `HAS_SECTION` | Document → Concept | titolo/heading | 1.0 |
| `CONTAINS` (requirement) | Document → Requirement | frase con verbo modale | 1.0 |
| `REFERENCES` (documentazione) | Document → Function\|Class\|Type\|Macro\|KconfigOption\|SourceFile\|Document | ruolo Sphinx/RST o code span Markdown risolto contro la symbol table, con proprietà `role` (es. `"c:func"`, `"kconfig:option"`, `"ref"`, `"file"`) | 1.0 se risolto univocamente |
| `SATISFIES` | Requirement → Function\|Class\|KconfigOption | SOLO se la stessa frase del requisito contiene un `REFERENCES` già risolto verso quel simbolo | 0.9 |

Ogni relazione porta comunque `source_file`, `line`, `rationale`, e quando
disponibile `evidence_chunk_uid`, coerentemente con l'impianto di provenance
già in uso in v1/v2.

## DOC-TO-CODE LINKING RULES (NUOVA SEZIONE, SOSTITUISCE IL CROSS-LINKING PER NOME DI v2)

Il cross-linking documentazione↔codice di v2 usava
`nodes_by_name[entity.name.casefold()]` su TUTTA la repository: qualunque
parola in un documento che corrispondesse per caso al nome di un simbolo in
un file scorrelato produceva una relazione. Questo è **vietato** in v5.

Le uniche fonti ammesse per collegare un documento a un simbolo di codice
sono marcatori strutturali espliciti:

1. **Ruoli Sphinx/RST** (`:c:func:`, `:c:struct:`, `:c:macro:`, `:c:type:`,
   `:c:enum:`, `:cpp:func:`, `:option:`, `:kconfig:option:`, `:file:`,
   `:ref:`, `:term:`): estratti dal nodo `role` + `interpreted_text` di
   tree-sitter-rst, poi risolti contro la symbol table (funzioni/tipi/macro/
   opzioni Kconfig) o l'indice dei file. Se il target non risolve contro
   nessuna entità reale, la relazione NON viene creata: va registrata in
   `ambiguities.json` con motivo `"reference target not found in inventory"`.
2. **Code span Markdown** (`` `nome` `` o blocco ` ``` `): stesso principio,
   risolto solo se `nome` corrisponde esattamente (case-sensitive) a un
   simbolo già estratto da un parser.
3. **Ancore RST** (`.. _label:`) per risolvere `:ref:` verso il documento o
   la sezione di destinazione esatta, non un match testuale.

Se un simbolo è menzionato in prosa libera senza uno di questi marcatori,
NON produce una relazione. È un compromesso esplicito: meno collegamenti,
ma ogni collegamento rimasto è verificabile risalendo al marcatore esatto.

## REQUIREMENT EXTRACTION (INASPRITO)

Un nodo `:Requirement` è creato SOLO se la frase contiene esplicitamente uno
dei verbi modali `shall|must|should|required to` (il gruppo non è opzionale,
a differenza del regex di v2 che rendeva questi verbi facoltativi e per
questo catturava frasi come "API allows you to..." come requisito). La
frase intera è l'evidenza (`text`), la relazione `SATISFIES` verso
un'implementazione è creata solo se la stessa frase contiene anche un
riferimento Sphinx/RST già risolto (vedi DOC-TO-CODE LINKING RULES).

## CONFIDENCE SCORE - DEFINIZIONE RIGOROSA (INVARIATO)
Il confidence score (0-1) è **proprietà esclusiva della relazione**, mai
dell'entità.

| Range | Significato |
|-------|-------------|
| 1.0 | Fatto strutturale certo (dichiarazione esplicita nel parser: `DECLARES`, `HAS_CHUNK`, `CONTAINS`, `DEPENDS_ON`, `SELECTS`, `SETS`, `CHILD_OF`) |
| 0.8-0.95 | Risoluzione per nome univoca ma cross-file (`CALLS` globale, `REFERENCES` da ruolo risolto) |
| 0.5-0.79 | Riferimento esterno non risolvibile nell'albero locale (es. header Zephyr non presente in `raw_data/`), mantenuto per tracciabilità ma marcato come non verificato localmente |
| <0.5 | Non usare: se la relazione è così incerta, va scartata e loggata in `ambiguities.json`, non ingerita con basso punteggio |

## INCREMENTAL UPDATE SPECIFICATION (NUOVA SEZIONE, MANDATORY)

Obiettivo: dopo la prima ingestion completa, un cambiamento in `/raw_data/`
(file aggiunto, modificato, rimosso) deve poter essere riflesso nel grafo
ri-processando SOLO i file cambiati, non l'intera repository.

### Manifest
`/output/ingestion_manifest.json`:
```json
{
  "generated_at": "<iso8601>",
  "files": {
    "<relative_path>": {"sha256": "<hex>", "size_bytes": int, "mtime": float}
  }
}
```

### Modalità CLI
- `--full` (default se il manifest non esiste): processa tutti i file.
- `--update`: cammina comunque l'intero `/raw_data/` (per rilevare file
  rimossi, che altrimenti non sarebbero mai notati), calcola l'hash sha256
  di ogni file e lo confronta col manifest precedente per ottenere tre
  insiemi: `added`, `modified`, `removed`. I file `unchanged` NON vengono
  riletti né riparsati: le loro entità/relazioni/chunk precedenti (caricati
  da `entities.jsonl`/`relations.jsonl`/`source_chunks.jsonl`) sono
  riutilizzati as-is.
- Per `added`/`modified`: eseguire l'estrazione strutturale (Livello 1) solo
  su questi file.
- Per `removed`: rimuovere dal set in memoria tutte le entità/relazioni con
  `source_file` uguale al file rimosso, e produrre l'elenco dei file da
  cancellare da Memgraph.
- Il cross-linking (Livello 3: symbol table, CALLS, riferimenti
  documentazione↔codice) va SEMPRE ricalcolato sull'intero insieme di
  entità in memoria dopo il merge added/modified/removed/unchanged, perché
  un file cambiato può risolvere o invalidare riferimenti altrove. Questo
  passo non richiede ri-lettura dei file, quindi resta rapido anche su
  repository grandi.

### Identità stabile dei nodi (condizione necessaria per l'upsert)
Gli uid devono essere deterministici e dipendere solo da
`(entity_type, file, name)` (o solo `(entity_type, name)` per i tipi a
scope globale: `KconfigOption`, `DTBinding`), MAI da un contatore o da un
timestamp, altrimenti l'aggiornamento incrementale non può fare `MERGE`
sullo stesso nodo tra due run.

### Ingestion incrementale in Memgraph
1. Per ogni file in `added ∪ modified ∪ removed`, eseguire
   `MATCH (n) WHERE n.file = $file OR n.path = $file DETACH DELETE n`
   prima di re-ingerire, cosi' i simboli rimossi da un file modificato non
   restano come nodi orfani.
2. Re-ingerire con lo stesso pattern `MERGE` idempotente già in uso per il
   caricamento completo (upsert per `uid`), cosi' i nodi invariati non
   vengono duplicati.
3. Salvare il nuovo manifest solo se l'ingestion ha successo.

## GRAPH SCHEMA DEFINITION (MANDATORY, INVARIATO)
Lo script DEVE generare `/output/graph_schema.json` con `node_labels`
(label, count, required_properties), `relationship_types` (type, count,
start_labels, end_labels), `indexes_created`.

## MEMGRAPH INGESTION SPECIFICATION
- Indici per-label in autocommit separato (compatibilità Memgraph), come in
  v1/v2.
- Clean-load (`MATCH (n) DETACH DELETE n`) solo in modalità `--full`; MAI in
  modalità `--update`.
- Verifica post-ingestion: confronto `exported_count == ingested_count` per
  ogni label e per il conteggio totale delle relazioni.

## LOGGING STRATEGY (INVARIATO)
| Livello | File | Contenuto |
|---------|------|-----------|
| 1 | `extraction_ops_level_1.log` | Inventario file, chunking, parsing per-linguaggio |
| 2 | `extraction_ops_level_2.log` | Estrazione semantica documentazione (RST/Markdown), Kconfig fragment |
| 3 | `extraction_ops_level_3.log` | Cross-linking, symbol table, resolution CALLS/REFERENCES/phandle |
| 4 | `extraction_ops_level_4.log` | Metriche di grafo, nodi isolati |
| Access | `access_log.jsonl` | Tutti i file letti, con esito scope-check |

## GESTIONE ERRORI
| Scenario | Azione |
|----------|--------|
| File non leggibile o binario | `:SourceFile` comunque creato, nessun chunk/parsing, loggato |
| Parser tree-sitter produce nodi `ERROR` | Continuare la camminata sui figli non-errore (tree-sitter è error-tolerant); non fallire l'intero file |
| Riferimento doc→codice non risolvibile | Scartare la relazione, loggare in `ambiguities.json` |
| CALLS/phandle ambiguo (>1 candidato) | Scartare, loggare in `unresolved_relations.json` con tutti i candidati |
| Provenance mancante su una relazione | Fallire con exit code 1 |
| Memgraph connection failure | Fallire con exit code 1 |
| Conteggio file discovered/exported/ingested non coincide | Fallire con exit code 1 |

## VINCOLO DI ISOLAMENTO (AGGIORNATO v5)
**Fonte dati per l'estrazione**: `/raw_data/` e tutte le sue sottocartelle.
Nessun accesso a rete a runtime durante l'estrazione (i parser sono
librerie locali già installate).

**PERMESSO** (revoca esplicita del divieto v2 "no nuove dipendenze", che è
causa diretta del fallback a regex):
- Standard library Python
- `mgclient`, `PyYAML` (già presenti)
- `tree_sitter`, `tree_sitter_language_pack` (da aggiungere a
  `python_venv_requirements.txt`; installazione una tantum in fase di setup
  ambiente, non a runtime dello script di estrazione)

**VIETATO**: qualunque dipendenza non elencata sopra o non giustificata da
una riga della tabella PARSER REQUIREMENTS.

## PRIORITÀ OPERATIVE
1. File inventory completo (ogni file ha un nodo `:SourceFile`)
2. Source chunking (ogni file testo è segmentato)
3. Estrazione strutturale SOLO tramite i parser assegnati in PARSER REQUIREMENTS
4. Doc-to-code linking SOLO tramite marcatori strutturali (mai prosa libera)
5. Entity resolution rigoroso (scartare ambigui invece di indovinare)
6. Aggiornamento incrementale (manifest + MERGE, mai un reload completo per
   una singola modifica)

## NOTA OPERATIVA FINALE
Il trade-off resta lo stesso di v2: meno entità totali a fronte di
accuratezza verificabile. La differenza rispetto a v2 è che ora
"verificabile" significa "prodotto da un parser del linguaggio o da un
marcatore strutturale esplicito", non "prodotto da una regex che nella
pratica ha estratto rumore".
