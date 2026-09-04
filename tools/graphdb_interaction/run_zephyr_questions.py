"""Run read-only capability questions against the Zephyr Memgraph graph."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import mgclient


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = PROJECT_ROOT / "logs" / "graphdb_interaction"

QUESTIONS: list[dict[str, str]] = [
    {
        "id": "q01_node_inventory",
        "question": "How many nodes exist for each label in the graph?",
        "query": "MATCH (n) RETURN labels(n) AS labels, count(*) AS count ORDER BY count DESC",
    },
    {
        "id": "q02_relationship_inventory",
        "question": "How many relationships exist for each relationship type?",
        "query": "MATCH ()-[r]->() RETURN type(r) AS relationship, count(*) AS count ORDER BY count DESC",
    },
    {
        "id": "q03_bluetooth_documents",
        "question": "Which documentation files have titles or paths containing bluetooth?",
        "query": (
            "MATCH (d:Document) WHERE toLower(d.path) CONTAINS 'bluetooth' "
            "OR toLower(d.title) CONTAINS 'bluetooth' "
            "RETURN d.path AS path, d.title AS title ORDER BY d.path LIMIT 25"
        ),
    },
    {
        "id": "q04_driver_functions",
        "question": "Which functions are defined in files under a drivers path?",
        "query": (
            "MATCH (f:Function) WHERE toLower(f.file) CONTAINS 'drivers' "
            "RETURN f.name AS name, f.file AS file, f.kind AS kind "
            "ORDER BY f.file, f.name LIMIT 25"
        ),
    },
    {
        "id": "q05_uart_peripherals",
        "question": "Which hardware peripherals contain uart in their compatibility identifier?",
        "query": (
            "MATCH (p:HardwarePeripheral) "
            "WHERE toLower(p.compatible) CONTAINS 'uart' "
            "RETURN p.compatible AS compatible, p.source_file AS source_file "
            "ORDER BY p.compatible LIMIT 25"
        ),
    },
    {
        "id": "q06_nrf_boards",
        "question": "Which boards have nrf in their identifier or name?",
        "query": (
            "MATCH (b:Board) WHERE toLower(b.id) CONTAINS 'nrf' "
            "OR toLower(b.name) CONTAINS 'nrf' "
            "RETURN b.id AS id, b.name AS name, b.path AS path "
            "ORDER BY b.id LIMIT 25"
        ),
    },
    {
        "id": "q07_board_support",
        "question": "Which boards support which hardware peripherals?",
        "query": (
            "MATCH (b:Board)-[:SUPPORTS]->(p:HardwarePeripheral) "
            "RETURN b.id AS board_id, b.name AS board_name, "
            "p.compatible AS peripheral ORDER BY b.id, p.compatible LIMIT 25"
        ),
    },
    {
        "id": "q08_function_usage",
        "question": "Which functions are linked to hardware peripherals, and which peripherals do they use?",
        "query": (
            "MATCH (f:Function)-[:USES]->(p:HardwarePeripheral) "
            "RETURN f.name AS function, f.file AS file, "
            "p.compatible AS peripheral ORDER BY f.name, p.compatible LIMIT 25"
        ),
    },
    {
        "id": "q09_document_descriptions",
        "question": "Which documents describe functions whose names contain printk?",
        "query": (
            "MATCH (d:Document)-[:DESCRIBES]->(f:Function) "
            "WHERE toLower(f.name) CONTAINS 'printk' "
            "RETURN d.path AS document, f.name AS function, f.file AS file "
            "ORDER BY d.path, f.name LIMIT 25"
        ),
    },
    {
        "id": "q10_call_graph",
        "question": "Which functions call another function according to the graph?",
        "query": (
            "MATCH (caller:Function)-[:CALLS]->(callee:Function) "
            "RETURN caller.name AS caller, caller.file AS caller_file, "
            "callee.name AS callee, callee.file AS callee_file "
            "ORDER BY caller.name, callee.name LIMIT 25"
        ),
    },
]


def execute_query(connection: mgclient.Connection, query: str) -> list[dict[str, Any]]:
    cursor = connection.cursor()
    try:
        cursor.execute(query)
        columns = [column.name for column in cursor.description or []]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        cursor.close()


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc)
    log_path = LOG_DIR / f"{started_at.strftime('%Y%m%dT%H%M%SZ')}_zephyr_questions.jsonl"
    with log_path.open("w", encoding="utf-8") as log_file:
        log_file.write(json.dumps({
            "timestamp_utc": started_at.isoformat(),
            "operation": "suite_start",
            "host": "localhost",
            "port": 7687,
            "question_count": len(QUESTIONS),
        }) + "\n")
        try:
            connection = mgclient.connect(host="localhost", port=7687, username="", password="")
            log_file.write(json.dumps({
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "operation": "connect",
                "status": "ok",
            }) + "\n")
        except Exception as error:
            log_file.write(json.dumps({
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "operation": "connect",
                "status": "error",
                "error": str(error),
            }) + "\n")
            raise
        try:
            for item in QUESTIONS:
                record: dict[str, Any] = {
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "question_id": item["id"],
                    "question": item["question"],
                    "query": item["query"],
                }
                try:
                    record["rows"] = execute_query(connection, item["query"])
                    record["status"] = "ok"
                except Exception as error:
                    record["status"] = "error"
                    record["error"] = str(error)
                log_file.write(json.dumps(record, default=str) + "\n")
                print(f"{item['id']}: {record['status']} ({len(record.get('rows', []))} rows)")
        finally:
            connection.close()
            log_file.write(json.dumps({
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "operation": "disconnect",
                "status": "ok",
            }) + "\n")
        log_file.write(json.dumps({
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "operation": "suite_complete",
            "status": "ok",
        }) + "\n")
    print(f"Operations logged to {log_path}")


if __name__ == "__main__":
    main()
