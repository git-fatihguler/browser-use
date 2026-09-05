"""Minimal HTTP wrapper around the browser-use Agent, so this image can run as a Railway service.

The bundled `browser-use` CLI reads Python from stdin and the MCP server speaks stdio, so neither
stays up as a web service. This module exposes the Agent over HTTP instead:

	GET  /                                -> minimal HTML panel to submit tasks from a browser
	GET  /health                          -> {"ok": true, "busy": false}
	POST /run  {"task": "...", ...}       -> {"result": "...", "success": true, ...}

Stdlib only on purpose: fastapi/uvicorn are dev-dependencies in this repo and the image is built
with `uv sync --no-dev`, so they are not installed at runtime.

Environment:
	PORT                  port to bind (Railway sets this; defaults to 8000)
	BU_API_TOKEN          shared secret required as `Authorization: Bearer <token>` on /run
	BROWSER_USE_API_KEY   LLM credentials, or set DEFAULT_LLM plus the matching provider key
	BU_MAX_STEPS          default step budget per task (25)
	BU_MAX_STEPS_LIMIT    hard cap a request may ask for (100)
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

logging.basicConfig(level=os.getenv('BU_LOG_LEVEL', 'INFO').upper(), format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('railway_server')

API_TOKEN = os.getenv('BU_API_TOKEN', '')
DEFAULT_MAX_STEPS = int(os.getenv('BU_MAX_STEPS', '25'))
MAX_STEPS_LIMIT = int(os.getenv('BU_MAX_STEPS_LIMIT', '100'))
MAX_BODY_BYTES = 64 * 1024

# One agent at a time: each run drives its own Chromium, which is the memory hog in this container.
_run_lock = threading.Lock()

# Served at GET / so a browser visit gets a usable panel instead of a 404. The API token is typed
# into the form by the operator and only lives in that browser tab; it is never embedded here.
_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>browser-use</title>
<style>
:root { color-scheme: dark; }
body { margin: 0; font: 15px/1.5 system-ui, sans-serif; background: #0f1115; color: #e6e6e6; display: flex; justify-content: center; }
main { width: min(680px, 92vw); padding: 48px 0 64px; }
h1 { font-size: 22px; margin: 0 0 4px; }
h1 span { color: #f97316; }
p.sub { color: #9aa0a6; margin: 0 0 8px; }
#status { font-size: 13px; margin-bottom: 24px; }
.ok { color: #4ade80; } .bad { color: #f87171; }
label { display: block; font-size: 13px; color: #9aa0a6; margin: 16px 0 6px; }
input, textarea { width: 100%; box-sizing: border-box; background: #181b22; color: #e6e6e6; border: 1px solid #2a2f3a; border-radius: 8px; padding: 10px 12px; font: inherit; }
textarea { min-height: 96px; resize: vertical; }
input:focus, textarea:focus { outline: none; border-color: #f97316; }
button { margin-top: 20px; background: #f97316; border: none; color: #111; font: 600 15px system-ui, sans-serif; padding: 10px 22px; border-radius: 8px; cursor: pointer; }
button:disabled { opacity: .5; cursor: default; }
pre { background: #181b22; border: 1px solid #2a2f3a; border-radius: 8px; padding: 14px; white-space: pre-wrap; word-break: break-word; margin-top: 20px; min-height: 40px; }
</style>
</head>
<body>
<main>
<h1><span>browser-use</span> task runner</h1>
<p class="sub">Runs one browser agent task at a time on this container.</p>
<div id="status">checking health&hellip;</div>
<form id="form">
<label for="token">API token (the BU_API_TOKEN service variable)</label>
<input id="token" type="password" autocomplete="off" placeholder="Bearer token">
<label for="task">Task</label>
<textarea id="task" placeholder="Go to news.ycombinator.com and list the top 3 headlines"></textarea>
<label for="steps">Max steps</label>
<input id="steps" type="number" value="25" min="1" max="100">
<button id="go" type="submit">Run task</button>
</form>
<pre id="out">Result will appear here.</pre>
</main>
<script>
const statusEl = document.getElementById('status');
const out = document.getElementById('out');
const go = document.getElementById('go');
async function health() {
	try {
		const res = await fetch('/health');
		const data = await res.json();
		statusEl.innerHTML = data.busy
			? '<span class="bad">&#9679;</span> busy: a task is running'
			: '<span class="ok">&#9679;</span> healthy and idle';
	} catch (err) {
		statusEl.innerHTML = '<span class="bad">&#9679;</span> health check failed';
	}
}
health();
setInterval(health, 5000);
document.getElementById('form').addEventListener('submit', async (event) => {
	event.preventDefault();
	const body = {
		task: document.getElementById('task').value,
		max_steps: parseInt(document.getElementById('steps').value, 10) || 25,
	};
	go.disabled = true;
	out.textContent = 'Running... this can take a few minutes.';
	try {
		const res = await fetch('/run', {
			method: 'POST',
			headers: {
				'Content-Type': 'application/json',
				'Authorization': 'Bearer ' + document.getElementById('token').value,
			},
			body: JSON.stringify(body),
		});
		out.textContent = JSON.stringify(await res.json(), null, 2);
	} catch (err) {
		out.textContent = 'Request failed: ' + err;
	} finally {
		go.disabled = false;
		health();
	}
});
</script>
</body>
</html>
"""


def _log_bind_target(port: int) -> str:
	return f'0.0.0.0:{port}'


def _browser_profile() -> Any:
	from browser_use import BrowserProfile

	kwargs: dict[str, Any] = {'headless': os.getenv('BROWSER_USE_HEADLESS', 'true').lower() not in ('false', '0', 'no')}
	executable = os.getenv('BROWSER_USE_EXECUTABLE_PATH') or ('/usr/bin/chromium' if Path('/usr/bin/chromium').exists() else None)
	if executable:
		kwargs['executable_path'] = executable
	return BrowserProfile(**kwargs)


async def _run_task(task: str, max_steps: int) -> dict[str, Any]:
	from browser_use import Agent

	# llm=None lets browser-use resolve the model itself: DEFAULT_LLM env var, else ChatBrowserUse().
	agent = Agent(task=task, browser_profile=_browser_profile())
	try:
		history = await agent.run(max_steps=max_steps)
		return {
			'result': history.final_result(),
			'success': history.is_successful(),
			'done': history.is_done(),
			'steps': history.number_of_steps(),
			'duration_s': round(history.total_duration_seconds(), 1),
			'urls': [url for url in history.urls() if url],
			'errors': [error for error in history.errors() if error],
		}
	finally:
		await agent.close()


class Handler(BaseHTTPRequestHandler):
	server_version = 'browser-use-railway'
	protocol_version = 'HTTP/1.1'

	def _send_json(self, status: int, payload: dict[str, Any]) -> None:
		body = json.dumps(payload).encode('utf-8')
		self.send_response(status)
		self.send_header('Content-Type', 'application/json; charset=utf-8')
		self.send_header('Content-Length', str(len(body)))
		self.end_headers()
		self.wfile.write(body)

	def _authorized(self) -> bool:
		# The Railway URL is public, and every run spends LLM credits, so an unset token locks /run down.
		if not API_TOKEN:
			return False
		return hmac.compare_digest(self.headers.get('Authorization', ''), f'Bearer {API_TOKEN}')

	def _read_body(self) -> dict[str, Any] | None:
		length = int(self.headers.get('Content-Length') or 0)
		if length <= 0 or length > MAX_BODY_BYTES:
			return None
		try:
			payload = json.loads(self.rfile.read(length).decode('utf-8'))
		except (ValueError, UnicodeDecodeError):
			return None
		return payload if isinstance(payload, dict) else None

	def _send_html(self, status: int, html: str) -> None:
		body = html.encode('utf-8')
		self.send_response(status)
		self.send_header('Content-Type', 'text/html; charset=utf-8')
		self.send_header('Content-Length', str(len(body)))
		self.end_headers()
		self.wfile.write(body)

	def do_GET(self) -> None:  # noqa: N802
		path = self.path.split('?')[0]
		if path == '/':
			self._send_html(200, _INDEX_HTML)
			return
		if path == '/health':
			self._send_json(200, {'ok': True, 'busy': _run_lock.locked()})
			return
		self._send_json(404, {'error': 'not found', 'routes': ['GET /', 'GET /health', 'POST /run']})

	def do_POST(self) -> None:  # noqa: N802
		if self.path.split('?')[0] != '/run':
			self._send_json(404, {'error': 'not found', 'routes': ['GET /', 'GET /health', 'POST /run']})
			return

		if not self._authorized():
			detail = 'set BU_API_TOKEN in the service variables' if not API_TOKEN else 'send Authorization: Bearer <BU_API_TOKEN>'
			self._send_json(401, {'error': 'unauthorized', 'detail': detail})
			return

		payload = self._read_body()
		if payload is None:
			self._send_json(400, {'error': 'expected a JSON object body of at most 64KB'})
			return

		task = payload.get('task')
		if not isinstance(task, str) or not task.strip():
			self._send_json(400, {'error': 'field "task" is required and must be a non-empty string'})
			return

		try:
			max_steps = int(payload.get('max_steps', DEFAULT_MAX_STEPS))
		except (TypeError, ValueError):
			self._send_json(400, {'error': 'field "max_steps" must be an integer'})
			return
		max_steps = max(1, min(max_steps, MAX_STEPS_LIMIT))

		if not _run_lock.acquire(blocking=False):
			self._send_json(409, {'error': 'busy', 'detail': 'another task is running; this service handles one at a time'})
			return

		try:
			logger.info(f'running task ({max_steps} steps max): {task[:200]}')
			result = asyncio.run(_run_task(task, max_steps))
		except Exception as exc:
			logger.exception('task failed')
			self._send_json(500, {'error': type(exc).__name__, 'detail': str(exc)})
			return
		finally:
			_run_lock.release()

		self._send_json(200, result)

	def log_message(self, format: str, *args: Any) -> None:
		logger.info(f'{self.address_string()} {format % args}')


class Server(ThreadingHTTPServer):
	daemon_threads = True
	allow_reuse_address = True


def main() -> None:
	port = int(os.getenv('PORT', '8000'))
	if not API_TOKEN:
		logger.error('BU_API_TOKEN is not set: /run will reject every request with 401. Set it in the Railway variables.')
	server = Server(('0.0.0.0', port), Handler)
	logger.info(f'browser-use HTTP service listening on {_log_bind_target(port)}')
	try:
		server.serve_forever()
	except KeyboardInterrupt:
		pass
	finally:
		server.server_close()


if __name__ == '__main__':
	main()
