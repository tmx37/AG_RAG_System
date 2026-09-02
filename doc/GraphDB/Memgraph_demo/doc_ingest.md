# Analisi Logica della Pipeline di Ingestion

Ecco come funziona lo script, punto per punto.

---

## 1. Come vengono formattati fisicamente i dati?

I dati **non vengono trasformati in un formato intermedio**. Ogni file sorgente viene letto e inviato direttamente a Memgraph come nodi/relazioni.

| Formato Sorgente | Estrazione | Formato Finale in Memgraph |
|-----------------|------------|---------------------------|
| **Markdown/reST** (`*.md`, `*.rst`) | Lettura testo + regex per titolo | Nodo `:Document` con proprietà `path`, `title`, `content` |
| **Doxygen XML** (`*.xml`) | Parsing XML con `xml.etree.ElementTree` | Nodo `:Function` + relazioni `[:CALLS]` |
| **Device Tree** (`*.dtsi`) | Regex su `compatible = "..."` | Nodo `:HardwarePeripheral` con proprietà `compatible` |
| **YAML Board** (`*.yaml`) | Parsing YAML con `yaml.safe_load()` | Nodo `:Board` + relazioni `[:SUPPORTS]` |

**Esempio concreto:**

Un file `doc/intro.md`:
```markdown
# Introduction
This is the Zephyr RTOS documentation.
```

Diventa in Memgraph:
```cypher
(:Document {
  path: "doc/intro.md",
  title: "Introduction",
  content: "This is the Zephyr RTOS documentation.",
  type: "documentation"
})
```

---

## 2. Come vengono caricati nel sistema?

Il caricamento avviene tramite **query Cypher eseguite in tempo reale** sulla connessione Bolt.

### Flusso di caricamento:

```
File Sorgente → Python Script → Query Cypher → Memgraph DB
```

### Meccanismo tecnico:

```python
# 1. Apri connessione
conn = Connection(host="localhost", port=7687, username="", password="")

# 2. Prepara query con parametri
query = """
CREATE (d:Document {
    path: $path,
    title: $title,
    content: $content
})
"""

# 3. Esegui con parametri (sanitizzati automaticamente)
execute_query(conn, query, {
    "path": "doc/intro.md",
    "title": "Introduction",
    "content": "..."
})
```

**Nota:** Non c'è batch processing. Ogni file genera una o più query immediate. Per grandi volumi, andrebbe aggiunto il batching.

---

## 3. Come viene definito lo schema?

**Non esiste uno schema predefinito.** Memgraph è **schema-less** (come tutti i graph DB).

Lo schema emerge **implicitamente** dalle query di creazione:

| Label | Proprietà | Relazioni |
|-------|-----------|-----------|
| `:Document` | `path`, `title`, `content`, `type` | Nessuna (per ora) |
| `:Function` | `name`, `file`, `kind` | `[:CALLS]` → `:Function` |
| `:HardwarePeripheral` | `compatible`, `source_file` | `[:USES]` ← `:Function` |
| `:Board` | `id`, `name`, `path` | `[:SUPPORTS]` → `:HardwarePeripheral` |

### Dove è definito nello script:

Ogni funzione `ingest_*()` contiene le query che **creano implicitamente** lo schema:

```python
# Definizione implicita del nodo Document
query = """
CREATE (d:Document {
    path: $path,
    title: $title,
    content: $content,
    type: 'documentation'
})
"""

# Definizione implicita della relazione CALLS
query = """
MERGE (caller)-[:CALLS]->(callee)
"""
```

### Per verificare lo schema esistente:

In Memgraph Lab, esegui:
```cypher
CALL schema.get() YIELD label, property, type
RETURN label, collect(property) as properties;
```

Oppure per vedere i tipi di relazioni:
```cypher
MATCH ()-[r]->()
RETURN DISTINCT type(r) as relationship_type;
```

---

## 4. Come sai che fanno riferimento a dati reali?

Questa è la domanda critica. Attualmente, lo script **NON valida** che i riferimenti esistano.

### Problemi attuali:

```python
# Questa query può creare relazioni "orfane"
MATCH (caller:Function {name: $caller, file: $caller_file})
MATCH (callee:Function {name: $callee})
MERGE (caller)-[:CALLS]->(callee)
```

Se `callee` non esiste, **la query fallisce silenziosamente** (grazie al `try/except` che continua).

### Come garantire riferimenti validi:

#### Opzione A: Usa `MERGE` invece di `MATCH` + `MERGE`

```python
# Invece di:
MATCH (caller:Function {...})
MATCH (callee:Function {...})
MERGE (caller)-[:CALLS]->(callee)

# Usa:
MERGE (caller:Function {name: $caller, file: $caller_file})
MERGE (callee:Function {name: $callee})
MERGE (caller)-[:CALLS]->(callee)
```

Questo **crea i nodi mancanti** se non esistono, garantendo che la relazione sia sempre valida.

#### Opzione B: Verifica post-ingestion

Esegui una query per trovare relazioni "rotte":
```cypher
MATCH (f:Function)-[:CALLS]->(g)
WHERE NOT (g:Function)
RETURN f.name as Broken_Call;
```

#### Opzione C: Usa ID univoci invece di nomi

I nomi delle funzioni possono essere duplicati. Meglio usare ID univoci da Doxygen:

```python
# Doxygen XML fornisce un ID univoco per ogni elemento
func_id = compound.get("id")  # Es: "group__kernel_1ga123456"

query = """
MERGE (f:Function {id: $id})
ON CREATE SET f.name = $name, f.file = $file
"""
```

---

## Riepilogo del Flusso Completo

```
┌─────────────────────────────────────────────────────────────┐
│ 1. FILE SORGENTE                                            │
│    - doc/intro.md                                           │
│    - include/zephyr/kernel.h                                │
│    - dts/arm/nrf52.dtsi                                     │
│    - boards/nrf52840dk.yaml                                 │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│ 2. PARSING (Python)                                         │
│    - markdown: lettura testo + estrazione titolo            │
│    - XML: ElementTree.find() per nodi compounddef           │
│    - DTS: regex per "compatible = ..."                      │
│    - YAML: yaml.safe_load()                                 │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│ 3. QUERY CYPHER GENERATE                                    │
│    - CREATE (:Document {...})                               │
│    - MERGE (:Function {...})                                │
│    - MERGE ()-[:CALLS]->()                                  │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│ 4. MEMGRAPH DB                                              │
│    - Nodi memorizzati in RAM                                │
│    - Relazioni indicizzate                                  │
│    - Queryabili in tempo reale                              │
└─────────────────────────────────────────────────────────────┘
```

---

## Raccomandazioni per la Produzione

| Problema | Soluzione |
|----------|-----------|
| **Nessuno schema esplicito** | Crea un documento `schema.md` che descrive label e proprietà attese |
| **Riferimenti non validati** | Usa `MERGE` invece di `MATCH` + `MERGE`, o aggiungi verifica post-ingestion |
| **Nomi duplicati** | Usa ID univoci (da Doxygen o hash del path) invece di nomi |
| **Nessun batching** | Raggruppa query in transazioni da 100-500 nodi per performance |
| **Nessun versioning** | Aggiungi proprietà `ingested_at`, `source_version` ai nodi |

---