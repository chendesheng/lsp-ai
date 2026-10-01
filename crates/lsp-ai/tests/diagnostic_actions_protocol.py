#!/usr/bin/env python3
"""Offline stdio LSP + mock HTTP test. Run with the path to a built lsp-ai binary."""
import json
import os
import queue
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

received = []
slow_started = threading.Event()
slow_release = threading.Event()


class Model(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if request['messages'][-1]['content'] == 'Legacy':
            data = json.dumps({'choices': [{'message': {'role': 'assistant', 'content': 'legacy insert'}}]}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        payload = json.loads(request['messages'][-1]['content'])
        received.append(payload)
        assert payload['diagnostic']['source'] == 'typescript'
        assert 'foo' in payload['code']
        if payload['diagnostic']['message'] == 'Slow error':
            slow_started.set()
            assert slow_release.wait(10)
        if 'Return ONLY a JSON object' in request['messages'][0]['content']:
            response = json.dumps({'edits': [{'old_text': 'foo()', 'new_text': 'bar()'}]})
        else:
            response = 'Declare foo before calling it.'
        data = json.dumps({'choices': [{'message': {'role': 'assistant', 'content': response}}]}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main(binary, enabled=True):
    server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = os.environ.copy()
    env.pop('SSLKEYLOGFILE', None)
    env['LSP_AI_LOG'] = 'error'
    child = subprocess.Popen([binary, '--stdio'], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    messages = queue.Queue()
    def reader():
        try:
            while True:
                headers = {}
                while True:
                    line = child.stdout.readline()
                    if not line:
                        raise EOFError('lsp-ai closed stdout')
                    if line in (b'\r\n', b'\n'):
                        break
                    name, value = line.decode().strip().split(':', 1)
                    headers[name.lower()] = value.strip()
                messages.put(json.loads(child.stdout.read(int(headers['content-length']))))
        except Exception as error:
            messages.put(error)
    threading.Thread(target=reader, daemon=True).start()
    notifications = []
    sequence = 0
    def send(method, params, request=False):
        nonlocal sequence
        message = {'jsonrpc': '2.0', 'method': method, 'params': params}
        if request:
            sequence += 1
            message['id'] = sequence
        data = json.dumps(message).encode()
        child.stdin.write(f'Content-Length: {len(data)}\r\n\r\n'.encode() + data)
        child.stdin.flush()
        return sequence
    def response(identifier):
        while True:
            message = messages.get(timeout=15)
            if isinstance(message, Exception):
                raise message
            if 'method' in message:
                notifications.append(message)
                continue
            assert message['id'] == identifier, message
            return message
    def call(method, params):
        message = response(send(method, params, True))
        assert 'error' not in message, message
        return message['result']
    uri = 'file:///diagnostic-action-test.ts'
    error_range = {'start': {'line': 0, 'character': 0}, 'end': {'line': 0, 'character': 3}}
    error = {'range': error_range, 'message': 'Unknown foo', 'source': 'typescript', 'severity': 1}
    other = dict(error, message='Another foo error')
    def actions(diagnostics, only=None):
        context = {'diagnostics': diagnostics}
        if only is not None:
            context['only'] = only
        result = call('textDocument/codeAction', {'textDocument': {'uri': uri},
                    'range': error_range, 'context': context})
        assert any(action['title'] == 'Legacy action' for action in result)
        return [action for action in result if action['title'] != 'Legacy action']
    def change(version):
        send('textDocument/didChange', {'textDocument': {'uri': uri, 'version': version},
                                      'contentChanges': [{'text': 'foo();\n// changed\n'}]})
    try:
        configuration = {
            'memory': {'file_store': {}},
            'models': {'test': {'type': 'open_ai', 'model': 'mock', 'auth_token': 'not-a-secret',
                       'chat_endpoint': f'http://127.0.0.1:{server.server_port}/chat/completions'}},
            'actions': [{'model': 'test', 'action_display_name': 'Legacy action',
                         'parameters': {'messages': [{'role': 'user', 'content': 'Legacy'}]},
                         'post_process': {'remove_duplicate_start': False, 'remove_duplicate_end': False}}]}
        if enabled:
            configuration['diagnostic_actions'] = {'model': 'test'}
        call('initialize', {'capabilities': {}, 'initializationOptions': configuration})
        send('initialized', {})
        send('textDocument/didOpen', {'textDocument': {'uri': uri, 'languageId': 'typescript',
                                                    'version': 1, 'text': 'foo();\n'}})
        assert actions([]) == []
        legacy = call('textDocument/codeAction', {'textDocument': {'uri': uri},
                      'range': error_range, 'context': {'diagnostics': []}})[0]
        legacy_result = call('codeAction/resolve', legacy)
        assert legacy_result['edit']['changes'][uri][0]['newText'] == 'legacy insert'
        if not enabled:
            assert actions([error]) == []
            call('shutdown', None)
            send('exit', None)
            child.wait(timeout=10)
            assert child.returncode == 0
            print('PASS: feature omitted; legacy action listing and resolution unchanged')
            return
        listed = actions([error, other, dict(error, severity=2)])
        assert [action['title'] for action in listed] == [
            'Explain: Unknown foo', 'Fix: Unknown foo',
            'Explain: Another foo error', 'Fix: Another foo error']
        assert received == [], 'Listing actions must not call the model'
        assert actions([error], ['refactor']) == []
        assert len(actions([error], ['quickfix'])) == 2
        explained = call('codeAction/resolve', listed[0])
        assert 'edit' not in explained
        published = notifications[-1]['params']
        assert published['version'] == 1
        assert published['diagnostics'][0]['range'] == error_range
        assert published['diagnostics'][0]['source'] == 'lsp-ai'
        assert 'Declare foo' in published['diagnostics'][0]['message']
        assert actions(published['diagnostics']) == []
        call('codeAction/resolve', listed[2])
        assert len(notifications[-1]['params']['diagnostics']) == 2
        call('codeAction/resolve', listed[0])
        assert len(notifications[-1]['params']['diagnostics']) == 2
        fixed = call('codeAction/resolve', listed[1])
        edit = fixed['edit']['documentChanges'][0]
        assert edit['textDocument'] == {'uri': uri, 'version': 1}
        assert edit['edits'][0]['newText'] == 'bar()'
        assert edit['edits'][0]['range']['end']['character'] == 5
        change(2)
        actions([])  # Barrier: didChange has been processed.
        assert notifications[-1]['params']['diagnostics'] == []
        count = len(received)
        stale = response(send('codeAction/resolve', listed[1], True))
        assert stale['error']['code'] == -32801
        assert len(received) == count
        slow_action = actions([dict(error, message='Slow error')])[1]
        pending = send('codeAction/resolve', slow_action, True)
        assert slow_started.wait(10)
        change(3)
        # A request processed after the change acts as a barrier, but resolves first.
        barrier = send('textDocument/codeAction', {'textDocument': {'uri': uri},
                       'range': error_range, 'context': {'diagnostics': []}}, True)
        assert [action['title'] for action in response(barrier)['result']] == ['Legacy action']
        slow_release.set()
        assert response(pending)['error']['code'] == -32801
        fresh = actions([error])[0]
        call('codeAction/resolve', fresh)
        send('textDocument/didClose', {'textDocument': {'uri': uri}})
        actions([])
        assert notifications[-1]['params']['diagnostics'] == []
        call('shutdown', None)
        send('exit', None)
        child.wait(timeout=10)
        assert child.returncode == 0
        print('PASS: dynamic lists, diagnostic publication/retention/cleanup, versioned fixes, stale and in-flight rejection, shutdown')
    finally:
        slow_release.set()
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        server.shutdown()


if __name__ == '__main__':
    main(sys.argv[1], enabled='--disabled' not in sys.argv[2:])
