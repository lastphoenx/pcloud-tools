#!/usr/bin/env python3
"""
Monitoring Dashboard Web Server
Serves both pcloud-tools/dashboard and entropy-watcher-und-clamav-scanner/docs
from a common root directory for proper absolute path resolution.
"""

import http.server
import socketserver
import os
from pathlib import Path

class NoCacheHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    """HTTP request handler with disabled caching and custom routing."""
    
    def end_headers(self):
        """Add cache-control headers to disable caching for JSON/HTML."""
        if self.path.endswith(('.json', '.html')):
            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Expires', '0')
        super().end_headers()
    
    def translate_path(self, path):
        """
        Translate URL path to filesystem path.
        Handles both relative and absolute paths correctly.
        """
        # For root or empty path, redirect to dashboard
        if path == '/' or path == '':
            path = '/pcloud-tools/dashboard/index.html'
        
        # Remove query parameters
        path = path.split('?', 1)[0]
        path = path.split('#', 1)[0]
        
        # Convert URL path to filesystem path
        # The server runs from workspace root, so paths map directly
        return super().translate_path(path)
    
    def log_message(self, format, *args):
        """Custom log format with timestamp."""
        timestamp = self.log_date_time_string()
        print(f"[{timestamp}] {self.address_string()} - {format % args}")


if __name__ == '__main__':
    PORT = 8080
    
    # Ensure we're running from the correct directory
    script_dir = Path(__file__).parent.resolve()
    os.chdir(script_dir)
    
    print("=" * 60)
    print(f"🚀 Monitoring Dashboard Server")
    print("=" * 60)
    print(f"📁 Document Root: {script_dir}")
    print(f"🌐 Port: {PORT}")
    print(f"🔗 Dashboard: http://localhost:{PORT}/pcloud-tools/dashboard/index.html")
    print(f"📚 Docs: http://localhost:{PORT}/entropy-watcher-und-clamav-scanner/docs/")
    print("=" * 60)
    print("Press Ctrl+C to stop the server")
    print()
    
    with socketserver.TCPServer(('', PORT), NoCacheHTTPRequestHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n\n⏹️  Server stopped")
