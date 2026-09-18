# AG_RAG_System

This repository is a test implementation folder for a GraphRAG system with dedicated MCP server and tools for advanced graph-based research on a dataset.

Remember to enter `.venv` enviroment for python imports.
```bash
    # windows
    ./.venv/Scripts/Activate.ps1
    
    # linux
    source .venv/bin/activate

```

**Index**:
- `./DB`.........: docker-compose.yml for MemGraphDB container
- `./doc`........: documentation folder based on .md 
- `./agents`.....: agent-related prompts, rules and tools
- `./logs`.......: dev and agentic logs

### Requisites
- Docker
- Python 3.0
- doxygen
- Install requests in venv: `python -m pip install requests`
- Install requirements: `pip install -r requirements.txt`

## TO REVISE
La tua architettura combina diverse metodologie consolidate con una denominazione specifica emergente nel campo dell'AI Engineering. Ecco la classificazione tecnica:

## Denominazione Primaria

**Provenance-Aware Knowledge Graph Construction for Code Intelligence**

Questa è la definizione più precisa che descrive il tuo approccio. Si compone di:

| Componente | Disciplina di Riferimento |
|------------|--------------------------|
| Knowledge Graph Construction | Knowledge Representation & Reasoning (KR&R) |
| Provenance-Aware | Data Provenance / Scientific Workflow Management |
| Code Intelligence | Program Analysis / Software Engineering |
| Evidence-Backed Extraction | Information Extraction (IE) |

## Metodologie Specifiche Implementate

### 1. **Static Program Analysis** (Analisi Statica di Programma)
L'estrazione strutturale da codice sorgente senza esecuzione rientra in questa disciplina consolidata. Nello specifico:
- **AST-based extraction**: Parse tree traversal per identificare dichiarazioni
- **Control Flow Analysis**: Tracciamento chiamate e dipendenze
- **Data Flow Analysis**: Tracciamento variabili e scope

### 2. **Information Extraction (IE) Pipeline**
La tua architettura segue il pattern classico dell'IE:
```
Document → Tokenization → Entity Recognition → Relation Extraction → Knowledge Base
```
Con l'aggiunta critica del **provenance layer** che non è sempre presente nei sistemi IE tradizionali.

### 3. **Provenance-Aware Data Management**
Questa è la tua innovazione distintiva. Il tracciamento sistematico di:
- **Origin Provenance**: Da quale file/chunk proviene ogni entità
- **Derivation Provenance**: Come è stata inferita ogni relazione
- **Confidence Provenance**: Perché è stato assegnato un certo confidence score

In letteratura, questo approccio è chiamato **"Evidence-Backed Knowledge Graph"** o **"Auditable Knowledge Graph"**.

### 4. **Multi-Layer Graph Architecture**
La tua struttura a livelli (File → Chunk → Entity → Relation) implementa un pattern noto come:
- **Physical Layer**: File system inventory
- **Logical Layer**: Source chunks
- **Semantic Layer**: Entità tipizzate
- **Inferential Layer**: Relazioni estratte

Questa architettura è documentata in sistemi come **Amazon Neptune ML** e **Neo4j Knowledge Graph pipelines**.

### 5. **RAG Infrastructure Pattern** (Retrieval-Augmented Generation)
Il tuo grafo è progettato come **knowledge base per agent downstream**, il che lo classifica come infrastruttura RAG avanzata:
- **Graph-RAG**: Retrieval basato su traversal di grafo invece che vector similarity
- **Hybrid Retrieval**: Combinazione di lookup strutturato (graph) + full-text (chunks)

## Denominazioni Alternative nel Settore
| Contesto | Denominazione |
|----------|---------------|
| **AI Engineering** | "Graph-RAG Knowledge Base" |
| **Software Engineering** | "Semantic Code Index" |
| **Knowledge Management** | "Enterprise Knowledge Graph with Provenance" |
| **Research** | "Evidence-Aware Program Understanding System" |
| **Vendor (Neo4j)** | "Knowledge Graph with Data Lineage" |
| **Vendor (Microsoft)** | "Code Intelligence Graph" |

## Il Tuo Approccio Specifico: Cosa lo Distingue
La combinazione che stai implementando ha caratteristiche distintive:
```
┌────────────────────────────────────────────────────────────┐
│  YOUR APPROACH: Provenance-Aware Code Knowledge Graph      │
├────────────────────────────────────────────────────────────┤
│  ✓ File-level completeness (100% inventory)                │
│  ✓ Deterministic chunking con evidence preservation        │
│  ✓ AST-first extraction (parser > regex)                   │
│  ✓ Mandatory provenance per entity/relation                │
│  ✓ Confidence scoring su relazioni (non nodi)              │
│  ✓ Entity resolution rigoroso (scarto ambigui)             │
│  ✓ Post-ingestion verification con fail su errori          │
└───────────────────────────────────────────────────────────┘
```

Questa combinazione specifica è emergente nel 2024-2025 con l'adozione di **Graph-RAG** per applicazioni enterprise di AI. Non ha ancora una denominazione standardizzata singola, ma la descrizione più accurata è:

> **"Evidence-Backed Graph-RAG Knowledge Base for Code Intelligence"**

## Riferimenti Accademici Correlati

Se devi documentare questo lavoro, le aree di ricerca di riferimento sono:

1. **Program Comprehension** (ICPC conference)
2. **Mining Software Repositories** (MSR conference)
3. **Knowledge Graph Construction** (ISWC, SEMWEB conference)
4. **Data Provenance** (IPAW, TaPP workshop)
5. **Information Extraction** (ACL, EMNLP conference)

La tua implementazione si posiziona all'intersezione di queste discipline, con enfasi particolare sulla **tracciabilità delle inferenze** — un requisito critico per sistemi AI production-grade dove gli agent downstream devono poter validare ogni affermazione contro source evidence.