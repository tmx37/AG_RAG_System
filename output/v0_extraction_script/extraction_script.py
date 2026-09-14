#!/usr/bin/env python3
"""Isolated, multi-level knowledge-graph extraction for the repository raw_data tree."""

from __future__ import annotations

import argparse
import ast
import gc
import json
import logging
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
RAW_DATA_DIR = (ROOT_DIR / "raw_data").resolve()
OUTPUT_DIR = (ROOT_DIR / "output").resolve()
LOG_DIR = (ROOT_DIR / "logs" / "agents").resolve()
ACCESS_LOG = (ROOT_DIR / "logs" / "access_log.jsonl").resolve()

CODE_EXTENSIONS = {
    ".py", ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".ipp",
    ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".go", ".rs",
    ".gn", ".gni", ".sh", ".bash", ".ps1", ".swift",
}
DOC_EXTENSIONS = {".md", ".rst", ".txt", ".adoc", ".markdown", ".xml", ".html"}
CONFIG_EXTENSIONS = {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg"}
GENERIC_NAMES = {"config", "data", "temp", "buf", "handler", "manager"}
URL_PATTERN = re.compile(r"\b(?:https?|ftp)://[^\s<>()\"']+")
IDENTIFIER_PATTERN = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")


@dataclass
class Entity:
    name: str
    entity_type: str
    source_file: str
    line_start: int
    line_end: int
    description: str = ""
    confidence: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.entity_type, self.name.casefold(), self.source_file)


@dataclass
class Relation:
    subject: str
    predicate: str
    object: str
    confidence: float = 1.0
    source_file: str = ""
    line: int = 0
    rationale: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.subject, self.predicate, self.object)


class Extraction:
    def __init__(self) -> None:
        self.entities: dict[tuple[str, str, str], Entity] = {}
        self.relations: dict[tuple[str, str, str], Relation] = {}
        self.ambiguities: list[dict[str, Any]] = []
        self.file_contents: dict[str, str] = {}
        self.doc_entities: dict[str, list[Entity]] = defaultdict(list)
        self.code_entities: dict[str, list[Entity]] = defaultdict(list)
        self.loggers = setup_logging()

    def add_entity(self, entity: Entity) -> Entity:
        current = self.entities.get(entity.key)
        if current is None or entity.confidence > current.confidence:
            self.entities[entity.key] = entity
            current = entity
        return current

    def add_relation(self, relation: Relation) -> None:
        current = self.relations.get(relation.key)
        if current is None or relation.confidence > current.confidence:
            self.relations[relation.key] = relation

    def add_ambiguity(self, **entry: Any) -> None:
        self.ambiguities.append(entry)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def setup_logging() -> dict[str, logging.Logger]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    result: dict[str, logging.Logger] = {}
    for level, name in enumerate(("structural", "semantic", "crosslink", "graph"), 1):
        logger = logging.getLogger(f"kg_extraction.level_{level}")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        handler = logging.FileHandler(LOG_DIR / f"extraction_ops_level_{level}.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        result[name] = logger
    return result


def log_access(path: Path, within_scope: bool, operation: str = "read") -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with ACCESS_LOG.open("a", encoding="utf-8") as handle:
        json.dump({"timestamp": now(), "operation": operation,
                   "path": str(path), "within_scope": within_scope}, handle)
        handle.write("\n")


def checked_path(path: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(RAW_DATA_DIR)
    except ValueError:
        log_access(resolved, False)
        raise RuntimeError(f"Scope violation: {resolved}")
    return resolved


def read_text(path: Path, extraction: Extraction) -> Optional[str]:
    resolved = checked_path(path)
    log_access(resolved, True)
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        extraction.loggers["structural"].warning("Unreadable file %s: %s", resolved, exc)
        return None
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            text = data.decode(encoding)
            if resolved.suffix.lower() in DOC_EXTENSIONS | CONFIG_EXTENSIONS:
                extraction.file_contents[str(resolved.relative_to(RAW_DATA_DIR))] = text
            return text
        except UnicodeDecodeError:
            continue
    extraction.loggers["structural"].warning("Encoding failure: %s", resolved)
    return None


def line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def rel_path(path: Path) -> str:
    return str(path.resolve().relative_to(RAW_DATA_DIR)).replace("\\", "/")


def add_file_entity(extraction: Extraction, path: Path, kind: str = "Module") -> Entity:
    entity = extraction.add_entity(Entity(
        path.name or str(path), kind, rel_path(path), 1, 1,
        description=f"File or module {rel_path(path)}", confidence=1.0,
    ))
    return entity


def extract_python(path: Path, text: str, extraction: Extraction) -> None:
    source = rel_path(path)
    try:
        tree = ast.parse(text, filename=source)
    except SyntaxError as exc:
        extraction.loggers["structural"].warning("Syntax error in %s: %s", source, exc)
        extraction.add_ambiguity(entity_name=source, entity_type="Module", source_file=source,
                                 line_number=exc.lineno or 1, reason="Syntax error; partial extraction unavailable",
                                 confidence=0.35)
        return
    module = add_file_entity(extraction, path)
    extraction.code_entities[source].append(module)
    scopes: list[Entity] = [module]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            entity = extraction.add_entity(Entity(
                node.name, "Function", source, node.lineno,
                getattr(node, "end_lineno", node.lineno), ast.get_docstring(node) or "",
                0.95, {"language": "python"},
            ))
            extraction.code_entities[source].append(entity)
            parent = next((item for item in scopes if item.line_start <= node.lineno <= item.line_end), module)
            extraction.add_relation(Relation(parent.name, "CONTAINS", entity.name, 0.95, source, node.lineno,
                                              "Function declaration is explicit"))
            calls = {call.func.id for call in ast.walk(node)
                     if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)}
            for called in calls:
                extraction.add_relation(Relation(entity.name, "CALLS", called, 0.85, source, node.lineno,
                                                  "Direct call expression"))
        elif isinstance(node, ast.ClassDef):
            entity = extraction.add_entity(Entity(
                node.name, "Class", source, node.lineno,
                getattr(node, "end_lineno", node.lineno), ast.get_docstring(node) or "",
                0.95, {"language": "python"},
            ))
            extraction.code_entities[source].append(entity)
            extraction.add_relation(Relation(module.name, "CONTAINS", entity.name, 0.95, source, node.lineno,
                                              "Class declaration is explicit"))
            for base in node.bases:
                if isinstance(base, ast.Name):
                    extraction.add_relation(Relation(entity.name, "EXTENDS", base.id, 0.95, source, node.lineno,
                                                      "Explicit class base"))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and getattr(node, "lineno", 0) == 1 or (
            isinstance(node, (ast.Assign, ast.AnnAssign)) and len(scopes) == 1
        ):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    entity = extraction.add_entity(Entity(
                        target.id, "Variable", source, node.lineno, node.lineno,
                        "Module-level assignment", 0.9, {"language": "python"},
                    ))
                    extraction.code_entities[source].append(entity)
                    extraction.add_relation(Relation(module.name, "CONTAINS", target.id, 0.9, source, node.lineno,
                                                      "Module-level assignment"))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name for alias in node.names]
            for imported in names:
                extraction.add_relation(Relation(module.name, "DEPENDS_ON", imported, 0.95, source,
                                                  node.lineno, "Explicit import"))


def extract_generic_code(path: Path, text: str, extraction: Extraction) -> None:
    source = rel_path(path)
    module = add_file_entity(extraction, path)
    extraction.code_entities[source].append(module)
    patterns = [
        ("Class", re.compile(r"\b(?:class|struct|interface)\s+([A-Za-z_]\w*)")),
        ("Type", re.compile(r"\b(?:typedef|enum|union)\s+(?:class\s+)?([A-Za-z_]\w*)")),
        ("Function", re.compile(r"(?m)^\s*(?:[\w:<>*&]+\s+)+([A-Za-z_]\w*)\s*\([^;\n]*\)\s*(?:\{|$)")),
        ("Function", re.compile(r"\b(?:function|func)\s+([A-Za-z_]\w*)\s*\(")),
    ]
    for entity_type, pattern in patterns:
        for match in pattern.finditer(text):
            name = match.group(1)
            line = line_number(text, match.start())
            entity = extraction.add_entity(Entity(name, entity_type, source, line, line, "",
                                                   0.9, {"language": path.suffix}))
            extraction.code_entities[source].append(entity)
            extraction.add_relation(Relation(module.name, "CONTAINS", name, 0.9, source, line,
                                              "Declaration pattern"))
    for match in re.finditer(r"(?m)^\s*#?\s*(?:include|import)\s*[<\"]([^>\"]+)", text):
        extraction.add_relation(Relation(module.name, "DEPENDS_ON", match.group(1), 0.95, source,
                                          line_number(text, match.start()), "Explicit include/import"))
    known = {entity.name for entity in extraction.code_entities[source]}
    for call in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", text):
        name = call.group(1)
        if name in known and name not in {"if", "for", "while", "switch"}:
            extraction.add_relation(Relation(module.name, "CALLS", name, 0.75, source,
                                              line_number(text, call.start()), "Intra-file call pattern"))


def extract_document(path: Path, text: str, extraction: Extraction) -> None:
    source = rel_path(path)
    document = extraction.add_entity(Entity(path.name, "Module", source, 1, max(1, text.count("\n") + 1),
                                            "Documentation document", 0.95, {"document": True}))
    extraction.doc_entities[source].append(document)
    for match in re.finditer(r"(?m)^(#{1,6})\s+(.+?)\s*$", text):
        title = match.group(2).strip()
        line = line_number(text, match.start())
        concept = extraction.add_entity(Entity(title, "Concept", source, line, line, "",
                                               0.85, {"heading_level": len(match.group(1))}))
        extraction.doc_entities[source].append(concept)
        extraction.add_relation(Relation(document.name, "CONTAINS", title, 0.95, source, line,
                                          "Document heading"))
    requirement = re.compile(r"(?im)^\s*(?:(REQ[-_]\w+)\s*[:.-]\s*|(?:the system|it|this)\s+)(?:shall|must|should)?\s*(.+)$")
    for match in requirement.finditer(text):
        body = (match.group(1) or "") + " " + match.group(2)
        line = line_number(text, match.start())
        name = body.strip()[:180]
        entity = extraction.add_entity(Entity(name, "Requirement", source, line, line, body.strip(),
                                               0.75, {"document": source}))
        extraction.doc_entities[source].append(entity)
        extraction.add_relation(Relation(document.name, "CONTAINS", name, 0.9, source, line,
                                          "Requirement language in documentation"))
    for match in re.finditer(r"(?i)\b(?:api|endpoint|interface|function)\s*[:`]*\s*([A-Za-z_]\w*)", text):
        name = match.group(1)
        line = line_number(text, match.start())
        api = extraction.add_entity(Entity(name, "API", source, line, line, match.group(0), 0.75))
        extraction.doc_entities[source].append(api)
        extraction.add_relation(Relation(document.name, "DESCRIBES", name, 0.75, source, line,
                                          "API reference pattern"))


def level_one(extraction: Extraction, files: list[Path]) -> None:
    logger = extraction.loggers["structural"]
    logger.info("Starting structural extraction for %d files", len(files))
    for index, path in enumerate(files, 1):
        text = read_text(path, extraction)
        if text is None:
            continue
        if path.suffix.lower() == ".py":
            extract_python(path, text, extraction)
        elif path.suffix.lower() in CODE_EXTENSIONS:
            extract_generic_code(path, text, extraction)
        if index % 128 == 0:
            gc.collect()
    logger.info("Structural extraction complete: %d entities, %d relations",
                len(extraction.entities), len(extraction.relations))


def level_two(extraction: Extraction, files: list[Path]) -> None:
    logger = extraction.loggers["semantic"]
    logger.info("Starting semantic extraction")
    for path in files:
        if path.suffix.lower() in DOC_EXTENSIONS or path.suffix.lower() in CONFIG_EXTENSIONS:
            text = extraction.file_contents.get(rel_path(path))
            if text is None:
                text = read_text(path, extraction)
            if text is not None:
                extract_document(path, text, extraction)
                for match in URL_PATTERN.finditer(text):
                    extraction.add_ambiguity(entity_name=match.group(0), entity_type="ExternalResource",
                                             source_file=rel_path(path), line_number=line_number(text, match.start()),
                                             reason="External URL found in source content", confidence=1.0)
    logger.info("Semantic extraction complete")


def level_three(extraction: Extraction) -> None:
    logger = extraction.loggers["crosslink"]
    code_by_name: dict[str, list[Entity]] = defaultdict(list)
    for entity in extraction.entities.values():
        if entity.entity_type in {"Function", "Class", "API"}:
            code_by_name[entity.name.casefold()].append(entity)
    for source, docs in extraction.doc_entities.items():
        text = extraction.file_contents.get(source, "").casefold()
        mentioned_names = set(IDENTIFIER_PATTERN.findall(text)) & set(code_by_name)
        for doc in docs:
            if doc.entity_type not in {"Concept", "Requirement", "API"}:
                continue
            for name in mentioned_names:
                candidates = code_by_name[name]
                for candidate in candidates:
                    confidence = 0.95 if candidate.name.casefold() == name else 0.75
                    predicate = "DOCUMENTED_IN" if candidate.entity_type == "Function" else "REFERENCED_IN"
                    extraction.add_relation(Relation(candidate.name, predicate, doc.name, confidence, source,
                                                      doc.line_start, "Name match in documentation"))
                    if len(candidates) > 1:
                        extraction.add_ambiguity(entity_name=name, entity_type=candidate.entity_type,
                                                 source_file=source, line_number=doc.line_start,
                                                 reason="Multiple code entities match documentation name",
                                                 confidence=confidence)
    for entity in extraction.entities.values():
        if entity.entity_type == "Requirement":
            words = set(IDENTIFIER_PATTERN.findall(entity.description.casefold()))
            for candidate in extraction.entities.values():
                if candidate.entity_type in {"Function", "Class", "Module"} and candidate.name.casefold() in words:
                    extraction.add_relation(Relation(entity.name, "SATISFIES", candidate.name, 0.7,
                                                      entity.source_file, entity.line_start,
                                                      "Requirement names implementation entity"))
    logger.info("Cross-linking complete: %d relations", len(extraction.relations))


def level_four(extraction: Extraction) -> dict[str, Any]:
    logger = extraction.loggers["graph"]
    degree: Counter[str] = Counter()
    for relation in extraction.relations.values():
        degree[relation.subject] += 1
        degree[relation.object] += 1
    isolated = [asdict(entity) for entity in extraction.entities.values() if degree[entity.name] == 0]
    metrics = {
        "total_entities": len(extraction.entities),
        "total_relations": len(extraction.relations),
        "entity_type_distribution": dict(Counter(e.entity_type for e in extraction.entities.values())),
        "relation_type_distribution": dict(Counter(r.predicate for r in extraction.relations.values())),
        "confidence_distribution": {
            "high_0.9-1.0": sum(r.confidence >= 0.9 for r in extraction.relations.values()),
            "medium_0.6-0.9": sum(0.6 <= r.confidence < 0.9 for r in extraction.relations.values()),
            "low_0.4-0.6": sum(0.4 <= r.confidence < 0.6 for r in extraction.relations.values()),
        },
        "ambiguities_flagged": len(extraction.ambiguities),
        "cross_links_established": sum(r.predicate in {"DOCUMENTED_IN", "REFERENCED_IN", "SATISFIES"}
                                      for r in extraction.relations.values()),
        "isolated_nodes": len(isolated),
        "high_centrality_nodes": [{"name": name, "degree": count}
                                  for name, count in degree.most_common(10)],
    }
    for entity in isolated:
        extraction.add_ambiguity(entity_name=entity["name"], entity_type=entity["entity_type"],
                                 source_file=entity["source_file"], line_number=entity["line_start"],
                                 reason="Entity has degree zero; possible dead code or orphan documentation",
                                 confidence=0.5)
    logger.info("Graph enrichment complete: %s", metrics)
    return metrics


def detect_ambiguities(extraction: Extraction) -> None:
    for entity in extraction.entities.values():
        if entity.name.casefold() in GENERIC_NAMES:
            extraction.add_ambiguity(entity_name=entity.name, entity_type=entity.entity_type,
                                     source_file=entity.source_file, line_number=entity.line_start,
                                     reason="Generic name requires contextual review", confidence=0.5)
    for relation in extraction.relations.values():
        if relation.confidence < 0.6:
            extraction.add_ambiguity(entity_name=relation.subject, entity_type="Relation",
                                     source_file=relation.source_file, line_number=relation.line,
                                     reason=f"Low-confidence {relation.predicate} to {relation.object}: {relation.rationale}",
                                     confidence=relation.confidence)
    for source, text in extraction.file_contents.items():
        for match in re.finditer(r"(?m)^\s*(?:def|function|[\w:<>*&]+\s+)\w+\s*\([^)]*\)", text):
            block = text[match.start():match.start() + 8000]
            if block.count("\n") > 100 and not re.search(r"(?m)^\s*(?:#|//|/\*|\*)", block):
                extraction.add_ambiguity(entity_name=match.group(0).strip(), entity_type="Function",
                                         source_file=source, line_number=line_number(text, match.start()),
                                         reason="Function exceeds 100 lines without nearby comments", confidence=0.5)


def export_results(extraction: Extraction, metrics: dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with (OUTPUT_DIR / "entities.jsonl").open("w", encoding="utf-8") as handle:
        for entity in sorted(extraction.entities.values(), key=lambda item: item.key):
            handle.write(json.dumps(asdict(entity), ensure_ascii=True) + "\n")
    with (OUTPUT_DIR / "relations.jsonl").open("w", encoding="utf-8") as handle:
        for relation in sorted(extraction.relations.values(), key=lambda item: item.key):
            handle.write(json.dumps(asdict(relation), ensure_ascii=True) + "\n")
    report = {
        "generated_at": now(),
        "metrics": metrics,
        "ambiguities": extraction.ambiguities,
    }
    (OUTPUT_DIR / "ambiguities.json").write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")


def connect_memgraph() -> Any:
    try:
        import mgclient
    except ImportError as exc:
        raise RuntimeError("mgclient is required for database ingestion; use --skip-db for file export") from exc
    return mgclient.connect(
        host=os.getenv("MEMGRAPH_HOST", "localhost"),
        port=int(os.getenv("MEMGRAPH_PORT", "7687")),
        username=os.getenv("MEMGRAPH_USERNAME", ""),
        password=os.getenv("MEMGRAPH_PASSWORD", ""),
    )


def ingest_memgraph(extraction: Extraction) -> None:
    logger = extraction.loggers["graph"]
    logger.info("Starting Memgraph ingestion: %d entities, %d relations",
                len(extraction.entities), len(extraction.relations))
    conn = connect_memgraph()
    try:
        cursor = conn.cursor()
        batch_size = 1000
        entity_query = (
            "UNWIND $rows AS row "
            "MERGE (n:Entity {name: row.name, type: row.type, file: row.file}) "
            "SET n.line_start=row.line, n.line_end=row.line_end, "
            "n.description=row.description, n.confidence=row.confidence"
        )
        entities = list(extraction.entities.values())
        for start in range(0, len(entities), batch_size):
            rows = [{
                "name": entity.name, "type": entity.entity_type, "file": entity.source_file,
                "line": entity.line_start, "line_end": entity.line_end,
                "description": entity.description, "confidence": entity.confidence,
            } for entity in entities[start:start + batch_size]]
            cursor.execute(entity_query, {"rows": rows})
            conn.commit()
            logger.info("Memgraph entities ingested: %d/%d", min(start + batch_size, len(entities)), len(entities))

        relation_query = (
            "UNWIND $rows AS row "
            "CALL { WITH row MATCH (a:Entity {name: row.subject}) RETURN a LIMIT 1 } "
            "CALL { WITH row MATCH (b:Entity {name: row.object}) RETURN b LIMIT 1 } "
            "MERGE (a)-[r:RELATED {predicate: row.predicate}]->(b) "
            "SET r.confidence=row.confidence, r.file=row.file, "
            "r.line=row.line, r.rationale=row.rationale"
        )
        relations = list(extraction.relations.values())
        relation_batch_size = batch_size
        for start in range(0, len(relations), relation_batch_size):
            rows = [{
                "subject": relation.subject, "object": relation.object,
                "predicate": relation.predicate, "confidence": relation.confidence,
                "file": relation.source_file, "line": relation.line,
                "rationale": relation.rationale,
            } for relation in relations[start:start + batch_size]]
            cursor.execute(relation_query, {"rows": rows})
            conn.commit()
            logger.info("Memgraph relations ingested: %d/%d",
                        min(start + relation_batch_size, len(relations)), len(relations))

        cursor.execute("MATCH (n:Entity) RETURN count(n)")
        entity_count = cursor.fetchone()[0]
        cursor.execute("MATCH ()-[r:RELATED]->() RETURN count(r)")
        relation_count = cursor.fetchone()[0]
        conn.commit()
        logger.info("Memgraph ingestion complete: %d Entity nodes, %d RELATED relationships",
                    entity_count, relation_count)
    except Exception:
        logger.exception("Memgraph ingestion failed")
        raise
    finally:
        conn.close()


def load_exported_extraction() -> Extraction:
    extraction = Extraction()
    with (OUTPUT_DIR / "entities.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            entity = Entity(**json.loads(line))
            extraction.entities[entity.key] = entity
    with (OUTPUT_DIR / "relations.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            relation = Relation(**json.loads(line))
            extraction.relations[relation.key] = relation
    return extraction


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-db", action="store_true", help="Export files without connecting to Memgraph")
    parser.add_argument("--ingest-existing", action="store_true",
                        help="Ingest previously exported entities.jsonl and relations.jsonl")
    args = parser.parse_args(argv)
    if args.ingest_existing:
        if not (OUTPUT_DIR / "entities.jsonl").is_file() or not (OUTPUT_DIR / "relations.jsonl").is_file():
            print("Missing exported entities.jsonl or relations.jsonl", file=sys.stderr)
            return 1
        ingest_memgraph(load_exported_extraction())
        return 0
    if not RAW_DATA_DIR.is_dir():
        print(f"Missing raw_data directory: {RAW_DATA_DIR}", file=sys.stderr)
        return 1
    extraction = Extraction()
    files = sorted(path for path in RAW_DATA_DIR.rglob("*") if path.is_file())
    for path in files:
        checked_path(path)
    processable = [
        path for path in files
        if path.suffix.lower() in CODE_EXTENSIONS
        or path.suffix.lower() in DOC_EXTENSIONS
        or path.suffix.lower() in CONFIG_EXTENSIONS
        or path.name.upper() in {"README", "LICENSE", "NOTICE", "AGENTS", "CLAUDE", "GEMINI"}
    ]
    level_one(extraction, processable)
    level_two(extraction, processable)
    level_three(extraction)
    metrics = level_four(extraction)
    detect_ambiguities(extraction)
    metrics["ambiguities_flagged"] = len(extraction.ambiguities)
    export_results(extraction, metrics)
    if not args.skip_db:
        ingest_memgraph(extraction)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
