#!/usr/bin/env python3
"""Read-only Kickstart server: no directory listing, writes, or secrets."""
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory='/srv', **kwargs)

    def list_directory(self, path):
        self.send_error(403, 'directory listing disabled')
        return None

    def do_GET(self):
        if self.path.split('?', 1)[0] == '/healthz':
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'ok\n')
            return
        return super().do_GET()


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', 8088), Handler).serve_forever()
