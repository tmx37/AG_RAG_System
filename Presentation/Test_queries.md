# Query esemplificative per dimostrare contenuto del grafico
"Quali funzioni UART esistono?"
```cypher
MATCH (f:Function)
WHERE f.name CONTAINS 'uart'
RETURN f.name, f.file, f.kind
ORDER BY f.file, f.name;
```
"Quali relazioni coinvolgono funzioni UART?"
```cypher
MATCH (f:Function)-[r]-(other)
WHERE f.name CONTAINS 'uart'
RETURN TYPE(r) AS rel_type, COUNT(*) AS count
ORDER BY count DESC;
```
"Quali Document esistono?"
```cypher
MATCH (d:Document)
RETURN d.path, d.description
LIMIT 10;
```
"Ci sono Requirement nel grafo?"
```cypher
MATCH (r:Requirement)
RETURN r.description AS text, r.file AS source
LIMIT 10;
```

### Query Strutturata Base
"Trova tutte le funzioni che gestiscono interrupt UART"
```cypher
MATCH (f:Function)
WHERE (f.name CONTAINS 'uart' OR f.file CONTAINS 'uart')
  AND (f.name CONTAINS 'interrupt' OR f.name CONTAINS 'irq' OR f.name CONTAINS 'isr')
RETURN 
    f.uid AS function_uid,
    f.name AS function_name,
    f.file AS file,
    f.line_start AS line_start,
    f.kind AS kind
ORDER BY f.file, f.line_start;
```
### Query di Graph Traversal
"Traccia il percorso di chiamata da main() a uart_init()"
```cypher
MATCH path = 
    (source:Function {name: 'main'})
    -[:CALLS*1..10]->
    (target:Function {name: 'uart_init'})
WITH path, nodes(path) AS path_nodes, relationships(path) AS path_rels
UNWIND path_nodes AS node
WITH path, path_rels, COLLECT(node.uid) AS uid_chain, COLLECT(node.file) AS file_chain
RETURN 
    uid_chain AS function_chain,
    file_chain AS files_involved,
    size(path) AS call_depth,
    path_rels AS traversal_relationships
ORDER BY size(path) ASC
LIMIT 5;
```
"Trova dipendenze circolari tra moduli"
```cypher
MATCH path = (sf:SourceFile)-[:INCLUDES*2..5]->(sf)
WITH path, nodes(path) AS path_nodes
UNWIND path_nodes AS node
WITH path, COLLECT(node.path) AS cycle_path
RETURN 
    cycle_path,
    size(path) AS cycle_length
ORDER BY size(path) ASC
LIMIT 10;
```

