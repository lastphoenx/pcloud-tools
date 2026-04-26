#!/usr/bin/env python3
"""Simple HTTP server for monitoring dashboard with cache-control headers."""

import http.server
import socketserver

class NoCacheHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    """HTTP request handler with disabled caching."""
    
    def end_headers(self):
        """Add cache-control headers to disable caching."""
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        super().end_headers()

if __name__ == '__main__':
    PORT = 8080
    with socketserver.TCPServer(('', PORT), NoCacheHTTPRequestHandler) as httpd:
        print(f'Dashboard server started on port {PORT}')
        httpd.serve_forever()
