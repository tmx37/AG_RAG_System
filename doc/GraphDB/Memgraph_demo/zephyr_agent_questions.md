# Zephyr GraphDB capability questions

These questions are designed for the graph currently produced by
[`DB/ingest_zephyr_data.py`](../../../DB/ingest_zephyr_data.py). They deliberately
cover node discovery, relationship discovery, filtering, traversals, empty
results, and result limits.

| ID | Question | GraphDB capability |
| --- | --- | --- |
| Q01 | How many nodes exist for each label in the graph? | Label aggregation |
| Q02 | How many relationships exist for each relationship type? | Relationship aggregation |
| Q03 | Which documentation files have titles or paths containing `bluetooth`? | Property filtering |
| Q04 | Which functions are defined in files under a `drivers` path? | Function filtering |
| Q05 | Which hardware peripherals contain `uart` in their compatibility identifier? | Peripheral filtering |
| Q06 | Which boards have `nrf` in their identifier or name? | Board filtering |
| Q07 | Which boards support which hardware peripherals? | `SUPPORTS` traversal, including empty results |
| Q08 | Which functions are linked to hardware peripherals, and which peripherals do they use? | `USES` traversal |
| Q09 | Which documents describe functions whose names contain `printk`? | Multi-hop `DESCRIBES` traversal |
| Q10 | Which functions call another function according to the graph? | `CALLS` traversal, including empty results |

The executable query definitions are in
[`tools/graphdb_interaction/run_zephyr_questions.py`](../../../tools/graphdb_interaction/run_zephyr_questions.py).
Each query is read-only and uses only labels, properties, and relationships
created by the ingestion script.
