#!/usr/bin/env python3
"""Offline instruction-rewrite protocol test; uses the shared stdio client."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from refactor_actions_protocol import LspClient, apply_edits


def main(binary):
    requests=[]
    slow=threading.Event()
    started=threading.Event()
    release=threading.Event()

    class Model(BaseHTTPRequestHandler):
        def log_message(self,*_):
            pass

        def do_POST(self):
            request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            payload=json.loads(request['messages'][-1]['content'])
            requests.append(payload)
            assert payload['instruction'].startswith(('rewrite','implement'))
            assert payload['target_range']['start']=={'line':payload['selected_text'].count('\n') if payload['mode']=='implement' else payload['selected_text'].count('\n')-1,'character':0}
            assert 'instruction field' in request['messages'][0]['content']
            if slow.is_set():
                started.set()
                assert release.wait(10)
            replacement='const add = (a: number, b: number): number => a + b;\n' if payload['language']=='typescript' else 'doubleEvens xs = [x * 2 | x <- xs, even x]\n'
            if payload['mode']=='implement':
                replacement="visitors.sort((a, b) => a.status.localeCompare(b.status));\n"
            response=json.dumps({'replacement':replacement})
            data=json.dumps({'choices':[{'message':{'role':'assistant','content':response}}]}).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server=ThreadingHTTPServer(('127.0.0.1',0),Model)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    client=LspClient([binary,'--stdio'])
    try:
        config={'memory':{'file_store':{}},'models':{'test':{'type':'open_ai','model':'mock','auth_token':'not-a-secret','chat_endpoint':f'http://127.0.0.1:{server.server_port}/chat/completions'}},'refactor_actions':{'model':'test'}}
        client.call('initialize',{'capabilities':{},'initializationOptions':config})
        client.send('initialized',{})
        for language,comment,target,suffix in [
            ('typescript','// rewrite use arrow function','function add(a: number, b: number): number { return a + b; }\n','// untouched\n'),
            ('haskell','-- rewrite use list comprehension\n-- keep the function name','doubleEvens xs = map (* 2) (filter even xs)\n','main = print (doubleEvens [1..6])\n'),
            ('typescript','// implement sort by visitor.status\n// ascending, in place','','console.log(visitors);\n')]:
            uri=f'file:///instruction.{"ts" if language=="typescript" else "hs"}'
            original=comment+'\n'+target+suffix
            region={'start':{'line':0,'character':0},'end':{'line':original[:len(comment)+1+len(target)].count('\n'),'character':0}}
            def actions(only=None):
                context={'diagnostics':[]}
                if only is not None:
                    context['only']=only
                return client.call('textDocument/codeAction',{'textDocument':{'uri':uri},'range':region,'context':context})
            client.send('textDocument/didOpen',{'textDocument':{'uri':uri,'languageId':language,'version':1,'text':original}})
            before=len(requests)
            listed=actions()
            expected = ['Refactor: Follow instruction','Refactor: Extract function'] if target else ['Implement: Follow instruction']
            assert [a['title'] for a in listed]==expected
            assert len(requests)==before
            assert actions(['quickfix'])==[]
            action=actions(['refactor.rewrite' if target else 'source.generate'])[0]
            assert action['kind']==('refactor.rewrite.instruction' if target else 'source.generate.instruction')
            resolved=client.call('codeAction/resolve',action)
            document=resolved['edit']['documentChanges'][0]
            assert len(document['edits'])==1
            assert document['edits'][0]['range']=={'start':{'line':comment.count('\n')+1,'character':0},'end':region['end']}
            updated=apply_edits(original,document,uri,1)
            assert updated.startswith(comment+'\n') and updated.endswith(suffix)
            assert requests[-1]['selected_text']==comment+'\n'+target
            assert requests[-1]['target_text']==target
            # Before-generation stale action: no additional model request.
            client.send('textDocument/didChange',{'textDocument':{'uri':uri,'version':2},'contentChanges':[{'text':original}]})
            before=len(requests)
            assert client.response(client.send('codeAction/resolve',action,True))['error']['code']==-32801
            assert len(requests)==before
            # In-flight modification must also reject the replacement.
            slow.set();started.clear();release.clear()
            action=actions(['refactor.rewrite' if target else 'source.generate'])[0]
            identifier=client.send('codeAction/resolve',action,True)
            assert started.wait(10)
            client.send('textDocument/didChange',{'textDocument':{'uri':uri,'version':3},'contentChanges':[{'text':original}]})
            actions(['refactor.rewrite' if target else 'source.generate'])
            release.set()
            assert client.response(identifier)['error']['code']==-32801
            slow.clear()
            client.send('textDocument/didClose',{'textDocument':{'uri':uri}})
        assert not client.notifications
        print('PASS: TS/Haskell comment instructions, exact target/context payload, multiple-comment retention and comment-only implementation, scoped versioned edits, listing without model calls, kind filters and stale/in-flight rejection')
    finally:
        release.set()
        client.close()
        server.shutdown();server.server_close()


if __name__=='__main__':
    main(sys.argv[1])
