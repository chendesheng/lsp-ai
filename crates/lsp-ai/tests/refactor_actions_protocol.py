#!/usr/bin/env python3
"""Offline stdio LSP + mock HTTP refactor regression test; no API key needed."""
import json
import os
import queue
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SOURCE = ('function compute(a: number, b: number): number {\n'
          '  const sum = a + b;\n  return sum * 2;\n}\nconsole.log(compute(3, 4));\n')
SELECTED = '  const sum = a + b;\n  return sum * 2;\n'
RANGE = {'start': {'line': 1, 'character': 0}, 'end': {'line': 3, 'character': 0}}


class LspClient:
    def __init__(self, command, environment=None):
        env = os.environ.copy()
        env.pop('SSLKEYLOGFILE', None)
        env['LSP_AI_LOG'] = 'error'
        env.update(environment or {})
        self.child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, env=env)
        self.messages = queue.Queue()
        self.notifications = []
        self.sequence = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            while True:
                headers = {}
                while True:
                    line = self.child.stdout.readline()
                    if not line:
                        raise EOFError('LSP stdout closed')
                    if line in (b'\r\n', b'\n'):
                        break
                    name, value = line.decode().strip().split(':', 1)
                    headers[name.lower()] = value.strip()
                self.messages.put(json.loads(self.child.stdout.read(int(headers['content-length']))))
        except Exception as error:
            self.messages.put(error)

    def send(self, method, params, request=False):
        value = {'jsonrpc': '2.0', 'method': method, 'params': params}
        if request:
            self.sequence += 1
            value['id'] = self.sequence
        data = json.dumps(value).encode()
        self.child.stdin.write(f'Content-Length: {len(data)}\r\n\r\n'.encode() + data)
        self.child.stdin.flush()
        return self.sequence

    def response(self, identifier):
        while True:
            message = self.messages.get(timeout=35)
            if isinstance(message, Exception):
                raise message
            if 'method' in message:
                self.notifications.append(message)
                continue
            assert message['id'] == identifier, message
            return message

    def call(self, method, params):
        message = self.response(self.send(method, params, True))
        assert 'error' not in message, message
        return message['result']

    def close(self):
        try:
            if self.child.poll() is None:
                self.call('shutdown', None)
                self.send('exit', None)
                self.child.wait(timeout=10)
                assert self.child.returncode == 0
        finally:
            if self.child.poll() is None:
                self.child.kill()
                self.child.wait()


def offset(source, position):
    lines = source.splitlines(keepends=True)
    if position['line'] == len(lines) and position['character'] == 0:
        return len(source)
    line = lines[position['line']]
    units = chars = 0
    for char in line:
        if units == position['character']:
            break
        units += len(char.encode('utf-16-le')) // 2
        chars += 1
    assert units == position['character']
    return sum(map(len, lines[:position['line']])) + chars


def apply_edits(source, document, uri, version):
    assert document['textDocument'] == {'uri': uri, 'version': version}
    edits = [(offset(source, edit['range']['start']), offset(source, edit['range']['end']), edit['newText'])
             for edit in document['edits']]
    for (start, end, _), (next_start, _, _) in zip(sorted(edits), sorted(edits)[1:]):
        assert end <= next_start
    result = source
    for start, end, new_text in sorted(edits, reverse=True):
        result = result[:start] + new_text + result[end:]
    return result


def main(binary, with_diagnostics=False):
    received = []
    slow_started, slow_release = threading.Event(), threading.Event()
    slow = threading.Event()

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            payload = json.loads(request['messages'][-1]['content'])
            assert payload['language'] == 'typescript'
            assert payload['selected_text'] == SELECTED
            assert payload['range'] == RANGE
            assert 'diagnostic' not in payload
            assert request['messages'][0]['role'] == 'system'
            assert 'fim' not in request
            received.append(payload)
            if slow.is_set():
                slow_started.set()
                assert slow_release.wait(10)
            response = json.dumps({'edits': [
                {'old_text': SELECTED, 'new_text': '  return doubledSum(a, b);\n'},
                {'old_text': '}\nconsole.log(compute(3, 4));',
                 'new_text': '}\nfunction doubledSum(a: number, b: number): number {\n'
                             '  const sum = a + b;\n  return sum * 2;\n}\nconsole.log(compute(3, 4));'}]})
            data = json.dumps({'choices': [{'message': {'role': 'assistant', 'content': response}}]}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = LspClient([binary, '--stdio'])
    uri = 'file:///refactor-action-test.ts'

    def open_file(version=1):
        client.send('textDocument/didOpen', {'textDocument': {'uri': uri, 'languageId': 'typescript',
                                                           'version': version, 'text': SOURCE}})

    def actions(region=RANGE, only=None, diagnostics=None):
        context = {'diagnostics': diagnostics or []}
        if only is not None:
            context['only'] = only
        return client.call('textDocument/codeAction', {'textDocument': {'uri': uri},
                                                      'range': region, 'context': context})

    def changed(version):
        client.send('textDocument/didChange', {'textDocument': {'uri': uri, 'version': version},
                    'contentChanges': [{'range': {'start': {'line': 0, 'character': 0},
                                                 'end': {'line': 0, 'character': 0}}, 'text': ''}]})

    try:
        config = {'memory': {'file_store': {}},
                  'models': {'test': {'type': 'open_ai', 'model': 'mock', 'auth_token': 'not-a-secret',
                             'chat_endpoint': f'http://127.0.0.1:{server.server_port}/chat/completions'}},
                  'refactor_actions': {'model': 'test', 'parameters': {'max_context': 1024}}}
        if with_diagnostics:
            config['diagnostic_actions'] = {'model': 'test'}
        client.call('initialize', {'capabilities': {}, 'initializationOptions': config})
        client.send('initialized', {})
        open_file()
        listed = actions()
        assert [action['title'] for action in listed] == ['Refactor: Extract function']
        assert listed[0]['kind'] == 'refactor.extract.function'
        assert received == [], 'Listing must not call the model'
        assert actions({'start': {'line': 0, 'character': 0}, 'end': {'line': 0, 'character': 1}}) == []
        assert actions({'start': {'line': 1, 'character': 0}, 'end': {'line': 1, 'character': 2}}) == []
        assert actions(only=['quickfix']) == []
        assert actions(only=['refactor.inline']) == []
        assert len(actions(only=['refactor.extract'])) == 1
        if with_diagnostics:
            error = {'range': RANGE, 'message': 'Example error', 'severity': 1, 'source': 'typescript'}
            assert [a['title'].split(':')[0] for a in actions(diagnostics=[error])] == ['Explain', 'Fix', 'Refactor']
        resolved = client.call('codeAction/resolve', listed[0])
        document = resolved['edit']['documentChanges'][0]
        assert len(document['edits']) == 2
        updated = apply_edits(SOURCE, document, uri, 1)
        assert 'return doubledSum(a, b);' in updated
        assert 'function doubledSum' in updated
        assert not client.notifications
        changed(2)
        error = client.response(client.send('codeAction/resolve', listed[0], True))
        assert error['error']['code'] == -32801
        assert len(received) == 1, 'Stale actions must not call the model'
        slow.set()
        action = actions()[0]
        identifier = client.send('codeAction/resolve', action, True)
        assert slow_started.wait(10)
        changed(3)
        # A later listing serves as a barrier: didChange has been processed before release.
        barrier = client.send('textDocument/codeAction', {'textDocument': {'uri': uri}, 'range': RANGE,
                                                        'context': {'diagnostics': []}}, True)
        client.response(barrier)
        slow_release.set()
        assert client.response(identifier)['error']['code'] == -32801
        slow.clear()
        old = actions()[0]
        client.send('textDocument/didClose', {'textDocument': {'uri': uri}})
        open_file(3)
        assert client.response(client.send('codeAction/resolve', old, True))['error']['code'] == -32801
        old = actions()[0]
        new_uri = 'file:///renamed-refactor-test.ts'
        client.send('workspace/didRenameFiles', {'files': [{'oldUri': uri, 'newUri': new_uri}]})
        assert client.response(client.send('codeAction/resolve', old, True))['error']['code'] == -32801
        uri = new_uri
        assert len(actions()) == 1
        client.close()
        print('PASS: refactor listing, exact selection, multiple versioned edits, incremental change, '
              'stale/in-flight rejection, close/reopen and rename' + ('; Explain/Fix coexist' if with_diagnostics else '; refactor-only config'))
    finally:
        slow_release.set()
        client.close()
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main(sys.argv[1], '--with-diagnostics' in sys.argv[2:])
