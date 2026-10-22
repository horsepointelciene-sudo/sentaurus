"""SVisual postprocessing of hash-verified, existing results; never invokes a solver."""
import argparse
import base64
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import subprocess
import uuid

HERE = Path(__file__).resolve().parent


def remote_python(config, source, timeout=60):
    from tcad_workflow import ssh_args
    proc = subprocess.run(ssh_args(config) + [config['remote_python'] + ' -'], input=source.encode(), capture_output=True, timeout=timeout)
    if proc.returncode:
        raise RuntimeError(proc.stderr.decode('utf-8', 'replace')[-4000:])
    return json.loads(proc.stdout)


def tcl_word(value):
    if any(c in str(value) for c in '{}\n\r\\'):
        raise ValueError('Unsupported Tcl value')
    return '{' + str(value) + '}'


def export(config, inputs, script, destination, graphics=True):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    for item in inputs:
        if not re.fullmatch(r'/[A-Za-z0-9_./-]+', item['remote_path']) or not re.fullmatch(r'[a-f0-9]{64}', item['sha256']):
            raise ValueError('Invalid source manifest')
    remote_dir = config['parent'] + '/.tcad-postprocess/' + uuid.uuid4().hex
    request = {'inputs': inputs, 'script': script, 'directory': remote_dir,
               'command': [config['tools'] + '/svisual', '-tcl', '-bx' if graphics else '-b', '-nowait', '--max_threads', '1', 'export.tcl']}
    payload = base64.b64encode(json.dumps(request).encode()).decode()
    source = '''from __future__ import print_function
import os,sys,json,base64,hashlib,subprocess,time,datetime,signal
r=json.loads(base64.b64decode(%r))
def sha(p):
 h=hashlib.sha256()
 with open(p,'rb') as f:
  for b in iter(lambda:f.read(1048576),b''):h.update(b)
 return h.hexdigest()
for item in r['inputs']:
 if sha(item['remote_path'])!=item['sha256']:raise ValueError('Source changed before SVisual')
if not os.path.isdir(os.path.dirname(r['directory'])):os.mkdir(os.path.dirname(r['directory']),0o700)
os.mkdir(r['directory'],0o700)
script=r['directory']+'/export.tcl'
open(script,'wb').write(r['script'].encode('utf-8'))
started=datetime.datetime.utcnow().isoformat()+'Z'
with open(r['directory']+'/stdout.log','wb') as out,open(r['directory']+'/stderr.log','wb') as err,open(os.devnull,'rb') as inp:
 p=subprocess.Popen(r['command'],cwd=r['directory'],stdin=inp,stdout=out,stderr=err,preexec_fn=os.setsid)
 limit=time.time()+240
 while p.poll() is None and time.time()<limit:time.sleep(.5)
 timed_out=p.poll() is None
 if timed_out:os.killpg(p.pid,signal.SIGTERM)
 end=time.time()+5
 while p.poll() is None and time.time()<end:time.sleep(.2)
 if p.poll() is None:os.killpg(p.pid,signal.SIGKILL)
 p.wait()
rc=p.returncode
unchanged=all(sha(x['remote_path'])==x['sha256'] for x in r['inputs'])
result={'command':r['command'],'directory':r['directory'],'exit_code':rc,'timed_out':timed_out,'started_server_utc':started,'finished_server_utc':datetime.datetime.utcnow().isoformat()+'Z','source_unchanged':unchanged,'inputs':r['inputs'],'files':{}}
for n in os.listdir(r['directory']):
 p=r['directory']+'/'+n
 if os.path.isfile(p):result['files'][n]={'remote_path':p,'sha256':sha(p),'bytes':os.path.getsize(p)}
open(r['directory']+'/export_record.json','w').write(json.dumps(result,indent=2))
print(json.dumps(result))
''' % payload
    result = remote_python(config, source, timeout=280)
    result['client_utc'] = dt.datetime.now(dt.timezone.utc).isoformat()
    from tcad_workflow import digest
    scp = str(Path(config['ssh_exe']).with_name('scp.exe'))
    for name, item in result['files'].items():
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', name):
            raise ValueError('Unsupported output name')
        proc = subprocess.run([scp, '-F', config['ssh_config'], '-o', 'BatchMode=yes', config['host'] + ':' + item['remote_path'], str(destination / name)], capture_output=True, timeout=120)
        if proc.returncode or digest(destination / name) != item['sha256']:
            raise RuntimeError('SVisual artifact transfer failed: ' + name)
    (destination / 'export_record.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


def discover_historical(config):
    source = '''import os,json,hashlib
p='/home/liangxun/STDB/Codex/4E17SPICE_dark_flowcheck_20261019'
result=[]
for n in sorted(os.listdir(p)):
 if 'n5_' in n and n.endswith('.tdr'):
  f=p+'/'+n
  result.append({'remote_path':f,'sha256':hashlib.sha256(open(f,'rb').read()).hexdigest(),'bytes':os.path.getsize(f),'node':5})
print(json.dumps(result))
'''
    return remote_python(config, source)


SPATIAL_FIELDS = ['ElectrostaticPotential', 'Abs(ElectricField-V)', 'Abs(TotalCurrentDensity-V)', 'TotalCurrentDensity-X', 'TotalCurrentDensity-Y', 'eDensity', 'hDensity']


def numerical_script(inputs):
    parts = ['set failed 0']
    for index, item in enumerate(inputs):
        prefix = 'tdr_%02d' % index
        parts.append('set d [load_file %s]\nset p [create_plot -dataset $d]' % tcl_word(item['remote_path']))
        parts.append('set prefix ' + prefix)
        parts.append('''set available [list_fields -dataset $d]
set regions [list_regions -plot $p]
set f [open "${prefix}_extrema.csv" w]
puts $f "field,min,max,max_x_um,max_y_um"
foreach field {ElectrostaticPotential Abs(ElectricField-V) Abs(TotalCurrentDensity-V) TotalCurrentDensity-X TotalCurrentDensity-Y eDensity hDensity} {
 if {[lsearch -exact $available $field] < 0} {puts "TCAD_MISSING_FIELD:$field"; continue}
 set lo [calculate_field_value -plot $p -field $field -min]
 set hi [calculate_field_value -plot $p -field $field -max]
 set xyz [lindex $hi 1]
 puts $f [join [list $field [lindex $lo 0] [lindex $hi 0] [lindex $xyz 0] [lindex $xyz 1]] ,]
}
close $f
set regionArgs [list]
set reason whole_geometry_midline
if {[lsearch -exact $regions Ref_channel] >= 0} {
 set regionArgs [list -regions Ref_channel]
 set reason Ref_channel_midline
}
set xmin [lindex [eval [list calculate_field_value -plot $p -field X -min] $regionArgs] 0]
set xmax [lindex [eval [list calculate_field_value -plot $p -field X -max] $regionArgs] 0]
set ymin [lindex [eval [list calculate_field_value -plot $p -field Y -min] $regionArgs] 0]
set ymax [lindex [eval [list calculate_field_value -plot $p -field Y -max] $regionArgs] 0]
if {[regexp -nocase {nan|inf} "$xmin $xmax $ymin $ymax"]} {
 set reason whole_geometry_midline_Ref_channel_empty
 set xmin [lindex [calculate_field_value -plot $p -field X -min] 0]
 set xmax [lindex [calculate_field_value -plot $p -field X -max] 0]
 set ymin [lindex [calculate_field_value -plot $p -field Y -min] 0]
 set ymax [lindex [calculate_field_value -plot $p -field Y -max] 0]
}
set at [expr {($xmin+$xmax)/2.0}]
set f [open "${prefix}_geometry.csv" w]
puts $f "reason,x_min_um,x_max_um,y_min_um,y_max_um,cut_x_um,tdr_state_index"
puts $f [join [list $reason $xmin $xmax $ymin $ymax $at [get_plot_prop -plot $p -tdr_state_index]] ,]
close $f
set cut [create_cutline -plot $p -type x -at $at]
set variables [list Y]
foreach field {ElectrostaticPotential Abs(ElectricField-V) Abs(TotalCurrentDensity-V) TotalCurrentDensity-X TotalCurrentDensity-Y eDensity hDensity} {
 if {[lsearch -exact $available $field] >= 0} {lappend variables $field}
}
if {[catch {export_variables $variables -dataset $cut -filename "${prefix}_cutline.csv"} message]} {
 puts stderr "TCAD_CUTLINE_ERROR:$message"
 set failed 1
}
puts "TCAD_NUMERICAL_COMPLETE:$prefix"
''')
    parts.append('exit $failed\n')
    return '\n'.join(parts)


def attach_spatial(config, report_directory, historical=False):
    import csv
    from tcad_reporting import dump, pyplot, render_report
    folder = Path(report_directory)
    analysis = json.loads((folder / 'analysis.json').read_text(encoding='utf-8'))
    if historical:
        inputs = discover_historical(config)
    else:
        wanted = {item['node'] for item in analysis['spatial_selection']}
        inputs = [item for item in analysis['tdr_manifest'].values()
                  if item.get('role') == 'output' and item.get('node') in wanted and
                  re.search(r'(?:ppl1|noppl1)_n\d+_des\.tdr$', item['remote_path'])]
        inputs.sort(key=lambda x: (x['node'], x['remote_path']))
    if not inputs:
        analysis['spatial_status'] = 'no_eligible_TDR_yet'
        analysis['spatial'] = {'plots': [], 'reason': '所选节点尚无可验证的稳定 TDR；未调用 SVisual。'}
        dump(folder / 'analysis.json', analysis); render_report(folder, analysis)
        return analysis['spatial']
    spatial_name = 'spatial' if not (folder / 'spatial').exists() else 'spatial_' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    dest = folder / spatial_name
    result = export(config, inputs, numerical_script(inputs), dest, graphics=False)
    plots, geometries, extrema, warnings = [], [], [], []
    if result['exit_code'] or not result['source_unchanged']:
        warnings.append('SVisual numerical export incomplete; inspect export_record.json and stderr.log')
    plt = pyplot()
    scales = {}
    for index, item in enumerate(inputs):
        prefix = 'tdr_%02d' % index
        name = Path(item['remote_path']).name
        geo_file = dest / (prefix + '_geometry.csv')
        extrema_file = dest / (prefix + '_extrema.csv')
        if not geo_file.exists() or not extrema_file.exists():
            warnings.append('Missing geometry or extrema: ' + name); continue
        with geo_file.open() as stream:geometry = next(csv.DictReader(stream))
        geometries.append(dict(geometry, source=name, sha256=item['sha256']))
        with extrema_file.open() as stream:
            values = list(csv.DictReader(stream))
        extrema.append({'source': name, 'values': values})
        for row in values:
            field, lo, hi = row['field'], float(row['min']), float(row['max'])
            old = scales.get(field, [lo, hi]); scales[field] = [min(old[0], lo), max(old[1], hi)]
        curve_file = dest / (prefix + '_cutline.csv')
        if not curve_file.exists():
            warnings.append('Missing cutline: ' + name); continue
        with curve_file.open() as stream:
            reader = csv.DictReader(stream); rows = list(reader); fields = reader.fieldnames
        coordinates = [float(row['Y']) for row in rows]
        fig, axes = plt.subplots(3, 1, figsize=(10, 12), sharex=True)
        panels = [('ElectrostaticPotential', 'Potential (V)'), ('Abs(ElectricField-V)', 'Electric field (V/cm)'), ('eDensity', 'Carrier density (cm$^{-3}$)')]
        for ax, (field, label) in zip(axes, panels):
            if field in fields:
                ax.plot(coordinates, [float(row[field]) for row in rows], label=field, color='#0072B2')
            if field == 'eDensity' and 'hDensity' in fields:
                ax.plot(coordinates, [float(row['hDensity']) for row in rows], label='hDensity', color='#D55E00', linestyle='--')
                positive = all(float(row[field]) > 0 and float(row['hDensity']) > 0 for row in rows)
                if positive:ax.set_yscale('log')
            ax.set_ylabel(label); ax.legend(); ax.grid(alpha=.25)
        axes[-1].set_xlabel('Y coordinate (um)')
        axes[0].set_title(name + '\nX = %.6g um | %s' % (float(geometry['cut_x_um']), geometry['reason']))
        fig.tight_layout()
        for ext in ('png','svg'):fig.savefig(dest / (prefix + '_cutline.' + ext), dpi=300, facecolor='white')
        plt.close(fig)
        plots.append({'file': spatial_name + '/' + prefix + '_cutline.png', 'caption': name + '：沿记录的固定 X 截线；不是完整二维场图。'})
    # Ready-to-run GUI script shares scales across this report. It is not silently
    # executed after the verified Xvfb/GLIBC incompatibility on this server.
    gui = ['# Run in a logged-in SVisual GUI: File > Run Script; outputs go to this script directory.',
           'cd [file dirname [info script]]']
    for index, item in enumerate(inputs):
        gui += ['set d [load_file %s]' % tcl_word(item['remote_path']), 'set p [create_plot -dataset $d]',
                'set_plot_prop -plot $p -title %s -title_font_size 16 -show_legend' % tcl_word(Path(item['remote_path']).name)]
        for field, limits in scales.items():
            safe = re.sub('[^A-Za-z0-9_]', '_', field)
            lo, hi = limits
            if lo == hi:hi = lo + max(1e-12, abs(lo)*.01)
            gui += ['set_field_prop %s -plot $p -show_bands -show -range {%s %s}' % (tcl_word(field), lo, hi),
                    'export_view tdr_%02d_%s.png -plots $p -resolution 2400x1800 -format PNG' % (index, safe)]
    (dest / 'export_in_svisual_gui.tcl').write_text('\n'.join(gui) + '\n', encoding='utf-8')
    (dest / 'GUI_STEPS.md').write_text('''# 二维空间图待完成

已验证：本服务器 SVisual 支持 -bx，但自带 Xvfb 因缺少 GLIBC_2.26/2.27 无法运行；系统也没有 /usr/bin/Xvfb。未修改系统库。

数值极值、坐标和截线由 SVisual -b 实际导出。这里的截线图不是完整二维诊断。

在已有远程桌面会话中打开 Sentaurus Visual，运行 export_in_svisual_gui.tcl（将此脚本放在服务器可写的独立导图目录）。脚本使用原始 TDR 的绝对路径，并统一同一场量的色标范围；执行前核对源文件哈希。脚本的 GUI 导图部分尚未运行验证，若版本提示命令差异，使用 GUI 调整并保存新的操作记录。

若需要无头自动导图，由管理员提供与当前系统兼容的显示环境后，重新验收图片导出。不要通过升级服务器 libc 解决单项导图问题。
''', encoding='utf-8')
    analysis['spatial_status'] = 'numeric_verified_2D_images_blocked' if not warnings else 'numeric_partial_2D_images_blocked'
    analysis['spatial'] = {'plots': plots, 'inputs': inputs, 'geometries': geometries, 'extrema': extrema,
                           'shared_field_ranges': scales, 'warnings': warnings, 'source_unchanged': result['source_unchanged'],
                           'geometry_policy': 'midpoint of Ref_channel X extent; fallback explicitly labelled whole_geometry_midline',
                           'state_limit': 'TDR stored state index recorded; no invented intermediate bias snapshots',
                           'graphics_blocker': 'Bundled Xvfb requires unavailable GLIBC_2.26/2.27; no system Xvfb'}
    dump(dest / 'spatial_manifest.json', analysis['spatial'])
    dump(folder / 'analysis.json', analysis); render_report(folder, analysis)
    return {'plots': len(plots), 'warnings': warnings, 'status': analysis['spatial_status']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, default=HERE / 'tcad_118u.json')
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding='utf-8'))
    inputs = discover_historical(config)
    if not inputs:
        raise SystemExit('No historical TDR available')
    script = 'set d [load_file %s]\nset p [create_plot -dataset $d]\n' % tcl_word(inputs[0]['remote_path'])
    script += '''puts "TCAD_PROBE_DATASET=$d"
puts "TCAD_PROBE_COMMANDS=[lsort [info commands *field*]]"
puts "TCAD_PROBE_REGIONS=[list_regions -plot $p]"
foreach cmd {list_fields get_field_data get_plot_prop get_axis_prop} {
 if {[catch {$cmd -help} value]} {puts "HELP $cmd: $value"} else {puts "HELP $cmd: $value"}
}
if {[catch {list_fields -dataset $d} value]} {puts "FIELDS_ERROR: $value"} else {puts "FIELDS: $value"}
if {[catch {get_field_data X -dataset $d} value]} {puts "X_ERROR: $value"} else {puts "X_SAMPLE: [lrange $value 0 5]"}
exit 0
'''
    result = export(config, inputs[:1], script, args.output, graphics=False)
    print(json.dumps(result, indent=2))
