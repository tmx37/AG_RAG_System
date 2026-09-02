import os
import re
import sys
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Dict, Optional

import yaml
import markdown
from mgclient import Connection

# --- CONFIGURATION ---
MEMGRAPH_HOST = "localhost"
MEMGRAPH_PORT = 7687

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ZEPHYR_ROOT_DIR = os.path.join(SCRIPT_DIR, "zephyr-demo-data")
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

def execute_query(conn: Connection, query: str, params: dict = None):
    try:
        cursor = conn.cursor()
        
        # Replace all '$' values in 'query' with related values in 'params'
        if params is not None:
            for value_key in params.keys():
                query.replace(value_key, params.get(value_key))
        
        # Execute query 
        cursor.execute(query)
        
        # Collect output if present
        output = cursor.fetchall()
        cursor.close()
        
        # Make db changes persistent
        conn.commit()
        
        return list(output)
    except Exception as e:
        print(f"⚠️ Query error: {e}")
        return []

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
            CREATE (d:Document {
                path: $path,
                title: $title,
                content: $content,
                type: 'documentation'
            })
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

    count_funcs = 0
    count_calls = 0

    # Parse compounddef (functions/classes)
    for xml_file in xml_dir.rglob("*.xml"):
        try:
            tree = ET.parse(xml_file)
            root = tree.getroot()
            
            # Find compounddef (function, class, struct)
            for compound in root.findall(".//compounddef"):
                kind = compound.get("kind")
                if kind not in ["function", "member"]:
                    continue

                name_node = compound.find("name")
                if name_node is None:
                    continue
                
                func_name = name_node.text
                file_node = compound.find("location")
                file_path = file_node.get("file", "unknown") if file_node is not None else "unknown"

                # Create Function Node
                query = """
                MERGE (f:Function {name: $name, file: $file})
                ON CREATE SET f.kind = $kind
                """
                execute_query(conn, query, {"name": func_name, "file": file_path, "kind": kind})
                count_funcs += 1

                # Extract Calls (references to other functions)
                for ref in compound.findall(".//ref"):
                    if ref.get("kindref") == "function":
                        called_func = ref.text
                        if called_func:
                            call_query = """
                            MATCH (caller:Function {name: $caller, file: $caller_file})
                            MATCH (callee:Function {name: $callee})
                            MERGE (caller)-[:CALLS]->(callee)
                            """
                            # Note: This is a simplified match. In prod, use unique IDs.
                            execute_query(conn, call_query, {
                                "caller": func_name, 
                                "caller_file": file_path, 
                                "callee": called_func
                            })
                            count_calls += 1
        except Exception as e:
            continue # Skip malformed XML

    print(f"✅ Ingested {count_funcs} functions and {count_calls} call relationships.")

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

    for dts_file in dts_dir.rglob("*.dtsi"): # .dtsi are the include files with definitions
        try:
            content = dts_file.read_text(encoding='utf-8', errors='ignore')
            matches = compat_pattern.findall(content)
            
            for compat in matches:
                # Create Peripheral Node
                query = """
                MERGE (p:HardwarePeripheral {compatible: $compat})
                ON CREATE SET p.source_file = $file
                """
                execute_query(conn, query, {"compat": compat, "file": str(dts_file.relative_to(ZEPHYR_ROOT))})
                count_peripherals += 1
        except Exception as e:
            continue

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
            ON CREATE SET b.name = $name, b.path = $path
            """
            execute_query(conn, query, {
                "id": board_id,
                "name": board_name,
                "path": str(yaml_file.relative_to(ZEPHYR_ROOT))
            })
            count_boards += 1

            # Link Board to Peripherals (Simulated via compatible strings in YAML if present)
            # Real Zephyr YAMLs often list 'soc' or 'arch', we look for 'compatible' if available
            if "compatible" in data:
                compatibles = data["compatible"] if isinstance(data["compatible"], list) else [data["compatible"]]
                for compat in compatibles:
                    link_query = """
                    MATCH (b:Board {id: $id})
                    MATCH (p:HardwarePeripheral {compatible: $compat})
                    MERGE (b)-[:SUPPORTS]->(p)
                    """
                    res = execute_query(conn, link_query, {"id": board_id, "compat": compat})
                    if res: count_links += 1
                    
        except Exception as e:
            continue

    print(f"✅ Ingested {count_boards} boards and created {count_links} board-peripheral links.")

# --- MAIN EXECUTION ---
if __name__ == "__main__":
    print("🚀 Starting Zephyr GraphRAG Ingestion Pipeline...")
    
    conn = get_connection()

    # Step 0: Generate XML (Optional)
    generate_doxygen_xml()

    # Step 1: Documents
    ingest_documents(conn)

    # Step 2: Code Graph
    ingest_code_graph(conn)

    # Step 3: Hardware
    ingest_hardware(conn)

    # Step 4: Boards
    ingest_boards(conn)

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