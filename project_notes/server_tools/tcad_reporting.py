"""Traceable local analysis, scientific plots and offline reports. Never submits simulations."""
import argparse
import csv
import datetime as dt
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
FIELDS = ['time', 'v(1)', 'v(2)', 'i(vd,2)', 'i(ppl1,2)', 'i(noppl1,2)', 'i(vg,1)', 'i(ppl1,1)', 'i(noppl1,1)']
COLORS = ['#0072B2', '#D55E00', '#009E73', '#CC79A7', '#E69F00', '#56B4E9', '#000000', '#999999']


def stamp():
    return dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def dump(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def read_curve(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != FIELDS:
            raise ValueError('Unexpected circuit columns: ' + repr(reader.fieldnames))
        rows = [{k: float(v) for k, v in row.items()} for row in reader]
    if not rows or any(not math.isfinite(v) for row in rows for v in row.values()):
        raise ValueError('Empty or non-finite data')
    if any(b['time'] <= a['time'] or b['v(2)'] <= a['v(2)'] for a, b in zip(rows, rows[1:])):
        raise ValueError('Expected increasing time and drain sweep; refusing to sort away evidence')
    if any(abs(r['v(1)'] - 15) > 1e-6 or abs(r['v(2)'] - .1 * (r['time'] - 100)) > 1e-6 for r in rows):
        raise ValueError('Curve differs from reviewed 118u ramp; do not apply its metric mapping')
    return rows


def compare_svisual(rows, reference):
    with Path(reference).open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        names = [n.replace(':', ',') for n in reader.fieldnames]
        other = [{k: float(v) for k, v in zip(names, row.values())} for row in reader]
    if names != FIELDS or len(rows) != len(other):
        raise ValueError('SVisual row/column mismatch')
    failures = []
    maximum = 0.0
    for i, (left, right) in enumerate(zip(rows, other)):
        for field in FIELDS:
            delta = abs(left[field] - right[field])
            maximum = max(maximum, delta)
            if not math.isfinite(right[field]) or delta > max(1e-18, abs(left[field]) * 5e-8):
                failures.append({'row': i, 'field': field, 'delta': delta})
    return {'passed': not failures, 'rows': len(rows), 'max_absolute_difference': maximum,
            'tolerance': '5e-8 relative OR 1e-18 absolute; observed SVisual export has about 8 significant digits',
            'failures': failures, 'reference_sha256': sha(reference)}


def metrics(rows):
    endpoint = next((r for r in rows if abs(r['v(2)'] - 10) <= 1e-8), None)
    kcl = [r['i(vd,2)'] + r['i(ppl1,2)'] + r['i(noppl1,2)'] for r in rows]
    result = {'points': len(rows), 'voltage_min_V': rows[0]['v(2)'], 'voltage_max_V': rows[-1]['v(2)'],
              'Id_10V_A': endpoint['i(vd,2)'] if endpoint else None,
              'PPL_current_10V_raw_A': endpoint['i(ppl1,2)'] if endpoint else None,
              'NOPPL_current_10V_raw_A': endpoint['i(noppl1,2)'] if endpoint else None,
              'PPL_fraction_10V': -endpoint['i(ppl1,2)'] / endpoint['i(vd,2)'] if endpoint and abs(endpoint['i(vd,2)']) > 1e-12 else None,
              'max_abs_KCL_residual_A': max(map(abs, kcl)), 'normalization': 'none; existing AreaFactor retained',
              'sign_convention': 'raw supply i(vd,2), raw branches i(ppl1,2), i(noppl1,2); their sum is KCL residual'}
    return result


def interpolate(rows, voltage):
    if voltage < rows[0]['v(2)'] or voltage > rows[-1]['v(2)']:
        raise ValueError('No extrapolation allowed')
    for row in rows:
        if row['v(2)'] == voltage:
            return row['i(vd,2)']
    for a, b in zip(rows, rows[1:]):
        if a['v(2)'] < voltage < b['v(2)']:
            f = (voltage - a['v(2)']) / (b['v(2)'] - a['v(2)'])
            return a['i(vd,2)'] + f * (b['i(vd,2)'] - a['i(vd,2)'])
    raise ValueError('Could not align voltage')


def curve_difference(a, b):
    lo, hi = max(a[0]['v(2)'], b[0]['v(2)']), min(a[-1]['v(2)'], b[-1]['v(2)'])
    grid = sorted(set(r['v(2)'] for rows in (a, b) for r in rows if lo <= r['v(2)'] <= hi))
    if len(grid) < 2:
        return None
    return {'rms_A': math.sqrt(sum((interpolate(a, v) - interpolate(b, v)) ** 2 for v in grid) / len(grid)),
            'voltage_min_V': lo, 'voltage_max_V': hi, 'points': len(grid),
            'method': 'RMS on union of common voltage samples; linear interpolation only, no extrapolation'}


def select_spatial(entries, node_order):
    selected, comparisons = [], []
    for intensity, baseline in ((0, 15), (5, 16)):
        base = next((x for x in entries if x['node'] == baseline), None)
        selected.append({'node': baseline, 'reason': 'baseline', 'available_curve': bool(base)})
        choices = []
        if base:
            for item in entries:
                if item['parameters'].get('int') == intensity and item['node'] != baseline:
                    difference = curve_difference(base['rows'], item['rows'])
                    if difference:
                        comparisons.append(dict(difference, baseline=baseline, node=item['node']))
                        choices.append((difference['rms_A'], item['node']))
        if choices:
            best = sorted(choices, key=lambda x: (-x[0], x[1]))[0][1]
            selected.append({'node': best, 'reason': 'largest_RMS_difference', 'available_curve': True})
    return selected, comparisons


def pyplot():
    deps = ROOT / '.workflow_deps'
    if deps.is_dir():
        sys.path.insert(0, str(deps))
    os.environ.setdefault('MPLCONFIGDIR', str(ROOT / '.workflow_cache' / 'matplotlib'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams.update({'font.size': 12, 'axes.titlesize': 14, 'axes.labelsize': 12,
                         'legend.fontsize': 12, 'xtick.labelsize': 12, 'ytick.labelsize': 12,
                         'lines.linewidth': 1.8, 'svg.fonttype': 'none', 'savefig.dpi': 300})
    return plt


def plot_curves(entries, dest, run_id):
    if not entries:
        return []
    plt = pyplot()
    figures = []
    out = dest / 'figures'
    out.mkdir()
    def save(fig, name, caption):
        fig.text(.02, .015, run_id + ' | raw signed current, no additional AreaFactor', fontsize=12)
        fig.tight_layout(rect=(0, .06, 1, 1))
        for ext in ('png', 'svg'):
            fig.savefig(out / (name + '.' + ext), dpi=300, facecolor='white')
        plt.close(fig)
        figures.append({'file': 'figures/' + name + '.png', 'svg': 'figures/' + name + '.svg', 'caption': caption})
    for item in entries:
        rows, node = item['rows'], item['node']
        x = [r['v(2)'] for r in rows]
        fig, axes = plt.subplots(2, 1, figsize=(10, 9), sharex=True, gridspec_kw={'height_ratios': [3, 1]})
        for field, color in zip(['i(vd,2)', 'i(ppl1,2)', 'i(noppl1,2)'], COLORS):
            axes[0].plot(x, [r[field] for r in rows], label=field, color=color)
        axes[0].set(ylabel='Signed current (A)', title='Node %s | int=%s | rho_c_drain=%s' % (node, item['parameters'].get('int'), item['parameters'].get('rho_c_drain')))
        axes[0].legend()
        if item['metrics']['Id_10V_A'] is not None:
            axes[0].text(.03, .94, 'I_D(10 V) = %.8g A' % item['metrics']['Id_10V_A'], transform=axes[0].transAxes, va='top')
        axes[1].plot(x, [r['i(vd,2)'] + r['i(ppl1,2)'] + r['i(noppl1,2)'] for r in rows], color=COLORS[3])
        axes[1].set(xlabel='Drain voltage (V)', ylabel='KCL residual (A)')
        for ax in axes:
            ax.grid(alpha=.25)
        save(fig, 'node%d_currents' % node, '节点 %s：保留原始电流方向；下图为三支路电流之和。' % node)
    groups = [('int_%s' % value, [e for e in entries if e['parameters'].get('int') == value], 'int=%s' % value) for value in (0, 5)]
    resistances = sorted({e['parameters'].get('rho_c_drain') for e in entries if e['parameters'].get('rho_c_drain') is not None}, key=float)
    groups += [('rho_' + str(value), [e for e in entries if e['parameters'].get('rho_c_drain') == value], 'rho_c_drain=%s' % value) for value in resistances]
    for name, items, title in groups:
        if len(items) < 2:
            continue
        fig, ax = plt.subplots(figsize=(10, 7))
        for i, entry in enumerate(items):
            label = 'n%s int=%s rho=%s' % (entry['node'], entry['parameters'].get('int'), entry['parameters'].get('rho_c_drain'))
            ax.plot([r['v(2)'] for r in entry['rows']], [r['i(vd,2)'] for r in entry['rows']], color=COLORS[i % 8], linestyle=['-', '--', '-.', ':'][i % 4], label=label)
        ax.set(xlabel='Drain voltage (V)', ylabel='Supply drain current (A)', title=title)
        ax.legend(); ax.grid(alpha=.25)
        save(fig, name, '共同条件下的曲线对比：' + title)
    return figures


def build_report(config, run_id, collection, destination, live=None, historical=False):
    from tcad_workflow import plt_to_csv
    destination.mkdir(parents=True, exist_ok=False)
    (destination / 'data').mkdir()
    evidence = json.loads((collection / 'retrieval_manifest.json').read_text(encoding='utf-8')) if collection else {}
    snapshot = evidence.get('status_snapshot', {})
    if live and live.get('remote', {}).get('status_snapshot'):
        snapshot = live['remote']['status_snapshot']
    params = {str(n): detail['parameters'] for n, detail in snapshot.get('nodes', {}).items()}
    if historical:
        params = {'5': {'int': 0, 'rho_c_drain': '1e-4'}}
    entries, errors = [], list(evidence.get('errors', []))
    for raw in sorted((collection / 'raw').rglob('*_circuit.plt')) if collection else []:
        # Only the reviewed final drain sweep, never the earlier gate ramp.
        match = re.fullmatch(r'(dark|light)_IdVd_n(\d+)_circuit\.plt', raw.name)
        if not match:
            continue
        node = int(match.group(2))
        try:
            relative = raw.relative_to(collection / 'raw').as_posix()
            expected = evidence.get('files', {}).get(relative) or evidence.get('files', {}).get('raw/' + relative)
            if not expected or sha(raw) != expected['sha256']:
                raise ValueError('Missing/mismatching retrieval hash')
            target = destination / 'data' / (raw.stem + '.csv')
            plt_to_csv(raw, target)
            rows = read_curve(target)
            if str(node) not in params:
                raise ValueError('Missing verified parameter mapping')
            item = {'node': node, 'parameters': params[str(node)], 'rows': rows, 'metrics': metrics(rows),
                    'source_file': str(raw), 'source_sha256': sha(raw), 'csv': 'data/' + target.name,
                    'state': snapshot.get('state', {}).get('nodes', {}).get(str(node), {}).get('status', 'historical_verified')}
            if historical:
                reference = collection / 'svisual_export/node5_svisual_raw.csv'
                item['svisual_validation'] = compare_svisual(rows, reference)
                if not item['svisual_validation']['passed']:
                    raise ValueError('SVisual cross-validation failed')
            entries.append(item)
        except Exception as exc:
            errors.append({'file': raw.name, 'stage': 'analysis', 'error': str(exc)})
    selected, comparisons = select_spatial(entries, config.get('nodes', [])) if not historical else ([{'node': 5, 'reason': 'historical_validation'}], [])
    for n in config.get('nodes', []):
        if snapshot.get('state', {}).get('nodes', {}).get(str(n), {}).get('status') == 'failed':
            if n not in [s['node'] for s in selected]:
                selected.append({'node': n, 'reason': 'first_failed'})
            break
    data_export_ok = not errors
    plot_ok = True
    try:
        figures = plot_curves(entries, destination, run_id)
    except Exception as exc:
        figures, plot_ok = [], False
        errors.append({'stage': 'plot', 'error': repr(exc)})
    manifest_path = collection / 'raw/manifest.json' if collection else None
    manifest = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path and manifest_path.exists() else {}
    analysis = {'schema_version': 1, 'run_id': run_id, 'created_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
                'source_commit': manifest.get('source_commit'), 'task_id': manifest.get('task_id'),
                'project': snapshot.get('project') or manifest.get('project'), 'collection': str(collection) if collection else None,
                'source_input_sha256': manifest.get('source_input_sha256', {}),
                'connection_ok': live.get('connection_ok', True) if live else None,
                'simulation_status': 'historical_verified' if historical else snapshot.get('observed_status', 'unknown'),
                'snapshot': snapshot, 'metrics': [{k: v for k, v in e.items() if k != 'rows'} for e in entries],
                'curve_comparisons': comparisons, 'spatial_selection': selected,
                'tdr_manifest': evidence.get('tdr_retained_on_server', {}), 'errors': errors,
                'data_export_ok': data_export_ok, 'plot_ok': plot_ok, 'spatial_status': 'pending',
                'figures': figures, 'report_complete': False}
    with (destination / 'metrics.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        keys = ['node', 'int', 'rho_c_drain', 'points', 'voltage_max_V', 'Id_10V_A', 'PPL_fraction_10V', 'max_abs_KCL_residual_A']
        writer = csv.DictWriter(stream, fieldnames=keys); writer.writeheader()
        for e in entries:
            values = dict(e['metrics'], node=e['node'], **e['parameters'])
            writer.writerow({k: values.get(k) for k in keys})
    dump(destination / 'analysis.json', analysis)
    render_report(destination, analysis)
    return {'directory': str(destination), 'curves': len(entries), 'figures': len(figures), 'errors': errors}


def render_report(folder, data):
    snapshot = data.get('snapshot', {})
    monitor = snapshot.get('monitor') or {}
    progress = monitor.get('progress', {})
    def shown(value):
        return '未记录或不适用' if value is None else str(value)
    lines = ['# TCAD 运行与结果报告', '', '运行编号：`%s`' % data['run_id'], '',
             '更新时间：%s。此页面是静态快照，需再次 refresh 才会更新。' % data['created_utc'], '',
             '## 问题与修改', '', '本轮记录指定节点的已有输入、运行与结果；报告生成不修改物理模型、不提交新仿真。',
             '研究假设及授权修改范围见对应任务文件。', '',
             '## 输入与环境', '', '实际工程：`%s`' % shown(data.get('project')),
             '输入提交：`%s`。历史记录无提交时保留为空，以输入哈希为依据。' % shown(data.get('source_commit')),
             '任务：`%s`；回收证据：`%s`。' % (shown(data.get('task_id')), shown(data.get('collection'))), '',
             '## 运行与监控', '', '状态：**%s**；连接：%s。' % (data['simulation_status'], shown(data.get('connection_ok'))),
             '最近服务器监控：%s；当前节点：%s；阶段：%s；已接受仿真时间：%s s；漏压：%s V。' %
             tuple(shown(v) for v in (monitor.get('server_utc'), monitor.get('current_node'), progress.get('phase'), progress.get('accepted_time_s'), progress.get('accepted_vds_V'))),
             '上述时间为仿真时间，不是运行耗时；不预测剩余时间。',
             '监控提示：`%s`' % monitor.get('alerts', []), '',
             '| 节点 | 状态 | 实际求解器版本 |', '|---|---|---|']
    for node, state in snapshot.get('state', {}).get('nodes', {}).items():
        lines.append('| %s | %s | %s |' % (node, state['status'], snapshot.get('nodes', {}).get(node, {}).get('solver_version', '未启动或未识别')))
    lines += ['', '## 曲线与指标', '', '| 节点 | int | rho_c_drain | 点数 | 最大 Vds/V | Id(10V)/A | 最大 KCL 残差/A |', '|---|---|---|---|---|---|---|']
    for e in data['metrics']:
        m = e['metrics']
        lines.append('| %s | %s | %s | %s | %s | %s | %s |' % (e['node'], e['parameters']['int'], e['parameters']['rho_c_drain'], m['points'], m['voltage_max_V'], m['Id_10V_A'], m['max_abs_KCL_residual_A']))
    if not data['metrics']:
        lines += ['', '尚无通过校验的完整漏压扫描曲线；当前阶段不生成器件性能结论。']
    lines += ['', '保留原始有符号电流，不再次乘 AreaFactor。KCL 残差为电源与两个器件支路电流之和。10 V 指标仅使用实际到达的点，不外推。', '', '## 图表']
    for figure in data['figures']:
        lines += ['', '![%s](%s)' % (figure['caption'], figure['file']), '', figure['caption']]
    lines += ['', '## 空间分析', '', '状态：%s。' % data.get('spatial_status', 'pending')]
    spatial = data.get('spatial', {})
    for item in spatial.get('plots', []):
        lines += ['', '![%s](%s)' % (item.get('caption', ''), item['file'])]
    if not spatial.get('plots'):
        lines += ['', '尚未完成空间图验证；不能仅凭曲线推断高场、热或电流拥挤位置。']
    spatial_dirs = sorted({str(Path(item['file']).parent).replace('\\', '/') for item in spatial.get('plots', [])})
    spatial_location = '、'.join('`%s/`' % p for p in spatial_dirs) if spatial_dirs else '尚未生成空间导出目录'
    lines += ['', '空间极值、截线坐标和状态依据：%s；数值清单及 CSV 与截线图存放在同一目录。' % spatial_location, '',
              '## 结论、限制与下一步', '',
              '- 求解完成、数据导出、出图和报告完整性分别记录。失败、未运行与缺失数据分别保留。',
              '- 单次求解收敛不等于物理模型或网格精度已验证。',
              '- 当前 Id–Vd 数据不用于推导 Vth、击穿电压或热失控结论。',
              '- 下一步仅提出建议：补齐缺失证据，核对曲线与空间分布；新计算需另行明确要求。', '',
              '## 证据与异常', '', '输入哈希、原始结果哈希、指标算法及 TDR 清单见 analysis.json；指标见 metrics.csv。', '',
              '异常数：%d。' % len(data['errors'])]
    for error in data['errors']:
        lines.append('- ' + json.dumps(error, ensure_ascii=False))
    markdown = '\n'.join(lines) + '\n'
    (folder / 'REPORT.md').write_text(markdown, encoding='utf-8')
    # Render an intentionally small Markdown subset without external web assets.
    blocks = []
    for line in lines:
        image = re.fullmatch(r'!\[(.*?)\]\((.*?)\)', line)
        if image:
            blocks.append('<figure><img src="%s" alt="%s"><figcaption>%s</figcaption></figure>' % (html.escape(image.group(2), quote=True), html.escape(image.group(1)), html.escape(image.group(1))))
        elif line.startswith('# '):
            blocks.append('<h1>' + html.escape(line[2:]) + '</h1>')
        elif line.startswith('## '):
            blocks.append('<h2>' + html.escape(line[3:]) + '</h2>')
        elif line.startswith('|'):
            if not line.startswith('|---'):
                blocks.append('<div class="row">' + ''.join('<span>' + html.escape(cell.strip()) + '</span>' for cell in line.strip('|').split('|')) + '</div>')
        elif line:
            blocks.append('<p>' + html.escape(line) + '</p>')
    page = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>TCAD report</title><style>body{font:16px/1.7 system-ui;max-width:1180px;margin:32px auto;padding:0 24px;color:#172435}h2{margin-top:36px;border-bottom:1px solid #ddd}img{max-width:100%;height:auto}.row{display:flex;border-bottom:1px solid #ddd;overflow-wrap:anywhere}.row span{flex:1;padding:8px;min-width:0}figure{margin:24px 0}figcaption{color:#46566b}p{overflow-wrap:anywhere}</style>' + '\n'.join(blocks) + '</html>'
    (folder / 'report.html').write_text(page, encoding='utf-8')
    files = {p.relative_to(folder).as_posix(): {'sha256': sha(p), 'bytes': p.stat().st_size} for p in folder.rglob('*') if p.is_file() and p.name != 'artifact_manifest.json'}
    dump(folder / 'artifact_manifest.json', {'run_id': data['run_id'], 'files': files})


def refresh_report(config, run_id, local, result):
    collection = Path(result['collection']['directory']) if result.get('collection') else None
    if collection is None:
        previous = sorted(p for p in local.glob('collection_*') if (p / 'retrieval_manifest.json').exists())
        collection = previous[-1] if previous else None
    destination = ROOT / 'reports' / run_id / ('report_' + stamp())
    report = build_report(config, run_id, collection, destination, live=result)
    try:
        from tcad_visualization import attach_spatial
        report['spatial'] = attach_spatial(config, destination)
    except Exception as exc:
        data = json.loads((destination / 'analysis.json').read_text(encoding='utf-8'))
        data['errors'].append({'stage': 'spatial', 'error': repr(exc)})
        data['spatial_status'] = 'export_failed'
        dump(destination / 'analysis.json', data); render_report(destination, data)
        report['spatial'] = {'error': repr(exc)}
    (ROOT / 'reports/LATEST.md').write_text('# 最新报告\n\n[%s](%s/REPORT.md)\n' % (run_id, destination.relative_to(ROOT / 'reports').as_posix()), encoding='utf-8')
    return report


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--historical', type=Path, required=True)
    args = parser.parse_args()
    out = ROOT / 'reports' / 'historical_node5' / ('report_' + stamp())
    print(json.dumps(build_report({'nodes': [5]}, 'historical_node5', args.historical, out, historical=True), ensure_ascii=False, indent=2))
