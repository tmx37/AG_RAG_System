# Implementation notes

### Requisites
- Docker
- Python 3.0
- Python libraries: `memgraph`, `pymgclient`, `lxml`, `markdown`, `pyyaml`

### Logs
#### 01/09/2026: DB SETUP + Ingestion Script
I've ensured to have docker.service up and running, then run the following cmd to start the container from `/DB/docker-compose.yml`:
```bash
    docker-compose up -d   
```
This container includes `Memgraph Lab`, a web UI view usefull for debug porpouse and visual graphing.
Once `memgraph` container is running, you can check for logs with:
```bash
    docker-compose logs -f
```

I decided to use the `Zephyr Project` (https://github.com/zephyrproject-rtos/zephyr) as my raw data, because:
- It's a similar use case 
- Contains a wide range of different documents (.md, .c, .xml, .yaml, .json, etc..)
- Its big to be stressfull enough for a GraphDB
```bash
    git clone --depth 1 git@github.com:zephyrproject-rtos/zephyr.git zephyr-demo-data
```

Now I need a python script that acts as an Ingestion Pipeline. 
This will process the different formats and load them into Memgraph.

I'd like to evaluate 2 ways of doing this:
1. by a deterministic schema
2. with the help of a llm to create and fill the schema 

I will stick with the first approach and try the second one later.

```schema example
Nodes: (:Document), (:Function), (:HardwarePeripheral), (:ConfigOption), (:Board)

Relationships:

    (:Document)-[:DESCRIBES]->(:Function)
    (:Function)-[:CALLS]->(:Function)
    (:Function)-[:USES]->(:HardwarePeripheral)
    (:Board)-[:SUPPORTS]->(:HardwarePeripheral)
    (:ConfigOption)-[:AFFECTS]->(:Function)
```

I'll try to get this schema with copilot to get a better result.

I'm going to install the following libraries: 
```shell
    pip install memgraph pymgclient lxml markdown pyyaml
```