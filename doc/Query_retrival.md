# Sì, esatto. Ma con distinzioni importanti.

Lo script ha creato la **knowledge base strutturata**. Ora serve un **Agent Layer** che sappia interrogarla. Ecco come funziona.

---

## 1. Cosa può fare l'Agent

Un agente LLM può interagire con Memgraph in **3 modi**, dal più semplice al più sofisticato:

| Livello | Descrizione | Esempio |
|---------|-------------|---------|
| **A. Query Predefinite** | L'agent seleziona da un menu di query Cypher già scritte | "Mostrami tutte le funzioni che usano UART" → esegue query template |
| **B. Cypher Generato** | L'LLM scrive Cypher al volo basandosi sullo schema | "Quali board supportano nRF52?" → genera `MATCH (b:Board)...` |
| **C. GraphRAG Ibrido** | Vector search + graph traversal combinati | "Come gestisco I2C su Nordic?" → trova documenti + traversa relazioni |

---

## 2. Architettura dell'Agent Query Layer

```
┌─────────────────────────────────────────────────────────────┐
│  USER QUESTION                                              │
│  "Quali funzioni controllano la UART sulla board nRF52?"    │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│  LLM AGENT (con contesto dello schema)                      │
│  - Riceve domanda in linguaggio naturale                    │
│  - Conosce label: :Board, :Function, :HardwarePeripheral    │
│  - Conosce relazioni: [:SUPPORTS], [:USES], [:CALLS]        │
│  - Genera query Cypher                                      │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│  VALIDATION LAYER (CRITICO)                                 │
│  - Whitelist di operazioni (solo READ, no DROP/DELETE)      │
│  - Timeout massimo (es. 30 secondi)                         │
│  - Limita risultati (es. MAX 100 righe)                     │
│  - Sanitizza input (previene injection)                     │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│  MEMGRAPH DB                                                │
│  - Esegue query Cypher                                      │
│  - Restituisce nodi e relazioni                             │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│  LLM AGENT (interpretazione)                                │
│  - Riceve risultati grezzi                                  │
│  - Li sintetizza in linguaggio naturale                     │
│  - Cita le fonti (path dei documenti, file sorgente)        │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│  USER ANSWER                                                │
│  "Le funzioni sono: uart_init(), uart_write()... "          │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. Esempio Concreto: Agent che Genera Cypher

### Prompt dell'Agent (system message):

```
You are a GraphRAG assistant for embedded software documentation.
You have access to a Memgraph database with the following schema:

NODES:
- (:Document {path, title, content, type})
- (:Function {name, file, kind})
- (:HardwarePeripheral {compatible, source_file})
- (:Board {id, name, path})

RELATIONSHIPS:
- (:Board)-[:SUPPORTS]->(:HardwarePeripheral)
- (:Function)-[:USES]->(:HardwarePeripheral)
- (:Function)-[:CALLS]->(:Function)
- (:Document)-[:DESCRIBES]->(:Function)

You can generate Cypher queries to answer user questions.
ONLY generate READ queries (MATCH, RETURN, WHERE). 
NEVER generate CREATE, DELETE, DROP, or WRITE operations.

Format: Return ONLY the Cypher query, no explanation.
```

### User Question:
```
"Quali funzioni usano la UART sulla board nRF52840?"
```

### Agent Output (generated Cypher):
```cypher
MATCH (b:Board {id: "nrf52840dk_nrf52840"})
      -[:SUPPORTS]->(p:HardwarePeripheral {compatible: "vendor,uart"})
      <-[:USES]-(f:Function)
RETURN f.name, f.file
LIMIT 20
```

### Agent Post-Processing (risposta finale):
```
Ho trovato 3 funzioni che usano UART su nRF52840:

1. `uart_init()` - file: drivers/serial/uart_nrf52.c
2. `uart_write()` - file: drivers/serial/uart_nrf52.c  
3. `uart_read()` - file: drivers/serial/uart_nrf52.c

Queste funzioni sono descritte nel documento: doc/drivers/uart.md
```

---

## 4. Implementazione Pratica (Python + LLM)

Ecco uno scheletro di agent query layer:

```python
from neo4j import GraphDatabase
from openai import OpenAI  # o qualsiasi LLM

class GraphRAGAgent:
    def __init__(self, memgraph_uri, llm_client):
        self.driver = GraphDatabase.driver(memgraph_uri, auth=("", ""))
        self.llm = llm_client
        
        # Schema noto all'agent
        self.schema = """
        NODES: Document, Function, HardwarePeripheral, Board
        RELATIONSHIPS: SUPPORTS, USES, CALLS, DESCRIBES
        """
    
    def generate_cypher(self, question: str) -> str:
        """Chiede all'LLM di generare Cypher dalla domanda."""
        prompt = f"""
        You are a Cypher query generator for Memgraph.
        
        Schema:
        {self.schema}
        
        Question: {question}
        
        Generate ONLY a READ Cypher query (MATCH/RETURN). 
        No CREATE, DELETE, or WRITE operations.
        No explanations, just the query.
        """
        
        response = self.llm.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content.strip()
    
    def validate_query(self, query: str) -> bool:
        """Valida che la query sia sicura (solo READ)."""
        dangerous_keywords = ["CREATE", "DELETE", "DROP", "SET", "MERGE", "REMOVE"]
        query_upper = query.upper()
        
        for keyword in dangerous_keywords:
            if keyword in query_upper:
                print(f"⚠️ Query bloccata: contiene {keyword}")
                return False
        return True
    
    def execute_query(self, query: str):
        """Esegue la query validata su Memgraph."""
        with self.driver.session() as session:
            result = session.run(query)
            return [record.data() for record in result]
    
    def answer(self, question: str) -> str:
        """Flusso completo: domanda → Cypher → esecuzione → risposta."""
        # 1. Genera Cypher
        cypher = self.generate_cypher(question)
        print(f"🔍 Cypher generato:\n{cypher}")
        
        # 2. Valida
        if not self.validate_query(cypher):
            return "❌ Non posso eseguire questa query."
        
        # 3. Esegui
        results = self.execute_query(cypher)
        
        # 4. Sintetizza risposta
        prompt = f"""
        Question: {question}
        Query results: {results}
        
        Provide a natural language answer citing the sources.
        """
        response = self.llm.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content

# Utilizzo
agent = GraphRAGAgent("bolt://localhost:7687", openai_client)
answer = agent.answer("Quali funzioni usano la UART su nRF52840?")
print(answer)
```

---

## 5. Precauzioni di Sicurezza (OBBLIGATORIE)

| Rischio | Mitigazione |
|---------|-------------|
| **Query Injection** | Validare che contenga solo `MATCH`, `RETURN`, `WHERE` |
| **Query Costose** | Imporre `LIMIT` massimo (es. 100 risultati) |
| **Timeout** | Configurare timeout di sessione (es. 30 secondi) |
| **WRITE accidentali** | Utente DB con permessi **sola lettura** |
| **Schema Exposure** | Non rivelare tutto lo schema, solo label necessarie |
| **Data Leakage** | Filtra nodi sensibili (es. `WHERE d.type <> 'internal'`) |

---

## 6. GraphRAG Ibrido: Vector + Graph

Il vero potere di GraphRAG emerge quando combini:

1. **Vector Search** (semantico): "Trova documenti simili a..."
2. **Graph Traversal** (strutturale): "Segui le relazioni da..."

### Esempio di Query Ibrida:

```cypher
// 1. Trova documenti semanticamente simili (vector similarity)
// 2. Traversa le relazioni per trovare funzioni correlate
MATCH (d:Document)-[:DESCRIBES]->(f:Function)-[:CALLS]->(related:Function)
WHERE d.content CONTAINS "UART initialization"
RETURN d.title, f.name, related.name
```

Memgraph supporta **vector search nativo** tramite moduli. In alternativa, usa un vector store esterno (Qdrant, FAISS) e fai il join in Python.

---

## 7. Cosa Serve per Produrre

| Componente | Stato | Note |
|------------|-------|------|
| **GraphDB popolato** | ✅ Fatto (ingest script) | |
| **Agent Query Layer** | ⚠️ Da implementare | Vedi codice sopra |
| **Validation Layer** | ⚠️ Da implementare | Critico per sicurezza |
| **Vector Search** | ⚠️ Opzionale | Aggiunge semantica |
| **UI/Chat Interface** | ⚠️ Da implementare | Streamlit, FastAPI, ecc. |

