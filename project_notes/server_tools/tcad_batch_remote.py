"""Server-side batch lifecycle. Python 2.7/3 compatible; no third-party packages."""
from __future__ import print_function
import base64
import datetime
import errno
import getpass
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import traceback

NODES = [15, 21, 27, 33, 16, 23, 29, 35]
PARAMETERS = dict((str(n), {'int': 0 if i < 4 else 5,
                          'rho_c_drain': ['1e-4', '1e-3', '1e-2', '1e-1'][i % 4]})
                  for i, n in enumerate(NODES))
TERMINAL = ('succeeded', 'failed', 'cancelled', 'worker_error', 'blocked')


def now():
    return datetime.datetime.utcnow().isoformat() + 'Z'


def read(path):
    with open(path, 'rb') as f:
        return f.read().decode('utf-8', 'replace')


def tail(path, size=12000):
    if not os.path.isfile(path):
        return ''
    with open(path, 'rb') as f:
        f.seek(max(0, os.fstat(f.fileno()).st_size - size))
        return f.read().decode('utf-8', 'replace')


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def exclusive(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def save(path, obj, new=False):
    data = (json.dumps(obj, indent=2, sort_keys=True) + '\n').encode('utf-8')
    if new:
        exclusive(path, data)
    else:
        tmp = path + '.tmp.' + str(os.getpid())
        exclusive(tmp, data)
        getattr(os, 'replace', os.rename)(tmp, path)


def load(path):
    return json.loads(read(path))


def identity(pid):
    p = '/proc/' + str(pid)
    if os.stat(p).st_uid != os.getuid():
        raise ValueError('Process belongs to another user')
    fields = read(p + '/stat').rsplit(') ', 1)[1].split()
    return {'pid': int(pid), 'start_ticks': int(fields[19]),
            'ppid': int(fields[1]), 'session': int(fields[3]),
            'cwd': os.readlink(p + '/cwd'), 'name': read(p + '/comm').strip(),
            'cmdline': read(p + '/cmdline').replace('\x00', ' ')}


def alive(item):
    try:
        return identity(item['pid'])['start_ticks'] == item['start_ticks']
    except (OSError, IOError, KeyError, ValueError):
        return False


def processes(project):
    result = []
    for pid in os.listdir('/proc'):
        if not pid.isdigit():
            continue
        try:
            p = identity(int(pid))
            if (p['cwd'] == project or project in p['cmdline'].split()) and any(
                    word in p['name'] for word in ('sdevice', 'gjob', 'gsub', 'spp')):
                result.append(p)
        except (OSError, IOError, ValueError):
            pass
    return result


def root_for(c, run_id):
    if not re.match(r'^sdevice1_[0-9]{8}T[0-9]{6}Z_[a-f0-9]{8}$', run_id):
        raise ValueError('Invalid run_id')
    root = os.path.join(c['parent'], '4E17SPICE_1.18u_' + run_id)
    if os.path.realpath(root) != root:
        raise ValueError('Unexpected resolved run path')
    return root


def validate(c):
    if (getpass.getuser() != 'liangxun' or
            c['source'] != '/home/liangxun/STDB/Codex/4E17SPICE_1.18u' or
            c['parent'] != '/home/liangxun/STDB/Codex' or c['nodes'] != NODES or
            c['threads'] != 8 or c['max_concurrent_nodes'] != 1 or not c['stop_on_failure']):
        raise ValueError('Configuration is outside the approved batch scope')
    for p in [c['source'], c['parent']]:
        if os.path.realpath(p) != p:
            raise ValueError('Symlinked project/root is not supported')
    for p in ['/home/liangxun', '/home/liangxun/STDB', c['parent'], c['source']]:
        if os.path.lexists(os.path.join(p, 'AGENTS.md')):
            raise ValueError('New remote instructions require review: ' + p)


def input_names(c):
    return c['input_files'] + [n for n in c['optional_inputs']
                              if os.path.isfile(os.path.join(c['source'], n))]


def hashes(directory, names):
    result = {}
    for n in names:
        p = os.path.join(directory, n)
        if os.path.islink(p) or not stat.S_ISREG(os.lstat(p).st_mode):
            raise ValueError('Input must be a regular file: ' + p)
        result[n] = sha(p)
    return result


def verify_hashes(directory, expected):
    actual = hashes(directory, sorted(expected))
    changed = [n for n in expected if actual[n] != expected[n]]
    if changed:
        raise ValueError('Frozen input changed: ' + ', '.join(changed))


def tree_content(text):
    # spp/gsub rewrite the provenance comment; all executable tree lines remain frozen.
    return '\n'.join(line for line in text.splitlines()
                     if not re.match(r'^# swbtree vcurrent, saved by (?:swb|spp|gsub)$', line))


def verify_work_inputs(project, expected, original_tree):
    verify_hashes(project, dict((n, h) for n, h in expected.items() if n != 'gtree.dat'))
    if tree_content(read(project + '/gtree.dat')) != tree_content(original_tree):
        raise ValueError('Project tree content changed beyond the tool provenance comment')


def tree_parameters(text):
    body = text.split('# --- simulation tree', 1)[1]
    rows = {}
    for line in body.splitlines():
        m = re.match(r'^(\d+)\s+(\d+)\s+(\d+)\s+\{([^}]*)\}', line)
        if m:
            level, node, parent, value = m.groups()
            rows[int(node)] = (int(level), int(parent), value)
    if 'sdevice1 rho_c_drain' not in text or 'sdevice int' not in text:
        raise ValueError('Unexpected tool/parameter tree')
    actual = {}
    for node in NODES:
        level, parent, resistance = rows[node]
        tool_level, ancestor, _ = rows[parent]
        intensity_level, _, intensity = rows[ancestor]
        if (level, tool_level, intensity_level) != (6, 5, 4):
            raise ValueError('Unexpected node ancestry')
        actual[str(node)] = {'int': int(intensity), 'rho_c_drain': resistance}
    if actual != PARAMETERS:
        raise ValueError('Node parameters changed: ' + repr(actual))
    return actual


def check(c):
    validate(c)
    h = hashes(c['source'], input_names(c))
    verify_hashes(c['source'], dict((n, h) for n, h in c['reviewed_hashes'].items() if n != 'gtree.dat'))
    tree = read(c['source'] + '/gtree.dat')
    expected_tree = c['reviewed_hashes'].get('gtree.dat')
    # Only a verified tool-generated provenance comment may differ.
    variants = [re.sub(r'^# swbtree vcurrent, saved by (?:swb|spp|gsub)$',
                       '# swbtree vcurrent, saved by ' + tool, tree, flags=re.M)
                for tool in ('swb', 'spp', 'gsub')]
    if expected_tree and not any(hashlib.sha256(v.encode('utf-8')).hexdigest() == expected_tree for v in variants + [tree]):
        raise ValueError('Reviewed project tree changed')
    params = tree_parameters(read(c['source'] + '/gtree.dat'))
    active = processes(c['source'])
    fs = os.statvfs(c['parent'])
    return {'ok': True, 'server_utc': now(), 'source': c['source'], 'user': getpass.getuser(),
            'active_source_processes': active, 'ready_to_prepare': not active,
            'input_sha256': h, 'parameters': params, 'loadavg': os.getloadavg(),
            'free_bytes': fs.f_bavail * fs.f_frsize,
            'remote_python': sys.version, 'simulation_submitted': False}


def project_for(root):
    path = root + '/manifest.json'
    return load(path)['project'] if os.path.isfile(path) else root + '/project'


def input_tree(root):
    for path in (root + '/inputs/gtree.dat', root + '/snapshot/gtree.dat'):
        if os.path.isfile(path):
            return read(path)
    raise ValueError('Missing frozen project tree')


def lock_project(c, project, run_id):
    base = c['parent'] + '/.tcad-project-locks'
    if not os.path.isdir(base):
        try:
            os.mkdir(base, 0o700)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
    path = base + '/' + hashlib.sha256(project.encode('utf-8')).hexdigest()
    # Fail closed even if an earlier owner died. Do not reclaim unknown jobs.
    os.mkdir(path, 0o700)
    save(path + '/owner.json', {'run_id': run_id, 'project': project, 'created_utc': now()}, new=True)
    return path


def unlock_project(m):
    path = m.get('project_lock')
    if not path or not os.path.isdir(path):
        return
    if processes(m['project']):
        raise ValueError('Processes remain; retaining project lock')
    if load(path + '/owner.json')['run_id'] != m['run_id']:
        raise ValueError('Project lock owner changed')
    os.unlink(path + '/owner.json')
    os.rmdir(path)


def node_of(name):
    m = re.search(r'(?:^|_)n(\d+)(?:_|\.)', name)
    if not m:
        m = re.match(r'(?:pp|gsub_node)(\d+)(?:_|\.)', name)
    return int(m.group(1)) if m else None


def copy_verified(source, target):
    if os.path.islink(source) or not stat.S_ISREG(os.lstat(source).st_mode):
        raise ValueError('Not a regular artifact: ' + source)
    if os.path.exists(target):
        raise ValueError('Refusing artifact overwrite: ' + target)
    before = sha(source)
    shutil.copy2(source, target)
    if sha(target) != before or sha(source) != before:
        raise ValueError('Artifact changed while archiving: ' + source)
    return {'sha256': before, 'bytes': os.path.getsize(target), 'source': source, 'archive': target}


def backup_overwritten(c, root, project):
    target = root + '/before_run'
    os.mkdir(target, 0o700)
    # spp regenerates the execution graph and preprocessed decks; keep these,
    # shared SWB metadata and outputs of selected nodes only. Never full-copy.
    common = ('gtree.dat', 'gvars.dat', 'gexec.cmd', '.status', '.database')
    chosen = [n for n in sorted(os.listdir(project)) if n in common or
              n.startswith('pp') and n.endswith('.cmd') or node_of(n) in c['nodes']]
    total = sum(os.path.getsize(project + '/' + n) for n in chosen)
    fs = os.statvfs(root)
    if total + 1024 ** 3 > fs.f_bavail * fs.f_frsize:
        raise ValueError('Insufficient free space for necessary output backup')
    records = dict((n, copy_verified(project + '/' + n, target + '/' + n)) for n in chosen)
    save(root + '/before_run_manifest.json', records, new=True)
    return records


def archive_node(root, project, node, started):
    target = root + '/artifacts/project'
    if not os.path.isdir(target):
        os.makedirs(target, 0o700)
    records = {}
    for name in sorted(os.listdir(project)):
        source = project + '/' + name
        if node_of(name) == node and os.path.isfile(source) and os.path.getmtime(source) >= started - 1:
            records[name] = copy_verified(source, target + '/' + name)
    save(root + '/artifacts/node%d.json' % node, records, new=True)
    return records


def run_bounded(cmd, cwd, prefix, timeout):
    with open(prefix + '.stdout.log', 'wb') as out, open(prefix + '.stderr.log', 'wb') as err:
        with open(os.devnull, 'rb') as inp:
            p = subprocess.Popen(cmd, cwd=cwd, stdin=inp, stdout=out, stderr=err,
                                 preexec_fn=os.setsid, close_fds=True)
        deadline = time.time() + timeout
        while p.poll() is None and time.time() < deadline:
            time.sleep(0.2)
        timed_out = p.poll() is None
        if timed_out:
            os.killpg(p.pid, signal.SIGTERM)
            time.sleep(1)
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGKILL)
        return {'argv': cmd, 'exit_code': p.wait(), 'timed_out': timed_out}


def validate_preprocessed(c, project):
    graph = read(project + '/gexec.cmd')
    jobs = dict((int(n), rest) for n, rest in re.findall(r'^job\s+(\d+)\s+(.*)$', graph, re.M))
    for node in NODES:
        if node not in jobs or ('pp%d_des.cmd' % node) not in jobs[node]:
            raise ValueError('Missing execution graph node %s' % node)
        # The reviewed circuit templates load fixed meshes, not upstream solutions.
        if re.search(r'-(?:dep|parent|require)', jobs[node]):
            raise ValueError('Unexpected graph dependency: ' + jobs[node])
        text = read(project + '/pp%d_des.cmd' % node)
        if re.search(r'@[^\s@]+@', text):
            raise ValueError('Unexpanded input token for node %s' % node)
        if not re.search(r'NumberofThreads\s*=\s*8\b', text):
            raise ValueError('Thread count differs from approved value')
        resistance = PARAMETERS[str(node)]['rho_c_drain']
        values = re.findall(r'DistResist\s*=\s*([^\s}]+)', text)
        if len(values) != 2 or any(float(v) != float(resistance) for v in values):
            raise ValueError('Preprocessed drain resistance mismatch')
        goals = re.findall(r'ModelParameter\s*=\s*"Intensity"\s+Value\s*=\s*([^\s}]+)', text)
        intensity = PARAMETERS[str(node)]['int']
        if (intensity == 0 and goals) or (intensity == 5 and
                (len(goals) != 2 or any(float(v) != 5 for v in goals))):
            raise ValueError('Preprocessed intensity mismatch')
    return {'selected_nodes': NODES, 'graph_job_ids': sorted(jobs), 'graph': graph}


def _prepare_new(c, run_id, agent_source):
    preflight = check(c)
    if not preflight['ready_to_prepare']:
        raise ValueError('Original batch still active; waiting policy applies')
    root = root_for(c, run_id)
    os.mkdir(root, 0o700)  # Exclusive; never overwrite an existing run or retry implicitly.
    mode = c.get('mode', 'original')
    if mode not in ('original', 'copy'):
        raise ValueError('Unsupported run mode')
    snapshot = root + '/snapshot'
    project = root + '/project' if mode == 'copy' else c['source']
    save(root + '/config.json', c, new=True)
    save(root + '/preflight.json', preflight, new=True)
    exclusive(root + '/agent.py', agent_source.encode('utf-8'))
    project_lock = lock_project(c, project, run_id)
    save(root + '/preparation.json', {'run_id': run_id, 'project': project, 'project_lock': project_lock}, new=True)
    before = preflight['input_sha256']
    os.mkdir(root + '/inputs', 0o700)
    for n in input_names(c):
        if not n.lower().endswith('.tdr'):
            copy_verified(c['source'] + '/' + n, root + '/inputs/' + n)
    if mode == 'copy':
        os.mkdir(snapshot, 0o700)
        os.mkdir(project, 0o700)
        names = sorted(os.listdir(c['source']))
        before = hashes(c['source'], names)
        total = sum(os.path.getsize(c['source'] + '/' + n) for n in names)
        if total * 2 + 1024 ** 3 > preflight['free_bytes']:
            raise ValueError('Insufficient free space for snapshot and working copy')
        for n in names:
            copy_verified(c['source'] + '/' + n, snapshot + '/' + n)
        verify_hashes(c['source'], before)
        for n in input_names(c):
            copy_verified(snapshot + '/' + n, project + '/' + n)
    else:
        if processes(project):
            raise ValueError('Project became active during preparation')
        backup_overwritten(c, root, project)
    verify_hashes(project, preflight['input_sha256'])
    cmd = [c['tools'] + '/spp', '-verbose', '-n', ' '.join(str(n) for n in NODES), '-i', project]
    preprocessing = run_bounded(cmd, project, root + '/preprocess', 180)
    save(root + '/preprocess.json', preprocessing, new=True)
    if preprocessing['exit_code'] or preprocessing['timed_out']:
        raise ValueError('Preprocessing failed; full logs retained in ' + root)
    details = validate_preprocessed(c, project)
    verify_work_inputs(project, preflight['input_sha256'], input_tree(root))
    if mode == 'copy':
        verify_hashes(c['source'], before)
    frozen = hashes(project, sorted(set(input_names(c) + ['gexec.cmd'] +
                                       ['pp%d_des.cmd' % n for n in NODES])))
    manifest = {'run_id': run_id, 'root': root, 'project': project, 'source': c['source'],
                'prepared_utc': now(), 'source_snapshot_sha256': before,
                'source_input_sha256': preflight['input_sha256'], 'frozen_input_sha256': frozen,
                'agent_sha256': sha(root + '/agent.py'), 'nodes': NODES, 'parameters': PARAMETERS,
                'preprocessing': preprocessing, 'graph_check': details,
                'physics_modified': False, 'threads': 8, 'max_concurrent_nodes': 1,
                'mode': mode, 'project_lock': project_lock, 'task_id': c.get('task_id'),
                'source_commit': c.get('source_commit'), 'source_commit_scope': 'workflow and allowlisted local inputs; remote hashes authoritative'}
    save(root + '/manifest.json', manifest, new=True)
    save(root + '/state.json', {'status': 'prepared', 'run_id': run_id, 'updated_utc': now(),
         'nodes': dict((str(n), {'status': 'not_started'}) for n in NODES)}, new=True)
    return {'ok': True, 'manifest': manifest, 'simulation_submitted': False}


def prepare(c, run_id, agent_source):
    root = root_for(c, run_id)
    if os.path.exists(root):
        raise ValueError('Run directory exists; query it instead of preparing again')
    try:
        return _prepare_new(c, run_id, agent_source)
    except BaseException as exc:
        if os.path.isdir(root):
            save(root + '/preparation_failure.json', {'error': repr(exc), 'server_utc': now(),
                  'simulation_submitted': False})
            preparation = root + '/preparation.json'
            if os.path.isfile(preparation):
                item = load(preparation)
                if not processes(item['project']):
                    unlock_project(item)
        raise


def claim(root):
    try:
        exclusive(root + '/submission.lock', (now() + '\n').encode('ascii'))
        return True
    except OSError as e:
        if e.errno == errno.EEXIST:
            return False
        raise


def submit(c, run_id):
    root = root_for(c, run_id)
    m = load(root + '/manifest.json')
    if m['run_id'] != run_id or load(root + '/config.json') != c:
        raise ValueError('Run/config mismatch')
    if os.path.exists(root + '/submission.lock'):
        return {'ok': True, 'duplicate_submission_prevented': True, 'run_id': run_id,
                'state': load(root + '/state.json')}
    if processes(c['source']) or processes(m['project']):
        raise ValueError('Source or work project already active')
    if m.get('mode') != 'original':
        verify_hashes(c['source'], m['source_input_sha256'])
    else:
        verify_work_inputs(m['project'], m['source_input_sha256'], input_tree(root))
    verify_hashes(m['project'], m['frozen_input_sha256'])
    if sha(root + '/agent.py') != m['agent_sha256']:
        raise ValueError('Worker source differs from preparation manifest')
    if not claim(root):
        return {'ok': True, 'duplicate_submission_prevented': True, 'run_id': run_id}
    state = load(root + '/state.json')
    state.update(status='launching', submitted_utc=now(), updated_utc=now())
    save(root + '/state.json', state)
    with open(root + '/worker.console.log', 'ab') as out, open(os.devnull, 'rb') as inp:
        p = subprocess.Popen([sys.executable, root + '/agent.py', 'worker', root],
                             cwd=root, stdin=inp, stdout=out, stderr=subprocess.STDOUT,
                             close_fds=True, preexec_fn=os.setsid)
    process = identity(p.pid)
    save(root + '/launch.json', {'worker': process, 'started_utc': now()}, new=True)
    return {'ok': True, 'run_id': run_id, 'worker': process,
            'status': 'launch_requested', 'simulation_success': False}


def completion(project, node, code, started, gsub_log):
    log = tail(project + '/n%d_des.log' % node, 64000)
    job = tail(project + '/n%d_des.job' % node, 64000)
    sta = tail(project + '/n%d_des.sta' % node)
    scheduled = sorted(set(int(n) for n in re.findall(r"job '(\d+)' status changed", read(gsub_log))))
    prefix = 'dark' if PARAMETERS[str(node)]['int'] == 0 else 'light'
    curves = ['%s_IdVd_n%d_circuit.plt' % (prefix, node),
              '%s_IdVd_ppl1_n%d_des.plt' % (prefix, node),
              '%s_IdVd_noppl1_n%d_des.plt' % (prefix, node)]
    required = ['n%d_des.log' % node, 'n%d_des.job' % node, 'n%d_des.sta' % node] + curves
    fresh = all(os.path.isfile(project + '/' + n) and os.path.getsize(project + '/' + n) > 0
                and os.path.getmtime(project + '/' + n) >= started - 1 for n in required)
    checks = {'gsub_exit_zero': code == 0, 'only_requested_node_scheduled': scheduled == [node],
              'job_exit_zero': ('job %d <sdevice> done: exit(0)' % node) in job and
                               'gjob exits with status 0' in job,
              'node_state_done': '|done|' in sta,
              'solver_finished': 'Good Bye !' in log and 'Sentaurus Device simulation finished' in log,
              'fresh_logs_and_curves': fresh}
    return {'passed': all(checks.values()), 'checks': checks, 'scheduled_nodes': scheduled,
            'curve_files': curves, 'gsub_exit_code': code}


def stop_owned(project, min_start):
    records = []
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for p in processes(project):
            if p['start_ticks'] >= min_start and alive(p):
                os.kill(p['pid'], sig)
                records.append({'identity': p, 'signal': int(sig)})
        deadline = time.time() + (10 if sig == signal.SIGTERM else 2)
        while time.time() < deadline and processes(project):
            time.sleep(0.2)
    return records


def run_one(c, root, node, state, publish):
    m = load(root + '/manifest.json')
    project = m['project']
    # gsub may regenerate gexec/pp; verify immutable source inputs before each node.
    verify_work_inputs(project, m['source_input_sha256'], input_tree(root))
    if processes(project):
        raise ValueError('Unexpected active process in isolated project')
    cmd = [c['tools'] + '/gsub', '-verbose', '-n', str(node), '-q', 'local:default', '-max', '1', project]
    prefix = root + '/gsub_node%d' % node
    start = time.time()
    with open(prefix + '.stdout.log', 'wb') as out, open(prefix + '.stderr.log', 'wb') as err:
        with open(os.devnull, 'rb') as inp:
            child = subprocess.Popen(cmd, cwd=project, stdin=inp, stdout=out, stderr=err,
                                     preexec_fn=os.setsid, close_fds=True)
        child_id = identity(child.pid)
        state['nodes'][str(node)].update(status='running', command=cmd, process=child_id,
                                        started_utc=now(), started_epoch=start)
        publish(state)
        cancellation = None
        while child.poll() is None:
            if os.path.exists(root + '/cancel.request'):
                cancellation = stop_owned(project, child_id['start_ticks'])
                break
            time.sleep(1)
        code = child.wait()
    remaining = processes(project)
    if remaining:
        raise ValueError('Child processes remain after gsub; outputs are not stable')
    archives = archive_node(root, project, node, start)
    if cancellation is not None:
        return {'status': 'cancelled', 'finished_utc': now(), 'signals': cancellation,
                'exit_code': code, 'archives': archives, 'stopped_confirmed': True}
    result = completion(project, node, code, start, prefix + '.stdout.log')
    result.update(status='succeeded' if result['passed'] else 'failed', finished_utc=now(), archives=archives)
    return result


def run_queue(nodes, state, execute, publish, cancelled=lambda: False):
    """Pure orchestration seam shared by the real worker and local failure tests."""
    for node in nodes:
        if cancelled():
            state['status'] = 'cancelled'
            break
        state.update(status='running', current_node=node)
        publish(state)
        result = execute(node, state, publish)
        state['nodes'][str(node)].update(result)
        publish(state)
        if result['status'] != 'succeeded':
            state['status'] = result['status']
            break
    else:
        state['status'] = 'succeeded'
    for node in nodes:
        item = state['nodes'][str(node)]
        if item['status'] == 'not_started':
            item.update(status='blocked', reason='Batch stopped before this node')
    state['finished_utc'] = now()
    publish(state)
    return state


def worker(root):
    c = load(root + '/config.json')
    validate(c)
    m = load(root + '/manifest.json')
    if root_for(c, m['run_id']) != root:
        raise ValueError('Invalid worker root')
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    state = load(root + '/state.json')
    state['worker'] = identity(os.getpid())
    state['worker_started_utc'] = now()

    def publish(value):
        value['updated_utc'] = now()
        save(root + '/state.json', value)
    publish(state)
    try:
        run_queue(c['nodes'], state, lambda n, s, pub: run_one(c, root, n, s, pub), publish,
                  lambda: os.path.exists(root + '/cancel.request'))
    except BaseException as exc:
        state.update(status='worker_error', error=repr(exc), traceback=traceback.format_exc(),
                     finished_utc=now())
        # Do not erase evidence of a possibly still-running child on worker errors.
        for item in state['nodes'].values():
            if item['status'] == 'not_started':
                item.update(status='blocked', reason='Worker error')
        publish(state)
    finally:
        # Keep the lock when there is uncertainty or a live child.
        if not processes(m['project']) and not any(x['status'] == 'running' for x in state['nodes'].values()):
            unlock_project(m)


def status(c, run_id):
    root = root_for(c, run_id)
    project = project_for(root)
    state = load(root + '/state.json')
    worker_id = state.get('worker')
    if not worker_id and os.path.isfile(root + '/launch.json'):
        worker_id = load(root + '/launch.json').get('worker')
    worker_alive = bool(worker_id and alive(worker_id))
    observed = state['status']
    if observed in ('running', 'launching') and not worker_alive:
        observed = 'attention_required_worker_not_alive'
    details = {}
    for node in NODES:
        item = {'parameters': PARAMETERS[str(node)]}
        archived = root + '/artifacts/project'
        base = archived if os.path.isfile(archived + '/n%d_des.log' % node) else project
        path = base + '/n%d_des.log' % node
        if os.path.isfile(path):
            with open(path, 'rb') as f:
                head = f.read(5000).decode('utf-8', 'replace')
            version = re.search(r'Version\s+(\S+)', head)
            item.update(log_bytes=os.path.getsize(path), log_mtime=os.path.getmtime(path),
                        solver_version=version.group(1) if version else None,
                        log_tail=tail(path, 9000))
        item['job_tail'] = tail(base + '/n%d_des.job' % node, 4000)
        item['sta'] = tail(base + '/n%d_des.sta' % node, 1000)
        details[str(node)] = item
    observation = load(root + '/monitor/latest.json') if os.path.isfile(root + '/monitor/latest.json') else None
    if observation:
        observation['age_seconds'] = max(0, time.time() - observation['server_epoch'])
        observation['stale'] = observation['age_seconds'] > 180 and state['status'] not in TERMINAL
    return {'ok': True, 'server_utc': now(), 'root': root, 'run_id': run_id,
            'state': state, 'observed_status': observed, 'worker_alive': worker_alive,
            'project': project, 'active_processes': processes(project), 'nodes': details,
            'monitor': observation,
            'cancel_requested': os.path.exists(root + '/cancel.request'),
            'simulation_success': state['status'] == 'succeeded'}


def collect_manifest(c, run_id):
    root = root_for(c, run_id)
    state = load(root + '/state.json')
    current = status(c, run_id)
    files = {}
    tdr = {}
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ('snapshot', 'before_run', 'visualization') and
                   not (d == 'monitor' and state['status'] not in TERMINAL)]
        if base == root and os.path.isdir(root + '/artifacts/project'):
            dirs[:] = [d for d in dirs if d != 'project']  # Archived outputs are the stable authority.
        for n in names:
            p = base + '/' + n
            if os.path.islink(p) or not stat.S_ISREG(os.lstat(p).st_mode):
                raise ValueError('Unexpected file type: ' + p)
            rel = os.path.relpath(p, root).replace(os.sep, '/')
            s = os.stat(p)
            # Running node output is not a stable completed artifact.
            match = re.search(r'(?:^|_)n(\d+)(?:_|\.)', n)
            match2 = re.match(r'gsub_node(\d+)\.', n)
            node = match.group(1) if match else (match2.group(1) if match2 else None)
            node_state = state['nodes'].get(node, {}) if node else {}
            if node and (node_state.get('status') not in TERMINAL or not node_state.get('started_epoch')):
                continue
            if not node and base == root and state['status'] not in TERMINAL:
                if n not in ('manifest.json', 'config.json', 'preflight.json', 'preprocess.json',
                             'agent.py', 'launch.json', 'preprocess.stdout.log', 'preprocess.stderr.log'):
                    continue
            digest = sha(p)
            after = os.stat(p)
            if (s.st_size, s.st_mtime) != (after.st_size, after.st_mtime):
                raise ValueError('File changed during collection manifest: ' + p)
            item = {'bytes': s.st_size, 'mtime': s.st_mtime, 'sha256': digest, 'remote_path': p,
                    'role': 'output' if node and node_state.get('started_epoch') and s.st_mtime >= node_state['started_epoch'] - 1 else 'input_or_metadata',
                    'node': int(node) if node else None}
            (tdr if n.lower().endswith('.tdr') else files)[rel] = item
    return {'ok': True, 'run_id': run_id, 'root': root, 'status_snapshot': current,
            'files': files, 'tdr_retained_on_server': tdr}


def cancel(c, run_id):
    root = root_for(c, run_id)
    s = status(c, run_id)
    if s['state']['status'] in TERMINAL:
        return {'ok': True, 'already_terminal': True, 'state': s['state']}
    if s['state']['status'] == 'prepared' and not s['active_processes']:
        m = load(root + '/manifest.json')
        state = s['state']
        state.update(status='cancelled', finished_utc=now())
        for item in state['nodes'].values():
            item['status'] = 'blocked'
        save(root + '/state.json', state)
        unlock_project(m)
        return {'ok': True, 'cancel_requested': True, 'stopped_confirmed': True}
    if not s['worker_alive']:
        raise ValueError('Worker unavailable; inspect process evidence before targeted intervention')
    try:
        exclusive(root + '/cancel.request', (now() + '\n').encode('ascii'))
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise
    return {'ok': True, 'run_id': run_id, 'cancel_requested': True, 'stopped_confirmed': False}


def start_monitor(c, run_id, source):
    root = root_for(c, run_id)
    m = load(root + '/manifest.json')
    if m['run_id'] != run_id:
        raise ValueError('Manifest run mismatch')
    identity_path = root + '/monitor/identity.json'
    if os.path.isfile(identity_path) and alive(load(identity_path)):
        return {'ok': True, 'monitor_already_running': True, 'monitor': load(identity_path)}
    digest = hashlib.sha256(source.encode('utf-8')).hexdigest()
    script = root + '/observer_' + digest[:16] + '.py'
    if not os.path.exists(script):
        exclusive(script, source.encode('utf-8'))
    if sha(script) != digest:
        raise ValueError('Observer checksum mismatch')
    with open(root + '/observer.console.log', 'ab') as out, open(os.devnull, 'rb') as inp:
        child = subprocess.Popen([sys.executable, script, root], cwd=root, stdin=inp,
                                 stdout=out, stderr=subprocess.STDOUT, close_fds=True, preexec_fn=os.setsid)
    return {'ok': True, 'observer_launch_requested': True, 'pid': child.pid, 'source_sha256': digest}


def dispatch(request):
    c = request['config']
    validate(c)
    action = request['action']
    if action == 'check':
        return check(c)
    run_id = request['run_id']
    if action == 'prepare':
        return prepare(c, run_id, request['agent_source'])
    if action == 'monitor':
        return start_monitor(c, run_id, request['monitor_source'])
    return {'submit': submit, 'status': status, 'collect': collect_manifest, 'cancel': cancel}[action](c, run_id)


if __name__ == '__main__' and len(sys.argv) == 3 and sys.argv[1] == 'worker':
    worker(sys.argv[2])
