"""Loopback-only local MVP API; binary video upload avoids multipart dependencies."""
import json
import os
import shutil
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
from .core import ROOT, connect, create_batch, job_json, now, providers
from .media import probe

class Handler(BaseHTTPRequestHandler):
    def json(self, code, value):
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def file(self, path, kind, download=False):
        if not path.is_file():
            return self.json(404, {'error': 'Not found'})
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(path.stat().st_size))
        if download:
            self.send_header('Content-Disposition', 'attachment; filename="cut.mp4"')
        self.end_headers()
        with path.open('rb') as f:
            shutil.copyfileobj(f, self.wfile)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/':
            return self.file(ROOT / 'frontend/index.html', 'text/html; charset=utf-8')
        if path == '/api/health':
            return self.json(200, {'status': 'ok', 'planner': 'rules', 'worker': 'separate process'})
        with connect() as c:
            if path == '/api/assets':
                return self.json(200, [dict(x) for x in c.execute('SELECT * FROM assets ORDER BY created_at DESC')])
            if path == '/api/jobs':
                return self.json(200, [job_json(x) for x in c.execute('SELECT * FROM jobs ORDER BY created_at DESC LIMIT 200')])
            if path.startswith('/api/batches/'):
                rows = c.execute('SELECT * FROM jobs WHERE batch_id=? ORDER BY created_at,id', (path.split('/')[-1],)).fetchall()
                return self.json(200 if rows else 404, {'jobs': [job_json(x) for x in rows]})
            if path.startswith('/api/jobs/'):
                parts = path.split('/')
                row = c.execute('SELECT * FROM jobs WHERE id=?', (parts[3],)).fetchone()
                if row and len(parts) == 4:
                    return self.json(200, job_json(row))
                if row and len(parts) == 5 and parts[4] == 'output' and row['status'] == 'succeeded':
                    return self.file(providers()[0].local_path(row['output_key']), 'video/mp4', True)
        return self.json(404, {'error': 'Not found or output not ready'})

    def do_POST(self):
        path = urlsplit(self.path).path
        try:
            # Reject cross-site browser writes. Local UI uses same-origin requests without CORS.
            origin = self.headers.get('Origin')
            if origin and origin != 'http://' + self.headers.get('Host', ''):
                return self.json(403, {'error': 'Cross-origin writes disabled'})
            size = int(self.headers.get('Content-Length', '0'))
            limit = int(os.getenv('CUT_MAX_UPLOAD_MB', '200')) * 1024 * 1024
            if not 0 < size <= (limit if path == '/api/assets' else 65536):
                self.close_connection = True
                return self.json(413, {'error': 'Missing or oversized Content-Length'})
            if path == '/api/assets':
                q = parse_qs(urlsplit(self.path).query)
                category = q.get('category', ['general'])[0]
                name = q.get('name', ['video'])[0]
                if not 1 <= len(category) <= 64 or not 1 <= len(name) <= 255:
                    raise ValueError('Invalid category or name length')
                id = uuid.uuid4().hex
                storage = providers()[0]
                target = storage.local_path('assets/' + id)
                try:
                    with target.open('wb') as f:
                        left = size
                        while left:
                            chunk = self.rfile.read(min(1024 * 1024, left))
                            if not chunk:
                                raise ValueError('Incomplete upload')
                            f.write(chunk)
                            left -= len(chunk)
                    duration = probe(target)
                    with connect() as c:
                        c.execute('INSERT INTO assets VALUES(?,?,?,?,?)', (id, name, category, duration, now()))
                except Exception:
                    target.unlink(missing_ok=True)
                    raise
                return self.json(201, {'id': id, 'name': name, 'category': category, 'duration': duration})
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError('Expected JSON object')
            if path == '/api/batches':
                batch = create_batch(body)
                return self.json(202, {'batch_id': batch, 'status_url': '/api/batches/' + batch})
            if path.startswith('/api/jobs/') and path.endswith('/retry'):
                with connect() as c:
                    changed = c.execute("UPDATE jobs SET status='queued',progress=0,error=NULL,updated_at=? WHERE id=? AND status='failed'", (now(), path.split('/')[3])).rowcount
                return self.json(202 if changed else 409, {'requeued': bool(changed)})
            return self.json(404, {'error': 'Not found'})
        except (ValueError, KeyError, TypeError) as e:
            return self.json(400, {'error': str(e)})
        except Exception:
            import logging
            logging.exception('Request failed')
            return self.json(500, {'error': 'Internal error; inspect server logs'})

def main():
    providers()
    with connect():
        pass
    server = ThreadingHTTPServer(('127.0.0.1', int(os.getenv('CUT_PORT', '8000'))), Handler)
    print(f'Open http://127.0.0.1:{server.server_port}', flush=True)
    server.serve_forever()

if __name__ == '__main__':
    main()
