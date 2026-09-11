## Edge Cases
Casi d'Uso Anomali:

- Documentazione Malformata/Incompleta:
    - File con metadati mancanti, riferimenti circolari, codice deprecato non marcato
    - Strategia: LLM estrae con confidence score, entità <0.6 flaggate per review manuale
    - Fallback: Nodi "orphan" collegati a nodo radice "Unverified_Documentation"

- Query Ambigue o Multi-Intent:
    - Esempio: "Come gestisco gli interrupt sul driver UART?" (potrebbe riferirsi a più versioni/varianti)
    - Strategia: Agent decomponi query in sub-query, restituisce multiple subgraph con confidence ranking
    - Chiedere chiarimenti se top-3 risultati hanno score <0.5

- Aggiornamenti Documentali Durante Query:
    - Commit mentre query in corso su grafo
    - Strategia: Snapshot isolation a livello di query, ingestione incrementale in transazione separata
    - Versionamento nodi: proprietà valid_from, valid_to per tracciare evoluzione

- Scrittura Concorrente:
    - Multiple agenti che aggiornano stessa entità (es. due sviluppatori documentano stessa funzione)
    - Strategia: Lock a livello di entità per scritture, merge conflict resolution semi-automatico
    - Rischio critico: Race condition su entity resolution → usare transaction graph DB

- Codice Generato Automaticamente:
    - File auto-generati (es. da tool di configurazione) che duplicano logica
    - Strategia: Rilevamento pattern generazione automatica, nodi marcati come "Derived", esclusi da query standard


## Strategia di Estrazione Nodi e Relazioni
Approccio Multi-Livello per l'Agente

#### Livello 1: Estrazione Strutturale (Static Analysis)
Per Codice:
- Nodi: File, Class, Function, Variable, Type, Module, Include/Import
- Relazioni: 
  - (Function)-[:CALLS]->(Function)
  - (File)-[:CONTAINS]->(Class/Function)
  - (Function)-[:USES]->(Variable/Type)
  - (Module)-[:DEPENDS_ON]->(Module)
  - (File)-[:INCLUDES]->(File)

Per Documentazione:
- Nodi: Document, Section, Concept, Requirement, API_Reference, Example
- Relazioni:
  - (Document)-[:HAS_SECTION]->(Section)
  - (Section)-[:DESCRIBES]->(Concept/Function/Class)
  - (Requirement)-[:SATISFIED_BY]->(Function/Module)
  - (Example)-[:ILLUSTRATES]->(Concept/Function)

#### Livello 2: Estrazione Semantica (LLM-based)
- Prompt engineering per estrarre entità implicite:
    - "Quali concetti di dominio embedded sono menzionati?" (es. interrupt, DMA, real-time constraint)
    - "Quali sono le dipendenze funzionali non esplicite nel codice?"
    - "Quali pattern architetturali sono utilizzati?" (es. state machine, producer-consumer)

#### Livello 3: Cross-Linking Codice-Documentazione
- Entity Resolution: Matching tra funzioni/classi nel codice e riferimenti nella documentazione
    - Usare nomi + signature + context embedding per disambiguare
    - Confidence threshold: 0.8 per auto-link, 0.5-0.8 per review manuale

- Relazioni Chiave:
    - (Function)-[:DOCUMENTED_IN]->(Section)
    - (Module)-[:ARCHITECTURE_DESCRIBED_IN]->(Document)
    - (Requirement)-[:IMPLEMENTED_BY]->(Module)

#### Livello 4: Arricchimento con Graph Algorithms
- Community Detection: Identificare cluster funzionali (es. tutti i moduli relativi a comunicazione UART)
- Centrality Measures: Identificare nodi critici (funzioni con highest betweenness = potenziali single point of failure)
- Path Finding: Pre-calcolare percorsi frequenti per query multi-hop

### Prompt Strategy per LLM Extraction
Template Base per Estrazione Triplette:

```prompt
Sei un esperto di software embedded. Estrai entità e relazioni dal seguente chunk di codice/documentazione.

Regole:
1. Identifica entità con tipi specifici: [Function, Class, Module, Variable, Type, Concept, Requirement, API]
2. Estrai relazioni esplicite e implicite con predicati semanticamente ricchi
3. Per ogni entità, includi: nome, tipo, file di origine, righe, descrizione breve
4. Per ogni relazione, includi: soggetto, predicato, oggetto, confidence score (0-1)
5. Segnala ambiguità o entità con nome generico (es. "config", "data")

Formato output: JSON con arrays "entities" e "relationships"

[CHUNK DI TESTO]
```

### Entity Resolution Post-Processing:
- Normalizzazione nomi (lowercase, rimozione prefissi/suffissi comuni)
- Clustering embedding per identificare entità duplicate con nomi diversi
- Merge manuale per conflitti non risolvibili automaticamente

