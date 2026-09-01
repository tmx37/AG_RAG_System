# Implementation notes

### Requisites
- Docker

### Logs
#### 01/09/2026: 
I've ensured to have docker.service up and running, then run the following cmd to start the container from `/DB/docker-compose.yml`:
```bash
    docker-compose up -d   
```
This container includes `Memgraph Lab`, a web UI view usefull for debug porpouse and visual graphing.
Once `memgraph` container is running, you can check for logs with:
```bash
    docker-compose logs -f
```

