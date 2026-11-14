import os
import inspect
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

def load_handler(filepath):
    """Dynamically loads the request handler class from a given python file path."""
    if not os.path.exists(filepath):
        return None
    try:
        spec = importlib.util.spec_from_file_location("dynamic_mod", filepath)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for name, obj in inspect.getmembers(mod, inspect.isclass):
            if issubclass(obj, BaseHTTPRequestHandler) and obj is not BaseHTTPRequestHandler:
                return obj
    except Exception as e:
        print(f"Error loading {filepath}: {e}")
    return None

# Load handlers from both folders safely at startup
GemHandler = load_handler("gem/api.py")
OpenHandler = load_handler("open/api.py")

class RouterHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.route_request()

    def do_POST(self):
        self.route_request()

    def route_request(self):
        parsed_url = urlparse(self.path)
        query_params = parse_qs(parsed_url.query, keep_blank_values=True)
        
        # Extract model parameter (?model=open or ?model=gem), default to 'gem'
        model_list = query_params.pop("model", ["gem"])
        model = model_list[0].lower()

        target_handler_cls = OpenHandler if model == "open" else GemHandler

        if not target_handler_cls:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f"Handler for model '{model}' not found or failed to load.".encode())
            return

        # Reconstruct URL without 'model' so gem/api.py or open/api.py receives clean ?q= params
        new_query = urlencode(query_params, doseq=True)
        new_parts = parsed_url._replace(query=new_query)
        cleaned_path = urlunparse(new_parts)

        # Instantiate and execute the target script's handler directly in-memory
        handler = target_handler_cls(self.request, self.client_address, self.server)
        handler.path = cleaned_path
        handler.command = self.command
        handler.request_version = self.request_version
        handler.headers = self.headers
        handler.rfile = self.rfile
        handler.wfile = self.wfile

        try:
            if self.command == "GET":
                handler.do_GET()
            elif self.command == "POST":
                handler.do_POST()
            else:
                self.send_response(405)
                self.end_headers()
        except Exception as e:
            print(f"Error handling request for model {model}: {e}")

def run():
    port = int(os.environ.get("PORT", 10000))
    server = ThreadingHTTPServer(("0.0.0.0", port), RouterHandler)
    print(f"Server successfully running and listening on port {port}")
    server.serve_forever()

if __name__ == "__main__":
    run()
