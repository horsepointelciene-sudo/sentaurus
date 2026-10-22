"""TCAD batch client: check/prepare/submit/status/collect/cancel via existing ProxyJump."""
import argparse
import base64
import csv
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import uuid

HERE = Path(__file__).resolve().parent


def stamp():
    return dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')


def new_id():
    return 'sdevice1_' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ_') + uuid.uuid4().hex[:8]


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def write_json(path, value):
    with path.open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)


def ssh_args(config):
    return [config['ssh_exe'], '-F', config['ssh_config'], '-T', '-o', 'BatchMode=yes', config['host']]


def remote_call(config, action, run_id):
    started_client_utc = dt.datetime.now(dt.timezone.utc).isoformat()
    source = (HERE / 'tcad_batch_remote.py').read_text(encoding='utf-8')
    request = {'config': config, 'action': action, 'run_id': run_id}
    if action == 'prepare':
        request['agent_source'] = source
    if action == 'monitor':
        request['monitor_source'] = (HERE / 'tcad_monitor_remote.py').read_text(encoding='utf-8')
    source64 = base64.b64encode(source.encode()).decode('ascii')
    request64 = base64.b64encode(json.dumps(request).encode()).decode('ascii')
    wrapper = """from __future__ import print_function
import base64,json,sys,traceback
scope={'__name__':'tcad_agent'}
try:
    exec(compile(base64.b64decode(%r),'tcad_batch_remote.py','exec'),scope)
    request=json.loads(base64.b64decode(%r))
    print(json.dumps(scope['dispatch'](request),sort_keys=True))
except BaseException as exc:
    print(json.dumps({'ok':False,'error':repr(exc),'traceback':traceback.format_exc()}))
    sys.exit(2)
""" % (source64, request64)
    argv = ssh_args(config) + [config['remote_python'] + ' -']
    try:
        p = subprocess.run(argv, input=wrapper.encode(), capture_output=True,
                           timeout=300 if action in ('prepare', 'collect') else 60)
        result = {'ssh_exit_code': p.returncode, 'stderr': p.stderr.decode('utf-8', 'replace')}
        try:
            result['remote'] = json.loads(p.stdout)
        except ValueError:
            result['stdout'] = p.stdout.decode('utf-8', 'replace')
        result['connection_ok'] = p.returncode == 0 or 'remote' in result
        result['ok'] = p.returncode == 0 and result.get('remote', {}).get('ok', False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        result = {'ok': False, 'connection_ok': False, 'error': str(exc)}
        if isinstance(exc, subprocess.TimeoutExpired):
            result['stdout'] = (exc.stdout or b'').decode('utf-8', 'replace')
            result['stderr'] = (exc.stderr or b'').decode('utf-8', 'replace')
        result['recovery'] = 'Do not resubmit blindly. Query this run_id to reconcile server state.'
    result.update(action=action, run_id=run_id, client_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                  ssh_config=config['ssh_config'], remote_agent_sha256=hashlib.sha256(source.encode()).hexdigest(),
                  command=argv, started_client_utc=started_client_utc)
    return result


def plt_to_csv(source, dest):
    text = source.read_text(encoding='ascii')
    if not text.startswith('DF-ISE text'):
        raise ValueError('Only verified DF-ISE ASCII PLT is supported; raw file retained')
    if len(re.findall(r'\bData\s*\{', text)) != 1:
        raise ValueError('Expected one verified Data block; multiple blocks need separate validation')
    header, body = re.split(r'\bData\s*\{', text, maxsplit=1)
    if not re.fullmatch(r'[^{}]*\}\s*', body):
        raise ValueError('Incomplete or unsupported Data block')
    if not re.search(r'type\s*=\s*xyplot', header):
        raise ValueError('Not an xyplot')
    match = re.search(r'datasets\s*=\s*\[(.*?)\]', header, re.S)
    if not match:
        raise ValueError('Missing datasets')
    fields = re.findall(r'"([^"]+)"', match.group(1))
    values = [float(t.replace('D', 'E').replace('d', 'e')) for t in body.rsplit('}', 1)[0].split()]
    if not fields or not values or len(set(fields)) != len(fields) or len(values) % len(fields) or not all(map(math.isfinite, values)):
        raise ValueError('Invalid or incomplete curve data')
    with dest.open('x', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(fields)
        writer.writerows(values[i:i + len(fields)] for i in range(0, len(values), len(fields)))
    return {'rows': len(values) // len(fields), 'columns': fields, 'sha256': digest(dest)}


def collect(config, data, local):
    folder = local / ('collection_' + stamp())
    folder.mkdir()
    write_json(folder / 'requested_manifest.json', data)
    report = {'run_id': data['run_id'], 'files': {}, 'csv': {}, 'errors': [],
              'tdr_retained_on_server': data['tdr_retained_on_server'],
              'status_snapshot': data['status_snapshot']}
    scp = str(Path(config['ssh_exe']).with_name('scp.exe'))
    for relative, item in data['files'].items():
        parts = Path(relative).parts
        if Path(relative).is_absolute() or '..' in parts or ':' in relative:
            raise ValueError('Invalid artifact path')
        dest = folder / 'raw' / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            raise ValueError('Refusing artifact overwrite')
        # Paths are generated by this workflow; reject shell/SCP metacharacters.
        if not re.fullmatch(r'/[A-Za-z0-9_./-]+', item['remote_path']):
            raise ValueError('Unsupported transfer path: ' + item['remote_path'])
        argv = [scp, '-F', config['ssh_config'], '-o', 'BatchMode=yes',
                config['host'] + ':' + item['remote_path'], str(dest)]
        try:
            p = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=180)
            if p.returncode:
                raise RuntimeError(p.stderr.decode('utf-8', 'replace'))
            actual = digest(dest)
            if actual != item['sha256']:
                raise ValueError('Transfer hash mismatch: ' + relative)
            report['files'][relative] = dict(item, local_sha256=actual)
            if dest.suffix.lower() == '.plt':
                csv_dest = folder / 'csv' / (dest.stem + '.csv')
                csv_dest.parent.mkdir(exist_ok=True)
                try:
                    report['csv'][relative] = plt_to_csv(dest, csv_dest)
                except Exception as exc:
                    report['errors'].append({'file': relative, 'stage': 'csv_export', 'error': str(exc)})
        except Exception as exc:
            report['errors'].append({'file': relative, 'stage': 'transfer', 'error': str(exc)})
    write_json(folder / 'retrieval_manifest.json', report)
    state = data['status_snapshot']['state']
    lines = ['# sdevice1 运行汇总', '', '运行编号：`' + data['run_id'] + '`', '',
             '批次状态：`' + data['status_snapshot']['observed_status'] + '`', '',
             '| 节点 | int | rho_c_drain | 状态 |', '|---|---|---|---|']
    for n in config['nodes']:
        params = data['status_snapshot']['nodes'][str(n)]['parameters']
        lines.append('| %s | %s | %s | %s |' % (n, params['int'], params['rho_c_drain'], state['nodes'][str(n)]['status']))
    lines += ['', 'TDR 保留服务器，详见 retrieval_manifest.json。', '',
              '传输/导出错误数：%d。原始文件与完整错误记录均保留。' % len(report['errors'])]
    (folder / 'SUMMARY.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return {'directory': str(folder), 'files_verified': len(report['files']),
            'csv_exported': len(report['csv']), 'errors': report['errors']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['check', 'prepare', 'submit', 'status', 'collect', 'cancel', 'monitor', 'refresh'])
    parser.add_argument('--config', type=Path, default=HERE / 'tcad_118u.json')
    parser.add_argument('--run-id')
    parser.add_argument('--mode', choices=['original', 'copy'], default='original')
    parser.add_argument('--task-id')
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding='utf-8'))
    if args.action == 'prepare':
        config['mode'] = args.mode
        config['task_id'] = args.task_id
        git = subprocess.run(['git', '-C', str(HERE.parents[1]), 'rev-parse', 'HEAD'], capture_output=True, text=True)
        config['source_commit'] = git.stdout.strip() if git.returncode == 0 else None
    if args.action not in ('check', 'prepare') and not args.run_id:
        parser.error('--run-id is required; no implicit latest-run selection')
    run_id = args.run_id or (new_id() if args.action == 'prepare' else None)
    if run_id and not re.fullmatch(r'sdevice1_[0-9]{8}T[0-9]{6}Z_[a-f0-9]{8}', run_id):
        parser.error('Invalid --run-id')
    local = Path(config['local_results']) / (run_id or 'checks')
    local.mkdir(parents=True, exist_ok=True)
    if args.action in ('submit', 'cancel') and (local / 'prepare_request.json').exists():
        config = json.loads((local / 'prepare_request.json').read_text(encoding='utf-8'))['config']
    if args.action == 'prepare':
        write_json(local / 'prepare_request.json', {'config': config, 'run_id': run_id})
        print('RUN_ID=' + run_id, flush=True)
    record_path = local / (args.action + '_' + stamp() + '.json')
    result = remote_call(config, 'collect' if args.action == 'refresh' else args.action, run_id)
    if args.action in ('collect', 'refresh') and result['ok']:
        try:
            result['collection'] = collect(config, result['remote'], local)
            result['ok'] = not result['collection']['errors']
        except Exception as exc:
            result.update(ok=False, collection_error=repr(exc))
    if args.action == 'submit' and result['ok']:
        result['observer'] = remote_call(config, 'monitor', run_id)
    if args.action == 'refresh':
        from tcad_reporting import refresh_report
        result['report'] = refresh_report(config, run_id, local, result)
    write_json(record_path, result)
    print('EVIDENCE=' + str(record_path))
    remote = result.get('remote', {})
    if args.action == 'status' and result['ok']:
        print(json.dumps({'run_id': run_id, 'status': remote['observed_status'],
                          'worker_alive': remote['worker_alive'],
                          'current_node': remote['state'].get('current_node'),
                          'nodes': {n: s['status'] for n, s in remote['state']['nodes'].items()},
                          'monitor': remote.get('monitor')}, ensure_ascii=False, indent=2))
    elif args.action == 'refresh':
        print(json.dumps({'ok': result['ok'], 'collection': result.get('collection'),
                          'report': result.get('report')}, ensure_ascii=False, indent=2))
    elif args.action == 'collect' and result['ok']:
        print(json.dumps(result['collection'], ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ok'] else 2


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
