"""Read-only solver observer. Python 2.7/3, standard library; never submits/stops jobs."""
from __future__ import print_function
import datetime
import json
import os
import re
import signal
import sys
import time

NUMBER = r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eEdD][+-]?\d+)?'
STEP = re.compile(r'Computing (?:BE|TR|TRBDF|BDF\d?)-step from (' + NUMBER + r') s to (' + NUMBER + r') s \(Stepsize: (' + NUMBER + r') s\)')


def number(value):
    return float(value.replace('D', 'E').replace('d', 'e'))


def parse_lines(lines, progress):
    """Only a convergence message following a transient target accepts that step."""
    for line in lines:
        step = STEP.search(line)
        if step:
            start, target, size = map(number, step.groups())
            progress.update(target_time_s=target, step_s=size,
                            phase='drain_ramp' if start >= 100 else 'gate_ramp',
                            pending_target=target)
        elif 'Quasistationary' in line or 'quasistationary step' in line.lower():
            progress.update(phase='quasistationary', pending_target=None)
        elif ('Error smaller than' in line or 'RHS smaller than' in line) and progress.get('pending_target') is not None:
            progress['accepted_time_s'] = progress.pop('pending_target')
        elif 'step rejected' in line.lower() or 'retrying' in line.lower() or 'Newton didn' in line:
            progress['pending_target'] = None
        match = re.match(r'^\s*(\d+)\s+(' + NUMBER + r')\s+(' + NUMBER + r')\s+(' + NUMBER + r')\s+(' + NUMBER + r')\s+', line)
        if match:
            progress['last_iteration'] = {'iteration': int(match.group(1)), 'rhs': number(match.group(2)), 'error': number(match.group(5))}
        if re.search(r'FATAL|Step-size is too small|license checkout failed|Cannot check out', line, re.I):
            progress['diagnostic'] = line.strip()[:300]
    accepted = progress.get('accepted_time_s')
    # These mappings are specific to the reviewed 118u template, not general TCAD.
    if accepted is not None:
        progress['accepted_vgs_V'] = min(15.0, max(0.0, accepted * 0.15))
        progress['accepted_vds_V'] = min(10.0, max(0.0, (accepted - 100.0) * 0.1))
        progress['drain_scan_fraction'] = progress['accepted_vds_V'] / 10.0
    return progress


def read_increment(path, cursor, progress):
    if not os.path.isfile(path):
        return cursor, progress
    st = os.stat(path)
    if cursor.get('inode') != st.st_ino or st.st_size < cursor.get('offset', 0):
        cursor = {'inode': st.st_ino, 'offset': 0, 'partial': ''}
        progress = {'phase': 'initialization'}
    with open(path, 'rb') as stream:
        stream.seek(cursor['offset'])
        block = stream.read(1024 * 1024)
        cursor['offset'] = stream.tell()
    data = cursor.get('partial', '') + block.decode('utf-8', 'replace')
    lines = data.split('\n')
    cursor['partial'] = lines.pop()
    progress = parse_lines(lines, progress)
    progress.update(log_bytes=st.st_size, log_mtime_epoch=st.st_mtime,
                    unread_bytes=max(0, st.st_size - cursor['offset']))
    return cursor, progress


def resource_sample(processes, previous, now_epoch):
    output = []
    for p in processes:
        try:
            fields = open('/proc/%d/stat' % p['pid']).read().rsplit(') ', 1)[1].split()
            ticks = int(fields[11]) + int(fields[12])
            key = '%s:%s' % (p['pid'], p['start_ticks'])
            old = previous.get(key)
            cpu = None if old is None else 100.0 * (ticks - old[0]) / os.sysconf('SC_CLK_TCK') / max(0.001, now_epoch - old[1])
            previous[key] = (ticks, now_epoch)
            rss = int(fields[21]) * os.sysconf('SC_PAGE_SIZE')
            output.append({'pid': p['pid'], 'start_ticks': p['start_ticks'], 'name': p['name'],
                           'rss_bytes': rss, 'cpu_percent_one_core_100': cpu})
        except (IOError, OSError, ValueError, IndexError):
            continue
    return output


def observe(root, interval=60):
    import fcntl
    import imp  # Server Python 2.7; intentionally imported only in the server entrypoint.
    agent = imp.load_source('frozen_agent', root + '/agent.py')
    config = agent.load(root + '/config.json')
    manifest = agent.load(root + '/manifest.json')
    dest = root + '/monitor'
    if not os.path.isdir(dest):
        try:
            os.mkdir(dest, 0o700)
        except OSError:
            if not os.path.isdir(dest):
                raise
    lock = open(dest + '/observer.lock', 'a+')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        return  # Existing observer owns the run; never start a second sampler.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    agent.save(dest + '/identity.json', agent.identity(os.getpid()))
    cursors, progresses, resources, progress_times = {}, {}, {}, {}
    last_event = None
    while True:
        epoch = time.time()
        state = agent.load(root + '/state.json')
        live = agent.processes(manifest['project'])
        worker = state.get('worker')
        worker_alive = bool(worker and agent.alive(worker))
        current = str(state.get('current_node', ''))
        progress = progresses.setdefault(current, {'phase': 'initialization'})
        if current and state['nodes'].get(current, {}).get('started_epoch'):
            old_accepted = progress.get('accepted_time_s')
            cursors[current], progress = read_increment(manifest['project'] + '/n%s_des.log' % current,
                                                       cursors.get(current, {}), progress)
            progresses[current] = progress
            if current not in progress_times or old_accepted != progress.get('accepted_time_s'):
                progress_times[current] = epoch
        alerts = []
        if state['status'] in ('running', 'launching') and not worker_alive:
            alerts.append('worker_not_alive')
        if state['status'] == 'running' and epoch - progress_times.get(current, epoch) >= 900:
            alerts.append('no_confirmed_progress_for_15_minutes')
        fs = os.statvfs(root)
        if fs.f_bavail * fs.f_frsize < 1024 ** 3:
            alerts.append('free_disk_below_1GiB')
        sample = {'schema_version': 1, 'run_id': manifest['run_id'], 'server_utc': agent.now(),
                  'server_epoch': epoch, 'state': state['status'], 'current_node': state.get('current_node'),
                  'nodes': dict((n, v['status']) for n, v in state['nodes'].items()),
                  'progress': progress, 'last_progress_epoch': progress_times.get(current),
                  'worker_alive': worker_alive, 'alerts': alerts, 'free_bytes': fs.f_bavail * fs.f_frsize,
                  'resources': resource_sample(live, resources, epoch), 'interval_seconds': interval,
                  'progress_mapping': '118u template: gate 0..100 s, drain 100..200 s; not wall time or ETA'}
        agent.save(dest + '/latest.json', sample)
        with open(dest + '/telemetry.jsonl', 'a') as stream:
            stream.write(json.dumps(sample, sort_keys=True) + '\n')
        event = json.dumps([sample['nodes'], current, progress.get('phase'), progress.get('accepted_time_s'), alerts], sort_keys=True)
        if event != last_event:
            with open(dest + '/events.jsonl', 'a') as stream:
                stream.write(json.dumps({'server_utc': sample['server_utc'], 'nodes': sample['nodes'],
                                         'current_node': current, 'progress': progress, 'alerts': alerts}) + '\n')
            last_event = event
        if state['status'] in agent.TERMINAL and not live:
            break
        time.sleep(interval)


if __name__ == '__main__':
    observe(sys.argv[1])
