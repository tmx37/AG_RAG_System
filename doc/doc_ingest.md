# Analisi Logica della Pipeline di Ingestion (v5 / v3 dello script)

Questo documento descrive `output/extraction_script.py` così come è realmente
implementato oggi. Le versioni precedenti di questo documento descrivevano
un'architettura basata su Doxygen XML e regex mai realmente implementata in
quella forma; questa versione riflette lo script eseguibile e verificato su
`raw_data/sdk-nrf`. Per il razionale completo del perché la pipeline è stata
riprogettata, vedi `output/v2_extraction_script_nrf_example/17_09_2026.md` e
la specifica in `agents/rules/prompt_knowledge_graph_extraction.md`.

---

## 1. Come vengono formattati fisicamente i dati?

Ogni file in `raw_data/` viene instradato al parser reale del suo linguaggio;
non esiste un formato intermedio, ma non esiste nemmeno estrazione via regex
su prosa libera per le entità semantiche.

| Formato Sorgente | Estrazione | Nodi/relazioni prodotti |
|---|---|---|
| `.py` | modulo nativo `ast` | `:Function`, `:Class`, `:Variable`, `DECLARES`, `CALLS` |
| `.c .h .cc .cpp .cxx .hh .hpp` | `tree_sitter_language_pack` grammatica `c`/`cpp` | `:Function`, `:Class` (struct/union), `:Type` (typedef/enum), `:Macro`, `:Variable`, `DECLARES`, `CALLS`, `INCLUDES` |
| `Kconfig`, `Kconfig.*` | tree-sitter grammatica `kconfig` | `:KconfigOption`, `DEPENDS_ON`, `SELECTS` |
| `.conf`, `*_defconfig` | parser di linea dedicato (`CONFIG_X=valore`) | `SETS` verso `:KconfigOption` (uid globale, senza prefisso `CONFIG_`) |
| `.dts .dtsi .overlay` | tree-sitter grammatica `devicetree` | `:DTNode`, `CHILD_OF`, `REFERENCES` (phandle `&label`), `COMPATIBLE_WITH` |
| `.yaml`/`.yml` sotto `dts/bindings/**` | `PyYAML` (`yaml.safe_load`) | `:DTBinding` (uid = stringa `compatible`) |
| `.rst` | tree-sitter grammatica `rst` | `:Document`, `:Concept` (titoli sezione), `:Requirement` (frasi con verbo modale esplicito), `REFERENCES` risolte da ruoli Sphinx (`:c:func:`, `:kconfig:option:`, `:file:`, `:ref:`, ...) |
| `.md`/`.markdown` | regex di linea deterministico su heading/code-span (sintassi non ambigua, non serve un parser dedicato) | `:Document`, `:Concept`, `REFERENCES` da code span |
| Tutto il resto (immagini, binari, CMake, certificati, ...) | nessuno | solo `:SourceFile` + `:SourceChunk` (inventario e testo, nessuna entità semantica inventata) |

**Esempio reale, verificato sul grafo popolato in questa sessione:**

```cypher
MATCH (n:DTNode {name:"hfxo"})-[:COMPATIBLE_WITH]->(b:DTBinding)
RETURN n.file, b.name
```
restituisce `sdk-nrf/dts/common/nordic/nrf9251.dtsi` →
`nordic,nrf92-hfxo`, letto direttamente dal nodo Devicetree
`hfxo: hfxo { compatible = "nordic,nrf92-hfxo"; ... }` e dal binding YAML
corrispondente in `dts/bindings/clock/`.

---

## 2. Come vengono caricati nel sistema?

Il caricamento avviene in batch tramite `UNWIND $rows AS row MERGE (...)`,
mai una query per singolo nodo/relazione:

```python
query = (
    f"UNWIND $rows AS row MERGE (n:{label} {{uid: row.uid}}) "
    "SET n.name=row.name, n.file=row.file, ..."
)
cursor.execute(query, {"rows": rows[start:start + 1000]})
```

`MERGE` per `uid` rende l'operazione idempotente: ri-eseguire l'ingestion con
gli stessi dati non crea duplicati. Questo è anche il meccanismo che rende
possibile l'aggiornamento incrementale (sezione 5).

Le relazioni sono raggruppate per coppia `(label soggetto, label oggetto)`
prima della `MATCH`, in modo che ogni query usi l'indice per-label su `uid`
invece di una scansione di proprietà senza label (che su un grafo da ~150.000
nodi impiegherebbe ore invece di minuti: verificato empiricamente in questa
sessione, vedi nota di performance più sotto).

---

## 3. Come viene definito lo schema?

Lo schema non è imposto da Memgraph (che resta schema-less), ma è **esplicito
e verificabile** in due punti:

1. `agents/rules/prompt_knowledge_graph_extraction.md`, sezioni NODE SCHEMA e
   RELATIONSHIP SCHEMA: la specifica dichiarativa di ogni label/relazione e
   di chi la genera.
2. `output/graph_schema.json`, generato ad ogni esecuzione dello script:
   conteggio reale per label/relazione, `required_properties`, e per ogni
   relazione i `start_labels`/`end_labels` osservati realmente nei dati (non
   dichiarati a priori, calcolati dal grafo estratto).

Per ispezionare lo schema a runtime:
```cypher
MATCH (n) RETURN labels(n)[0] AS label, count(*) AS n ORDER BY n DESC;
MATCH ()-[r]->() RETURN type(r) AS rel, count(*) AS n ORDER BY n DESC;
```

---

## 4. Come sai che i riferimenti fanno riferimento a dati reali?

Ogni relazione che collega due entità per nome (non per struttura esplicita
del parser, es. `CALLS`, `REFERENCES`, phandle Devicetree) passa da una
**symbol table** costruita dopo che tutti i file sono stati analizzati
(`build_symbol_indices`), e viene creata solo se il nome risolve a
**esattamente un** candidato:

```python
def pick_unique(candidates, referencing_file=""):
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    same_file = [c for c in candidates if c.source_file == referencing_file]
    ...
    return None  # ambiguo: la relazione NON viene creata
```

Se i candidati sono zero o più di uno (e non risolvibili per priorità
same-file/definizione), la relazione non viene creata: l'occorrenza finisce
in `output/ambiguities.json` o `output/unresolved_relations.json` con il
motivo esatto. Non esistono relazioni "orfane" create per costruzione: la
query di ingestion usa `MATCH` (non `MERGE`) sugli endpoint, quindi una riga
il cui `subject`/`object` non esiste come nodo viene semplicemente scartata
dalla UNWIND, mai trasformata in un nodo fantasma.

---

## 5. Aggiornamento incrementale (assente nelle versioni precedenti)

```
python output/extraction_script.py --full     # prima ingestion / reset completo
python output/extraction_script.py --update   # solo i file cambiati da raw_data
python output/extraction_script.py --skip-db  # solo export dei file, nessuna scrittura su Memgraph
python output/extraction_script.py --ingest-existing  # ri-carica gli export già presenti
```

`--update` calcola lo sha256 di ogni file in `raw_data/` e lo confronta con
`output/ingestion_manifest.json` dell'esecuzione precedente, ottenendo tre
insiemi: `added`, `modified`, `removed`. Solo i file `added`/`modified`
vengono ri-analizzati; i file `removed` (e le rispettive cartelle rimaste
vuote) vengono cancellati da Memgraph. Il cross-linking (symbol table,
`CALLS`, riferimenti documentazione↔codice) viene invece sempre ricalcolato
per intero sull'insieme unito in memoria, operazione che non richiede
ri-lettura dei file ed è quindi rapida anche su tutta la repository.

Verificato in questa sessione con un file di test usa-e-getta: aggiunta →
`1 added`, il nuovo simbolo compare nel grafo; modifica → il simbolo vecchio
sparisce e il nuovo compare, il resto del grafo resta invariato; rimozione →
il grafo torna esattamente al conteggio nodi/relazioni precedente.

---

## Nota di performance (rilevante per chi modifica lo script)

La prima versione della query di ingestion delle relazioni usava
`MATCH (a) WHERE a.uid = row.subject` senza specificare la label. Su un
grafo con poche migliaia di nodi è indistinguibile in velocità dalla verifica
con label; su questo grafo (~150.000 nodi) portava il tempo stimato di
ingestion delle sole relazioni a diverse ore. Raggruppare le righe per
`(subject_label, object_label)` e specificare la label in ogni `MATCH` (così
da usare l'indice creato con `CREATE INDEX ON :Label(uid)`) ha ridotto il
tempo totale di ingestion (nodi + 268.268 relazioni) a circa due minuti.

## Nota su una label riservata

`Directory` è una parola riservata nella grammatica Cypher di Memgraph
(`CREATE INDEX ON :Directory(uid)` fallisce con un errore di parsing). La
label usata per le cartelle di `raw_data/` è quindi `:Folder`.
