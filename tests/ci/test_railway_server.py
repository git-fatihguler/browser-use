import importlib.util
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_railway_server(monkeypatch):
	# The module reads BU_API_TOKEN at import time; clear it so /run is locked down deterministically
	monkeypatch.delenv('BU_API_TOKEN', raising=False)
	spec = importlib.util.spec_from_file_location('railway_server_under_test', ROOT / 'railway_server.py')
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def test_railway_server_serves_panel_health_and_locked_down_run(monkeypatch):
	railway_server = _load_railway_server(monkeypatch)
	server = railway_server.Server(('127.0.0.1', 0), railway_server.Handler)
	threading.Thread(target=server.serve_forever, daemon=True).start()
	base = f'http://127.0.0.1:{server.server_address[1]}'

	try:
		with urllib.request.urlopen(f'{base}/', timeout=5) as response:
			assert response.status == 200
			assert 'text/html' in response.headers['Content-Type']
			assert b'browser-use' in response.read()

		with urllib.request.urlopen(f'{base}/health', timeout=5) as response:
			assert json.loads(response.read()) == {'ok': True, 'busy': False}

		run_request = urllib.request.Request(
			f'{base}/run',
			data=json.dumps({'task': 'noop'}).encode('utf-8'),
			headers={'Content-Type': 'application/json'},
			method='POST',
		)
		try:
			urllib.request.urlopen(run_request, timeout=5)
			raise AssertionError('expected /run to reject requests while BU_API_TOKEN is unset')
		except urllib.error.HTTPError as error:
			assert error.code == 401

		try:
			urllib.request.urlopen(f'{base}/nope', timeout=5)
			raise AssertionError('expected an unknown route to 404')
		except urllib.error.HTTPError as error:
			assert error.code == 404
			assert json.loads(error.read())['routes'] == ['GET /', 'GET /health', 'POST /run']
	finally:
		server.shutdown()
		server.server_close()
