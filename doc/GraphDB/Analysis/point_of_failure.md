## Possible Points of Failure

| Component | Single Point of Failure | Redundancy Strategy | Mitigation |
|-----------|-------------------------|---------------------|------------|
| **Graph DB** | Yes (single node demo) | Read-only replica (production) | Daily backup + snapshots |
| **Embedding Pipeline** | Yes (batch processing) | Queue-based (Redis/RabbitMQ) | Retry logic + dead letter queue |
| **MCP Server** | Yes (centralized) | Load balancer + health check | Circuit breaker + fallback cache |
| **Vector Store** | Yes (if separate) | Synchronous replica | Periodic consistency check |
| **LLM API** | Yes (if external) | Multi-provider fallback | Cache frequent responses |

**Critical Failure Modes:**
- **RAM Saturation** (Memgraph/FalkorDB): Continuous monitoring + alert at 80% usage
- **Graph Corruption** (ETL bug): Snapshot versioning + automatic rollback
- **Slow Queries** (missing indexes): Query profiling + automatic indexes on frequent properties

## Edge Cases

**Anomalous Use Cases:**

1. **Malformed Documentation**:
   - Scanned PDFs (OCR required)
   - Corrupted or partial files
   - **Mitigation**: Pre-ingestion validation pipeline + error logging

2. **Ambiguous or Multi-Intent Queries**:
   - "Show me all I2C drivers" → does it mean code, documentation, or both?
   - **Mitigation**: Query clarification layer (LLM asks for context)

3. **Document Updates During Queries**:
   - Concurrent writes on graph nodes
   - **Mitigation**: Node-level locking or ACID transactions (Neo4j/ArangoDB)

4. **Ambiguous Entity Resolution**:
   - "RTC" → Real-Time Clock or Remote Terminal Controller?
   - **Mitigation**: Context-based disambiguation (namespace per project)

5. **Hybrid Dataset (Code + Documents)**:
   - Code graph (AST) + documentation graph (concepts)
   - **Mitigation**: Separate graphs with bridge nodes or multi-model (ArangoDB)