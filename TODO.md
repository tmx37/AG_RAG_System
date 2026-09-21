# V1 -> zephyr data used (problem: too much data I don't know)
# V2 -> use actual business data based on Tested indexing.

# Preparation_1
- [X] Retrive raw data -> Confluence pull, 12 out and all modules used (use Enrico Benso repo)
- [X] Find a good model and prompt for serialization -> Make the model find linking methods between the files and use as template the ingestion script made for reference on common methods used (verify how usefull is doxygen)
- [X] Create a GraphDB 

# Test_1: Fill the GraphDB
- [ ] Find out how many tokens are required to serialize the nodes from raw data
- [x] Find out how many time it takes to get a valid output: più di 3 ore se non ottimizzato
- [ ] capire come gestire le liste di dati presenti in ambiguities e unresolved_relations
- [ ] verificare validità e natura dei dati contenuti nel db

# Preparation_2
- [ ] Find a clear way to count tokens (for both input and output operations) -> proxy server?
- [ ] Find out 100 questions with a related "score" (generic/specific, define "score")

# Test_2: Test Agentic Retrival
Use the *same prompt* for 4 different approaches:
1. Agent + GraphRAG
2. Agent + Document-index (todo: create a generic index on .md file)
3. Agent + Raw "database" without index
4. Agent + Raw "database" retrived by internet

# NB:
- Provare ad implementare tecniche di prompt caching per le operazioni comuni di graph traversal
- Implementare policy di economia di token nei prompt in cache (small contexts, output ottimizzato, ) + prompt di "istruzioni" per fare economia

