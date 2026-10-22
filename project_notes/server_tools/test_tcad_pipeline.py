"""Behavior tests for original-project mode, progress parsing, data and publication boundaries."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import tcad_batch_remote as remote
import tcad_monitor_remote as monitor
import tcad_reporting as reporting
import tcad_workflow as client
import tcad_publish as publish


class PipelineTests(unittest.TestCase):
    def test_public_snapshot_redacts_paths_processes_and_logs(self):
        value={'source':r'D:\onedrive\Desktop\TCAD_Work\results', 'ssh_config':'private',
               'snapshot':{'worker':{'cmdline':'private'},'log_tail':'raw license text',
                           'path':'/home/liangxun/STDB/Codex/project','sha256':'abc'}}
        result=publish.redact(value)
        self.assertNotIn('ssh_config',result)
        self.assertNotIn('worker',result['snapshot'])
        self.assertNotIn('log_tail',result['snapshot'])
        self.assertEqual(result['snapshot']['sha256'],'abc')
        self.assertTrue(result['source'].startswith('<LOCAL_WORKSPACE>'))

    def test_archive_preserves_old_output_when_source_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            project=Path(tmp,'project');project.mkdir()
            path=project/'n15_des.log';path.write_text('first result')
            remote.archive_node(tmp,str(project),15,0)
            path.write_text('second result')
            self.assertEqual(Path(tmp,'artifacts/project/n15_des.log').read_text(),'first result')
            with self.assertRaisesRegex(ValueError,'overwrite'):
                remote.archive_node(tmp,str(project),15,0)

    def test_plot_failure_is_separate_from_solver_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            collection=Path(tmp,'collection');collection.mkdir();(collection/'raw').mkdir()
            (collection/'retrieval_manifest.json').write_text(json.dumps({'status_snapshot':{'observed_status':'succeeded'}}))
            dest=Path(tmp,'report')
            with patch.object(reporting,'plot_curves',side_effect=RuntimeError('renderer failed')):
                reporting.build_report({'nodes':[]},'test',collection,dest)
            result=json.loads((dest/'analysis.json').read_text(encoding='utf-8'))
            self.assertEqual(result['simulation_status'],'succeeded')
            self.assertTrue(result['data_export_ok'])
            self.assertFalse(result['plot_ok'])

    def test_original_mode_no_project_or_snapshot_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, project = Path(tmp, 'run'), Path(tmp, 'source')
            project.mkdir()
            for name in ('gtree.dat', 'sdevice1_des.cmd', 'PPL.tdr'):
                (project / name).write_text('input ' + name)
            (project / 'dark_IdVd_n15_circuit.plt').write_text('previous result')
            (project / 'unrelated_n99_des.tdr').write_text('unrelated result')
            cfg = {'source': str(project), 'parent': tmp, 'nodes': remote.NODES,
                   'input_files': ['gtree.dat', 'sdevice1_des.cmd', 'PPL.tdr'], 'optional_inputs': [], 'tools': '/tools'}
            hashes = remote.hashes(str(project), cfg['input_files'])
            preflight = {'ready_to_prepare': True, 'input_sha256': hashes, 'free_bytes': 10**12}
            def preprocess(cmd, cwd, prefix, timeout):
                (project / 'gexec.cmd').write_text('graph')
                for node in remote.NODES:
                    (project / ('pp%d_des.cmd' % node)).write_text('generated')
                return {'exit_code': 0, 'timed_out': False, 'argv': cmd}
            with patch.object(remote, 'root_for', return_value=str(root)), patch.object(remote, 'check', return_value=preflight), patch.object(remote, 'processes', return_value=[]), patch.object(remote, 'run_bounded', side_effect=preprocess), patch.object(remote, 'validate_preprocessed', return_value={}):
                # statvfs does not exist on Windows; exercise portable lifecycle using a fake FS capacity.
                with patch.object(remote.os, 'statvfs', create=True) as fs:
                    fs.return_value.f_bavail = 10**9; fs.return_value.f_frsize = 4096
                    result = remote.prepare(cfg, 'test', 'worker source')
            self.assertEqual(result['manifest']['project'], str(project))
            self.assertEqual(result['manifest']['mode'], 'original')
            self.assertFalse((root / 'project').exists())
            self.assertFalse((root / 'snapshot').exists())
            self.assertFalse((root / 'inputs/PPL.tdr').exists())
            self.assertEqual((root / 'before_run/dark_IdVd_n15_circuit.plt').read_text(), 'previous result')
            self.assertFalse((root / 'before_run/unrelated_n99_des.tdr').exists())
            self.assertEqual(remote.hashes(str(project), cfg['input_files']), hashes)

    def test_project_lock_blocks_different_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            remote.lock_project({'parent': tmp}, '/project', 'first')
            with self.assertRaises(FileExistsError):
                remote.lock_project({'parent': tmp}, '/project', 'second')

    def test_no_acceptance_of_tentative_solver_target(self):
        p = monitor.parse_lines(['Computing BE-step from 100 s to 110 s (Stepsize: 10 s)'], {})
        self.assertNotIn('accepted_time_s', p)
        p = monitor.parse_lines(['Error smaller than 1 (0.01).'], p)
        self.assertEqual(p['accepted_time_s'], 110)
        self.assertEqual(p['accepted_vds_V'], 1)

    def test_rejected_step_never_counts_as_progress(self):
        p = monitor.parse_lines(['Computing BE-step from 100 s to 110 s (Stepsize: 10 s)', 'step rejected', 'Error smaller than 1'], {})
        self.assertNotIn('accepted_time_s', p)

    def test_partial_log_line_waits_and_truncation_resets(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp, 'solver.log')
            p.write_text('Computing BE-step from 100 s to 110 s (Stepsize: 10 s)\nError smaller')
            cursor, progress = monitor.read_increment(str(p), {}, {})
            self.assertNotIn('accepted_time_s', progress)
            with p.open('a') as stream:stream.write(' than 1\n')
            cursor, progress = monitor.read_increment(str(p), cursor, progress)
            self.assertEqual(progress['accepted_time_s'], 110)
            p.write_text('new log\n')
            cursor, progress = monitor.read_increment(str(p), cursor, progress)
            self.assertNotIn('accepted_time_s', progress)

    def test_truncated_and_multiblock_plt_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, out = Path(tmp, 'a.plt'), Path(tmp, 'a.csv')
            for text in ['DF-ISE text\nInfo {type=xyplot datasets=["time"]}\nData { 1',
                         'DF-ISE text\nInfo {type=xyplot datasets=["time"]}\nData {1}\nData {2}']:
                p.write_text(text)
                with self.assertRaises(ValueError):client.plt_to_csv(p, out)
                self.assertFalse(out.exists())

    def test_no_endpoint_extrapolation_or_duplicate_area_scaling(self):
        row = {k: 0.0 for k in reporting.FIELDS}
        row.update({'time': 150, 'v(1)': 15, 'v(2)': 5, 'i(vd,2)': 3, 'i(ppl1,2)': -1, 'i(noppl1,2)': -2})
        result = reporting.metrics([row])
        self.assertIsNone(result['Id_10V_A'])
        self.assertEqual(result['max_abs_KCL_residual_A'], 0)
        row['v(2)'] = 10
        self.assertEqual(reporting.metrics([row])['Id_10V_A'], 3)
        self.assertAlmostEqual(reporting.metrics([row])['PPL_fraction_10V'], 1/3)

    def test_interpolation_is_limited_to_shared_voltage_range(self):
        a = [{'v(2)': 0, 'i(vd,2)': 0}, {'v(2)': 10, 'i(vd,2)': 10}]
        b = [{'v(2)': 2, 'i(vd,2)': 3}, {'v(2)': 5, 'i(vd,2)': 6}]
        result = reporting.curve_difference(a, b)
        self.assertEqual((result['voltage_min_V'], result['voltage_max_V'], result['rms_A']), (2, 5, 1))
        with self.assertRaises(ValueError):reporting.interpolate(b, 10)

    def test_connection_failure_never_submits(self):
        config = {'ssh_exe': 'ssh', 'ssh_config': 'config', 'host': 'target', 'remote_python': '/usr/bin/python'}
        with patch.object(client.subprocess, 'run', side_effect=OSError('network unavailable')):
            result = client.remote_call(config, 'status', 'test')
        self.assertFalse(result['connection_ok'])
        self.assertIn('Do not resubmit', result['recovery'])

    def test_cancel_with_live_processes_does_not_confirm_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(remote, 'root_for', return_value=tmp), patch.object(remote, 'status', return_value={'state': {'status': 'running'}, 'worker_alive': True, 'active_processes': [{'pid': 1}]}):
                result = remote.cancel({}, 'test')
            self.assertFalse(result['stopped_confirmed'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
