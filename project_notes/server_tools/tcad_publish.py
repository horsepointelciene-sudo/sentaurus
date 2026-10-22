"""Prepare a sanitized public snapshot and validate the staged Git files before push."""
import argparse
import csv
import json
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
REPLACEMENTS = [
    (r'D:\onedrive\Desktop\TCAD_Work', '<LOCAL_WORKSPACE>'),
    ('D:/onedrive/Desktop/TCAD_Work', '<LOCAL_WORKSPACE>'),
    (r'C:\Users\30757', '<LOCAL_USER>'),
    ('C:/Users/30757', '<LOCAL_USER>'),
    ('/home/liangxun/STDB/Codex', '<REMOTE_WORKSPACE>'),
    ('/home/liangxun', '<REMOTE_USER>'),
]


def redact(value):
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items() if k not in ('ssh_config', 'active_processes', 'worker', 'process', 'cmdline', 'log_tail', 'job_tail')}
    if isinstance(value, list):return [redact(x) for x in value]
    if isinstance(value, str):
        for old, new in REPLACEMENTS:value = value.replace(old, new)
    return value


def public_report(source):
    source = Path(source).resolve()
    if not source.is_relative_to((ROOT / 'reports').resolve()) or 'public' in source.parts:
        raise ValueError('Expected a local report below reports, outside public')
    data = json.loads((source / 'analysis.json').read_text(encoding='utf-8'))
    dest = ROOT / 'reports/public' / data['run_id'] / source.name
    dest.mkdir(parents=True, exist_ok=True)
    allowed_directories = {'data', 'figures'}
    allowed_directories.update(Path(p['file']).parts[0] for p in data.get('spatial', {}).get('plots', []))
    for path in source.rglob('*'):
        if not path.is_file():continue
        relative = path.relative_to(source)
        if len(relative.parts) > 1 and relative.parts[0] not in allowed_directories:continue
        if path.suffix.lower() not in ('.md','.html','.json','.csv','.png','.svg'):continue
        if path.name in ('export_record.json','artifact_manifest.json'):continue
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix.lower() == '.png':shutil.copy2(path,target)
        elif path.suffix.lower() == '.json':
            value = json.loads(path.read_text(encoding='utf-8'))
            target.write_text(json.dumps(redact(value),ensure_ascii=False,indent=2),encoding='utf-8')
        else:
            text = redact(path.read_text(encoding='utf-8-sig'))
            if path.name == 'GUI_STEPS.md':
                text += '\n公开版不包含带本地路径的 GUI 脚本；请在本机原始报告的同名空间目录中找到 export_in_svisual_gui.tcl。\n'
            target.write_text(text,encoding='utf-8')
    from tcad_reporting import sha, render_report
    # Render from sanitized structured data so placeholders are escaped in HTML.
    render_report(dest, json.loads((dest/'analysis.json').read_text(encoding='utf-8')))
    files={p.relative_to(dest).as_posix():{'sha256':sha(p),'bytes':p.stat().st_size} for p in dest.rglob('*') if p.is_file() and p.name!='artifact_manifest.json'}
    (dest/'artifact_manifest.json').write_text(json.dumps({'public_copy':True,'original_evidence_retained_locally':True,'files':files},ensure_ascii=False,indent=2),encoding='utf-8')
    return dest


def audit_staged():
    result=subprocess.run(['git','-C',str(ROOT),'diff','--cached','--name-only','-z'],capture_output=True,check=True)
    names=[n for n in result.stdout.decode('utf-8').split('\0') if n]
    forbidden={'.tdr','.plt','.sav','.pdf','.docx','.pptx','.zip','.pem','.key','.log'}
    secrets=re.compile(rb'gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----')
    errors=[];total=0
    for name in names:
        path=Path(name)
        if not (name in ('.gitignore','.gitattributes','AGENTS.md','README.md','WORKFLOW_START_HERE.md') or name.startswith(('tasks/','project_notes/server_tools/','reports/public/','projects/4E17SPICE_1.18u/'))):
            errors.append(name+': outside publish allowlist');continue
        if path.suffix.lower() in forbidden or path.name=='tcad_118u.json' or any(x in path.parts for x in ('raw','.workflow_deps','.workflow_backups','example','sentaurus_manual')):
            errors.append(name+': excluded type');continue
        blob=subprocess.run(['git','-C',str(ROOT),'show',':'+name],capture_output=True,check=True).stdout
        total+=len(blob)
        if len(blob)>25*1024*1024:errors.append(name+': exceeds 25 MiB publication limit')
        if secrets.search(blob):errors.append(name+': credential-like content')
    if not names:errors.append('No staged files')
    if errors:raise ValueError('\n'.join(errors))
    return {'files':len(names),'bytes':total,'audit':'allowlist, excluded formats, file sizes, credential signatures checked'}


if __name__=='__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--report',type=Path,action='append')
    p.add_argument('--audit-staged',action='store_true')
    a=p.parse_args()
    if a.report:
        outputs=[public_report(s) for s in a.report]
        index=ROOT/'reports/public/README.md'
        rows=['# TCAD 结果与报告','','公开版已去除连接配置、原始日志和机器路径；完整证据保留在本地。','']
        for report in sorted((ROOT/'reports/public').glob('*/*/REPORT.md')):
            rows.append('- [%s / %s](%s)'%(report.parents[1].name,report.parent.name,report.relative_to(index.parent).as_posix()))
        index.write_text('\n'.join(rows)+'\n',encoding='utf-8')
        print(json.dumps({'public_reports':[str(p) for p in outputs]},ensure_ascii=False))
    if a.audit_staged:print(json.dumps(audit_staged(),ensure_ascii=False))
