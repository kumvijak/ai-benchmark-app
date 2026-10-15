"""Bounded diagnostic capture, shipped to the inference VM as a standalone script."""
from __future__ import annotations

import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import fcntl
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import time
import urllib.request
import zipfile

ROOT = Path('/tmp/aig-benchmark-control')
MAX_BUNDLE_BYTES = 32 * 1024 * 1024
MAX_TRACE_BYTES = 128 * 1024 * 1024
MAX_LOG_BYTES = 2 * 1024 * 1024


def trace_dir(port):
    return str(ROOT/f'traces-{int(port)}')


def summarize_trace(raw, filename):
    if filename.endswith('.gz'):
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            raw = stream.read(MAX_TRACE_BYTES + 1)
    if len(raw) > MAX_TRACE_BYTES:
        raise ValueError('Trace exceeds the 128 MiB analysis limit; inspect downloaded raw trace')
    data = json.loads(raw)
    events = data.get('traceEvents', []) if isinstance(data, dict) else data
    operations = Counter(); families = Counter(); duration = Counter()
    for event in events:
        name = event.get('name', '')
        elapsed = event.get('dur')
        if event.get('ph') != 'X' or not isinstance(elapsed, (int,float)) or elapsed <= 0:
            continue
        if '::' in name:
            families[name.split('::',1)[0]] += 1
        if name.startswith('zentorch::') and not re.search(r'prepack|reorder', name, re.I):
            operations[name] += 1
            duration[name] += elapsed
    return dict(zentorch_operations=dict(operations), inclusive_duration_us=dict(duration), operator_families=dict(families))


def http_call(port, route, payload=None, timeout=120):
    request = urllib.request.Request(f'http://127.0.0.1:{int(port)}{route}', method='POST',
        data=json.dumps(payload).encode() if payload is not None else b'', headers={'Content-Type':'application/json'})
    # Local diagnostics must never follow a proxy or an HTTP redirect elsewhere.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=timeout) as response:
        raw = response.read(2*1024*1024)
    return json.loads(raw) if payload is not None else None


def validate_identity(spec, root):
    evidence=json.loads((root/f'runtime-{spec["port"]}.json').read_text())
    if any(evidence.get(k) != spec[k] for k in ('server_pid','server_start_ticks')):
        raise RuntimeError('Runtime identity changed; restart the diagnostic capture')
    stat=Path(f'/proc/{spec["server_pid"]}/stat').read_text()
    if stat.rsplit(')',1)[1].split()[19] != spec['server_start_ticks']:
        raise RuntimeError('Runtime process exited or PID was reused')


def capture(spec, root=ROOT, call=http_call):
    port=int(spec['port']); concurrency=int(spec['concurrency']); requests=int(spec['requests'])
    if not 1 <= concurrency <= 16 or not concurrency <= requests <= 16 or not 1 <= int(spec['max_tokens']) <= 128:
        raise ValueError('Diagnostic capture requires concurrency 1–16, concurrency <= requests <= 16 and output tokens 1–128')
    root=Path(root); root.mkdir(parents=True,exist_ok=True)
    directory=root/f'traces-{port}'
    result=dict(diagnostic_only=True, ok=False, requests_completed=0, errors=[], warnings=[],
                runtime_identity={k:spec[k] for k in ('server_pid','server_start_ticks')},
                decoder_int8_execution='unverified', trace_files=[], trace_summaries=[])
    def inventory():
        return {p.name:(p.stat().st_mtime_ns,p.stat().st_size) for p in directory.glob('*.json*') if p.is_file() and not p.is_symlink()}
    with (root/f'trace-{port}.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            result['errors'].append('Another diagnostic capture holds this runtime lock')
            return result
        validate_identity(spec,root)
        state_path=root/f'trace-active-{port}.json'
        if state_path.exists():
            try:
                previous=json.loads(state_path.read_text())
            except ValueError:
                previous=result['runtime_identity']
            if previous==result['runtime_identity']:
                result['errors'].append('Previous profiler state is uncertain; restart runtime before another capture')
                return result
        directory.mkdir(mode=0o700,exist_ok=True)
        directory.chmod(0o700)
        body=dict(model=spec['model_ref'],prompt='Explain CPU inference briefly.',max_tokens=spec['max_tokens'],temperature=0)
        log_path=root/f'vllm-{port}.log'
        before=inventory(); log_offset=log_path.stat().st_size if log_path.exists() else 0
        start_attempted=False; stopped=False
        started=time.time()
        try:
            call(port,'/v1/completions',body,timeout=120)  # outside the measured capture
            before=inventory(); log_offset=log_path.stat().st_size if log_path.exists() else 0
            state_path.write_text(json.dumps(result['runtime_identity']))
            start_attempted=True
            call(port,'/start_profile',timeout=120)
            def inference(_):
                response=call(port,'/v1/completions',body,timeout=120)
                if not isinstance(response,dict) or not response.get('choices'):
                    raise RuntimeError('Inference returned no completion choices')
                return True
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures=[executor.submit(inference,index) for index in range(requests)]
                for future in as_completed(futures):
                    try:
                        result['requests_completed'] += int(future.result())
                    except Exception as exc:
                        result['errors'].append(str(exc)[:1000])
        except Exception as exc:
            result['errors'].append(str(exc)[:1000])
        finally:
            if start_attempted:
                try:
                    call(port,'/stop_profile',timeout=300)
                    stopped=True
                    state_path.unlink(missing_ok=True)
                except Exception as exc:
                    result['errors'].append('Profiler stop/flush failed; restart runtime before another capture: '+str(exc)[:800])
        result['profiler_stopped']=stopped
        result['duration_seconds']=round(time.time()-started,3)
        try:
            validate_identity(spec,root)
        except Exception as exc:
            result['errors'].append(str(exc)[:1000])
        files={}; budget=0
        for name,signature in sorted(inventory().items()):
            if before.get(name) == signature:
                continue
            path=directory/name
            size=path.stat().st_size
            if budget+size > MAX_BUNDLE_BYTES-3*1024*1024 or len(files)>=64:
                result['errors'].append('Trace collection limit reached; raw files remain on VM')
                continue
            raw=path.read_bytes(); budget+=len(raw); files['traces/'+name]=raw
            result['trace_files'].append(dict(name=name,bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest()))
            try:
                result['trace_summaries'].append(dict(filename=name,**summarize_trace(raw,name)))
            except Exception as exc:
                result['warnings'].append(f'{name}: analysis unavailable: {str(exc)[:200]}')
        if not result['trace_files']:
            result['errors'].append('No new worker trace files were produced')
        if log_path.exists():
            with log_path.open('rb') as stream:
                current=log_path.stat().st_size
                stream.seek(max(log_offset if current>=log_offset else 0,current-MAX_LOG_BYTES))
                log_data=stream.read(MAX_LOG_BYTES)
            files['server-execution.log']=log_data
        result['zentorch_operations_observed']=any(s['zentorch_operations'] for s in result['trace_summaries'])
        result['interpretation']=('Timed ZenTorch operations observed during inference. Native ZenDNN kernel and INT8 decoder attribution still require review.'
            if result['zentorch_operations_observed'] else 'No timed ZenTorch operation evidence identified; this does not prove ZenDNN was unused.')
        result['ok']=not result['errors'] and stopped and result['requests_completed']==requests
        files['summary.json']=json.dumps(result,indent=2).encode()
        files['capture.json']=json.dumps(spec,indent=2).encode()
        files['manifest.json']=json.dumps({name:hashlib.sha256(raw).hexdigest() for name,raw in files.items()},indent=2).encode()
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
            for name,raw in files.items():archive.writestr(name,raw)
        bundle=buffer.getvalue()
        if len(bundle)>MAX_BUNDLE_BYTES:
            result['ok']=False;result['errors'].append('ZIP exceeds transfer limit; files remain on VM')
        else:
            result['bundle']=dict(content_b64=base64.b64encode(bundle).decode(),bytes=len(bundle),sha256=hashlib.sha256(bundle).hexdigest())
        return result


def remote_trace_command(spec):
    source=Path(__file__).read_text()
    source=source.split("if __name__ == '__main__':",1)[0]
    encoded=base64.b64encode(source.encode()).decode()
    return ("python3 - <<'TRACE'\nimport base64,json\n"
            f"code=base64.b64decode({encoded!r}).decode()\n"
            "scope={'__name__':'aig_remote_trace'}\nexec(code,scope)\n"
            f"print(json.dumps(scope['capture'](json.loads({json.dumps(spec)!r}))))\nTRACE\n")
