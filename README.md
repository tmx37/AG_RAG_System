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