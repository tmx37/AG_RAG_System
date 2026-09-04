import os
import re
import sys
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

import yaml
import markdown
from mgclient import Connection

# --- CONFIGURATION ---
MEMGRAPH_HOST = "localhost"
MEMGRAPH_PORT = 7687

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ZEPHYR_ROOT_DIR = os.path.join(SCRIPT_DIR, "zephyr-demo-data")
ZEPHYR_ROOT = ZEPHYR_ROOT_DIR
DEMO_DOXYGEN_PATH = os.path.join(SCRIPT_DIR, "Doxyfile.demo")
DOXYGEN_OUTPUT_DIR = os.path.join(SCRIPT_DIR, "doxygen_xml")

# --- MEMGRAPH CONNECTION ---
def get_connection() -> Connection:
    try:
        conn = Connection(host=MEMGRAPH_HOST, port=MEMGRAPH_PORT, username="", password="")
        print(f"✅ Connected to Memgraph at {MEMGRAPH_HOST}:{MEMGRAPH_PORT}")
        return conn
    except Exception as e:
        print(f"❌ Failed to connect to Memgraph: {e}")
        sys.exit(1)

def execute_query(conn: Connection, query: str, params: Optional[dict] = None):
    try:
        cursor = conn.cursor()

        # Let the Bolt driver bind parameters; string replacement is unsafe and
        # also did not modify the query because str.replace returns a new string.
        if params is None:
            params = {}
        
        cursor.execute(query, params)
        output = cursor.fetchall() if cursor.description is not None else []
        cursor.close()
        conn.commit()
        return list(output)
    except Exception as e:
        print(f"⚠️ Query error: {e}")
        return []


def find_values(value, key: str) -> Iterable[str]:
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


# --- 1. PROCESS MARKDOWN/RESTRUCTURED TEXT ---
def ingest_documents(conn: Connection):
    print("\n📄 Processing Documentation (Markdown/reST)...")
    doc_dir = Path(ZEPHYR_ROOT) / "doc"
    if not doc_dir.exists():
        print("⚠️ Doc directory not found. Skipping.")
        return

    count = 0
    files = list(doc_dir.rglob("*.md")) + list(doc_dir.rglob("*.rst"))
    
    for file_path in files:
        try:
            content = file_path.read_text(encoding='utf-8', errors='ignore')
            # Simple title extraction (first # or first line)
            title = "Untitled"
            if content.startswith("#"):
                title = content.split('\n')[0].replace("#", "").strip()
            elif content.strip():
                title = content.strip().split('\n')[0][:100]

            # Relative path for uniqueness
            rel_path = str(file_path.relative_to(ZEPHYR_ROOT))

            # Create Document Node
            query = """
            MERGE (d:Document {path: $path})
            SET d.title = $title, d.content = $content, d.type = 'documentation'
            """
            execute_query(conn, query, {
                "path": rel_path,
                "title": title,
                "content": content[:20000] # Truncate for demo safety
            })
            count += 1
        except Exception as e:
            print(f"⚠️ Error processing {file_path}: {e}")

    print(f"✅ Ingested {count} documentation files.")

# --- 2. PROCESS DOXYGEN XML (FUNCTIONS & CALLS) ---
def generate_doxygen_xml():
    """Runs Doxygen to generate XML if not present."""
    if os.path.exists(DOXYGEN_OUTPUT_DIR):
        print("✅ Doxygen XML already exists.")
        return

    print("🛠️ Generating Doxygen XML (this may take a minute)...")
    # Check if doxygen is installed
    if subprocess.run(["doxygen", "--version"], capture_output=True).returncode != 0:
        print("⚠️ Doxygen not found. Skipping XML generation. Install doxygen to enable code graph.")
        return

    # Create a minimal Doxyfile for XML output only
    doxyfile_content = f"""
    GENERATE_HTML = NO
    GENERATE_XML = YES
    XML_OUTPUT = {DOXYGEN_OUTPUT_DIR}
    RECURSIVE = YES
    FILE_PATTERNS = *.c *.h
    QUIET = YES
    WARNINGS = NO
    """
    # Limit to a small subdir for demo speed (e.g., kernel)
    target_dir = os.path.join(SCRIPT_DIR, "zephyr-demo-data", "include", "zephyr")
    
    try:
        with open(DEMO_DOXYGEN_PATH, "w") as f:
            f.write(doxyfile_content)
    except Exception as e:
        print(f"⚠️ Failed to create/open Doxyfile.demo: {e}", e)
    
    try:
        subprocess.run(["doxygen", DEMO_DOXYGEN_PATH], check=True, cwd=target_dir)
        # Move output to expected location
        generated_xml = os.path.join(SCRIPT_DIR, "xml")
        if os.path.exists(generated_xml):
            os.rename(generated_xml, DOXYGEN_OUTPUT_DIR)
        print("✅ Doxygen XML generated.")
    except Exception as e:
        print(f"⚠️ Doxygen failed: {e}")
    finally:
        if os.path.exists(DEMO_DOXYGEN_PATH):
            os.remove(DEMO_DOXYGEN_PATH)

def ingest_code_graph(conn: Connection):
    print("\n💻 Processing Code Graph (Doxygen XML)...")
    xml_dir = Path(DOXYGEN_OUTPUT_DIR)
    if not xml_dir.exists():
        print("⚠️ No XML directory found. Run Doxygen first or skip.")
        return

    functions: Set[Tuple[str, str]] = set()
    calls: Set[Tuple[str, str, str]] = set()

    # Parse compounddef (functions/classes)
    for xml_file in xml_dir.rglob("*.xml"):
        try:
            tree = ET.parse(xml_file)
            root = tree.getroot()
            
            # Doxygen normally stores C functions as memberdef elements inside
            # a file/class compounddef, not as compounddef kind="function".
            definitions = list(root.findall(".//memberdef[@kind='function']"))
            definitions += [
                compound for compound in root.findall(".//compounddef")
                if compound.get("kind") == "function"
            ]
            for definition in definitions:
                kind = definition.get("kind", "function")
                name_node = definition.find("name")
                if name_node is None:
                    continue
                
                func_name = (name_node.text or "").strip()
                if not func_name:
                    continue
                file_node = definition.find("location")
                file_path = file_node.get("file", "unknown") if file_node is not None else "unknown"
                functions.add((func_name, file_path))

                query = """
                MERGE (f:Function {name: $name, file: $file})
                SET f.kind = $kind
                """
                execute_query(conn, query, {"name": func_name, "file": file_path, "kind": kind})

                for ref in definition.findall(".//references/ref") + definition.findall(".//ref"):
                    if ref.get("kindref") == "function":
                        called_func = (ref.text or "").strip()
                        if called_func:
                            calls.add((func_name, file_path, called_func))
        except Exception as e:
            print(f"⚠️ Error processing XML {xml_file}: {e}")

    count_calls = 0
    for caller, caller_file, callee in calls:
        call_query = """
        MATCH (caller:Function {name: $caller, file: $caller_file})
        MATCH (callee:Function {name: $callee})
        MERGE (caller)-[:CALLS]->(callee)
        RETURN caller
        """
        if execute_query(conn, call_query, {
            "caller": caller, "caller_file": caller_file, "callee": callee
        }):
            count_calls += 1

    print(f"✅ Ingested {len(functions)} functions and {count_calls} call relationships.")

# --- 3. PROCESS DEVICE TREES (.dts) ---
def ingest_hardware(conn: Connection):
    print("\n🔌 Processing Hardware (Device Trees)...")
    dts_dir = Path(ZEPHYR_ROOT) / "dts"
    if not dts_dir.exists():
        print("⚠️ DTS directory not found.")
        return

    count_peripherals = 0
    # Regex to find compatible strings: compatible = "vendor,device";
    compat_pattern = re.compile(r'compatible\s*=\s*"([^"]+)"')

    for dts_file in list(dts_dir.rglob("*.dts")) + list(dts_dir.rglob("*.dtsi")):
        try:
            content = dts_file.read_text(encoding='utf-8', errors='ignore')
            matches = compat_pattern.findall(content)
            
            for compat in matches:
                # Create Peripheral Node
                query = """
                MERGE (p:HardwarePeripheral {compatible: $compat})
                SET p.source_file = $file
                """
                execute_query(conn, query, {"compat": compat, "file": str(dts_file.relative_to(ZEPHYR_ROOT))})
                count_peripherals += 1
        except Exception as e:
            print(f"⚠️ Error processing DTS {dts_file}: {e}")

    print(f"✅ Ingested {count_peripherals} hardware peripherals.")

# --- 4. PROCESS YAML (BOARDS) ---
def ingest_boards(conn: Connection):
    print("\n📟 Processing Boards (YAML)...")
    board_dir = Path(ZEPHYR_ROOT) / "boards"
    if not board_dir.exists():
        print("⚠️ Boards directory not found.")
        return

    count_boards = 0
    count_links = 0

    for yaml_file in board_dir.rglob("*.yaml"):
        # Skip schemas
        if "schema" in str(yaml_file):
            continue
            
        try:
            with open(yaml_file, 'r') as f:
                data = yaml.safe_load(f)
            
            if not data or "name" not in data:
                continue

            board_name = data["name"]
            board_id = yaml_file.stem # e.g., "nrf52840dk_nrf52840"

            # Create Board Node
            query = """
            MERGE (b:Board {id: $id})
            SET b.name = $name, b.path = $path
            """
            execute_query(conn, query, {
                "id": board_id,
                "name": board_name,
                "path": str(yaml_file.relative_to(ZEPHYR_ROOT))
            })
            count_boards += 1

            # Link Board to Peripherals (Simulated via compatible strings in YAML if present)
            # Real Zephyr YAMLs often list 'soc' or 'arch', we look for 'compatible' if available
            compatibles = list(find_values(data, "compatible"))
            for compat in compatibles:
                link_query = """
                MATCH (b:Board {id: $id})
                MATCH (p:HardwarePeripheral {compatible: $compat})
                MERGE (b)-[:SUPPORTS]->(p)
                RETURN b
                """
                if execute_query(conn, link_query, {"id": board_id, "compat": compat}):
                    count_links += 1
                    
        except Exception as e:
            print(f"⚠️ Error processing board {yaml_file}: {e}")

    print(f"✅ Ingested {count_boards} boards and created {count_links} board-peripheral links.")


def link_documents_to_functions(conn: Connection):
    """Link documentation that explicitly mentions an ingested function name."""
    query = """
    MATCH (d:Document), (f:Function)
    WHERE d.content CONTAINS f.name
    MERGE (d)-[:DESCRIBES]->(f)
    RETURN count(*) AS links
    """
    result = execute_query(conn, query)
    print(f"✅ Created {result[0]} document-function links.")


def link_functions_to_hardware(conn: Connection):
    """Link functions to peripherals referenced by Zephyr DT compatibility macros."""
    function_query = """
    MATCH (f:Function)
    RETURN f.name AS name, f.file AS file
    """
    peripheral_query = """
    MATCH (p:HardwarePeripheral)
    RETURN p.compatible AS compatible
    """
    functions = execute_query(conn, function_query)
    peripherals = execute_query(conn, peripheral_query)
    links = 0
    for function in functions:
        source = Path(function[1])
        if not source.is_absolute():
            source = Path(os.path.join(ZEPHYR_ROOT, "include", "zephyr", function[1])) 
        if not source.is_file():
            continue
        content = source.read_text(encoding="utf-8", errors="ignore")
        for peripheral in peripherals:
            compatible = peripheral[0]
            if compatible not in content and compatible.replace(",", "_") not in content:
                continue
            link_query = """
            MATCH (f:Function {name: $name, file: $file})
            MATCH (p:HardwarePeripheral {compatible: $compatible})
            MERGE (f)-[:USES]->(p)
            RETURN f
            """
            if execute_query(conn, link_query, {
                "name": function[0],
                "file": function[1],
                "compatible": compatible,
            }):
                links += 1
    print(f"✅ Created {links} function-peripheral links.")


# --- MAIN EXECUTION ---
if __name__ == "__main__":
    print("🚀 Starting Zephyr GraphRAG Ingestion Pipeline...")
    
    conn = get_connection()

    # Step 0: Generate XML (Optional)
    # generate_doxygen_xml()

    # Step 1: Documents
    # ingest_documents(conn)

    # Step 2: Code Graph
    # ingest_code_graph(conn)

    # Step 3: Hardware
    # ingest_hardware(conn)

    # Step 4: Boards
    # ingest_boards(conn)

    # Step 5: Deterministic links required by the graph schema
    link_documents_to_functions(conn)
    link_functions_to_hardware(conn)

    # Verification Query
    print("\n🔍 Running Verification Query...")
    verify_query = """
    MATCH (b:Board)-[:SUPPORTS]->(p:HardwarePeripheral)<-[:USES]-(f:Function)
    RETURN b.name as Board, p.compatible as Peripheral, f.name as Function
    LIMIT 5
    """
    results = execute_query(conn, verify_query)
    
    if results:
        print("✅ Graph Ready! Sample connections found:")
        for r in results:
            print(f"   - Board '{r['Board']}' supports Peripheral '{r['Peripheral']}' used by Function '{r['Function']}'")
    else:
        print("⚠️ Graph populated, but no complex chains found yet. Try ingesting more data or checking specific files.")

    print("\n🎉 Ingestion Complete. Ready for GraphRAG queries!")