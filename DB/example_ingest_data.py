"""
Knowledge Graph Extraction Pipeline - Generic Implementation
Version: 2.2 - Production Reference

This script extracts entities and relationships from source code and documentation,
populating a graph database with a multi-level extraction approach.

Architecture follows: Structural → Semantic → Cross-Linking → Graph Enrichment
"""

import os
import re
import sys
import logging
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple, Any
from dataclasses import dataclass, field
from datetime import datetime

import yaml
from mgclient import Connection

# --- CONFIGURATION ---
MEMGRAPH_HOST = os.getenv("MEMGRAPH_HOST", "localhost")
MEMGRAPH_PORT = int(os.getenv("MEMGRAPH_PORT", "7687"))
MEMGRAPH_USERNAME = os.getenv("MEMGRAPH_USERNAME", "")
MEMGRAPH_PASSWORD = os.getenv("MEMGRAPH_PASSWORD", "")

SCRIPT_DIR = Path(__file__).parent.absolute()
RAW_DATA_DIR = os.getenv("RAW_DATA_DIR", SCRIPT_DIR / "raw_data")
LOG_DIR = SCRIPT_DIR / "logs"
OUTPUT_DIR = SCRIPT_DIR / "output"

# Entity types as defined in extraction prompt
ENTITY_TYPES = ["Function", "Class", "Module", "Variable", "Type", "Concept", "Requirement", "API"]
RELATION_TYPES = ["CONTAINS", "CALLS", "USES", "DEPENDS_ON", "DESCRIBES", "SATISFIES", 
                  "ILLUSTRATES", "DOCUMENTED_IN", "IMPLEMENTED_BY", "EXPOSED_BY"]

# Confidence thresholds
CONFIDENCE_HIGH = 0.90
CONFIDENCE_MEDIUM = 0.60
CONFIDENCE_LOW = 0.40

# --- LOGGING SETUP ---
def setup_logging(level: int = logging.INFO) -> Dict[str, logging.Logger]:
    """Initialize loggers for each extraction level."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    
    loggers = {}
    level_names = ["structural", "semantic", "crosslink", "graph"]
    
    for i, name in enumerate(level_names, 1):
        logger = logging.getLogger(f"level_{i}_{name}")
        logger.setLevel(level)
        
        handler = logging.FileHandler(LOG_DIR / f"level_{i}_{name}.log")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s - %(levelname)s - %(message)s"
        ))
        logger.addHandler(handler)
        loggers[name] = logger
    
    return loggers

# --- DATABASE CONNECTION ---
def get_connection() -> Connection:
    """Establish connection to Memgraph database."""
    try:
        conn = Connection(
            host=MEMGRAPH_HOST,
            port=MEMGRAPH_PORT,
            username=MEMGRAPH_USERNAME,
            password=MEMGRAPH_PASSWORD
        )
        logging.info(f"Connected to Memgraph at {MEMGRAPH_HOST}:{MEMGRAPH_PORT}")
        return conn
    except Exception as exception:
        logging.error(f"Failed to connect to Memgraph: {exception}")
        sys.exit(1)


def execute_query(conn: Connection, query: str, params: Optional[dict] = None) -> List[Any]:
    """Execute a Cypher query with parameter binding."""
    try:
        cursor = conn.cursor()
        if params is None:
            params = {}
        
        cursor.execute(query, params)
        output = cursor.fetchall() if cursor.description is not None else []
        cursor.close()
        conn.commit()
        return list(output)
    except Exception as exception:
        logging.warning(f"Query error: {exception}")
        return []


# --- DATA STRUCTURES ---
@dataclass
class Entity:
    """Represents an extracted entity from code or documentation."""
    name: str
    entity_type: str
    source_file: str
    line_start: int
    line_end: int
    description: str = ""
    confidence: float = 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Relation:
    """Represents a relationship between two entities."""
    subject_id: str
    predicate: str
    object_id: str
    confidence: float = 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Ambiguity:
    """Represents an ambiguous extraction requiring human review."""
    entity_name: str
    entity_type: str
    source_file: str
    line_number: int
    reason: str
    confidence: float


# --- HELPER FUNCTIONS ---
def find_values(value: Any, key: str) -> Iterable[str]:
    """Yield scalar values for a key at any depth in a YAML document."""
    if isinstance(value, dict):
        for current_key, current_value in value.items():
            if current_key == key:
                values = current_value if isinstance(current_value, list) else [current_value]
                for item in values:
                    if isinstance(item, str) and item.strip():
                        yield item.strip()
            yield from find_values(current_value, key)
    elif isinstance(value, list):
        for item in value:
            yield from find_values(item, key)


def normalize_entity_name(name: str) -> str:
    """Normalize entity name by removing common prefixes and converting to lowercase."""
    prefixes = ["get_", "set_", "m_", "_", "is_", "has_", "can_"]
    normalized = name.lower()
    for prefix in prefixes:
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
    return normalized


def calculate_confidence(explicit: bool, pattern_matches: int, context_available: bool) -> float:
    """Calculate confidence score based on extraction criteria."""
    if explicit:
        return 0.95
    
    score = 0.50
    if pattern_matches > 3:
        score += 0.25
    elif pattern_matches > 1:
        score += 0.15
    
    if context_available:
        score += 0.15
    
    return min(score, 0.99)


# --- LEVEL 1: STRUCTURAL EXTRACTION ---
def extract_structural_entities(conn: Connection, loggers: Dict[str, logging.Logger]) -> Tuple[int, int]:
    """
    Level 1: Extract structural entities from source code files.
    Uses pattern matching for languages without AST parsers available.
    """
    loggers["structural"].info("Starting structural extraction")
    
    entity_count = 0
    relation_count = 0
    
    code_extensions = [".py", ".c", ".cpp", ".h", ".hpp", ".js", ".ts", ".java", ".go", ".rs"]
    
    for file_path in Path(RAW_DATA_DIR).rglob("*"):
        if file_path.is_file() and file_path.suffix in code_extensions:
            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
                rel_path = str(file_path.relative_to(RAW_DATA_DIR))
                
                # Extract functions
                function_pattern = r"(?:def|function|void|int|char|bool|auto)\s+(\w+)\s*\("
                for match in re.finditer(function_pattern, content):
                    func_name = match.group(1)
                    line_start = content[:match.start()].count("\n") + 1
                    
                    entity_query = """
                    MERGE (e:Function {name: $name, file: $file})
                    SET e.line_start = $line_start, e.type = 'Function', e.confidence = $confidence
                    """
                    execute_query(conn, entity_query, {
                        "name": func_name,
                        "file": rel_path,
                        "line_start": line_start,
                        "confidence": 1.0
                    })
                    entity_count += 1
                    loggers["structural"].debug(f"Found function: {func_name} in {rel_path}:{line_start}")
                
                # Extract classes
                class_pattern = r"(?:class|struct)\s+(\w+)"
                for match in re.finditer(class_pattern, content):
                    class_name = match.group(1)
                    line_start = content[:match.start()].count("\n") + 1
                    
                    entity_query = """
                    MERGE (e:Class {name: $name, file: $file})
                    SET e.line_start = $line_start, e.type = 'Class', e.confidence = $confidence
                    """
                    execute_query(conn, entity_query, {
                        "name": class_name,
                        "file": rel_path,
                        "line_start": line_start,
                        "confidence": 1.0
                    })
                    entity_count += 1
                    loggers["structural"].debug(f"Found class: {class_name} in {rel_path}:{line_start}")
                
                # Extract imports/dependencies
                import_pattern = r"(?:import|from)\s+([\w.]+)"
                imports = set(re.findall(import_pattern, content))
                for imported_module in imports:
                    relation_query = """
                    MATCH (source:Function {file: $file})
                    MERGE (target:Module {name: $module})
                    MERGE (source)-[:DEPENDS_ON]->(target)
                    """
                    if execute_query(conn, relation_query, {"file": rel_path, "module": imported_module}):
                        relation_count += 1
                
            except Exception as exception:
                loggers["structural"].error(f"Error processing {file_path}: {exception}")
    
    loggers["structural"].info(f"Structural extraction complete: {entity_count} entities, {relation_count} relations")
    return entity_count, relation_count


# --- LEVEL 2: SEMANTIC EXTRACTION ---
def extract_semantic_entities(conn: Connection, loggers: Dict[str, logging.Logger]) -> Tuple[int, int]:
    """
    Level 2: Extract semantic entities from documentation.
    Identifies concepts, requirements, and API references.
    """
    loggers["semantic"].info("Starting semantic extraction")
    
    entity_count = 0
    relation_count = 0
    
    doc_extensions = [".md", ".rst", ".txt", ".adoc"]
    
    for file_path in Path(RAW_DATA_DIR).rglob("*"):
        if file_path.is_file() and file_path.suffix in doc_extensions:
            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
                rel_path = str(file_path.relative_to(RAW_DATA_DIR))
                
                # Extract document metadata
                title = "Untitled"
                if content.startswith("#"):
                    title = content.split("\n")[0].replace("#", "").strip()
                
                doc_query = """
                MERGE (d:Document {path: $path})
                SET d.title = $title, d.type = 'documentation'
                """
                execute_query(conn, doc_query, {"path": rel_path, "title": title})
                
                # Extract requirements (patterns like "REQ-", "Requirement:", "Must", "Shall")
                requirement_patterns = [
                    r"(REQ[-_]\d+):\s*([^\n]+)",
                    r"Requirement[:\s]+([^\n]+)",
                    r"(?:The system|It|This)\s+(?:shall|must|should)\s+([^\n]+)"
                ]
                
                for pattern in requirement_patterns:
                    for match in re.finditer(pattern, content, re.IGNORECASE):
                        req_text = match.group(0)
                        line_number = content[:match.start()].count("\n") + 1
                        
                        req_query = """
                        MERGE (r:Requirement {text: $text, source: $source})
                        SET r.line = $line, r.type = 'Requirement', r.confidence = $confidence
                        """
                        execute_query(conn, req_query, {
                            "text": req_text[:500],
                            "source": rel_path,
                            "line": line_number,
                            "confidence": 0.75
                        })
                        entity_count += 1
                        loggers["semantic"].debug(f"Found requirement in {rel_path}:{line_number}")
                
                # Extract concepts (section headers, defined terms)
                concept_pattern = r"^#{1,3}\s+(.+)$"
                for match in re.finditer(concept_pattern, content, re.MULTILINE):
                    concept_name = match.group(1).strip()
                    line_number = content[:match.start()].count("\n") + 1
                    
                    concept_query = """
                    MERGE (c:Concept {name: $name, source: $source})
                    SET c.line = $line, c.type = 'Concept', c.confidence = $confidence
                    """
                    execute_query(conn, concept_query, {
                        "name": concept_name,
                        "source": rel_path,
                        "line": line_number,
                        "confidence": 0.85
                    })
                    entity_count += 1
                
                # Link document sections to concepts
                section_query = """
                MATCH (d:Document {path: $path})
                MATCH (c:Concept {source: $path})
                MERGE (d)-[:HAS_SECTION]->(c)
                """
                if execute_query(conn, section_query, {"path": rel_path}):
                    relation_count += 1
                
            except Exception as exception:
                loggers["semantic"].error(f"Error processing {file_path}: {exception}")
    
    loggers["semantic"].info(f"Semantic extraction complete: {entity_count} entities, {relation_count} relations")
    return entity_count, relation_count


# --- LEVEL 3: CROSS-LINKING ---
def create_cross_links(conn: Connection, loggers: Dict[str, logging.Logger]) -> int:
    """
    Level 3: Create cross-links between code entities and documentation.
    Uses name matching and content analysis for entity resolution.
    """
    loggers["crosslink"].info("Starting cross-linking")
    
    link_count = 0
    
    # Link documents to functions (when function name appears in document)
    doc_function_query = """
    MATCH (d:Document), (f:Function)
    WHERE d.content CONTAINS f.name OR d.title CONTAINS f.name
    MERGE (d)-[:DESCRIBES]->(f)
    SET relationship.confidence = 0.85
    RETURN count(*) AS links
    """
    result = execute_query(conn, doc_function_query)
    if result:
        link_count += result[0].get("links", 0)
    loggers["crosslink"].info(f"Created {link_count} document-function links")
    
    # Link requirements to implementing functions (heuristic: same file or referenced by name)
    req_function_query = """
    MATCH (r:Requirement), (f:Function)
    WHERE r.source = f.file OR r.text CONTAINS f.name
    MERGE (r)-[:SATISFIED_BY]->(f)
    SET relationship.confidence = 0.70
    RETURN count(*) AS links
    """
    result = execute_query(conn, req_function_query)
    if result:
        link_count += result[0].get("links", 0)
    loggers["crosslink"].info(f"Created requirement-function links")
    
    # Link concepts to APIs
    concept_api_query = """
    MATCH (c:Concept), (f:Function)
    WHERE c.name CONTAINS f.name OR f.name CONTAINS c.name
    MERGE (c)-[:ILLUSTRATES]->(f)
    SET relationship.confidence = 0.75
    RETURN count(*) AS links
    """
    result = execute_query(conn, concept_api_query)
    if result:
        link_count += result[0].get("links", 0)
    
    loggers["crosslink"].info(f"Cross-linking complete: {link_count} total links")
    return link_count


# --- LEVEL 4: GRAPH ENRICHMENT ---
def enrich_graph(conn: Connection, loggers: Dict[str, logging.Logger]) -> Dict[str, Any]:
    """
    Level 4: Apply graph algorithms for enrichment.
    Computes metrics and identifies structural patterns.
    """
    loggers["graph"].info("Starting graph enrichment")
    
    metrics = {}
    
    # Count total nodes and edges
    count_query = """
    MATCH (n) RETURN count(n) AS nodes
    """
    result = execute_query(conn, count_query)
    metrics["total_nodes"] = result[0]["nodes"] if result else 0
    
    edge_query = """
    MATCH ()-[r]->() RETURN count(r) AS edges
    """
    result = execute_query(conn, edge_query)
    metrics["total_edges"] = result[0]["edges"] if result else 0
    
    # Entity type distribution
    type_query = """
    MATCH (n)
    WITH labels(n)[0] AS type, count(*) AS count
    RETURN type, count
    """
    result = execute_query(conn, type_query)
    metrics["entity_distribution"] = {row["type"]: row["count"] for row in result} if result else {}
    
    # Relation type distribution
    rel_query = """
    MATCH ()-[r]->()
    WITH type(r) AS type, count(*) AS count
    RETURN type, count
    """
    result = execute_query(conn, rel_query)
    metrics["relation_distribution"] = {row["type"]: row["count"] for row in result} if result else {}
    
    # Confidence distribution
    confidence_query = """
    MATCH (n)
    WHERE n.confidence IS NOT NULL
    WITH 
        sum(CASE WHEN n.confidence >= 0.9 THEN 1 ELSE 0 END) AS high,
        sum(CASE WHEN n.confidence >= 0.6 AND n.confidence < 0.9 THEN 1 ELSE 0 END) AS medium,
        sum(CASE WHEN n.confidence < 0.6 THEN 1 ELSE 0 END) AS low
    RETURN high, medium, low
    """
    result = execute_query(conn, confidence_query)
    if result:
        metrics["confidence_distribution"] = {
            "high_0.9-1.0": result[0]["high"],
            "medium_0.6-0.9": result[0]["medium"],
            "low_0.4-0.6": result[0]["low"]
        }
    
    # Identify isolated nodes (potential issues)
    isolated_query = """
    MATCH (n)
    WHERE NOT (n)--()
    RETURN count(n) AS isolated
    """
    result = execute_query(conn, isolated_query)
    metrics["isolated_nodes"] = result[0]["isolated"] if result else 0
    
    # Identify high-centrality nodes (functions with many connections)
    centrality_query = """
    MATCH (f:Function)
    OPTIONAL MATCH (f)-[:CALLS]->(outgoing)
    OPTIONAL MATCH (incoming)-[:CALLS]->(f)
    WITH f, count(outgoing) + count(incoming) AS degree
    WHERE degree > 5
    RETURN f.name AS name, degree
    ORDER BY degree DESC
    LIMIT 10
    """
    result = execute_query(conn, centrality_query)
    metrics["high_centrality_nodes"] = [{"name": row["name"], "degree": row["degree"]} for row in result] if result else []
    
    loggers["graph"].info(f"Graph enrichment complete: {metrics}")
    return metrics


# --- AMBIGUITY DETECTION ---
def detect_ambiguities(conn: Connection, loggers: Dict[str, logging.Logger]) -> List[Ambiguity]:
    """Detect and flag ambiguous entities requiring human review."""
    loggers["crosslink"].info("Detecting ambiguities")
    
    ambiguities = []
    generic_names = ["config", "data", "temp", "buf", "handler", "manager", "init", "setup"]
    
    # Find entities with generic names
    for generic_name in generic_names:
        query = """
        MATCH (n)
        WHERE toLower(n.name) CONTAINS $name
        RETURN n.name AS name, labels(n)[0] AS type, n.file AS file, n.line_start AS line
        """
        result = execute_query(conn, query, {"name": generic_name})
        for row in result:
            ambiguities.append(Ambiguity(
                entity_name=row["name"],
                entity_type=row["type"],
                source_file=row["file"],
                line_number=row["line"],
                reason="Generic name pattern",
                confidence=0.50
            ))
    
    # Find low-confidence entities
    low_conf_query = """
    MATCH (n)
    WHERE n.confidence < 0.6
    RETURN n.name AS name, labels(n)[0] AS type, n.file AS file, n.line_start AS line, n.confidence AS confidence
    """
    result = execute_query(conn, low_conf_query)
    for row in result:
        ambiguities.append(Ambiguity(
            entity_name=row["name"],
            entity_type=row["type"],
            source_file=row["file"],
            line_number=row["line"],
            reason=f"Low confidence score: {row['confidence']}",
            confidence=row["confidence"]
        ))
    
    loggers["crosslink"].info(f"Detected {len(ambiguities)} ambiguities")
    return ambiguities


# --- EXPORT FUNCTIONS ---
def export_to_jsonl(conn: Connection, output_dir: Path) -> None:
    """Export entities and relations to JSONL format."""
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Export entities
    entity_query = """
    MATCH (n)
    RETURN n.name AS name, labels(n)[0] AS type, n.file AS file, 
           n.line_start AS line_start, n.confidence AS confidence
    """
    result = execute_query(conn, entity_query)
    
    with open(output_dir / "entities.jsonl", "w") as entity_file:
        for row in result:
            entity = {
                "name": row["name"],
                "type": row["type"],
                "source_file": row["file"],
                "line_start": row["line_start"],
                "confidence": row["confidence"]
            }
            entity_file.write(yaml.dump(entity) + "\n")
    
    # Export relations
    relation_query = """
    MATCH (a)-[r]->(b)
    RETURN a.name AS subject, type(r) AS predicate, b.name AS object
    """
    result = execute_query(conn, relation_query)
    
    with open(output_dir / "relations.jsonl", "w") as relation_file:
        for row in result:
            relation = {
                "subject": row["subject"],
                "predicate": row["predicate"],
                "object": row["object"]
            }
            relation_file.write(yaml.dump(relation) + "\n")
    
    logging.info(f"Exported entities and relations to {output_dir}")


# --- MAIN EXECUTION ---
def main():
    """Main execution pipeline."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s"
    )
    
    logging.info("Starting Knowledge Graph Extraction Pipeline")
    
    # Initialize
    loggers = setup_logging()
    conn = get_connection()
    
    # Ensure output directory exists
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    # Execute extraction levels
    logging.info("Level 1: Structural Extraction")
    entity_count, relation_count = extract_structural_entities(conn, loggers)
    
    logging.info("Level 2: Semantic Extraction")
    semantic_entities, semantic_relations = extract_semantic_entities(conn, loggers)
    
    logging.info("Level 3: Cross-Linking")
    link_count = create_cross_links(conn, loggers)
    
    logging.info("Level 4: Graph Enrichment")
    metrics = enrich_graph(conn, loggers)
    
    logging.info("Detecting Ambiguities")
    ambiguities = detect_ambiguities(conn, loggers)
    
    # Export results
    export_to_jsonl(conn, OUTPUT_DIR)
    
    # Summary
    logging.info("=" * 60)
    logging.info("EXTRACTION SUMMARY")
    logging.info("=" * 60)
    logging.info(f"Total entities: {entity_count + semantic_entities}")
    logging.info(f"Total relations: {relation_count + semantic_relations + link_count}")
    logging.info(f"Ambiguities flagged: {len(ambiguities)}")
    logging.info(f"Graph metrics: {metrics}")
    logging.info("=" * 60)
    logging.info("Extraction Complete")
    
    return {
        "entities": entity_count + semantic_entities,
        "relations": relation_count + semantic_relations + link_count,
        "ambiguities": len(ambiguities),
        "metrics": metrics
    }


if __name__ == "__main__":
    main()