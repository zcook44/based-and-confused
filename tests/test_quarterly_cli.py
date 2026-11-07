"""Operator entry point, signed approval and protected evidence tests."""
import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from quarterly import cli
from quarterly.vm import load_manifest


class QuarterlyCLITests(unittest.TestCase):
    def test_inventory_and_read_only_network_output(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(['inventory']), 0)
        nodes = json.loads(output.getvalue())
        self.assertEqual(len(nodes), 8)
        self.assertEqual(nodes[0]['name'], 'qa-rhel8-node1')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(['network-xml']), 0)
        self.assertIn('<name>stigqa-lab</name>', output.getvalue())

    def test_preflight_reports_existing_lab_collisions_without_mutation(self):
        with patch.object(cli.vm, 'preflight', return_value=['domain name collision: qa-rhel9-node1']):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(['preflight']), 2)

    def test_signed_approval_binds_exact_policy_and_benchmark(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            policy = root / 'policy.json'; benchmark = root / 'bench.xml'
            policy.write_text('{"policy_version":1}')
            benchmark.write_text('<Benchmark/>')
            ledger = root / 'ledger.json'; sig = root / 'ledger.sig'; key = root / 'private.pem'; pub = root / 'trusted.pub'
            subprocess.run(['openssl', 'genpkey', '-algorithm', 'EC', '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-out', str(key)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['openssl', 'pkey', '-in', str(key), '-pubout', '-out', str(pub)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            doc = {'schema': 1, 'policy_sha256': cli.digest_file(policy), 'benchmark_sha256': cli.digest_file(benchmark), 'approvals': [{'approval_id': 'A-1', 'rule_id': 'rule-1', 'approver': 'ISSO', 'disposition': 'documented_only', 'expires': '2030-01-01'}]}
            ledger.write_text(json.dumps(doc))
            subprocess.run(['openssl', 'dgst', '-sha256', '-sign', str(key), '-out', str(sig), str(ledger)], check=True)
            exceptions = [{'approval_id': 'A-1', 'rule_id': 'rule-1', 'approver': 'ISSO', 'disposition': 'documented_only', 'expires': '2030-01-01'}]
            self.assertEqual(cli.verify_approvals(exceptions, policy, benchmark, ledger, sig, pub), cli.digest_file(ledger))
            policy.write_text('{"policy_version":2}')
            with self.assertRaises(cli.ReleaseError):
                cli.verify_approvals(exceptions, policy, benchmark, ledger, sig, pub)
            policy.write_text('{"policy_version":1}')
            ledger.write_text(ledger.read_text() + ' ')
            with self.assertRaises(cli.ReleaseError):
                cli.verify_approvals(exceptions, policy, benchmark, ledger, sig, pub)

    def test_ledger_swap_after_openssl_does_not_authorize_unsigned_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            policy = root / 'policy.json'; benchmark = root / 'bench.xml'
            policy.write_text('{}'); benchmark.write_text('<Benchmark/>')
            ledger = root / 'ledger.json'; sig = root / 'ledger.sig'
            private = root / 'private.pem'; public = root / 'trusted.pub'
            subprocess.run(['openssl', 'genpkey', '-algorithm', 'EC', '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-out', str(private)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['openssl', 'pkey', '-in', str(private), '-pubout', '-out', str(public)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            bound = {'schema': 1, 'policy_sha256': cli.digest_file(policy), 'benchmark_sha256': cli.digest_file(benchmark), 'approvals': []}
            ledger.write_text(json.dumps(bound))
            subprocess.run(['openssl', 'dgst', '-sha256', '-sign', str(private), '-out', str(sig), str(ledger)], check=True)
            real_run = subprocess.run

            def swap_after_verify(*args, **kwargs):
                result = real_run(*args, **kwargs)
                ledger.write_text(json.dumps(bound, indent=2))  # valid JSON, not signed bytes
                return result

            with patch.object(cli.subprocess, 'run', side_effect=swap_after_verify):
                verified = cli.verify_approvals([], policy, benchmark, ledger, sig, public)
            self.assertEqual(verified, hashlib.sha256(json.dumps(bound).encode()).hexdigest())
            self.assertNotEqual(verified, cli.digest_file(ledger))

    def test_private_env_is_literal_strict_and_not_shell_evaluated(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'quarterly.env'
            path.write_text('QUARTERLY_USERNAME=operator\nQUARTERLY_PASSWORD=$(id)\nQUARTERLY_ROOT_PASSWORD=root secret\nQUARTERLY_LABADMIN_PUBLIC_KEY=ecdsa-sha2-nistp256 AAAA\n')
            path.chmod(0o600)
            self.assertEqual(cli.load_credentials(path)['QUARTERLY_PASSWORD'], '$(id)')
            path.chmod(0o644)
            with self.assertRaises(cli.ReleaseError):
                cli.load_credentials(path)
            path.chmod(0o600)
            link = Path(td) / 'link.env'; link.symlink_to(path)
            with self.assertRaises(cli.ReleaseError):
                cli.load_credentials(link)
            path.write_text(path.read_text() + 'QUARTERLY_USERNAME=other\n')
            with self.assertRaises(cli.ReleaseError):
                cli.load_credentials(path)

    def test_password_hash_uses_stdin_not_arguments(self):
        self.assertTrue(cli.hash_password('not logged').startswith('$6$'))

    def test_git_ignores_quarterly_secrets_and_generated_evidence(self):
        repo = Path(__file__).resolve().parents[1]
        for path in ('quarterly.env', 'quarterly/private/node.ks', 'quarterly/evidence/host.cklb'):
            with self.subTest(path=path):
                result = subprocess.run(['git', 'check-ignore', '-q', path], cwd=repo)
                self.assertEqual(result.returncode, 0)

    def test_network_xml_validates_against_local_libvirt_schema(self):
        import shutil
        if shutil.which('virt-xml-validate') is None:
            self.skipTest('libvirt schema validator not installed')
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'network.xml'
            path.write_text(cli.vm.render_network_xml(cli.vm.load_manifest()))
            result = subprocess.run(['virt-xml-validate', str(path), 'network'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_kickstart_command_uses_private_env_and_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env = root / 'quarterly.env'
            env.write_text('QUARTERLY_USERNAME=operator\nQUARTERLY_PASSWORD=local-user-secret\nQUARTERLY_ROOT_PASSWORD=local-root-secret\nQUARTERLY_LABADMIN_PUBLIC_KEY=ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTY= lab\n')
            env.chmod(0o600)
            target = root / 'node.ks'
            args = ['kickstart', '--node', 'qa-rhel9-node2', '--env', str(env), '--out', str(target)]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(args), 0)
            text = target.read_text()
            self.assertIn('@^graphical-server-environment', text)
            self.assertIn('labadmin ALL=(ALL) NOPASSWD: ALL', text)
            self.assertIn('rootpw --iscrypted $6$', text)
            self.assertNotIn('local-user-secret', text)
            self.assertNotIn('local-root-secret', text)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(args), 2)

    def test_inert_install_and_clone_plans_require_exact_names(self):
        output = io.StringIO()
        with patch.object(cli.vm, 'plan_install', return_value=['/usr/bin/python3', '/usr/sbin/virt-install', '--name', 'build-qa-rhel9-node1']) as mocked:
            with contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(['install-plan', '--node', 'qa-rhel9-node1', '--kickstart', '/safe/node.ks', '--confirm', 'build-qa-rhel9-node1']), 0)
            self.assertEqual(mocked.call_args.kwargs['confirm'], 'build-qa-rhel9-node1')
            self.assertIn('virt-install', output.getvalue())
        with patch.object(cli.vm, 'plan_clone', return_value={'node_name': 'qa-rhel9-node1', 'base_disk': '/sealed/base.qcow2', 'disk': '/new/clone.qcow2', 'base_sha256': 'a' * 64}) as mocked:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(['clone-plan', '--node', 'qa-rhel9-node1', '--base-sha256', 'a' * 64, '--confirm', 'qa-rhel9-node1']), 0)
            self.assertEqual(mocked.call_args.kwargs['confirm'], 'qa-rhel9-node1')
            self.assertEqual(json.loads(output.getvalue())['disk'], '/new/clone.qcow2')
        with patch.object(cli.vm, 'execute_clone', return_value='/new/clone.qcow2') as mocked:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['clone', '--node', 'qa-rhel9-node1', '--base-sha256', 'a' * 64, '--confirm', 'qa-rhel9-node1']), 0)
            self.assertEqual(mocked.call_args.kwargs['confirm'], 'qa-rhel9-node1')

        with patch.object(cli.vm, 'execute_clone', side_effect=subprocess.CalledProcessError(1, ['qemu-img', 'convert'])):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(['clone', '--node', 'qa-rhel9-node1', '--base-sha256', 'a' * 64, '--confirm', 'qa-rhel9-node1']), 2)

    def test_signed_full_coverage_release_and_missing_rule_rejected(self):
        from test_quarterly_cklb import BENCHMARK, SCAN, policy
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            host = 'qa-rhel9-node1.qa.stig.test'
            now = datetime.now(timezone.utc)
            started = (now - timedelta(minutes=5)).isoformat().replace('+00:00', 'Z')
            ended = (now - timedelta(minutes=4)).isoformat().replace('+00:00', 'Z')
            source = (SCAN.replace('host1', host)
                      .replace('2026-11-06T15:00:00Z', started)
                      .replace('2026-11-06T15:03:00Z', ended)
                      .replace('</TestResult>', '<rule-result idref="xccdf_mil.disa.stig_rule_SV-4"><result>notchecked</result></rule-result></TestResult>'))
            full = source.replace('idref="quarterly-profile"', 'idref="xccdf_mil.disa.stig_profile_MAC-2"')
            paths = {name: root / (name + '.xml') for name in ('benchmark', 'pre', 'post', 'coverage', 'tailoring')}
            paths['benchmark'].write_text(BENCHMARK)
            for name in ('post',):
                paths[name].write_text(source)
            from quarterly.policy import generate_tailoring
            paths['tailoring'].write_text(generate_tailoring(policy(), BENCHMARK, 'benchmark.xml'))
            paths['coverage'].write_text(full)
            pre = full.replace(started, (now - timedelta(minutes=10)).isoformat().replace('+00:00', 'Z')).replace(ended, (now - timedelta(minutes=9)).isoformat().replace('+00:00', 'Z'))
            paths['pre'].write_text(pre)
            p = root / 'policy.json'; p.write_text(json.dumps(policy()))
            schema = Path('/scratch/iso/quarterly/SV3_CKLB_1_0_JSON_SCHEMA.json')
            if not schema.is_file():
                self.skipTest('official DISA CKLB schema must be supplied outside the repository')
            from quarterly.cklb import OFFICIAL_CKLB_SCHEMA_SHA256
            self.assertEqual(cli.digest_file(schema), OFFICIAL_CKLB_SCHEMA_SHA256)
            key = root / 'private.pem'; pub = root / 'trusted.pub'; sig = root / 'ledger.sig'; ledger = root / 'ledger.json'
            subprocess.run(['openssl', 'genpkey', '-algorithm', 'EC', '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-out', str(key)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['openssl', 'pkey', '-in', str(key), '-pubout', '-out', str(pub)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            ledger.write_text(json.dumps({'schema':1, 'policy_sha256':cli.digest_file(p), 'benchmark_sha256':cli.digest_file(paths['benchmark']),
                                     'approvals':[{k:item[k] for k in ('approval_id','rule_id','approver','disposition','expires')} for item in policy()['exceptions']]}))
            subprocess.run(['openssl', 'dgst', '-sha256', '-sign', str(key), '-out', str(sig), str(ledger)], check=True)
            out = root / 'run-01'
            args = ['cklb', '--policy', str(p), '--benchmark', str(paths['benchmark']), '--pre', str(paths['pre']),
                    '--post', str(paths['post']), '--coverage', str(paths['coverage']), '--tailoring', str(paths['tailoring']),
                    '--schema', str(schema), '--approval-ledger', str(ledger), '--approval-signature', str(sig),
                    '--trusted-key', str(pub), '--evidence-dir', str(out), '--host', host]
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(args), 0)
            self.assertEqual(json.loads((out / (host + '.cklb')).read_text())['stigs'][0]['size'], 4)
            manifest = json.loads((out / 'manifest.json').read_text())
            retained = {'policy': p, **paths, 'schema': schema, 'approval_ledger': ledger,
                        'approval_signature': sig, 'trusted_key': pub}
            self.assertEqual(set(manifest['inputs']), set(retained))
            for name, original in retained.items():
                with self.subTest(retained=name):
                    copy = out / manifest['inputs'][name]['file']
                    self.assertEqual(copy.read_bytes(), original.read_bytes())
                    self.assertEqual(manifest['inputs'][name]['sha256'], cli.digest_file(copy))
                    self.assertEqual(copy.stat().st_mode & 0o777, 0o600)
            self.assertFalse(any('PRIVATE KEY' in f.read_text(errors='ignore') for f in out.iterdir()))
            original_coverage = paths['coverage'].read_bytes()
            original_ledger = ledger.read_bytes()
            real_verify = cli.verify_approvals

            def replace_sources_after_snapshot(*verify_args):
                result = real_verify(*verify_args)
                paths['coverage'].write_text('replaced after snapshot')
                ledger.write_text('replaced after snapshot')
                return result

            snapshot_out = root / 'run-snapshot'
            with patch.object(cli, 'verify_approvals', side_effect=replace_sources_after_snapshot), \
                    contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(args[:args.index('--evidence-dir')] + ['--evidence-dir', str(snapshot_out), '--host', host]), 0)
            snapshot_manifest = json.loads((snapshot_out / 'manifest.json').read_text())
            self.assertEqual((snapshot_out / snapshot_manifest['inputs']['coverage']['file']).read_bytes(), original_coverage)
            self.assertEqual((snapshot_out / snapshot_manifest['inputs']['approval_ledger']['file']).read_bytes(), original_ledger)
            paths['coverage'].write_bytes(original_coverage)
            ledger.write_bytes(original_ledger)
            ledger.write_bytes(original_ledger + b' ')
            real_snapshot = cli._snapshot_inputs
            captured_snapshots = []

            def capture_snapshot(snapshot_args, directory):
                captured_snapshots.append(Path(directory))
                self.assertEqual(Path(directory).stat().st_mode & 0o777, 0o700)
                return real_snapshot(snapshot_args, directory)

            with patch.object(cli, '_snapshot_inputs', side_effect=capture_snapshot), \
                    contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(args[:args.index('--evidence-dir')] + ['--evidence-dir', str(root / 'run-bad-signature'), '--host', host]), 2)
            self.assertFalse((root / 'run-bad-signature').exists())
            self.assertEqual(len(captured_snapshots), 1)
            self.assertFalse(captured_snapshots[0].exists())
            ledger.write_bytes(original_ledger)
            paths['coverage'].write_text(full.replace('<rule-result idref="xccdf_mil.disa.stig_rule_SV-4"><result>notchecked</result></rule-result>', ''))
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(args[:args.index('--evidence-dir')] + ['--evidence-dir', str(root / 'run-02'), '--host', host]), 2)
            self.assertFalse((root / 'run-02').exists())
            paths['coverage'].write_text(full)
            paths['pre'].write_text(pre.replace(host, 'wrong-target'))
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(args[:args.index('--evidence-dir')] + ['--evidence-dir', str(root / 'run-03'), '--host', host]), 2)
            self.assertFalse((root / 'run-03').exists())
            paths['pre'].write_text(pre)
            paths['tailoring'].write_text(paths['tailoring'].read_text().replace('quarterly-profile', 'tampered-profile'))
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(args[:args.index('--evidence-dir')] + ['--evidence-dir', str(root / 'run-04'), '--host', host]), 2)
            self.assertFalse((root / 'run-04').exists())
            from quarterly.policy import generate_tailoring
            paths['tailoring'].write_text(generate_tailoring(policy(), BENCHMARK, 'benchmark.xml'))
            paths['post'].write_text(source.replace('<result>pass</result>', '<result>fail</result>'))
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(args[:args.index('--evidence-dir')] + ['--evidence-dir', str(root / 'run-05'), '--host', host]), 2)
            self.assertFalse((root / 'run-05').exists())
            paths['post'].write_text(source.replace('<rule-result idref="xccdf_mil.disa.stig_rule_SV-2"><result>pass</result></rule-result>', ''))
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(args[:args.index('--evidence-dir')] + ['--evidence-dir', str(root / 'run-06'), '--host', host]), 2)
            self.assertFalse((root / 'run-06').exists())
            paths['post'].write_text(source.replace('<result>pass</result>', '<result>unknown</result>'))
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(args[:args.index('--evidence-dir')] + ['--evidence-dir', str(root / 'run-07'), '--host', host]), 2)
            self.assertFalse((root / 'run-07').exists())

    def test_bad_evidence_source_never_creates_final_run_directory(self):
        with tempfile.TemporaryDirectory() as td:
            parent = Path(td)
            valid = parent / 'valid.xml'; valid.write_text('safe')
            target = parent / 'run'
            with self.assertRaises(cli.ReleaseError):
                cli.write_evidence(target, 'host1', {'cklb_version': '1.0'},
                                   {'valid': valid, 'missing': parent / 'absent.xml'}, {})
            self.assertFalse(target.exists())

    def test_evidence_bundle_is_private_immutable_and_hashes_copied_inputs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inputs = {}
            for name in ('policy', 'benchmark', 'pre', 'post', 'coverage', 'tailoring'):
                p = root / (name + '.xml'); p.write_text(name)
                inputs[name] = p
            evidence = root / 'evidence'
            cklb = {'cklb_version':'1.0', 'target_data': {'host_name':'host1'}}
            result = cli.write_evidence(evidence, 'host1', cklb, inputs, {'approval_ledger_sha256': 'a' * 64})
            self.assertEqual(result, evidence / 'host1.cklb')
            self.assertEqual(json.loads(result.read_text()), cklb)
            self.assertEqual((evidence.stat().st_mode & 0o777), 0o700)
            self.assertEqual((result.stat().st_mode & 0o777), 0o600)
            manifest = json.loads((evidence / 'manifest.json').read_text())
            self.assertEqual(manifest['host'], 'host1')
            self.assertEqual(manifest['inputs']['pre']['sha256'], cli.digest_file(inputs['pre']))
            with self.assertRaises(cli.ReleaseError):
                cli.write_evidence(evidence, 'host1', cklb, inputs, {})
            self.assertEqual(json.loads(result.read_text()), cklb)


if __name__ == '__main__':
    unittest.main()
