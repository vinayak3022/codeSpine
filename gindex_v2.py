import os, sys, shutil, signal, subprocess, click, kuzu, logging
import tree_sitter_java as tsjava
from tree_sitter import Language, Parser, Query
from fastmcp import FastMCP

# Configuration
DB_PATH = os.path.expanduser("~/.gindex_db")
PID_FILE = os.path.expanduser("~/.gindex.pid")
LOG_FILE = os.path.expanduser("~/.gindex.log")

logging.basicConfig(filename=LOG_FILE, level=logging.INFO)

# Tree-sitter Setup
JAVA_LANGUAGE = Language(tsjava.language())
parser = Parser(JAVA_LANGUAGE)

class GIndexEngine:
    def __init__(self, read_only=False):
        try:
            self.db = kuzu.Database(DB_PATH, buffer_pool_size=1024**3 * 1) 
            self.conn = kuzu.Connection(self.db)
            if not read_only: self._init_schema()
        except Exception as e:
            click.secho(f"❌ DB Lock Error: {e}. Run 'gindex stop' first.", fg="red")
            sys.exit(1)

    def _init_schema(self):
        try:
            res = self.conn.execute("CALL SHOW_TABLES() RETURN name")
            tables = [row[0] for row in res.get_as_df().values]
            if "Class" not in tables:
                self.conn.execute("CREATE NODE TABLE Project(name STRING, path STRING, PRIMARY KEY (name))")
                self.conn.execute("CREATE NODE TABLE Class(fqcn STRING, name STRING, module STRING, project STRING, PRIMARY KEY (fqcn))")
                self.conn.execute("CREATE NODE TABLE Method(id STRING, name STRING, PRIMARY KEY (id))")
                self.conn.execute("CREATE REL TABLE CALLS(FROM Method TO Method)")
                self.conn.execute("CREATE REL TABLE HAS_METHOD(FROM Class TO Method)")
        except: pass

    def index_project(self, root_path):
        root_path = os.path.abspath(root_path)
        project_name = os.path.basename(root_path)
        self.conn.execute("MERGE (p:Project {name: $name}) SET p.path = $path", {"name": project_name, "path": root_path})
        
        for root, _, files in os.walk(root_path):
            if "src/main/java" in root and not any(x in root for x in ["target", "test", ".git", "build", "out", "bin"]):
                module_name = os.path.relpath(root, root_path).split(os.sep)[0]
                for file in files:
                    if file.endswith(".java"):
                        self._parse_java(os.path.join(root, file), module_name, project_name, root_path)

    def _parse_java(self, file_path, module, project, root_path):
        rel_path = os.path.relpath(file_path, root_path)
        class_fqcn = f"{project}:{rel_path.replace(os.sep, '.')}"
        
        try:
            with open(file_path, "rb") as f:
                source = f.read()
                tree = parser.parse(source)

            # 1. Class Node
            self.conn.execute("MERGE (c:Class {fqcn: $fqcn}) SET c.name = $name, c.module = $mod, c.project = $proj",
                            {"fqcn": class_fqcn, "name": os.path.basename(file_path), "mod": module, "proj": project})
            
            # 2. Method Declarations
            m_query = Query(JAVA_LANGUAGE, "(method_declaration name: (identifier) @name) @decl")
            captures = m_query.captures(tree.root_node)
            
            for node, tag in captures:
                if tag == "name":
                    m_name = node.text.decode()
                    m_id = f"{class_fqcn}#{m_name}"
                    
                    self.conn.execute("MERGE (m:Method {id: $id}) SET m.name = $n", {"id": m_id, "n": m_name})
                    self.conn.execute("MATCH (c:Class {fqcn: $cf}), (m:Method {id: $mi}) MERGE (c)-[:HAS_METHOD]->(m)", 
                                    {"cf": class_fqcn, "mi": m_id})
                    
                    # 3. Method Invocations (Stubbing target methods)
                    call_query = Query(JAVA_LANGUAGE, "(method_invocation name: (identifier) @call_name)")
                    calls = call_query.captures(node.parent)
                    for c_node, _ in calls:
                        target_name = c_node.text.decode()
                        # Merge into a stub if target not yet known
                        self.conn.execute("MERGE (target:Method {id: $tn}) SET target.name = $tn", {"tn": target_name})
                        self.conn.execute("""
                            MATCH (source:Method {id: $si}), (target:Method {id: $ti})
                            MERGE (source)-[:CALLS]->(target)
                        """, {"si": m_id, "ti": target_name})
        except Exception as e:
            logging.error(f"Error parsing {file_path}: {e}")

# --- CLI ---
@click.group()
def cli(): pass

@cli.command()
@click.argument('path', type=click.Path(exists=True))
def analyse(path):
    """Deep index project into the graph."""
    if os.path.exists(PID_FILE):
        click.secho("🛑 Stop MCP server first ('gindex stop').", fg="yellow")
        return
    click.echo(f"🔍 Digging through {path}...")
    GIndexEngine().index_project(path)
    click.secho(f"✅ Indexed successfully.", fg="green")

@cli.command()
@click.argument('q')
def search(q):
    """Fuzzy search for Classes or Methods (Case-Insensitive)."""
    engine = GIndexEngine(read_only=True)
    query = """
    MATCH (n) 
    WHERE lower(n.name) CONTAINS lower($q) 
       OR (n.id IS NOT NULL AND lower(n.id) CONTAINS lower($q))
       OR (n.fqcn IS NOT NULL AND lower(n.fqcn) CONTAINS lower($q))
    RETURN labels(n)[0] as Type, n.name as Name, coalesce(n.id, n.fqcn) as Identifier
    LIMIT 15
    """
    click.echo(engine.conn.execute(query, {"q": q}).get_as_df())

@cli.command()
def stats():
    """Show Database health and entity counts."""
    engine = GIndexEngine(read_only=True)
    click.secho("\n--- Storage ---", fg="cyan")
    click.echo(f"DB Path: {DB_PATH}")
    click.secho("\n--- File Distribution ---", fg="cyan")
    click.echo(engine.conn.execute("MATCH (c:Class) RETURN c.project, count(c)").get_as_df())
    click.secho("\n--- Graph Density ---", fg="cyan")
    calls = engine.conn.execute("MATCH ()-[r:CALLS]->() RETURN count(r)").get_as_df().iloc[0,0]
    has_m = engine.conn.execute("MATCH ()-[r:HAS_METHOD]->() RETURN count(r)").get_as_df().iloc[0,0]
    click.echo(f"CALLS: {calls} | HAS_METHOD: {has_m}")

@cli.command()
def nuke():
    """Safely wipe the database."""
    if click.confirm('Wipe the entire GIndex brain?', abort=True):
        if os.path.exists(DB_PATH):
            if os.path.isdir(DB_PATH): shutil.rmtree(DB_PATH)
            else: os.remove(DB_PATH)
            click.secho("💥 Database nuked.", fg="red")

@cli.command()
def start():
    """Start MCP background process."""
    if os.path.exists(PID_FILE): return click.echo("⚠️ Already running.")
    proc = subprocess.Popen([sys.executable, __file__, "run-mcp"], stdout=open(LOG_FILE, "a"), stderr=subprocess.STDOUT)
    with open(PID_FILE, "w") as f: f.write(str(proc.pid))
    click.secho("🚀 GIndex Online (MCP)", fg="cyan")

@cli.command()
def stop():
    """Stop the background process."""
    if os.path.exists(PID_FILE):
        with open(PID_FILE, "r") as f: os.kill(int(f.read()), signal.SIGTERM)
        os.remove(PID_FILE)
    click.echo("🛑 Stopped.")

@cli.command(hidden=True)
def run_mcp():
    mcp = FastMCP("gindex")
    engine = GIndexEngine(read_only=True)
    
    @mcp.tool()
    def search_brain(q: str):
        """Search for class or method names fuzzy."""
        return engine.conn.execute("MATCH (n) WHERE lower(n.name) CONTAINS lower($q) RETURN n.name, labels(n)[0] as type", {"q": q}).get_as_df().to_dict()

    @mcp.tool()
    def get_impact(method_name: str):
        """Trace callers 3 levels up."""
        return engine.conn.execute("MATCH (c:Method)-[:CALLS*1..3]->(t:Method) WHERE t.name = $n OR t.id = $n RETURN DISTINCT c.id", {"n": method_name}).get_as_df().values.tolist()

    mcp.run()

if __name__ == "__main__":
    cli()