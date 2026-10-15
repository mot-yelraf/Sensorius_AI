"""Verify bounded, read-only Ecowitt recovery workers.

Tests use simulated transports and subprocess replies; no gateway is required.
"""

import io
import json
import subprocess
from types import SimpleNamespace

import pytest

from sensorius import saiEcowittRecovery as recovery


@pytest.mark.parametrize('stdout,returncode', [
    ('', 1), ('not json', 0), ('[]', 0), ('{"error":"offline"}', 0),
    ('x' * (recovery.MAX_RESPONSE_BYTES + 1), 0),
])
def test_worker_failures_are_reported(monkeypatch, stdout, returncode):
    monkeypatch.setattr(recovery.subprocess, 'run', lambda *a, **k: SimpleNamespace(
        stdout=stdout, returncode=returncode))
    with pytest.raises((ValueError, RuntimeError)):
        recovery.request_in_fresh_process('http://gw.local', 'get_livedata_info', {})


def test_worker_uses_current_interpreter_and_hard_deadline(monkeypatch):
    def run(args, **kwargs):
        assert args == [recovery.sys.executable, '-m', 'sensorius.saiEcowittRecovery']
        assert kwargs['timeout'] == 8
        assert kwargs['capture_output']
        assert (kwargs['cwd'] / 'sensorius/saiEcowittRecovery.py').is_file()
        assert json.loads(kwargs['input'])['params'] == {'page': 2}
        return SimpleNamespace(stdout='{"payload":[]}', returncode=0)
    monkeypatch.setattr(recovery.subprocess, 'run', run)
    assert recovery.request_in_fresh_process('http://gw.local', 'get_sensors_info', {'page': 2}) == []


def test_worker_deadline_propagates(monkeypatch):
    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired('worker', 8)
    monkeypatch.setattr(recovery.subprocess, 'run', run)
    with pytest.raises(subprocess.TimeoutExpired):
        recovery.request_in_fresh_process('http://gw.local', 'get_livedata_info', {})


@pytest.mark.parametrize('endpoint', ['parse_quick_cmd_iot', 'set_network_info'])
def test_parent_and_child_reject_write_endpoints(monkeypatch, capsys, endpoint):
    with pytest.raises(ValueError):
        recovery.request_in_fresh_process('http://gw.local', endpoint, {})
    monkeypatch.setattr(recovery.sys, 'stdin', io.StringIO(json.dumps({
        'base_url': 'http://gw.local', 'endpoint': endpoint, 'params': {}})))
    recovery.main()
    assert 'Unsupported' in json.loads(capsys.readouterr().out)['error']


@pytest.mark.parametrize('oversized', [False, True])
def test_worker_bounds_response_and_closes_transport(monkeypatch, capsys, oversized):
    closed = []

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            closed.append(True)
        def raise_for_status(self):
            pass
        def iter_bytes(self, **kwargs):
            yield b'x' * (recovery.MAX_RESPONSE_BYTES + 1) if oversized else b'{"rain":[]}'

    def stream(method, url, **kwargs):
        assert method == 'GET'
        assert url == 'http://gw.local/get_livedata_info'
        assert kwargs['timeout'] == 5
        assert not kwargs['follow_redirects']
        return Response()
    monkeypatch.setattr(recovery.httpx, 'stream', stream)
    monkeypatch.setattr(recovery.sys, 'stdin', io.StringIO(json.dumps({
        'base_url': 'http://gw.local', 'endpoint': 'get_livedata_info', 'params': {}})))
    recovery.main()
    reply = json.loads(capsys.readouterr().out)
    assert closed == [True]
    if oversized:
        assert 'unexpectedly large' in reply['error']
    else:
        assert reply['payload'] == {'rain': []}


def test_installed_worker_rejects_commands_without_network_access():
    result = subprocess.run(
        [recovery.sys.executable, '-m', 'sensorius.saiEcowittRecovery'],
        input=json.dumps({'base_url': 'http://gw.local',
                          'endpoint': 'parse_quick_cmd_iot', 'params': {}}),
        capture_output=True, text=True, timeout=8,
    )
    assert result.returncode == 0
    assert 'Unsupported' in json.loads(result.stdout)['error']
