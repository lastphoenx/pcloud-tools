#!/usr/bin/env python3
"""
Simple HTTP server for monitoring dashboard with cache-control headers.

⚠️  DEPRECATED for Production Use (April 2026)
    This server is now only recommended for LOCAL DEVELOPMENT within the dashboard/ directory.
    
    For PRODUCTION, use the new monitoring-dashboard-server.py from the workspace root,
    which supports absolute paths to multiple repositories:
    - /pcloud-tools/dashboard/
    - /entropy-watcher-und-clamav-scanner/docs/
    
    See: ../docs/DEPLOYMENT_UPDATE_2026.md for migration guide
"""

import http.server
import socketserver
import sys

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
    
    print("=" * 70)
    print("⚠️  DEPRECATED: dashboard/server.py")
    print("=" * 70)
    print("This server is for LOCAL DEVELOPMENT only.")
    print("Absolute links to /entropy-watcher-und-clamav-scanner/ will NOT work!")
    print()
    print("For PRODUCTION, use: monitoring-dashboard-server.py (workspace root)")
    print("See: docs/DEPLOYMENT_UPDATE_2026.md for details")
    print("=" * 70)
    print()
    
    response = input("Continue anyway for local dev? [y/N]: ")
    if response.lower() != 'y':
        print("Aborted. Use monitoring-dashboard-server.py instead.")
        sys.exit(0)
    
    print(f'\nDashboard server started on port {PORT}')
    print(f'Access: http://localhost:{PORT}/index.html')
    print('Press Ctrl+C to stop\n')
    
    with socketserver.TCPServer(('', PORT), NoCacheHTTPRequestHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n\nServer stopped")
