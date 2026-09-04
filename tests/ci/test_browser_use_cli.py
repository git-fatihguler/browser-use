import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run_browser_use_cli(
	*args: str,
	env_overrides: dict[str, str] | None = None,
	stdin_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
	env = os.environ.copy()
	# Make the container-service detection deterministic regardless of the host running the tests
	env.pop('IN_DOCKER', None)
	env.pop('PORT', None)
	env['PYTHONPATH'] = os.pathsep.join(part for part in (str(ROOT), env.get('PYTHONPATH', '')) if part)
	env.update(env_overrides or {})
	return subprocess.run(
		[sys.executable, '-m', 'browser_use.cli', *args],
		cwd=ROOT,
		env=env,
		input=stdin_text,
		capture_output=True,
		text=True,
		timeout=20,
	)


def test_browser_use_doctor_help_prints_browser_use_usage():
	result = _run_browser_use_cli('doctor', '--help')

	assert result.returncode == 0
	assert result.stdout == 'usage: browser-use doctor [--fix-snap]\n'
	assert result.stderr == ''


def test_empty_stdin_prints_usage_error():
	result = _run_browser_use_cli(stdin_text='')

	assert result.returncode == 1
	assert 'received empty stdin' in result.stderr
	assert 'railway_server.py' not in result.stderr


def test_empty_stdin_in_container_service_points_at_http_wrapper():
	result = _run_browser_use_cli(stdin_text='', env_overrides={'IN_DOCKER': 'True', 'PORT': '8000'})

	assert result.returncode == 1
	assert 'received empty stdin' in result.stderr
	assert 'python /app/railway_server.py' in result.stderr


def test_railway_config_pins_the_http_wrapper_start_command():
	import json

	config = json.loads((ROOT / 'railway.json').read_text())

	assert config['build']['builder'] == 'DOCKERFILE'
	assert config['deploy']['startCommand'] == 'python /app/railway_server.py'
	assert config['deploy']['healthcheckPath'] == '/health'
	# The start command must point at a file that actually ships in the image (COPY . /app)
	assert (ROOT / 'railway_server.py').exists()
	assert 'CMD ["python", "/app/railway_server.py"]' in (ROOT / 'Dockerfile').read_text()


def test_normalize_captured_cli_output_handles_string_system_exit(capsys):
	from browser_use.cli import _normalize_captured_cli_output

	def exits_with_string(_argv):
		raise SystemExit('browser-harness failed')

	assert _normalize_captured_cli_output(exits_with_string, []) == 1
	captured = capsys.readouterr()
	assert captured.out == ''
	assert captured.err == 'browser-use failed\n'


def test_browser_use_tui_is_deprecated_alias(monkeypatch, capsys):
	import browser_use.cli as browser_use_cli

	monkeypatch.setattr(browser_use_cli, 'main', lambda: 0)

	assert browser_use_cli.browser_use_tui_main() == 0
	assert capsys.readouterr().err == 'browser-use-tui is deprecated; use browser-use instead.\n'
