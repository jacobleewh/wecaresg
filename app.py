"""Launch the current CareBridge app from the workspace root or Flask CLI."""
import importlib.util
from pathlib import Path
import sys

server_directory = Path(__file__).resolve().parent / "carebridge-ai"
sys.path.insert(0, str(server_directory))
spec = importlib.util.spec_from_file_location("carebridge_server", server_directory / "app.py")
server = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = server
spec.loader.exec_module(server)
app = server.app

if __name__ == "__main__":
    app.run(debug=True, use_reloader=False, threaded=True, host="0.0.0.0", port=5000)
