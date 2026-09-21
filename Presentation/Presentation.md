# Approccio utilizzato
1. È stato generato un `extraction_script.py` tramite prompt versionato per eseguire `inserire tipo di estrazione effettuata` sul contenuto della cartella `./raw_data/`
2. Eseguito l'ingestion massiva di tutti i dati e caricato il GraphDB 
3. Testato interrogazioni del DB delegando la redazioni e approccio ad un agent
4. Per il test di querying tramite Agent, su una chat dal contesto separato, il seguente prompt:
```prompt
You have a memgraph db with these credentials:
- host=os.getenv("MEMGRAPH_HOST", "localhost")
- port=int(os.getenv("MEMGRAPH_PORT", "7687"))
- username=os.getenv("MEMGRAPH_USERNAME", "")
- password=os.getenv("MEMGRAPH_PASSWORD", "")
Connect to it and find out what is described by the whole graph.
```

# Può essere utile per economia dei token? In che ambiti?
- Da analizzare
- Ottimizzazione retrival di relazioni codice-documentazione, documentazione-documentazione 

# Problematiche riscontrate, Limiti attuali e zone grigie da approfondire
- `V2` -> `V3` migliorata profondità ingestione del codice e inserito riferimenti ai file ingeriti.
- Finestre di contesto enorme ad ogni domanda: mancante strategia di "Querying" per gli agenti. Da sviluppare un modello di prompt per fornire delle strategie efficaci agli agent, asseconda della task.
- Mancante strategia di "Caching" per il modello. Da definire per permettere un eventuale traversal più fluido man mano che lo si utilizza

# Punti chiave dell'implementazione di attuale
- Tutte i file registrati ed ingeriti sono registrati tramite log e visualizzabili
- Il processo di ingestion è automatizzato tramite l'AI ed è un processo "una-tantum"
- Inserimenti futuri sono molto veloci da fare tramite agenti o a mano
- Permette il traversal di argomenti sparsi su rami molto differenti tra loro
- Ogni Nodo presenta metadati completi per rintracciare i file originali: uid, source_file, line, type

### 1. **Hybrid Retrieval Pipeline**
| Tecnica | Funzione | Valore |
|------------|----------|--------|
| Dense Vector Search | Similarità semantica | Trova concetti, sinonimi, intent |
| Sparse BM25 Search | Keyword matching | Trova identifier esatti (ticker, API, classi) |
| RRF Fusion | Merge ranking list | Combina i due segnali senza normalizzazione |
| Cross-Encoder Rerank | Rilevanza fine | Precisione >90% sui top result |

### 2. **Structured Chunk Metadata**
Ogni chunk non è solo testo, ma ha **metadata ricchi** per filtering pre-retrieval:
- File path, language, entity type
- Line ranges, byte offsets
- Code type (function, class, docstring, test)
- Complexity scores, token counts
- Embeddings (dense + sparse)

### 3. **Citation-Backed Response**
Ogni risposta include **provenance verificabile**:
- Chunk UID
- File + line range
- Text excerpt
- Relevance score
- Graph traversal path

---

## L'Idea di archiettura Ibrida:
L'implementazione di una pipeline di lavoro basata su GraphDB retrival, aggiunge uno strato sopra i dati già esistenti a supporto degli AI Agent per le task di retrival e reasoning su dati di diverso tipo.

Connettori come Atrassian MCP rimarrebbero fondamentali per permettere un accesso diretto al codice e documentazioni una volta portato a termine un eventuale "ragionamento" base, permettendo di recuperare nel contesto dell'agent solo le informazioni utili.

```
┌─────────────────────────────────────────────────────────────────┐
│                    HYBRID ARCHITECTURE                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Bitbucket ──┐                                                  │
│              ├──→ [Extraction Pipeline] ──→ Knowledge Graph     │
│  Confluence ─┘                                  ↓               │
│                                            [MCP Server]         │
│                                                 ↓               │
│                                            [AI Agent]           │
│                                                                 │
│  Atlassian MCP rimane per:           Knowledge Graph per:       │
│  - Edit/update documenti             - Semantic search avanzato │
│  - Workflow approval                 - Cross-document reasoning │
│  - Permission management             - Citation-backed answers  │
│  - Native Atlassian features         - Agent tool interface     │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Possibili benefici:
1. **Riduzione tempo debugging**: Agent trova dipendenze in secondi vs minuti di navigazione manuale
2. **Compliance audit**: Ogni affermazione ha evidence verificabile (critico per settori regolamentati)
3. **Onboarding developer**: Nuovi team member possono query il codebase semanticamente
4. **Technical debt detection**: Grafo evidenzia entità isolate, dipendenze circolari, SPOF


### Confronto tra capacità dei singoli approcci: Graph Approach vs Querying diretto con Atlassian MCP
| Dimensione | **Graph query Approach** (Graph + Hybrid Retrieval) | **Atlassian MCP** (Bitbucket + Confluence) |
|------------|---------------------------------------------------|-------------------------------------------|
| **Data Location** | Centralizzato in knowledge graph locale | Distribuito (Bitbucket repos + Confluence spaces) |
| **Retrieval Method** | Hybrid (dense + sparse + rerank + graph traversal) | Primarily keyword + basic semantic (Confluence search) |
| **Cross-Document Links** | **Espliciti**: relazioni estratte e indicizzate | **Impliciti**: link manuali tra pagine/commit |
| **Provenance** | **Automatica**: chunk_uid + line range per ogni entità | **Manuale**: dipende da come utenti linkano contenuti |
| **Entity Resolution** | **Deterministico**: UID univoci per funzione/classe/API | **Assente**: search per testo, non per entità |
| **Query Capability** | Strutturata + semantica + graph traversal | Primariamente keyword + filtri base |
| **Agent Support** | **Tool strutturati** con output typed | **Generic search** con output testuale |
| **Citation Quality** | **Evidence-backed**: excerpt + line range | **Page-level**: link a pagina, non a sezione |
| **Reasoning Path** | **Tracciabile**: grafo mostra dipendenze | **Non tracciabile**: utente deve navigare manualmente |
| **Setup Complexity** | **Alta**: richiede pipeline estrazione + indici multipli | **Bassa**: configurazione MCP connector esistente |
| **Maintenance** | **Attiva**: re-indexing su change, monitoraggio indici | **Passiva**: Atlassian gestisce infrastruttura |
| **Latency** | **Variabile**: 100-500ms (hybrid + rerank) | **Consistente**: 200-400ms (search Atlassian) |
| **Accuracy** | **>90%** su benchmark strutturati (hybrid + rerank) | **~60-70%** su query semantiche complesse |
| **Compliance/Audit** | **Full trail**: ogni risposta ha evidence verificabile | **Partial**: dipende da logging Atlassian |
| **Offline Capability** | **Sì**: grafo locale, nessun API call esterno | **No**: dipende da connettività Atlassian Cloud |
| **Data Sovereignty** | **Controllata**: dati in infrastruttura propria | **Vendor-dependent**: dati su cloud Atlassian |

### Possibili vantaggi di un approccio basato su GraphDB traversal rispetto ad uno standard con ricerca basata unicamente su Atlassian MCP
| Domanda esempio | GraphDB traversal | Atlassian MCP |
|------------|------------|---------------|
| "Trova tutte le funzioni che chiamano `uart_init`" | Graph traversal | Non supportato |
| "Mostrami il codice esatto con numero di riga" | Chunk provenance | Solo link a file |
| "Quali documenti descrivono questa API?" | Cross-link codice-doc | Search separato |
| "Traccia la dipendenza da main() a driver" | Path finding su grafo | Navigazione manuale |
| "Quali requisiti sono soddisfatti da questo modulo?" | Relation `SATISFIES` | Non estratto |
| "Dammi evidenza testuale per questa affermazione" | Excerpt + line range | Link a pagina |
| "Cerca per intent, non keyword" | Dense vector search | Limitato semantic |
| "Filtra per language + file pattern + entity type" | Structured filters | Filtri base |

# Spunti di evoluzione:
- Inserire una "Cache" persistente di memoria ad uso del modello stesso, costruita secondo regole precise. In questo modo un qualsiasi modello che usa questo grafo può evitare query ricorrenti. (Da definire/testare)
- esposizione di tool strutturati tramite mcp tool interface, es:
```
search_code(query, language, file_pattern, entity_type, limit)
get_function(uid, include_chunks, include_callers)
trace_dependency(source_uid, target_uid, max_depth)
```
- Verificare se è necessario ingerire anche il contenuto effettivo o anche solo i riferimenti correlati dai collegamenti
- Determinare strategie di querying efficaci per ridurre i token in uscita, quindi uso di crediti