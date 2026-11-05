"""Team Clock: a small, dependency-free time tracking application."""
import argparse
import csv
import io
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime, time as day_time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent


class Problem(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


class Store:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = str(path), clock
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS employees (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    CHECK(length(name) BETWEEN 1 AND 80));
                CREATE TABLE IF NOT EXISTS entries (
                    id INTEGER PRIMARY KEY,
                    employee_id INTEGER NOT NULL REFERENCES employees(id),
                    description TEXT NOT NULL CHECK(length(description) BETWEEN 1 AND 500),
                    started_at REAL NOT NULL,
                    stopped_at REAL CHECK(stopped_at IS NULL OR stopped_at >= started_at));
                CREATE UNIQUE INDEX IF NOT EXISTS one_running_timer
                    ON entries(employee_id) WHERE stopped_at IS NULL;
                CREATE INDEX IF NOT EXISTS entries_by_start ON entries(started_at);
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def text(value, label, maximum):
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
            raise Problem(f'{label}: от 1 до {maximum} символов.')
        return value.strip()

    def add_employee(self, value):
        name = self.text(value, 'Имя', 80)
        try:
            with self.db() as db:
                cursor = db.execute('INSERT INTO employees(name) VALUES (?)', (name,))
                return {'id': cursor.lastrowid, 'name': name}
        except sqlite3.IntegrityError:
            raise Problem('Сотрудник с таким именем уже существует.', 409) from None

    def start(self, employee_id, description):
        if type(employee_id) is not int or employee_id <= 0:
            raise Problem('Выберите сотрудника.')
        description = self.text(description, 'Описание задачи', 500)
        with self.db() as db:
            if not db.execute('SELECT 1 FROM employees WHERE id=?', (employee_id,)).fetchone():
                raise Problem('Сотрудник не найден.', 404)
            try:
                cursor = db.execute(
                    'INSERT INTO entries(employee_id, description, started_at) VALUES (?, ?, ?)',
                    (employee_id, description, self.clock()))
                return {'id': cursor.lastrowid}
            except sqlite3.IntegrityError:
                raise Problem('У сотрудника уже запущен таймер.', 409) from None

    def stop(self, entry_id):
        if type(entry_id) is not int or entry_id <= 0:
            raise Problem('Неверный идентификатор записи.')
        with self.db() as db:
            row = db.execute('SELECT * FROM entries WHERE id=?', (entry_id,)).fetchone()
            if row is None:
                raise Problem('Запись не найдена.', 404)
            # Repeated stop is idempotent; never replace the first stop time.
            if row['stopped_at'] is None:
                db.execute('UPDATE entries SET stopped_at=max(started_at, ?) '
                           'WHERE id=? AND stopped_at IS NULL', (self.clock(), entry_id))
        return {'id': entry_id}

    @staticmethod
    def bounds(params):
        try:
            first = date.fromisoformat(params['from']) if params.get('from') else None
            last = date.fromisoformat(params['to']) if params.get('to') else None
        except (ValueError, TypeError):
            raise Problem('Укажите даты в формате ГГГГ-ММ-ДД.') from None
        if first and last and first > last:
            raise Problem('Начало периода должно быть не позже конца.')
        if last == date.max:
            raise Problem('Конечная дата слишком велика.')
        lower = datetime.combine(first, day_time(), timezone.utc).timestamp() if first else float('-inf')
        upper = datetime.combine(last + timedelta(days=1), day_time(), timezone.utc).timestamp() if last else float('inf')
        return lower, upper

    def snapshot(self, params=None):
        params = params or {}
        lower, upper = self.bounds(params)
        employee_filter = params.get('employee_id', '')
        if employee_filter:
            try:
                employee_filter = int(employee_filter)
                if employee_filter <= 0:
                    raise ValueError
            except (ValueError, TypeError):
                raise Problem('Неверный сотрудник.') from None
        now = self.clock()
        with self.db() as db:
            employees = [dict(r) for r in db.execute('SELECT * FROM employees ORDER BY name, id')]
            rows = [dict(r) for r in db.execute(
                'SELECT entries.*, employees.name AS employee_name FROM entries '
                'JOIN employees ON employees.id=entries.employee_id ORDER BY started_at DESC, entries.id DESC')]
        entries, totals, running = [], {e['id']: 0 for e in employees}, []
        for row in rows:
            end = row['stopped_at'] if row['stopped_at'] is not None else now
            row['elapsed_seconds'] = max(0, end - row['started_at'])
            if row['stopped_at'] is None:
                running.append(dict(row))
            if employee_filter and row['employee_id'] != employee_filter:
                continue
            # Include overlap, clipping duration to the selected UTC dates.
            if row['started_at'] < upper and end >= lower:
                row['period_seconds'] = max(0, min(end, upper) - max(row['started_at'], lower))
                entries.append(row)
                totals[row['employee_id']] += row['period_seconds']
        summary = [dict(e, seconds=totals[e['id']]) for e in employees
                   if not employee_filter or e['id'] == employee_filter]
        return {'server_time': now, 'employees': employees, 'running': running,
                'entries': entries, 'summary': summary,
                'total_seconds': sum(r['seconds'] for r in summary)}


def make_handler(store):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, status, body, content_type='application/json; charset=utf-8', extra=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode()
            if isinstance(body, str):
                body = body.encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            try:
                url = urlsplit(self.path)
                params = {k: v[-1] for k, v in parse_qs(url.query).items()}
                if url.path == '/api/state':
                    return self.respond(200, store.snapshot(params))
                if url.path == '/api/export.csv':
                    state = store.snapshot(params)
                    output = io.StringIO(newline='')
                    writer = csv.writer(output)
                    writer.writerow(['Employee', 'Task', 'Started (UTC)', 'Stopped (UTC)', 'Seconds in period', 'Status'])
                    def safe(value):
                        return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) else value
                    def stamp(value):
                        return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else ''
                    for entry in state['entries']:
                        writer.writerow([safe(entry['employee_name']), safe(entry['description']),
                                         stamp(entry['started_at']), stamp(entry['stopped_at']),
                                         round(entry['period_seconds'], 3),
                                         'running' if entry['stopped_at'] is None else 'stopped'])
                    return self.respond(200, '\ufeff' + output.getvalue(), 'text/csv; charset=utf-8',
                                        {'Content-Disposition': 'attachment; filename="team-clock.csv"'})
                files = {'/': ('index.html', 'text/html; charset=utf-8'),
                         '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                         '/style.css': ('style.css', 'text/css; charset=utf-8')}
                if url.path in files:
                    filename, mime = files[url.path]
                    return self.respond(200, (ROOT / 'static' / filename).read_bytes(), mime)
                self.respond(404, {'error': 'Не найдено.'})
            except Problem as error:
                self.respond(error.status, {'error': error.message})

        def do_POST(self):
            try:
                # Browser same-origin guard; no permissive CORS.
                origin = self.headers.get('Origin')
                if origin and urlsplit(origin).netloc != self.headers.get('Host'):
                    raise Problem('Запрос с другого сайта запрещён.', 403)
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise Problem('Ожидается application/json.', 415)
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                except ValueError:
                    raise Problem('Неверная длина запроса.') from None
                if length < 1 or length > 8192:
                    raise Problem('Размер запроса должен быть от 1 до 8192 байт.', 413)
                try:
                    data = json.loads(self.rfile.read(length))
                except (ValueError, UnicodeDecodeError):
                    raise Problem('Неверный JSON.') from None
                if not isinstance(data, dict):
                    raise Problem('Ожидается JSON-объект.')
                path = urlsplit(self.path).path
                if path == '/api/employees':
                    return self.respond(201, store.add_employee(data.get('name')))
                if path == '/api/timers/start':
                    return self.respond(201, store.start(data.get('employee_id'), data.get('description')))
                if path == '/api/timers/stop':
                    return self.respond(200, store.stop(data.get('entry_id')))
                raise Problem('Не найдено.', 404)
            except Problem as error:
                self.respond(error.status, {'error': error.message})

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default=os.getenv('HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.getenv('PORT', '8000')))
    parser.add_argument('--db', default=os.getenv('DATABASE_PATH', str(ROOT / 'data' / 'team-clock.sqlite3')))
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(Store(args.db)))
    server.daemon_threads = True
    print(f'Team Clock: http://{args.host}:{args.port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
