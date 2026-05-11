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
        Maps URLs to the multi-repo structure with /main/ subdirectories:
        - /pcloud-tools/dashboard/ -> pcloud-tools/main/dashboard/
        - /entropy-watcher-und-clamav-scanner/docs/ -> entropy-watcher.../main/docs/
        
        This allows HTML files to use clean URLs without /main/ in the paths.
        """
        # For root or empty path, redirect to dashboard
        if path == '/' or path == '':
            path = '/pcloud-tools/dashboard/index.html'
        
        # Remove query parameters
        path = path.split('?', 1)[0]
        path = path.split('#', 1)[0]
        
        # Map clean URLs to /main/ subdirectories
        if path.startswith('/pcloud-tools/'):
            path = path.replace('/pcloud-tools/', '/pcloud-tools/main/', 1)
        elif path.startswith('/entropy-watcher-und-clamav-scanner/'):
            path = path.replace('/entropy-watcher-und-clamav-scanner/', 
                              '/entropy-watcher-und-clamav-scanner/main/', 1)
        
        # Convert URL path to filesystem path
        # The server runs from workspace root, so paths map directly
        return super().translate_path(path)
    
    def log_message(self, format, *args):
        """Custom log format with timestamp."""
        timestamp = self.log_date_time_string()
        print(f"[{timestamp}] {self.address_string()} - {format % args}")


if __name__ == '__main__':
    PORT = 8080
    
    # Change to parent directory (/opt/apps) for multi-repo serving
    # monitoring-dashboard-server.py lives in /opt/apps/pcloud-tools/main/
    # We need to serve from /opt/apps/ to access both repos
    script_dir = Path(__file__).parent.resolve()
    workspace_root = script_dir.parent.parent  # Go up from pcloud-tools/main/ to /opt/apps/
    os.chdir(workspace_root)
    
    print("=" * 60)
    print(f"🚀 Monitoring Dashboard Server")
    print("=" * 60)
    print(f"📁 Document Root: {workspace_root}")
    print(f"📂 Repos: pcloud-tools/main/, entropy-watcher-und-clamav-scanner/main/")
    print(f"🌐 Port: {PORT}")
    print(f"🔗 Dashboard: http://localhost:{PORT}/pcloud-tools/dashboard/index.html")
    print(f"📚 Docs: http://localhost:{PORT}/entropy-watcher-und-clamav-scanner/docs/")
    print(f"💡 URLs auto-mapped to /main/ subdirectories")
    print("=" * 60)
    print("Press Ctrl+C to stop the server")
    print()
    
    with socketserver.TCPServer(('', PORT), NoCacheHTTPRequestHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n\n⏹️  Server stopped")
