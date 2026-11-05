import concurrent.futures
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

from app import Problem, Store, make_handler


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'clock.sqlite3'
        self.now = datetime(2026, 1, 1, 23, 30, tzinfo=timezone.utc).timestamp()
        self.store = Store(self.path, lambda: self.now)
        self.employee = self.store.add_employee('Alice')['id']

    def tearDown(self):
        self.temp.cleanup()

    def test_start_stop_and_idempotent_stop(self):
        entry = self.store.start(self.employee, 'Build API')['id']
        self.now += 65
        self.store.stop(entry)
        self.now += 100
        self.store.stop(entry)
        result = self.store.snapshot()
        self.assertEqual(result['total_seconds'], 65)
        self.assertEqual(result['running'], [])

    def test_running_timer_survives_new_store_instance(self):
        self.store.start(self.employee, 'Deploy')
        self.now += 400
        reopened = Store(self.path, lambda: self.now)
        self.assertEqual(reopened.snapshot()['total_seconds'], 400)
        self.assertEqual(len(reopened.snapshot()['running']), 1)

    def test_duplicate_timer_rejected_but_other_employee_can_work(self):
        self.store.start(self.employee, 'First')
        with self.assertRaises(Problem) as error:
            self.store.start(self.employee, 'Second')
        self.assertEqual(error.exception.status, 409)
        bob = self.store.add_employee('Bob')['id']
        self.store.start(bob, 'Other')
        self.assertEqual(len(self.store.snapshot()['running']), 2)

    def test_concurrent_starts_have_exactly_one_winner(self):
        def start(_):
            try:
                self.store.start(self.employee, 'Concurrent')
                return 201
            except Problem as error:
                return error.status
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(start, range(8)))
        self.assertEqual(statuses.count(201), 1)
        self.assertEqual(statuses.count(409), 7)

    def test_midnight_overlap_clipped_to_inclusive_dates(self):
        entry = self.store.start(self.employee, 'Night shift')['id']
        self.now += 7200
        self.store.stop(entry)
        first = self.store.snapshot({'from': '2026-01-01', 'to': '2026-01-01'})
        second = self.store.snapshot({'from': '2026-01-02', 'to': '2026-01-02'})
        self.assertEqual(first['total_seconds'], 1800)
        self.assertEqual(second['total_seconds'], 5400)
        self.assertEqual(second['entries'][0]['elapsed_seconds'], 7200)

    def test_running_timer_is_clipped_to_period(self):
        self.store.start(self.employee, 'Night shift')
        self.now += 7200
        result = self.store.snapshot({'from': '2026-01-02', 'to': '2026-01-02'})
        self.assertEqual(result['total_seconds'], 5400)

    def test_employee_filter(self):
        bob = self.store.add_employee('Bob')['id']
        self.store.start(self.employee, 'A')
        self.now += 10
        self.store.start(bob, 'B')
        self.now += 10
        result = self.store.snapshot({'employee_id': str(bob)})
        self.assertEqual(result['total_seconds'], 10)
        self.assertEqual(len(result['entries']), 1)
        self.assertEqual(result['summary'][0]['name'], 'Bob')

    def test_validation_and_missing_records(self):
        for value in ['', '   ', 'a' * 81, None, 42]:
            with self.subTest(value=value), self.assertRaises(Problem):
                self.store.add_employee(value)
        with self.assertRaises(Problem):
            self.store.add_employee('alice')
        for employee, description in [(999, 'Task'), (True, 'Task'), (self.employee, ''), (self.employee, 'x' * 501)]:
            with self.subTest(employee=employee), self.assertRaises(Problem):
                self.store.start(employee, description)
        with self.assertRaises(Problem):
            self.store.stop(999)

    def test_bad_periods(self):
        for params in [{'from': 'bad'}, {'from': '2026-01-03', 'to': '2026-01-01'}, {'employee_id': 'NaN'}, {'to': '9999-12-31'}]:
            with self.subTest(params=params), self.assertRaises(Problem):
                self.store.snapshot(params)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'db.sqlite3')
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.store))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def call(self, method, path, data=None, headers=None):
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        body = json.dumps(data) if data is not None else None
        headers = headers or {'Content-Type': 'application/json'}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        result = response.status, response.read(), dict(response.getheaders())
        connection.close()
        return result

    def test_full_http_workflow(self):
        status, body, _ = self.call('POST', '/api/employees', {'name': 'Tester'})
        self.assertEqual(status, 201)
        employee = json.loads(body)['id']
        status, body, _ = self.call('POST', '/api/timers/start', {'employee_id': employee, 'description': 'HTTP task'})
        self.assertEqual(status, 201)
        entry = json.loads(body)['id']
        self.assertEqual(self.call('POST', '/api/timers/stop', {'entry_id': entry})[0], 200)
        status, body, _ = self.call('GET', '/api/state')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['entries'][0]['description'], 'HTTP task')

    def test_static_routes_and_path_traversal(self):
        for path in ['/', '/app.js', '/style.css']:
            status, body, headers = self.call('GET', path)
            self.assertEqual(status, 200)
            self.assertTrue(body)
            self.assertIn('Content-Security-Policy', headers)
        self.assertEqual(self.call('GET', '/../../app.py')[0], 404)

    def test_invalid_payload_and_cross_origin(self):
        self.assertEqual(self.call('POST', '/api/employees', [1, 2])[0], 400)
        self.assertEqual(self.call('POST', '/api/employees', {'name': 'A'}, {'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.call('POST', '/api/employees', {'name': 'A'}, {'Content-Type': 'application/json', 'Origin': 'https://other.example'})[0], 403)
        self.assertEqual(self.call('GET', '/api/state?from=not-a-date')[0], 400)

    def test_csv_quotes_and_formula_protection(self):
        employee = self.store.add_employee('=SUM(1,2)')['id']
        self.store.start(employee, 'Task, with "quotes"\nand newline')
        status, body, headers = self.call('GET', '/api/export.csv')
        self.assertEqual(status, 200)
        self.assertIn("'=SUM(1,2)", body.decode('utf-8-sig'))
        self.assertIn('""quotes""', body.decode('utf-8-sig'))
        self.assertIn('attachment', headers['Content-Disposition'])


if __name__ == '__main__':
    unittest.main()
