#!/usr/bin/env python3
"""Parser-first, incrementally-updatable knowledge-graph extraction for `raw_data/`.

See `agents/rules/prompt_knowledge_graph_extraction.md` (v5) for the full
specification this script implements. Summary of what changed vs the
archived v2 script (`output/v2_extraction_script_nrf_example/extraction_script.py`):

- Every semantic entity is produced by a real parser for its language
  (tree-sitter for C/C++/Kconfig/Devicetree/RST, Python's own `ast` module
  for Python, PyYAML for Devicetree bindings). No entity is created from a
  keyword-followed-by-prose regex.
- Documentation-to-code links only come from structured markers (Sphinx/RST
  cross-reference roles, Markdown code spans), resolved against a symbol
  table built from parsed code, never from casefolded name matching across
  the whole repository.
- Kconfig options and Devicetree nodes/bindings are first-class entities,
  because for a firmware SDK they are as much "implementation" as C
  functions.
- Confidence is a relationship-only property; entities carry
  `extraction_method` and `kind` instead of a fabricated confidence score.
- `--update` mode diffs `/raw_data/` against `output/ingestion_manifest.json`
  (sha256 per file) and only re-parses added/modified files, deleting the
  graph nodes of modified/removed files before re-ingesting.
"""

from __future__ import annotations

import argparse
import ast
import gc
import hashlib
import json
import logging
import os
import re
import sys
import yaml
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Optional

from tree_sitter_language_pack import get_parser as _get_ts_parser

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
RAW_DATA_DIR = (ROOT_DIR / "raw_data").resolve()
OUTPUT_DIR = (ROOT_DIR / "output").resolve()
LOG_DIR = (ROOT_DIR / "logs" / "agents").resolve()
ACCESS_LOG = (ROOT_DIR / "logs" / "access_log.jsonl").resolve()
MANIFEST_PATH = OUTPUT_DIR / "ingestion_manifest.json"

# ---------------------------------------------------------------------------
# Language dispatch (PARSER REQUIREMENTS table in the extraction prompt)
# ---------------------------------------------------------------------------
C_EXTENSIONS = {".c", ".h"}
CPP_EXTENSIONS = {".cc", ".cpp", ".cxx", ".hh", ".hpp", ".ipp"}
DT_EXTENSIONS = {".dts", ".dtsi", ".overlay"}
RST_EXTENSIONS = {".rst"}
MARKDOWN_EXTENSIONS = {".md", ".markdown"}
YAML_EXTENSIONS = {".yaml", ".yml"}

GLOBAL_SCOPE_TYPES = {"KconfigOption", "DTBinding"}
# Path-scoped types: the file/directory path alone is the unique identifier,
# so the uid is `{type}|{path}` (no separate "name" component). This matters
# because `source_file_uid()` is used all over the extractors to build the
# subject of DECLARES/INCLUDES relations and MUST match entity_uid() exactly
# for the same SourceFile entity.
PATH_SCOPED_TYPES = {"Folder", "SourceFile"}
KIND_PRIORITY = {
    "definition": 3, "struct": 3, "union": 3, "enum": 3, "typedef": 3,
    "node": 3, "declared": 3,
    "declaration": 2, "": 2,
    "referenced": 1, "stub": 0,
}

# role (Sphinx/RST domain:role, or "code-span" for Markdown) -> which symbol
# index in the cross-link pass should be used to resolve the target text.
DOC_ROLE_TARGET_KIND = {
    "c:func": "function", "cpp:func": "function", "c:function": "function",
    "c:struct": "typelike", "c:type": "typelike", "c:macro": "typelike",
    "c:enum": "typelike", "c:union": "typelike", "c:enumerator": "typelike",
    "cpp:type": "typelike", "cpp:struct": "typelike",
    "option": "kconfig", "kconfig:option": "kconfig",
    "file": "file", "ref": "anchor", "code-span": "symbol",
}

REQUIREMENT_PATTERN = re.compile(
    r"[^.!?\n]*\b(?:shall|must|should|required to)\b[^.!?\n]*[.!?]",
    re.IGNORECASE,
)
KCONFIG_FRAGMENT_LINE = re.compile(
    r"^\s*(?:#\s*(?P<disabled>CONFIG_[A-Z0-9_]+)\s+is not set"
    r"|(?P<name>CONFIG_[A-Z0-9_]+)\s*=\s*(?P<value>.+?))\s*$"
)
KCONFIG_IDENTIFIER = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")


def strip_config_prefix(name: str) -> str:
    name = name.strip()
    return name[len("CONFIG_"):] if name.startswith("CONFIG_") else name

# Node types tree-sitter-c/cpp use as "still file scope, keep looking inside"
# wrappers (conditional compilation, extern "C", etc.).
C_TRANSPARENT_TOP_LEVEL = {
    "translation_unit", "preproc_ifdef", "preproc_if", "preproc_ifndef",
    "preproc_elif", "preproc_else", "linkage_specification",
    "declaration_list",
}
C_TOP_LEVEL_KINDS = {
    "function_definition", "declaration", "type_definition",
    "struct_specifier", "enum_specifier", "union_specifier",
    "preproc_def", "preproc_function_def", "preproc_include",
}


def get_ts_parser(language: str):
    """Cache tree-sitter parsers; they are stateless and reusable per file."""
    cached = _TS_PARSER_CACHE.get(language)
    if cached is None:
        cached = _get_ts_parser(language)
        _TS_PARSER_CACHE[language] = cached
    return cached


_TS_PARSER_CACHE: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class Entity:
    name: str
    entity_type: str
    source_file: str
    line_start: int
    line_end: int
    kind: str = ""
    extraction_method: str = ""
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    source_text: str = ""
    signature: str = ""
    return_type: str = ""
    parameters: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, ...]:
        if self.entity_type in GLOBAL_SCOPE_TYPES:
            return (self.entity_type, self.name.casefold())
        return (self.entity_type, self.name.casefold(), self.source_file)


@dataclass
class Relation:
    subject: str
    predicate: str
    object: str
    confidence: float
    source_file: str = ""
    line: int = 0
    rationale: str = ""
    role: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.subject, self.predicate, self.object, self.source_file)


@dataclass
class SourceChunk:
    uid: str
    source_file: str
    line_start: int
    line_end: int
    content: str
    chunk_type: str = "fixed_window"


class Extraction:
    def __init__(self) -> None:
        self.entities: dict[tuple[str, ...], Entity] = {}
        self.relations: dict[tuple[str, str, str, str], Relation] = {}
        self.source_chunks: dict[str, SourceChunk] = {}
        self.ambiguities: list[dict[str, Any]] = []
        self.unresolved: list[dict[str, Any]] = []
        self.file_contents: dict[str, str] = {}

        # Deferred work resolved in the cross-link pass, once every file has
        # been parsed and the global symbol table exists.
        self.pending_calls: list[dict[str, Any]] = []
        self.pending_dt_phandles: list[dict[str, Any]] = []
        self.pending_dt_compatible: list[dict[str, Any]] = []
        self.pending_doc_references: list[dict[str, Any]] = []
        self.requirement_windows: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
        self.anchor_by_label: dict[str, str] = {}

        # Symbol indices, built by build_symbol_indices() after the
        # structural pass.
        self.functions_by_name: dict[str, list[Entity]] = defaultdict(list)
        self.typelike_by_name: dict[str, list[Entity]] = defaultdict(list)
        self.kconfig_by_name: dict[str, Entity] = {}
        self.dtnode_by_label: dict[str, list[Entity]] = defaultdict(list)
        self.dtbinding_by_compatible: dict[str, Entity] = {}
        self.file_entity_by_relpath: dict[str, Entity] = {}
        self.document_by_relpath: dict[str, Entity] = {}
        self.file_relpaths: list[str] = []
        self.current_folder_paths: set[str] = set()

        self.loggers = setup_logging()

    def add_entity(self, entity: Entity) -> Entity:
        current = self.entities.get(entity.key)
        if current is None or KIND_PRIORITY.get(entity.kind, 2) > KIND_PRIORITY.get(current.kind, 2):
            self.entities[entity.key] = entity
            return entity
        return current

    def add_relation(self, relation: Relation) -> None:
        self.relations[relation.key] = relation

    def add_ambiguity(self, **entry: Any) -> None:
        self.ambiguities.append(entry)

    def add_unresolved(self, **entry: Any) -> None:
        self.unresolved.append(entry)


# ---------------------------------------------------------------------------
# Generic helpers (logging, safe IO, chunking) - behaviour kept from v1/v2
# ---------------------------------------------------------------------------
def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def setup_logging() -> dict[str, logging.Logger]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    result: dict[str, logging.Logger] = {}
    for level, name in enumerate(("inventory", "structural", "crosslink", "graph"), 1):
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
    ACCESS_LOG.parent.mkdir(parents=True, exist_ok=True)
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
        extraction.loggers["inventory"].warning("Unreadable file %s: %s", resolved, exc)
        return None
    if b"\x00" in data:
        extraction.loggers["inventory"].info("Skipping binary content: %s", resolved)
        return None
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            text = data.decode(encoding)
            extraction.file_contents[rel_path(resolved)] = text
            return text
        except UnicodeDecodeError:
            continue
    extraction.loggers["inventory"].warning("Encoding failure: %s", resolved)
    return None


def line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def rel_path(path: Path) -> str:
    return str(path.resolve().relative_to(RAW_DATA_DIR)).replace("\\", "/")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_file_uid(source_file: str) -> str:
    return f"SourceFile|{source_file}"


def source_chunk_uid(source_file: str, line_start: int, line_end: int, content: str) -> str:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    return f"SourceChunk|{source_file}|{line_start}-{line_end}|{digest}"


def add_source_chunks(extraction: Extraction, source_file: str, text: str, chunk_lines: int = 200) -> None:
    lines = text.splitlines(keepends=True)
    if not lines:
        lines = [""]
    for start in range(0, len(lines), chunk_lines):
        end = min(start + chunk_lines, len(lines))
        content = "".join(lines[start:end])
        chunk = SourceChunk(
            source_chunk_uid(source_file, start + 1, end, content),
            source_file, start + 1, end, content,
        )
        extraction.source_chunks[chunk.uid] = chunk


def chunk_uid_for_line(extraction: Extraction, source_file: str, line: int) -> str:
    return next(
        (chunk.uid for chunk in extraction.source_chunks.values()
         if chunk.source_file == source_file and chunk.line_start <= line <= chunk.line_end),
        "",
    )


def entity_uid(entity: Entity) -> str:
    if entity.entity_type in GLOBAL_SCOPE_TYPES:
        return f"{entity.entity_type}|{entity.name}"
    if entity.entity_type in PATH_SCOPED_TYPES:
        return f"{entity.entity_type}|{entity.source_file}"
    return f"{entity.entity_type}|{entity.source_file}|{entity.name}"


# ---------------------------------------------------------------------------
# tree-sitter generic helpers
# ---------------------------------------------------------------------------
def iter_nodes(node) -> Iterable[Any]:
    yield node
    for child in node.children:
        yield from iter_nodes(child)


def node_text(data: bytes, node) -> str:
    return data[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def find_function_declarator(declarator):
    node = declarator
    while node is not None:
        if node.type == "function_declarator":
            return node
        node = node.child_by_field_name("declarator")
    return None


def find_identifier(declarator):
    node = declarator
    while node is not None:
        if node.type == "identifier":
            return node
        nxt = node.child_by_field_name("declarator")
        if nxt is None:
            return None
        node = nxt
    return None


# ---------------------------------------------------------------------------
# File classification / language dispatch
# ---------------------------------------------------------------------------
def classify_language(rel: str, path: Path) -> str:
    suffix = path.suffix.lower()
    name = path.name
    if suffix == ".py":
        return "python"
    if suffix in C_EXTENSIONS:
        return "c"
    if suffix in CPP_EXTENSIONS:
        return "cpp"
    if name == "Kconfig" or name.startswith("Kconfig."):
        return "kconfig-decl"
    if suffix in (".conf", ".defconfig") or name.endswith("_defconfig"):
        return "kconfig-fragment"
    if suffix in DT_EXTENSIONS:
        return "devicetree"
    if suffix in YAML_EXTENSIONS and "/dts/bindings/" in ("/" + rel):
        return "dt-binding-yaml"
    if suffix in RST_EXTENSIONS:
        return "rst"
    if suffix in MARKDOWN_EXTENSIONS:
        return "markdown"
    return "other"


def detect_type_bucket(language: str, path: Path, is_binary: bool) -> str:
    if is_binary:
        return "binary"
    if language in {"python", "c", "cpp"}:
        return "code"
    if language in {"kconfig-decl", "kconfig-fragment", "devicetree"}:
        return "config"
    if language in {"dt-binding-yaml"}:
        return "config"
    if language in {"rst", "markdown"}:
        return "doc"
    if path.suffix.lower() in (".yaml", ".yml", ".json", ".toml", ".ini", ".cfg"):
        return "config"
    return "other"


# ---------------------------------------------------------------------------
# Level 1: mandatory file-system inventory (Folder + SourceFile)
# ---------------------------------------------------------------------------
def build_inventory(extraction: Extraction) -> list[Path]:
    logger = extraction.loggers["inventory"]
    all_paths = sorted(RAW_DATA_DIR.rglob("*"))
    files = [p for p in all_paths if p.is_file()]
    dirs = {RAW_DATA_DIR} | {p for p in all_paths if p.is_dir()}
    # Every ancestor directory up to RAW_DATA_DIR must exist even if rglob
    # somehow skipped an intermediate (defensive, keeps completeness airtight).
    for file_path in files:
        dirs.update(file_path.parents)
    dirs = {d for d in dirs if d == RAW_DATA_DIR or RAW_DATA_DIR in d.parents}

    def dir_rel(d: Path) -> str:
        return "." if d == RAW_DATA_DIR else rel_path(d)

    for directory in sorted(dirs, key=dir_rel):
        rel = dir_rel(directory)
        parent = directory.parent
        parent_rel = dir_rel(parent) if (parent == RAW_DATA_DIR or RAW_DATA_DIR in parent.parents) else None
        entity = extraction.add_entity(Entity(
            name=directory.name or "raw_data", entity_type="Folder", source_file=rel,
            line_start=0, line_end=0, kind="directory", extraction_method="filesystem-walk",
            metadata={"parent_uid": f"Folder|{parent_rel}" if parent_rel is not None else ""},
        ))
        extraction.current_folder_paths.add(rel)
        if parent_rel is not None and rel != ".":
            extraction.add_relation(Relation(
                f"Folder|{parent_rel}", "CONTAINS", entity_uid(entity), 1.0,
                rel, 0, "Filesystem hierarchy",
            ))

    for file_path in files:
        rel = rel_path(file_path)
        try:
            size_bytes = file_path.stat().st_size
        except OSError:
            size_bytes = 0
        try:
            raw_bytes = checked_path(file_path).read_bytes()
        except OSError as exc:
            raw_bytes = b""
            logger.warning("Unreadable file during inventory %s: %s", rel, exc)
        log_access(file_path, True, operation="inventory")
        is_binary = b"\x00" in raw_bytes
        sha256_hash = sha256_bytes(raw_bytes)
        language = classify_language(rel, file_path)
        detected_type = detect_type_bucket(language, file_path, is_binary)
        line_count = 0 if is_binary else raw_bytes.count(b"\n") + (1 if raw_bytes else 0)
        entity = extraction.add_entity(Entity(
            name=file_path.name, entity_type="SourceFile", source_file=rel,
            line_start=1, line_end=max(1, line_count), kind=language,
            extraction_method="filesystem-walk",
            metadata={
                "extension": file_path.suffix.lower(), "size_bytes": size_bytes,
                "sha256_hash": sha256_hash, "detected_type": detected_type,
                "language": language,
            },
        ))
        extraction.file_entity_by_relpath[rel] = entity
        extraction.file_relpaths.append(rel)
        parent_rel = dir_rel(file_path.parent)
        extraction.add_relation(Relation(
            f"Folder|{parent_rel}", "CONTAINS", entity_uid(entity), 1.0,
            rel, 0, "Filesystem hierarchy",
        ))
    logger.info("Inventory complete: %d directories, %d files", len(dirs), len(files))
    return files


def chunk_all_files(extraction: Extraction, files: list[Path]) -> None:
    logger = extraction.loggers["inventory"]
    for path in files:
        rel = rel_path(path)
        text = extraction.file_contents.get(rel)
        if text is None:
            text = read_text(path, extraction)
        if text is None:
            continue
        add_source_chunks(extraction, rel, text)
    logger.info("Chunking complete: %d chunks", len(extraction.source_chunks))


# ---------------------------------------------------------------------------
# Python extractor (native `ast` module)
# ---------------------------------------------------------------------------
def source_segment(text: str, node: ast.AST) -> str:
    return ast.get_source_segment(text, node) or ""


def python_signature(node) -> str:
    arguments = ast.unparse(node.args)
    return f"def {node.name}({arguments})"


def extract_python(path: Path, text: str, extraction: Extraction) -> None:
    source = rel_path(path)
    try:
        tree = ast.parse(text, filename=source)
    except SyntaxError as exc:
        extraction.loggers["structural"].warning("Syntax error in %s: %s", source, exc)
        extraction.add_ambiguity(entity_name=source, entity_type="SourceFile", source_file=source,
                                 line_number=exc.lineno or 1,
                                 reason="Syntax error; partial extraction unavailable")
        return
    file_uid = source_file_uid(source)
    scopes: list[tuple[int, int, str]] = [(1, len(text.splitlines()) + 1, file_uid)]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            line_end = getattr(node, "end_lineno", node.lineno)
            entity = extraction.add_entity(Entity(
                node.name, "Function", source, node.lineno, line_end,
                kind="definition", extraction_method="python-ast",
                description=ast.get_docstring(node) or "",
                metadata={"language": "python"},
                source_text=source_segment(text, node), signature=python_signature(node),
                return_type=ast.unparse(node.returns) if node.returns else "",
                parameters=[arg.arg for arg in (*node.args.posonlyargs, *node.args.args,
                                                 *node.args.kwonlyargs)],
            ))
            parent = next((uid for start, end, uid in reversed(scopes) if start <= node.lineno <= end), file_uid)
            extraction.add_relation(Relation(parent, "DECLARES", entity_uid(entity), 1.0, source,
                                              node.lineno, "Function declaration is explicit"))
            scopes.append((node.lineno, line_end, entity_uid(entity)))
            for call in ast.walk(node):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                    extraction.pending_calls.append({
                        "caller_uid": entity_uid(entity), "callee_name": call.func.id,
                        "source_file": source, "line": getattr(call, "lineno", node.lineno),
                    })
        elif isinstance(node, ast.ClassDef):
            line_end = getattr(node, "end_lineno", node.lineno)
            entity = extraction.add_entity(Entity(
                node.name, "Class", source, node.lineno, line_end,
                kind="definition", extraction_method="python-ast",
                description=ast.get_docstring(node) or "",
                metadata={"language": "python"}, source_text=source_segment(text, node),
            ))
            extraction.add_relation(Relation(file_uid, "DECLARES", entity_uid(entity), 1.0, source,
                                              node.lineno, "Class declaration is explicit"))
            scopes.append((node.lineno, line_end, entity_uid(entity)))
            for base in node.bases:
                if isinstance(base, ast.Name):
                    extraction.pending_calls.append({
                        "caller_uid": entity_uid(entity), "callee_name": base.id,
                        "source_file": source, "line": node.lineno, "predicate": "EXTENDS",
                    })
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and len(scopes) == 1:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    entity = extraction.add_entity(Entity(
                        target.id, "Variable", source, node.lineno, node.lineno,
                        kind="definition", extraction_method="python-ast",
                        description="Module-level assignment", metadata={"language": "python"},
                    ))
                    extraction.add_relation(Relation(file_uid, "DECLARES", entity_uid(entity), 1.0, source,
                                                      node.lineno, "Module-level assignment"))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                resolved = resolve_include(extraction, source, alias.name.replace(".", "/") + ".py")
                target = source_file_uid(resolved) if resolved else None
                extraction.add_relation(Relation(
                    file_uid, "INCLUDES", target or f"external:{alias.name}",
                    1.0 if resolved else 0.5, source, node.lineno,
                    "Explicit Python import" + ("" if resolved else " (unresolved in raw_data)"),
                ))


# ---------------------------------------------------------------------------
# Include/import resolution against the real raw_data file tree
# ---------------------------------------------------------------------------
def resolve_include(extraction: Extraction, including_relpath: str, included: str) -> Optional[str]:
    included = included.strip().strip("/")
    if not included:
        return None
    including_dir = PurePosixPath(including_relpath).parent
    same_dir_candidate = str(including_dir / included) if str(including_dir) != "." else included
    same_dir_candidate = os.path.normpath(same_dir_candidate).replace("\\", "/")
    if same_dir_candidate in extraction.file_entity_by_relpath:
        return same_dir_candidate
    if included in extraction.file_entity_by_relpath:
        return included
    suffix = "/" + included
    matches = [p for p in extraction.file_relpaths if p == included or p.endswith(suffix)]
    if len(matches) == 1:
        return matches[0]
    return None


# ---------------------------------------------------------------------------
# C / C++ extractor (tree-sitter)
# ---------------------------------------------------------------------------
def iter_top_level(node) -> Iterable[Any]:
    for child in node.children:
        if child.type in C_TOP_LEVEL_KINDS:
            yield child
        elif child.type in C_TRANSPARENT_TOP_LEVEL or child.type == "ERROR":
            yield from iter_top_level(child)


def c_struct_like_entity(data: bytes, node, source: str, extraction: Extraction) -> Optional[Entity]:
    """node is a struct_specifier/enum_specifier/union_specifier WITH a body."""
    body = next((c for c in node.children if c.type in ("field_declaration_list", "enumerator_list")), None)
    if body is None:
        return None
    name_node = next((c for c in node.children if c.type == "type_identifier"), None)
    if name_node is None:
        return None
    kind = {"struct_specifier": "struct", "union_specifier": "union",
            "enum_specifier": "enum"}[node.type]
    label = "Class" if kind in ("struct", "union") else "Type"
    entity = extraction.add_entity(Entity(
        node_text(data, name_node), label, source,
        node.start_point[0] + 1, node.end_point[0] + 1,
        kind=kind, extraction_method="tree-sitter-c", source_text=node_text(data, node),
    ))
    extraction.add_relation(Relation(source_file_uid(source), "DECLARES", entity_uid(entity), 1.0,
                                      source, node.start_point[0] + 1, f"{kind} definition"))
    return entity


def c_collect_calls(data: bytes, body_node, caller_uid: str, source: str, extraction: Extraction) -> None:
    for node in iter_nodes(body_node):
        if node.type != "call_expression":
            continue
        func = node.child_by_field_name("function")
        if func is None or func.type != "identifier":
            continue
        extraction.pending_calls.append({
            "caller_uid": caller_uid, "callee_name": node_text(data, func),
            "source_file": source, "line": node.start_point[0] + 1,
        })


def extract_c_family(path: Path, text: str, extraction: Extraction, language: str) -> None:
    source = rel_path(path)
    data = text.encode("utf-8", errors="replace")
    tree = get_ts_parser(language).parse(data)
    file_uid = source_file_uid(source)

    for node in iter_top_level(tree.root_node):
        line_start = node.start_point[0] + 1
        line_end = node.end_point[0] + 1

        if node.type == "function_definition":
            declarator = node.child_by_field_name("declarator")
            func_decl = find_function_declarator(declarator) if declarator is not None else None
            ident = find_identifier(func_decl) if func_decl is not None else None
            if ident is None:
                continue
            params_node = func_decl.child_by_field_name("parameters") if func_decl else None
            parameters = []
            if params_node is not None:
                for param in params_node.children:
                    if param.type == "parameter_declaration":
                        parameters.append(node_text(data, param))
            return_type_node = node.child_by_field_name("type")
            entity = extraction.add_entity(Entity(
                node_text(data, ident), "Function", source, line_start, line_end,
                kind="definition", extraction_method=f"tree-sitter-{language}",
                signature=node_text(data, declarator) if declarator is not None else "",
                return_type=node_text(data, return_type_node) if return_type_node is not None else "",
                parameters=parameters, source_text=node_text(data, node),
            ))
            extraction.add_relation(Relation(file_uid, "DECLARES", entity_uid(entity), 1.0, source,
                                              line_start, "Function definition"))
            body = node.child_by_field_name("body")
            if body is not None:
                c_collect_calls(data, body, entity_uid(entity), source, extraction)

        elif node.type == "declaration":
            declarator = node.child_by_field_name("declarator")
            func_decl = find_function_declarator(declarator) if declarator is not None else None
            type_node = node.child_by_field_name("type")
            if type_node is not None and type_node.type in ("struct_specifier", "union_specifier", "enum_specifier"):
                c_struct_like_entity(data, type_node, source, extraction)
            if func_decl is not None:
                ident = find_identifier(func_decl)
                if ident is None:
                    continue
                entity = extraction.add_entity(Entity(
                    node_text(data, ident), "Function", source, line_start, line_end,
                    kind="declaration", extraction_method=f"tree-sitter-{language}",
                    signature=node_text(data, node),
                    return_type=node_text(data, type_node) if type_node is not None else "",
                ))
                extraction.add_relation(Relation(file_uid, "DECLARES", entity_uid(entity), 1.0, source,
                                                  line_start, "Function prototype"))
                continue
            for declarator_node in node.children_by_field_name("declarator"):
                ident = find_identifier(declarator_node)
                if ident is None:
                    continue
                entity = extraction.add_entity(Entity(
                    node_text(data, ident), "Variable", source, line_start, line_end,
                    kind="definition", extraction_method=f"tree-sitter-{language}",
                    source_text=node_text(data, node),
                ))
                extraction.add_relation(Relation(file_uid, "DECLARES", entity_uid(entity), 1.0, source,
                                                  line_start, "File-scope variable"))

        elif node.type == "type_definition":
            specifier = next((c for c in node.children
                               if c.type in ("struct_specifier", "union_specifier", "enum_specifier")), None)
            if specifier is not None:
                c_struct_like_entity(data, specifier, source, extraction)
            type_identifiers = [c for c in node.children if c.type == "type_identifier"]
            alias_node = type_identifiers[-1] if type_identifiers else None
            if alias_node is None:
                continue
            underlying = node_text(data, specifier) if specifier is not None else node_text(data, node)
            entity = extraction.add_entity(Entity(
                node_text(data, alias_node), "Type", source, line_start, line_end,
                kind="typedef", extraction_method=f"tree-sitter-{language}",
                metadata={"underlying": underlying[:200]}, source_text=node_text(data, node),
            ))
            extraction.add_relation(Relation(file_uid, "DECLARES", entity_uid(entity), 1.0, source,
                                              line_start, "typedef declaration"))

        elif node.type in ("struct_specifier", "enum_specifier", "union_specifier"):
            c_struct_like_entity(data, node, source, extraction)

        elif node.type == "preproc_def":
            name_node = next((c for c in node.children if c.type == "identifier"), None)
            if name_node is None:
                continue
            value_node = next((c for c in node.children if c.type == "preproc_arg"), None)
            entity = extraction.add_entity(Entity(
                node_text(data, name_node), "Macro", source, line_start, line_end,
                kind="object", extraction_method=f"tree-sitter-{language}",
                description=node_text(data, value_node).strip() if value_node is not None else "",
                source_text=node_text(data, node),
            ))
            extraction.add_relation(Relation(file_uid, "DECLARES", entity_uid(entity), 1.0, source,
                                              line_start, "Object-like macro"))

        elif node.type == "preproc_function_def":
            name_node = next((c for c in node.children if c.type == "identifier"), None)
            if name_node is None:
                continue
            params_node = next((c for c in node.children if c.type == "preproc_params"), None)
            parameters = [node_text(data, c) for c in params_node.children if c.type == "identifier"] \
                if params_node is not None else []
            value_node = next((c for c in node.children if c.type == "preproc_arg"), None)
            entity = extraction.add_entity(Entity(
                node_text(data, name_node), "Macro", source, line_start, line_end,
                kind="function", extraction_method=f"tree-sitter-{language}",
                parameters=parameters,
                description=node_text(data, value_node).strip() if value_node is not None else "",
                source_text=node_text(data, node),
            ))
            extraction.add_relation(Relation(file_uid, "DECLARES", entity_uid(entity), 1.0, source,
                                              line_start, "Function-like macro"))

        elif node.type == "preproc_include":
            path_node = next((c for c in node.children if c.type in ("string_literal", "system_lib_string")), None)
            if path_node is None:
                continue
            raw_included = node_text(data, path_node).strip('"<>')
            resolved = resolve_include(extraction, source, raw_included)
            target = source_file_uid(resolved) if resolved else None
            if resolved:
                extraction.add_relation(Relation(file_uid, "INCLUDES", target, 1.0, source, line_start,
                                                  f"#include of {raw_included}"))
            else:
                extraction.add_ambiguity(entity_name=raw_included, entity_type="Include", source_file=source,
                                         line_number=line_start,
                                         reason="Included file not present in raw_data (external dependency)")


# ---------------------------------------------------------------------------
# Kconfig declarations (files literally named Kconfig / Kconfig.*)
# ---------------------------------------------------------------------------
def ensure_kconfig_stub(extraction: Extraction, name: str, source: str, line: int) -> Entity:
    stub = Entity(name, "KconfigOption", source, line, line, kind="stub",
                  extraction_method="tree-sitter-kconfig-reference")
    return extraction.add_entity(stub)


def kconfig_dependency_names(expr_text: str) -> list[str]:
    names = KCONFIG_IDENTIFIER.findall(expr_text)
    return [n for n in names if n not in ("IF", "AND", "OR", "NOT")]


def extract_kconfig_declarations(path: Path, text: str, extraction: Extraction) -> None:
    source = rel_path(path)
    data = text.encode("utf-8", errors="replace")
    tree = get_ts_parser("kconfig").parse(data)
    file_uid = source_file_uid(source)

    for node in iter_nodes(tree.root_node):
        if node.type not in ("config", "menuconfig"):
            continue
        name_node = next((c for c in node.children if c.type == "name"), None)
        if name_node is None:
            continue
        option_name = node_text(data, name_node).strip()
        if not option_name:
            continue
        line_start = node.start_point[0] + 1
        line_end = node.end_point[0] + 1

        type_def = next((c for c in node.children if c.type == "type_definition"), None)
        option_type = ""
        prompt = ""
        if type_def is not None:
            type_kw = next((c for c in type_def.children
                             if c.type in ("bool", "tristate", "int", "hex", "string")), None)
            if type_kw is not None:
                option_type = type_kw.type
            prompt_node = next((c for c in type_def.children if c.type == "string"), None)
            if prompt_node is not None:
                prompt = node_text(data, prompt_node).strip('"')

        help_node = next((c for c in node.children if c.type == "help_text"), None)
        help_text = ""
        if help_node is not None:
            text_node = next((c for c in help_node.children if c.type == "text"), None)
            help_text = node_text(data, text_node).strip() if text_node is not None else ""

        entity = extraction.add_entity(Entity(
            option_name, "KconfigOption", source, line_start, line_end,
            kind="declared", extraction_method="tree-sitter-kconfig",
            description=help_text, metadata={"type": option_type, "prompt": prompt},
            source_text=node_text(data, node),
        ))
        extraction.add_relation(Relation(file_uid, "DECLARES", entity_uid(entity), 1.0, source,
                                          line_start, "Kconfig config declaration"))

        for dep in (c for c in node.children if c.type == "dependencies"):
            symbol_node = next((c for c in dep.children if c.type == "symbol"), None)
            if symbol_node is None:
                continue
            expr_text = node_text(data, symbol_node)
            for dep_name in kconfig_dependency_names(expr_text):
                target = ensure_kconfig_stub(extraction, dep_name, source, line_start)
                extraction.add_relation(Relation(entity_uid(entity), "DEPENDS_ON", entity_uid(target), 1.0,
                                                  source, line_start, f"depends on {expr_text.strip()}"))

        for sel in (c for c in node.children if c.type == "reverse_dependencies"):
            name_c = next((c for c in sel.children if c.type == "name"), None)
            if name_c is None:
                continue
            expr_text = node_text(data, name_c)
            for dep_name in kconfig_dependency_names(expr_text):
                target = ensure_kconfig_stub(extraction, dep_name, source, line_start)
                extraction.add_relation(Relation(entity_uid(entity), "SELECTS", entity_uid(target), 1.0,
                                                  source, line_start, f"select {expr_text.strip()}"))


# ---------------------------------------------------------------------------
# Kconfig value fragments (.conf / *_defconfig) - trivial line grammar
# ---------------------------------------------------------------------------
def extract_kconfig_fragment(path: Path, text: str, extraction: Extraction) -> None:
    source = rel_path(path)
    file_uid = source_file_uid(source)
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") and "is not set" not in stripped:
            continue
        match = KCONFIG_FRAGMENT_LINE.match(line)
        if not match:
            continue
        if match.group("disabled"):
            name, value = match.group("disabled"), "n"
        else:
            name, value = match.group("name"), match.group("value")
        name = strip_config_prefix(name)
        target = ensure_kconfig_stub(extraction, name, source, lineno)
        extraction.add_relation(Relation(file_uid, "SETS", entity_uid(target), 1.0, source, lineno,
                                          "Kconfig fragment assignment", metadata={"value": value}))


# ---------------------------------------------------------------------------
# Devicetree (.dts/.dtsi/.overlay)
# ---------------------------------------------------------------------------
def dt_property_strings(data: bytes, prop_node) -> list[str]:
    return [node_text(data, n).strip('"') for n in iter_nodes(prop_node) if n.type == "string_literal"]


def dt_property_references(data: bytes, prop_node) -> list[tuple[str, int]]:
    results = []
    for n in iter_nodes(prop_node):
        if n.type == "reference":
            ident = next((c for c in n.children if c.type == "identifier"), None)
            if ident is not None:
                results.append((node_text(data, ident), n.start_point[0] + 1))
    return results


def dt_node_name_parts(data: bytes, dt_node) -> tuple[Optional[str], str, Optional[str], Optional[str]]:
    """Return (label, local_name, unit_address, override_target) for a devicetree 'node'.

    `override_target` is set when the node is a reference-style override
    (`&label { ... };`, common in board overlays) instead of a fresh node
    definition; the node itself still gets its own DTNode entity (the
    override content lives in this specific file), but the caller records a
    REFERENCES edge back to the node the override extends.
    """
    reference = next((c for c in dt_node.children if c.type == "reference"), None)
    if reference is not None and dt_node.children and dt_node.children[0] is reference:
        ident = next((c for c in reference.children if c.type == "identifier"), None)
        target = node_text(data, ident) if ident is not None else "?"
        return target, target, None, target
    idents = [c for c in dt_node.children if c.type == "identifier"]
    has_colon = any(c.type == ":" for c in dt_node.children)
    unit_address_node = next((c for c in dt_node.children if c.type == "unit_address"), None)
    unit_address = node_text(data, unit_address_node) if unit_address_node is not None else None
    if has_colon and len(idents) >= 2:
        return node_text(data, idents[0]), node_text(data, idents[1]), unit_address, None
    if idents:
        return None, node_text(data, idents[0]), unit_address, None
    return None, "(anonymous)", unit_address, None


def walk_dt_container(data: bytes, container, parent_entity: Optional[Entity], parent_path: str,
                       source: str, extraction: Extraction) -> None:
    for child in container.children:
        if child.type == "node":
            label, local_name, unit_address, override_target = dt_node_name_parts(data, child)
            display_name = label or (f"{local_name}@{unit_address}" if unit_address else local_name)
            node_path = f"{parent_path}/{local_name}" if parent_path != "/" else f"/{local_name}"
            line_start = child.start_point[0] + 1
            line_end = child.end_point[0] + 1
            entity = extraction.add_entity(Entity(
                display_name, "DTNode", source, line_start, line_end,
                kind="node-override" if override_target else "node",
                extraction_method="tree-sitter-devicetree",
                metadata={"label": label or "", "local_name": local_name,
                          "unit_address": unit_address or "", "node_path": node_path},
                source_text=node_text(data, child)[:2000],
            ))
            extraction.add_relation(Relation(source_file_uid(source), "DECLARES", entity_uid(entity), 1.0,
                                              source, line_start, "Devicetree node"))
            if parent_entity is not None:
                extraction.add_relation(Relation(entity_uid(entity), "CHILD_OF", entity_uid(parent_entity),
                                                  1.0, source, line_start, "Devicetree node nesting"))
            if label and not override_target:
                extraction.dtnode_by_label[label.casefold()].append(entity)
            if override_target:
                extraction.pending_dt_phandles.append({
                    "dtnode_uid": entity_uid(entity), "target_label": override_target,
                    "property": "&override", "source_file": source, "line": line_start,
                })

            for prop in (c for c in child.children if c.type == "property"):
                prop_name_node = next((c for c in prop.children if c.type == "identifier"), None)
                prop_name = node_text(data, prop_name_node) if prop_name_node is not None else ""
                if prop_name == "compatible":
                    values = dt_property_strings(data, prop)
                    entity.metadata.setdefault("compatible", [])
                    entity.metadata["compatible"].extend(values)
                    for compatible in values:
                        extraction.pending_dt_compatible.append({
                            "dtnode_uid": entity_uid(entity), "compatible": compatible,
                            "source_file": source, "line": prop.start_point[0] + 1,
                        })
                for target_label, line in dt_property_references(data, prop):
                    extraction.pending_dt_phandles.append({
                        "dtnode_uid": entity_uid(entity), "target_label": target_label,
                        "property": prop_name, "source_file": source, "line": line,
                    })


            walk_dt_container(data, child, entity, node_path, source, extraction)
        else:
            walk_dt_container(data, child, parent_entity, parent_path, source, extraction)


def extract_devicetree(path: Path, text: str, extraction: Extraction) -> None:
    source = rel_path(path)
    data = text.encode("utf-8", errors="replace")
    tree = get_ts_parser("devicetree").parse(data)
    walk_dt_container(data, tree.root_node, None, "/", source, extraction)

    for node in iter_nodes(tree.root_node):
        if node.type != "preproc_include":
            continue
        path_node = next((c for c in node.children if c.type in ("string_literal", "system_lib_string")), None)
        if path_node is None:
            continue
        raw_included = node_text(data, path_node).strip('"<>')
        resolved = resolve_include(extraction, source, raw_included)
        if resolved:
            extraction.add_relation(Relation(source_file_uid(source), "INCLUDES",
                                              source_file_uid(resolved), 1.0, source,
                                              node.start_point[0] + 1, f"#include of {raw_included}"))
        else:
            extraction.add_ambiguity(entity_name=raw_included, entity_type="Include", source_file=source,
                                     line_number=node.start_point[0] + 1,
                                     reason="Included devicetree file not present in raw_data")


# ---------------------------------------------------------------------------
# Devicetree bindings (dts/bindings/**/*.yaml) - real YAML semantics via PyYAML
# ---------------------------------------------------------------------------
def extract_dt_binding_yaml(path: Path, text: str, extraction: Extraction) -> None:
    source = rel_path(path)
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        extraction.add_ambiguity(entity_name=path.name, entity_type="DTBinding", source_file=source,
                                 line_number=1, reason=f"YAML parse error: {exc}")
        return
    if not isinstance(document, dict):
        return
    compatible = document.get("compatible")
    if not isinstance(compatible, str) or not compatible:
        return
    description = document.get("description")
    entity = extraction.add_entity(Entity(
        compatible, "DTBinding", source, 1, max(1, text.count("\n") + 1),
        kind="binding", extraction_method="pyyaml",
        description=description if isinstance(description, str) else "",
    ))
    extraction.add_relation(Relation(source_file_uid(source), "DECLARES", entity_uid(entity), 1.0,
                                      source, 1, "Devicetree binding file"))
    extraction.dtbinding_by_compatible[compatible] = entity


# ---------------------------------------------------------------------------
# reStructuredText (tree-sitter-rst)
# ---------------------------------------------------------------------------
def extract_rst(path: Path, text: str, extraction: Extraction) -> None:
    source = rel_path(path)
    data = text.encode("utf-8", errors="replace")
    tree = get_ts_parser("rst").parse(data)
    root = tree.root_node

    first_section = next((c for c in root.children if c.type == "section"), None)
    title = path.name
    if first_section is not None:
        title_node = next((c for c in first_section.children if c.type == "title"), None)
        if title_node is not None:
            title = node_text(data, title_node).strip()

    document = extraction.add_entity(Entity(
        path.name, "Document", source, 1, max(1, text.count("\n") + 1),
        kind="rst", extraction_method="tree-sitter-rst", description=title,
    ))
    extraction.document_by_relpath[source] = document
    document_uid = entity_uid(document)

    def walk(node, depth: int) -> None:
        if node.type == "target":
            name_node = next((c for c in node.children if c.type == "name"), None)
            if name_node is not None:
                label = node_text(data, name_node).strip().strip("_:").strip()
                if label:
                    extraction.anchor_by_label[label.casefold()] = source
        elif node.type == "section":
            title_node = next((c for c in node.children if c.type == "title"), None)
            if title_node is not None:
                heading = node_text(data, title_node).strip()
                line = title_node.start_point[0] + 1
                concept = extraction.add_entity(Entity(
                    heading, "Concept", source, line, line,
                    kind=f"heading-{depth}", extraction_method="tree-sitter-rst",
                ))
                extraction.add_relation(Relation(document_uid, "HAS_SECTION", entity_uid(concept), 1.0,
                                                  source, line, "RST section title"))
        elif node.type == "paragraph":
            raw = node_text(data, node)
            line_start = node.start_point[0] + 1
            line_end = node.end_point[0] + 1
            for match in REQUIREMENT_PATTERN.finditer(raw):
                sentence = match.group(0).strip()
                if len(sentence) < 12:
                    continue
                req = extraction.add_entity(Entity(
                    sentence[:180], "Requirement", source, line_start, line_end,
                    kind="modal-sentence", extraction_method="tree-sitter-rst", description=sentence,
                ))
                extraction.add_relation(Relation(document_uid, "CONTAINS", entity_uid(req), 1.0,
                                                  source, line_start,
                                                  "Requirement sentence with explicit modal verb"))
                extraction.requirement_windows[source].append((line_start, line_end, entity_uid(req)))

        if node.type == "interpreted_text":
            role_node = next((c for c in node.children if c.type == "role"), None)
            target_node = next((c for c in node.children if c.type == "interpreted_text"), None)
            if role_node is not None and target_node is not None:
                role = node_text(data, role_node).strip(":").strip()
                target = node_text(data, target_node).strip("`")
                if target:
                    extraction.pending_doc_references.append({
                        "document_uid": document_uid, "role": role, "target": target,
                        "source_file": source, "line": node.start_point[0] + 1,
                    })

        next_depth = depth + 1 if node.type == "section" else depth
        for child in node.children:
            walk(child, next_depth)

    walk(root, 0)


# ---------------------------------------------------------------------------
# Markdown - deterministic line-based extraction (headings, code spans)
# ---------------------------------------------------------------------------
MARKDOWN_HEADING = re.compile(r"(?m)^(#{1,6})[ \t]+(.+?)[ \t]*$")
MARKDOWN_CODE_SPAN = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")


def extract_markdown(path: Path, text: str, extraction: Extraction) -> None:
    source = rel_path(path)
    document = extraction.add_entity(Entity(
        path.name, "Document", source, 1, max(1, text.count("\n") + 1),
        kind="markdown", extraction_method="markdown-heading",
    ))
    extraction.document_by_relpath[source] = document
    document_uid = entity_uid(document)

    for match in MARKDOWN_HEADING.finditer(text):
        title = match.group(2).strip()
        line = line_number(text, match.start())
        concept = extraction.add_entity(Entity(
            title, "Concept", source, line, line,
            kind=f"heading-{len(match.group(1))}", extraction_method="markdown-heading",
        ))
        extraction.add_relation(Relation(document_uid, "HAS_SECTION", entity_uid(concept), 1.0,
                                          source, line, "Markdown heading"))

    for match in MARKDOWN_CODE_SPAN.finditer(text):
        target = match.group(1)
        line = line_number(text, match.start())
        extraction.pending_doc_references.append({
            "document_uid": document_uid, "role": "code-span", "target": target,
            "source_file": source, "line": line,
        })

    for match in REQUIREMENT_PATTERN.finditer(text):
        sentence = match.group(0).strip()
        if len(sentence) < 12:
            continue
        line = line_number(text, match.start())
        req = extraction.add_entity(Entity(
            sentence[:180], "Requirement", source, line, line,
            kind="modal-sentence", extraction_method="markdown-heading", description=sentence,
        ))
        extraction.add_relation(Relation(document_uid, "CONTAINS", entity_uid(req), 1.0, source, line,
                                          "Requirement sentence with explicit modal verb"))
        extraction.requirement_windows[source].append((line, line, entity_uid(req)))


# ---------------------------------------------------------------------------
# Structural pass dispatcher
# ---------------------------------------------------------------------------
def structural_pass(extraction: Extraction, files: list[Path]) -> None:
    logger = extraction.loggers["structural"]
    dispatch = {
        "python": extract_python,
        "c": lambda p, t, e: extract_c_family(p, t, e, "c"),
        "cpp": lambda p, t, e: extract_c_family(p, t, e, "cpp"),
        "kconfig-decl": extract_kconfig_declarations,
        "kconfig-fragment": extract_kconfig_fragment,
        "devicetree": extract_devicetree,
        "dt-binding-yaml": extract_dt_binding_yaml,
        "rst": extract_rst,
        "markdown": extract_markdown,
    }
    processed = 0
    for index, path in enumerate(files, 1):
        rel = rel_path(path)
        language = classify_language(rel, path)
        handler = dispatch.get(language)
        if handler is None:
            continue
        text = extraction.file_contents.get(rel)
        if text is None:
            continue
        try:
            handler(path, text, extraction)
            processed += 1
        except Exception as exc:  # noqa: BLE001 - keep extraction resilient across 17k files
            logger.warning("Structural extraction failed for %s (%s): %s", rel, language, exc)
            extraction.add_ambiguity(entity_name=rel, entity_type="SourceFile", source_file=rel,
                                     line_number=1, reason=f"Parser error: {exc}")
        if index % 500 == 0:
            gc.collect()
    logger.info("Structural extraction complete: %d files parsed with a language handler, %d entities so far",
                processed, len(extraction.entities))


# ---------------------------------------------------------------------------
# Symbol table construction (post structural pass, pre cross-link pass)
# ---------------------------------------------------------------------------
def build_symbol_indices(extraction: Extraction) -> None:
    for entity in extraction.entities.values():
        if entity.entity_type == "Function":
            extraction.functions_by_name[entity.name.casefold()].append(entity)
        elif entity.entity_type in ("Class", "Type", "Macro"):
            extraction.typelike_by_name[entity.name.casefold()].append(entity)
        elif entity.entity_type == "KconfigOption":
            existing = extraction.kconfig_by_name.get(entity.name)
            if existing is None or KIND_PRIORITY.get(entity.kind, 2) > KIND_PRIORITY.get(existing.kind, 2):
                extraction.kconfig_by_name[entity.name] = entity


def pick_unique(candidates: list[Entity], referencing_file: str = "") -> Optional[Entity]:
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    same_file = [c for c in candidates if c.source_file == referencing_file]
    if len(same_file) == 1:
        return same_file[0]
    pool = same_file or candidates
    best_priority = max(KIND_PRIORITY.get(c.kind, 2) for c in pool)
    best = [c for c in pool if KIND_PRIORITY.get(c.kind, 2) == best_priority]
    if len(best) == 1:
        return best[0]
    return None


# ---------------------------------------------------------------------------
# Cross-link pass: resolve every deferred (pending_*) list against the
# symbol table built above. Ambiguous or unresolved endpoints are discarded
# and logged, never guessed (see DOC-TO-CODE LINKING RULES in the prompt).
# ---------------------------------------------------------------------------
def resolve_calls(extraction: Extraction) -> None:
    by_uid = {entity_uid(e): e for e in extraction.entities.values()}
    for item in extraction.pending_calls:
        candidates = extraction.functions_by_name.get(item["callee_name"].casefold(), [])
        resolved = pick_unique(candidates, item["source_file"])
        predicate = item.get("predicate", "CALLS")
        if resolved is None:
            if candidates:
                extraction.add_unresolved(subject=item["caller_uid"], predicate=predicate,
                                          object=item["callee_name"], source_file=item["source_file"],
                                          line=item["line"], reason="ambiguous callee",
                                          candidate_count=len(candidates))
            continue
        confidence = 1.0 if resolved.source_file == item["source_file"] else 0.8
        extraction.add_relation(Relation(item["caller_uid"], predicate, entity_uid(resolved), confidence,
                                          item["source_file"], item["line"],
                                          "Call/base expression resolved against symbol table"))


def dt_binding_known_compatibles(extraction: Extraction) -> set[str]:
    return set(extraction.dtbinding_by_compatible)


def resolve_dt_phandles(extraction: Extraction) -> None:
    for item in extraction.pending_dt_phandles:
        candidates = extraction.dtnode_by_label.get(item["target_label"].casefold(), [])
        resolved = pick_unique(candidates, item["source_file"])
        if resolved is None:
            if candidates:
                extraction.add_unresolved(subject=item["dtnode_uid"], predicate="REFERENCES",
                                          object=item["target_label"], source_file=item["source_file"],
                                          line=item["line"], reason="ambiguous devicetree label",
                                          candidate_count=len(candidates))
            else:
                extraction.add_ambiguity(entity_name=item["target_label"], entity_type="DTNode",
                                         source_file=item["source_file"], line_number=item["line"],
                                         reason="Phandle label not found in raw_data")
            continue
        extraction.add_relation(Relation(item["dtnode_uid"], "REFERENCES", entity_uid(resolved), 1.0,
                                          item["source_file"], item["line"],
                                          f"Devicetree property '{item['property']}' phandle reference"))


def resolve_dt_compatible(extraction: Extraction) -> None:
    for item in extraction.pending_dt_compatible:
        binding = extraction.dtbinding_by_compatible.get(item["compatible"])
        if binding is None:
            extraction.add_ambiguity(entity_name=item["compatible"], entity_type="DTBinding",
                                     source_file=item["source_file"], line_number=item["line"],
                                     reason="No binding yaml with this 'compatible' string in raw_data")
            continue
        extraction.add_relation(Relation(item["dtnode_uid"], "COMPATIBLE_WITH", entity_uid(binding), 1.0,
                                          item["source_file"], item["line"],
                                          "compatible property matches a binding in dts/bindings"))


def resolve_file_reference(extraction: Extraction, target: str) -> Optional[str]:
    target = target.strip().strip("`")
    if target in extraction.file_entity_by_relpath:
        return target
    matches = [p for p in extraction.file_relpaths if p.endswith("/" + target) or p == target]
    if len(matches) == 1:
        return matches[0]
    return None


def resolve_doc_references(extraction: Extraction) -> None:
    for item in extraction.pending_doc_references:
        role = item["role"]
        target = item["target"].strip()
        kind = DOC_ROLE_TARGET_KIND.get(role)
        resolved: Optional[Entity] = None
        if kind == "function":
            resolved = pick_unique(extraction.functions_by_name.get(target.casefold(), []), item["source_file"])
        elif kind == "typelike":
            resolved = pick_unique(extraction.typelike_by_name.get(target.casefold(), []), item["source_file"])
        elif kind == "symbol":
            combined = (extraction.functions_by_name.get(target.casefold(), [])
                        + extraction.typelike_by_name.get(target.casefold(), []))
            resolved = pick_unique(combined, item["source_file"])
        elif kind == "kconfig":
            name = strip_config_prefix(target.split("=")[0].strip())
            resolved = extraction.kconfig_by_name.get(name)
        elif kind == "file":
            resolved_path = resolve_file_reference(extraction, target)
            if resolved_path:
                resolved = extraction.file_entity_by_relpath.get(resolved_path)
        elif kind == "anchor":
            doc_path = extraction.anchor_by_label.get(target.casefold())
            if doc_path:
                resolved = extraction.document_by_relpath.get(doc_path)
        else:
            continue  # role not mapped to a resolvable target kind (e.g. :term:)

        if resolved is None:
            extraction.add_ambiguity(entity_name=target, entity_type=f"DocReference[{role}]",
                                     source_file=item["source_file"], line_number=item["line"],
                                     reason="Reference target not found (or ambiguous) in inventory")
            continue

        rel = Relation(item["document_uid"], "REFERENCES", entity_uid(resolved), 1.0,
                       item["source_file"], item["line"], f"Resolved '{role}' role/code-span", role=role)
        extraction.add_relation(rel)

        for window_start, window_end, req_uid in extraction.requirement_windows.get(item["source_file"], []):
            if window_start <= item["line"] <= window_end:
                extraction.add_relation(Relation(req_uid, "SATISFIES", entity_uid(resolved), 0.9,
                                                  item["source_file"], item["line"],
                                                  "Requirement sentence contains a resolved reference"))


def cross_link_pass(extraction: Extraction) -> None:
    logger = extraction.loggers["crosslink"]
    build_symbol_indices(extraction)
    resolve_calls(extraction)
    resolve_dt_phandles(extraction)
    resolve_dt_compatible(extraction)
    resolve_doc_references(extraction)
    logger.info("Cross-link pass complete: %d relations, %d ambiguities, %d unresolved",
                len(extraction.relations), len(extraction.ambiguities), len(extraction.unresolved))


# ---------------------------------------------------------------------------
# Graph metrics / ambiguity sweep
# ---------------------------------------------------------------------------
def graph_metrics(extraction: Extraction) -> dict[str, Any]:
    degree: Counter[str] = Counter()
    for relation in extraction.relations.values():
        degree[relation.subject] += 1
        degree[relation.object] += 1
    by_uid = {entity_uid(e): e for e in extraction.entities.values()}
    isolated = sum(
        1 for uid, e in by_uid.items()
        if degree[uid] == 0 and e.entity_type not in ("Folder",)
    )
    file_count = sum(1 for e in extraction.entities.values() if e.entity_type == "SourceFile")
    return {
        "total_entities": len(extraction.entities),
        "total_relations": len(extraction.relations),
        "entity_type_distribution": dict(Counter(e.entity_type for e in extraction.entities.values())),
        "relation_type_distribution": dict(Counter(r.predicate for r in extraction.relations.values())),
        "ambiguities_flagged": len(extraction.ambiguities),
        "unresolved_relations": len(extraction.unresolved),
        "isolated_nodes": isolated,
        "file_completeness": {
            "discovered_file_count": len(extraction.file_relpaths),
            "source_file_node_count": file_count,
        },
    }


def detect_ambiguities(extraction: Extraction) -> None:
    for entity in extraction.entities.values():
        if entity.entity_type == "KconfigOption" and entity.kind == "stub":
            extraction.add_ambiguity(
                entity_name=entity.name, entity_type="KconfigOption",
                source_file=entity.source_file, line_number=entity.line_start,
                reason="Referenced (depends on/select/SETS) but never declared in a Kconfig file present in raw_data",
            )


# ---------------------------------------------------------------------------
# Incremental update manifest
# ---------------------------------------------------------------------------
def compute_manifest(extraction: Extraction) -> dict[str, Any]:
    files = {}
    for rel, entity in extraction.file_entity_by_relpath.items():
        files[rel] = {
            "sha256": entity.metadata.get("sha256_hash", ""),
            "size_bytes": entity.metadata.get("size_bytes", 0),
        }
    return {"generated_at": now(), "files": files}


def load_manifest() -> dict[str, Any]:
    if not MANIFEST_PATH.is_file():
        return {"generated_at": "", "files": {}}
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"generated_at": "", "files": {}}


def save_manifest(manifest: dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=True), encoding="utf-8")


def diff_manifest(old: dict[str, Any], new: dict[str, Any]) -> tuple[set[str], set[str], set[str]]:
    old_files = old.get("files", {})
    new_files = new.get("files", {})
    added = {p for p in new_files if p not in old_files}
    removed = {p for p in old_files if p not in new_files}
    modified = {
        p for p in new_files
        if p in old_files and old_files[p].get("sha256") != new_files[p].get("sha256")
    }
    return added, modified, removed


def relation_touches_files(relation: Relation, files: set[str]) -> bool:
    if not files:
        return False
    if relation.source_file in files:
        return True
    for endpoint in (relation.subject, relation.object):
        parts = endpoint.split("|")
        if len(parts) >= 2 and parts[1] in files:
            return True
    return False


def merge_unchanged(extraction: Extraction, previous: "Extraction", changed_or_removed: set[str]) -> None:
    """Carry over entities/relations/chunks from files untouched by this update.

    Trade-off (documented in the extraction prompt, INCREMENTAL UPDATE
    SPECIFICATION): a cross-link relation whose *provenance file* was not
    reparsed but whose *endpoint* lives in a changed/removed file is dropped
    rather than kept stale, and is only regenerated once the referencing
    file itself is reprocessed. Run `--full` periodically to reconcile.
    """
    for entity in previous.entities.values():
        if entity.entity_type in ("Folder", "SourceFile"):
            # build_inventory() already recreated a complete, authoritative
            # set of these for every currently existing file/directory
            # before this function runs, regardless of mode. Carrying over
            # a stale one here would resurrect nodes for files/directories
            # that no longer exist on disk.
            continue
        if entity.source_file in changed_or_removed:
            continue
        extraction.entities.setdefault(entity.key, entity)
        if entity.entity_type == "Document" and entity.source_file not in extraction.document_by_relpath:
            extraction.document_by_relpath[entity.source_file] = entity
    for relation in previous.relations.values():
        if relation_touches_files(relation, changed_or_removed):
            continue
        extraction.relations.setdefault(relation.key, relation)
    for chunk in previous.source_chunks.values():
        if chunk.source_file in changed_or_removed:
            continue
        extraction.source_chunks.setdefault(chunk.uid, chunk)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
INDEX_SPECS = [
    {"label": "Folder", "property": "uid"},
    {"label": "SourceFile", "property": "uid"},
    {"label": "SourceFile", "property": "path"},
    {"label": "Function", "property": "uid"},
    {"label": "Function", "property": "name"},
    {"label": "Class", "property": "uid"},
    {"label": "Class", "property": "name"},
    {"label": "Type", "property": "uid"},
    {"label": "Macro", "property": "uid"},
    {"label": "Variable", "property": "uid"},
    {"label": "KconfigOption", "property": "uid"},
    {"label": "KconfigOption", "property": "name"},
    {"label": "DTNode", "property": "uid"},
    {"label": "DTBinding", "property": "uid"},
    {"label": "Document", "property": "uid"},
    {"label": "Document", "property": "path"},
    {"label": "Concept", "property": "uid"},
    {"label": "Requirement", "property": "uid"},
    {"label": "SourceChunk", "property": "uid"},
    {"label": "SourceChunk", "property": "source_file"},
]

REQUIRED_PROPERTIES = {
    "Folder": ["uid", "path", "name"],
    "SourceFile": ["uid", "path", "name", "sha256_hash", "detected_type"],
    "Function": ["uid", "name", "file", "line_start", "kind"],
    "Class": ["uid", "name", "file", "line_start", "kind"],
    "Type": ["uid", "name", "file", "line_start", "kind"],
    "Macro": ["uid", "name", "file", "line_start", "kind"],
    "Variable": ["uid", "name", "file", "line_start"],
    "KconfigOption": ["uid", "name", "kind"],
    "DTNode": ["uid", "name", "file", "node_path"],
    "DTBinding": ["uid", "compatible"],
    "Document": ["uid", "path", "title"],
    "Concept": ["uid", "name", "file", "line_start"],
    "Requirement": ["uid", "text", "file", "line_start"],
    "SourceChunk": ["uid", "source_file", "line_start", "line_end", "content"],
}


def export_results(extraction: Extraction, metrics: dict[str, Any], manifest: dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with (OUTPUT_DIR / "entities.jsonl").open("w", encoding="utf-8") as handle:
        for entity in sorted(extraction.entities.values(), key=lambda e: (e.entity_type, e.source_file, e.name)):
            handle.write(json.dumps(asdict(entity), ensure_ascii=True) + "\n")
    with (OUTPUT_DIR / "relations.jsonl").open("w", encoding="utf-8") as handle:
        for relation in sorted(extraction.relations.values(), key=lambda r: (r.predicate, r.subject, r.object)):
            handle.write(json.dumps(asdict(relation), ensure_ascii=True) + "\n")
    with (OUTPUT_DIR / "source_chunks.jsonl").open("w", encoding="utf-8") as handle:
        for chunk in sorted(extraction.source_chunks.values(), key=lambda c: c.uid):
            handle.write(json.dumps(asdict(chunk), ensure_ascii=True) + "\n")
    (OUTPUT_DIR / "ambiguities.json").write_text(
        json.dumps({"generated_at": now(), "count": len(extraction.ambiguities),
                    "items": extraction.ambiguities}, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )
    (OUTPUT_DIR / "unresolved_relations.json").write_text(
        json.dumps({"generated_at": now(), "count": len(extraction.unresolved),
                    "items": extraction.unresolved}, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )
    save_manifest(manifest)

    by_uid = {entity_uid(e): e for e in extraction.entities.values()}
    node_counts = Counter(e.entity_type for e in extraction.entities.values())
    relation_counts = Counter(r.predicate for r in extraction.relations.values())
    labels_by_relation: dict[str, tuple[set[str], set[str]]] = defaultdict(lambda: (set(), set()))
    for relation in extraction.relations.values():
        subj = by_uid.get(relation.subject)
        obj = by_uid.get(relation.object)
        starts, ends = labels_by_relation[relation.predicate]
        if subj is not None:
            starts.add(subj.entity_type)
        if obj is not None:
            ends.add(obj.entity_type)

    schema = {
        "generated_at": now(),
        "metrics": metrics,
        "node_labels": [
            {"label": label, "count": node_counts.get(label, 0),
             "required_properties": REQUIRED_PROPERTIES.get(label, ["name"])}
            for label in sorted(node_counts)
        ],
        "relationship_types": [
            {"type": relation_type, "count": count,
             "start_labels": sorted(labels_by_relation[relation_type][0]),
             "end_labels": sorted(labels_by_relation[relation_type][1])}
            for relation_type, count in sorted(relation_counts.items())
        ],
        "indexes_created": INDEX_SPECS,
    }
    (OUTPUT_DIR / "graph_schema.json").write_text(
        json.dumps(schema, indent=2, ensure_ascii=True), encoding="utf-8"
    )


def load_exported_extraction() -> Extraction:
    extraction = Extraction()
    entities_path = OUTPUT_DIR / "entities.jsonl"
    if entities_path.is_file():
        with entities_path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                entity = Entity(**json.loads(line))
                extraction.entities[entity.key] = entity
                if entity.entity_type == "SourceFile":
                    extraction.file_entity_by_relpath[entity.source_file] = entity
                    extraction.file_relpaths.append(entity.source_file)
                elif entity.entity_type == "Document":
                    extraction.document_by_relpath[entity.source_file] = entity
    relations_path = OUTPUT_DIR / "relations.jsonl"
    if relations_path.is_file():
        with relations_path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                relation = Relation(**json.loads(line))
                extraction.relations[relation.key] = relation
    chunks_path = OUTPUT_DIR / "source_chunks.jsonl"
    if chunks_path.is_file():
        with chunks_path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                chunk = SourceChunk(**json.loads(line))
                extraction.source_chunks[chunk.uid] = chunk
    return extraction


# ---------------------------------------------------------------------------
# Memgraph ingestion
# ---------------------------------------------------------------------------
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


def bootstrap_indexes() -> None:
    for spec in INDEX_SPECS:
        try:
            index_conn = connect_memgraph()
            index_conn.autocommit = True
            cursor = index_conn.cursor()
            cursor.execute(f"CREATE INDEX ON :{spec['label']}({spec['property']})")
            cursor.close()
            index_conn.close()
        except Exception as exc:  # noqa: BLE001 - Memgraph raises a generic error on duplicate index
            if "already exists" not in str(exc).lower():
                raise


def ingest_memgraph(extraction: Extraction, mode: str, changed_or_removed: set[str]) -> None:
    logger = extraction.loggers["graph"]
    conn = connect_memgraph()
    try:
        cursor = conn.cursor()
        if mode == "full":
            cursor.execute("MATCH (n) DETACH DELETE n")
            conn.commit()
            logger.info("Cleared existing Memgraph graph before full ingestion")
        elif changed_or_removed:
            files_list = sorted(changed_or_removed)
            batch = 200
            for start in range(0, len(files_list), batch):
                cursor.execute(
                    "UNWIND $files AS f MATCH (n) WHERE n.file = f OR n.path = f OR n.source_file = f "
                    "DETACH DELETE n",
                    {"files": files_list[start:start + batch]},
                )
                conn.commit()
            logger.info("Deleted stale nodes for %d changed/removed files before incremental ingestion",
                        len(files_list))

        if mode == "update":
            # A directory that lost its last file is not covered by the
            # per-file deletion above (its Folder node never appears in
            # `changed_or_removed`, which only lists files). Purge any
            # Folder whose path is no longer part of the current tree.
            cursor.execute("MATCH (d:Folder) RETURN d.path")
            existing_folders = {row[0] for row in cursor.fetchall()}
            stale_folders = sorted(existing_folders - extraction.current_folder_paths)
            if stale_folders:
                cursor.execute(
                    "UNWIND $paths AS p MATCH (d:Folder {path: p}) DETACH DELETE d",
                    {"paths": stale_folders},
                )
                conn.commit()
                logger.info("Deleted %d empty/removed Folder nodes", len(stale_folders))

        bootstrap_indexes()

        entities_by_label: dict[str, list[Entity]] = defaultdict(list)
        for entity in extraction.entities.values():
            if entity.entity_type != "SourceChunk":
                entities_by_label[entity.entity_type].append(entity)

        batch_size = 1000
        for label, items in entities_by_label.items():
            rows = [{
                "uid": entity_uid(entity), "name": entity.name, "file": entity.source_file,
                "path": entity.source_file, "line_start": entity.line_start, "line_end": entity.line_end,
                "kind": entity.kind, "extraction_method": entity.extraction_method,
                "description": entity.description,
                "text": entity.source_text or entity.description or entity.name,
                "source_text": entity.source_text, "signature": entity.signature,
                "return_type": entity.return_type,
                "parameters": json.dumps(entity.parameters, ensure_ascii=True),
                "metadata": json.dumps(entity.metadata, ensure_ascii=True),
            } for entity in items]
            query = (
                f"UNWIND $rows AS row MERGE (n:{label} {{uid: row.uid}}) "
                "SET n.name=row.name, n.file=row.file, n.path=row.path, "
                "n.line_start=row.line_start, n.line_end=row.line_end, n.kind=row.kind, "
                "n.extraction_method=row.extraction_method, n.description=row.description, "
                "n.text=row.text, n.title=row.name, n.source_text=row.source_text, "
                "n.signature=row.signature, n.return_type=row.return_type, "
                "n.parameters=row.parameters, n.metadata=row.metadata"
            )
            for start in range(0, len(rows), batch_size):
                cursor.execute(query, {"rows": rows[start:start + batch_size]})
                conn.commit()

        chunk_rows = [asdict(chunk) for chunk in extraction.source_chunks.values()]
        chunk_query = (
            "UNWIND $rows AS row MERGE (n:SourceChunk {uid: row.uid}) "
            "SET n.source_file=row.source_file, n.line_start=row.line_start, "
            "n.line_end=row.line_end, n.content=row.content, n.chunk_type=row.chunk_type"
        )
        for start in range(0, len(chunk_rows), batch_size):
            cursor.execute(chunk_query, {"rows": chunk_rows[start:start + batch_size]})
            conn.commit()
        for start in range(0, len(chunk_rows), batch_size):
            cursor.execute(
                "UNWIND $rows AS row "
                "MATCH (f:SourceFile {uid: 'SourceFile|' + row.source_file}) "
                "MATCH (c:SourceChunk {uid: row.uid}) "
                "MERGE (f)-[:HAS_CHUNK]->(c)",
                {"rows": chunk_rows[start:start + batch_size]},
            )
            conn.commit()

        relation_types = sorted({relation.predicate for relation in extraction.relations.values()})
        by_uid_label = {entity_uid(e): e.entity_type for e in extraction.entities.values()}
        for relation_type in relation_types:
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", relation_type):
                raise RuntimeError(f"Invalid relationship type: {relation_type}")
            # Group by (subject_label, object_label) so every MATCH can use the
            # per-label `uid` index instead of an unlabelled property scan,
            # which does not use any index at all and is unusably slow once
            # the graph has more than a few thousand nodes per label.
            grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
            skipped_unresolved = 0
            for relation in extraction.relations.values():
                if relation.predicate != relation_type:
                    continue
                subject_label = by_uid_label.get(relation.subject)
                object_label = by_uid_label.get(relation.object)
                if subject_label is None or object_label is None:
                    skipped_unresolved += 1
                    continue
                row = asdict(relation)
                row["metadata"] = json.dumps(row["metadata"], ensure_ascii=True)
                grouped[(subject_label, object_label)].append(row)
            if skipped_unresolved:
                logger.info("Skipped %d %s relations pointing at an unresolved/external endpoint",
                            skipped_unresolved, relation_type)
            for (subject_label, object_label), rows in grouped.items():
                query = (
                    "UNWIND $rows AS row "
                    f"MATCH (a:{subject_label} {{uid: row.subject}}) "
                    f"MATCH (b:{object_label} {{uid: row.object}}) "
                    f"MERGE (a)-[r:{relation_type}]->(b) "
                    "SET r.confidence=row.confidence, r.file=row.source_file, r.line=row.line, "
                    "r.rationale=row.rationale, r.role=row.role, r.metadata=row.metadata"
                )
                for start in range(0, len(rows), batch_size):
                    cursor.execute(query, {"rows": rows[start:start + batch_size]})
                    conn.commit()

        cursor.execute("MATCH (n) RETURN count(n)")
        node_count = cursor.fetchone()[0]
        cursor.execute("MATCH ()-[r]->() RETURN count(r)")
        relation_count = cursor.fetchone()[0]
        logger.info("Memgraph ingestion complete (mode=%s): %d nodes, %d relationships",
                    mode, node_count, relation_count)
    except Exception:
        logger.exception("Memgraph ingestion failed")
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update", action="store_true",
        help="Incremental mode: diff raw_data against output/ingestion_manifest.json and only "
             "re-parse added/modified files; removed files are purged from the graph.",
    )
    parser.add_argument("--skip-db", action="store_true", help="Export files without connecting to Memgraph")
    parser.add_argument(
        "--ingest-existing", action="store_true",
        help="Ingest previously exported entities.jsonl/relations.jsonl as a full (clean) load",
    )
    parser.add_argument(
        "--only-prefix", default="",
        help="TESTING ONLY: restrict chunking/parsing to files whose raw_data-relative path starts "
             "with this prefix. Breaks the full-corpus scope; never use for a production ingestion.",
    )
    args = parser.parse_args(argv)

    if args.ingest_existing:
        ingest_memgraph(load_exported_extraction(), mode="full", changed_or_removed=set())
        return 0

    if not RAW_DATA_DIR.is_dir():
        print(f"Missing raw_data directory: {RAW_DATA_DIR}", file=sys.stderr)
        return 1

    extraction = Extraction()
    files = build_inventory(extraction)

    if args.only_prefix:
        prefix = args.only_prefix.replace("\\", "/")
        files = [p for p in files if rel_path(p).startswith(prefix)]

    manifest = compute_manifest(extraction)
    changed_or_removed: set[str] = set()
    mode = "update" if args.update else "full"

    if mode == "update":
        old_manifest = load_manifest()
        added, modified, removed = diff_manifest(old_manifest, manifest)
        changed_or_removed = added | modified | removed
        previous = load_exported_extraction()
        merge_unchanged(extraction, previous, changed_or_removed)
        changed_paths = [p for p in files if rel_path(p) in (added | modified)]
        for path in changed_paths:
            text = read_text(path, extraction)
            if text is not None:
                add_source_chunks(extraction, rel_path(path), text)
        structural_pass(extraction, changed_paths)
        print(f"Incremental update: {len(added)} added, {len(modified)} modified, "
              f"{len(removed)} removed, {len(files) - len(added) - len(modified)} unchanged")
    else:
        chunk_all_files(extraction, files)
        structural_pass(extraction, files)

    cross_link_pass(extraction)
    detect_ambiguities(extraction)
    metrics = graph_metrics(extraction)
    export_results(extraction, metrics, manifest)

    if not args.skip_db:
        ingest_memgraph(extraction, mode=mode, changed_or_removed=changed_or_removed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
