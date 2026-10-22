"""Offline safety/behavior tests; never connect to SSH or invoke Sentaurus."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import tcad_batch_remote as remote
import tcad_workflow as client


class BatchTests(unittest.TestCase):
    def state(self):
        return {'status': 'prepared', 'nodes': {str(n): {'status': 'not_started'} for n in remote.NODES}}

    def test_serial_order_and_success(self):
        calls, history = [], []
        def execute(n, state, publish):
            self.assertEqual(state['current_node'], n)
            self.assertTrue(all(state['nodes'][str(p)]['status'] == 'succeeded' for p in calls))
            calls.append(n)
            return {'status': 'succeeded'}
        result = remote.run_queue(remote.NODES, self.state(), execute, lambda s: history.append(copy.deepcopy(s)))
        self.assertEqual(calls, remote.NODES)
        self.assertEqual(result['status'], 'succeeded')

    def test_failure_blocks_following_nodes(self):
        calls = []
        def execute(n, *unused):
            calls.append(n)
            return {'status': 'failed' if n == 21 else 'succeeded'}
        state = remote.run_queue(remote.NODES, self.state(), execute, lambda s: None)
        self.assertEqual(calls, [15, 21])
        self.assertEqual(state['nodes']['27']['status'], 'blocked')
        self.assertEqual(state['status'], 'failed')

    def test_cancel_before_start_launches_nothing(self):
        state = remote.run_queue(remote.NODES, self.state(), lambda *a: self.fail('unexpected launch'),
                                 lambda s: None, lambda: True)
        self.assertEqual(state['status'], 'cancelled')
        self.assertTrue(all(n['status'] == 'blocked' for n in state['nodes'].values()))

    def test_exclusive_submission_survives_lost_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(remote.claim(tmp))
            self.assertFalse(remote.claim(tmp))
            self.assertFalse(remote.claim(tmp))

    def test_duplicate_submit_never_launches_even_after_response_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {'nodes': remote.NODES}
            Path(tmp, 'manifest.json').write_text(json.dumps({'run_id': 'test'}))
            Path(tmp, 'config.json').write_text(json.dumps(cfg))
            Path(tmp, 'state.json').write_text(json.dumps({'status': 'launching'}))
            remote.claim(tmp)
            with patch.object(remote, 'root_for', return_value=tmp), patch.object(remote.subprocess, 'Popen') as launch:
                result = remote.submit(cfg, 'test')
                self.assertTrue(result['duplicate_submission_prevented'])
                launch.assert_not_called()

    def test_unknown_worker_does_not_look_successful(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'state.json').write_text(json.dumps({'status': 'running', 'worker': {'pid': 123}}))
            with patch.object(remote, 'root_for', return_value=tmp), patch.object(remote, 'alive', return_value=False), patch.object(remote, 'processes', return_value=[]):
                state = remote.status({}, 'test')
            self.assertEqual(state['observed_status'], 'attention_required_worker_not_alive')
            self.assertFalse(state['simulation_success'])

    def test_old_goodbye_alone_is_not_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'n15_des.log').write_text('Good Bye !\nSentaurus Device simulation finished')
            Path(tmp, 'gsub.log').write_text("job '15' status changed")
            evidence = remote.completion(tmp, 15, 0, 9999999999, tmp + '/gsub.log')
            self.assertFalse(evidence['passed'])

    def test_complete_real_log_evidence_is_accepted(self):
        fixture = Path(__file__).resolve().parents[2] / 'results/4E17SPICE_dark_flowcheck_20261019/node5_run_20261019T133220Z'
        if not fixture.exists():
            self.skipTest('Private local solver fixtures are not distributed')
        with tempfile.TemporaryDirectory() as tmp:
            for name in ['n5_des.log', 'n5_des.job', 'n5_des.sta', 'dark_IdVd_n5_circuit.plt',
                         'dark_IdVd_ppl1_n5_des.plt', 'dark_IdVd_noppl1_n5_des.plt']:
                text = (fixture / 'raw' / name).read_text(encoding='utf-8')
                text = text.replace('job 5 <sdevice>', 'job 15 <sdevice>')
                Path(tmp, name.replace('n5_', 'n15_')).write_text(text, encoding='utf-8')
            text = (fixture / 'run/gsub.stdout.log').read_text().replace("job '5'", "job '15'")
            Path(tmp, 'gsub.log').write_text(text)
            evidence = remote.completion(tmp, 15, 0, 0, tmp + '/gsub.log')
            self.assertTrue(evidence['passed'], evidence)
            self.assertFalse(remote.completion(tmp, 15, 1, 0, tmp + '/gsub.log')['passed'])

    def test_hash_change_blocks_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp, 'input.cmd')
            p.write_text('original')
            expected = {'input.cmd': remote.sha(str(p))}
            p.write_text('changed')
            with self.assertRaisesRegex(ValueError, 'Frozen input changed'):
                remote.verify_hashes(tmp, expected)

    def test_tree_provenance_comment_only(self):
        a = '# swbtree vcurrent, saved by swb\n6 15 9 {1e-4}\n'
        b = a.replace('saved by swb', 'saved by spp')
        self.assertEqual(remote.tree_content(a), remote.tree_content(b))
        self.assertNotEqual(remote.tree_content(a), remote.tree_content(b.replace('1e-4', '1e-3')))

    def test_real_saved_tree_maps_to_eight_nodes(self):
        source = Path(__file__).with_name('preflight_sdevice1_118u_20261022T034415Z.json')
        if not source.exists():
            self.skipTest('Private local preflight evidence is not distributed')
        tree = json.loads(source.read_text())['remote']['text']['gtree.dat']
        self.assertEqual(remote.tree_parameters(tree), remote.PARAMETERS)
        with self.assertRaises(ValueError):
            remote.tree_parameters(tree.replace('6 15 9 {1e-4}', '6 15 9 {1e-3}'))

    def test_actual_plt_csv_preserves_rows(self):
        p = Path(__file__).resolve().parents[2] / 'results/4E17SPICE_dark_flowcheck_20261019/node5_run_20261019T133220Z/raw/dark_IdVd_n5_circuit.plt'
        if not p.exists():
            self.skipTest('Private local solver fixtures are not distributed')
        with tempfile.TemporaryDirectory() as tmp:
            result = client.plt_to_csv(p, Path(tmp) / 'curve.csv')
            self.assertEqual(result['rows'], 41)
            self.assertEqual(result['columns'][:3], ['time', 'v(1)', 'v(2)'])

    def test_shell_meta_run_id_rejected(self):
        with self.assertRaises(ValueError):
            remote.root_for({'parent': '/tmp'}, '../other;whoami')


if __name__ == '__main__':
    unittest.main(verbosity=2)
