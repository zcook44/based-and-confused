import unittest
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from quarterly.cklb import EvidenceError, generate_cklb, validate_cklb_structure, validate_cklb_schema
from quarterly.policy import PolicyError

X = 'http://checklists.nist.gov/xccdf/1.2'
BENCHMARK = f'''<Benchmark xmlns="{X}" id="xccdf_mil.disa.stig_benchmark_RHEL">
<title>RHEL STIG</title><version>3</version><plain-text id="release-info">Release: 2 Benchmark Date: 2026-01-01</plain-text>
<Profile id="xccdf_mil.disa.stig_profile_MAC-2"/>
<Group id="xccdf_mil.disa.stig_group_V-1"><title>Vuln one</title><Rule id="xccdf_mil.disa.stig_rule_SV-1" severity="high" weight="10.0"><version>RHEL-09-000001</version><title>First rule</title><description>&lt;VulnDiscussion&gt;Must secure service&lt;/VulnDiscussion&gt;&lt;FalsePositives&gt;none&lt;/FalsePositives&gt;&lt;CCI_REF&gt;CCI-000001&lt;/CCI_REF&gt;</description><fixtext>fix service</fixtext><check><check-content>inspect service</check-content><check-content-ref href="manual" name="manual"/></check><reference><identifier>RHEL-9</identifier></reference></Rule></Group>
<Group id="xccdf_mil.disa.stig_group_V-2"><Rule id="xccdf_mil.disa.stig_rule_SV-2" severity="medium"><title>Second</title></Rule></Group>
<Group id="xccdf_mil.disa.stig_group_V-3"><Rule id="xccdf_mil.disa.stig_rule_SV-3" severity="low"><title>Third</title></Rule></Group>
<Group id="xccdf_mil.disa.stig_group_V-4"><Rule id="xccdf_mil.disa.stig_rule_SV-4" severity="low"><title>Fourth manual</title></Rule></Group>
</Benchmark>'''
SCAN = f'''<Benchmark xmlns="{X}" id="xccdf_mil.disa.stig_benchmark_RHEL"><version>3</version><plain-text id="release-info">Release: 2 Benchmark Date: 2026-01-01</plain-text>
<TestResult id="scan-1" start-time="2026-11-06T15:00:00Z" end-time="2026-11-06T15:03:00Z"><benchmark id="xccdf_mil.disa.stig_benchmark_RHEL" href="bench.xml"/><profile idref="quarterly-profile"/><target>host1</target>
<rule-result idref="xccdf_mil.disa.stig_rule_SV-1"><result>fail</result><message>service enabled</message><check><check-content-ref href="result.xml"/><check-content>systemctl is-enabled service: enabled</check-content></check></rule-result>
<rule-result idref="xccdf_mil.disa.stig_rule_SV-2"><result>pass</result></rule-result>
<rule-result idref="xccdf_mil.disa.stig_rule_SV-3"><result>notapplicable</result></rule-result>
</TestResult></Benchmark>'''


def policy():
    return {'policy_version': 1, 'benchmark_id': 'xccdf_mil.disa.stig_benchmark_RHEL', 'benchmark_version': '3', 'profile_id': 'xccdf_mil.disa.stig_profile_MAC-2', 'selections': {}, 'values': {}, 'exceptions': [
        {'rule_id': 'xccdf_mil.disa.stig_rule_SV-1', 'disposition': 'documented_only', 'rationale': 'requires maintenance', 'comments': 'tracked', 'approver': 'ISSO', 'approval_id': 'A-1', 'expires': '2027-01-01', 'external_verification': 'check external register'},
        {'rule_id': 'xccdf_mil.disa.stig_rule_SV-3', 'disposition': 'not_applicable', 'rationale': 'device absent', 'applicability_by_host': {'host1': 'host1 has no device of this type'}, 'comments': 'asset inventory confirms', 'approver': 'ISSO', 'approval_id': 'A-2', 'expires': '2027-01-01', 'external_verification': 'check external register'}]}

NOW = datetime(2026, 11, 6, 16, tzinfo=timezone.utc)


class CKLBTests(unittest.TestCase):
    def make(self, scan=SCAN, p=None):
        return generate_cklb(p or policy(), BENCHMARK, scan, 'host1', now=NOW)

    def test_all_official_rules_and_statuses_preserve_evidence(self):
        doc = self.make()
        validate_cklb_structure(doc)
        self.assertEqual(doc['cklb_version'], '1.0')
        stig = doc['stigs'][0]
        self.assertEqual(stig['size'], 4)
        self.assertEqual(stig['stig_id'], policy()['benchmark_id'])
        rules = {r['rule_id_src']: r for r in stig['rules']}
        self.assertEqual({k: r['status'] for k, r in rules.items()}, {
            'xccdf_mil.disa.stig_rule_SV-1': 'open', 'xccdf_mil.disa.stig_rule_SV-2': 'not_a_finding',
            'xccdf_mil.disa.stig_rule_SV-3': 'not_applicable', 'xccdf_mil.disa.stig_rule_SV-4': 'not_reviewed'})
        failed = rules['xccdf_mil.disa.stig_rule_SV-1']
        self.assertEqual(failed['group_id_src'], 'xccdf_mil.disa.stig_group_V-1')
        self.assertEqual(failed['rule_version'], 'RHEL-09-000001')
        self.assertEqual(failed['ccis'], ['CCI-000001'])
        self.assertIn('systemctl is-enabled service: enabled', failed['finding_details'])
        self.assertIn('A-1', failed['comments'])
        self.assertIn('external', failed['comments'])
        self.assertIn('host1 has no device', rules['xccdf_mil.disa.stig_rule_SV-3']['finding_details'])

    def test_strict_coverage_accepts_explicit_untailored_base_profile(self):
        complete = SCAN.replace('</TestResult>', '<rule-result idref="xccdf_mil.disa.stig_rule_SV-4"><result>notchecked</result></rule-result></TestResult>')
        base = complete.replace('idref="quarterly-profile"', 'idref="xccdf_mil.disa.stig_profile_MAC-2"')
        with self.assertRaises(EvidenceError):
            generate_cklb(policy(), BENCHMARK, base, 'host1', now=NOW, require_full_coverage=True)
        doc = generate_cklb(policy(), BENCHMARK, base, 'host1', now=NOW,
                            require_full_coverage=True, scan_profile=policy()['profile_id'])
        self.assertEqual(doc['stigs'][0]['rules'][-1]['status'], 'not_reviewed')

    def test_notapplicable_is_bound_to_exact_asset(self):
        scan = SCAN.replace('<target>host1</target>', '<target>host2</target>')
        doc = generate_cklb(policy(), BENCHMARK, scan, 'host2', now=NOW)
        status = {rule['rule_id_src']: rule['status'] for rule in doc['stigs'][0]['rules']}
        self.assertEqual(status['xccdf_mil.disa.stig_rule_SV-3'], 'not_reviewed')

    def test_notapplicable_requires_scan_and_asset_applicability(self):
        p = policy(); p['exceptions'] = p['exceptions'][:1]
        r = {x['rule_id_src']: x for x in self.make(p=p)['stigs'][0]['rules']}
        self.assertEqual(r['xccdf_mil.disa.stig_rule_SV-3']['status'], 'not_reviewed')
        scan = SCAN.replace('<result>notapplicable</result>', '<result>fail</result>')
        r = {x['rule_id_src']: x for x in self.make(scan=scan)['stigs'][0]['rules']}
        self.assertEqual(r['xccdf_mil.disa.stig_rule_SV-3']['status'], 'open')

    def test_explicit_deselection_cannot_hide_observed_failure(self):
        p = policy(); p['selections'] = {'xccdf_mil.disa.stig_rule_SV-1': False}
        rules = {r['rule_id_src']: r for r in self.make(p=p)['stigs'][0]['rules']}
        self.assertEqual(rules['xccdf_mil.disa.stig_rule_SV-1']['status'], 'open')

    def test_unknown_error_and_missing_results_not_reviewed(self):
        for status in ('unknown', 'error', 'notchecked', 'informational'):
            scan = SCAN.replace('<result>pass</result>', f'<result>{status}</result>')
            r = {x['rule_id_src']: x for x in self.make(scan=scan)['stigs'][0]['rules']}
            self.assertEqual(r['xccdf_mil.disa.stig_rule_SV-2']['status'], 'not_reviewed')

    def test_reject_stale_wrong_target_profile_version_and_unknown_rule(self):
        variants = [SCAN.replace('15:03:00Z', '10:03:00Z'), SCAN.replace('<target>host1</target>', '<target>host2</target>'),
                    SCAN.replace('idref="quarterly-profile"', 'idref="wrong-profile"'),
                    SCAN.replace('<version>3</version>', '<version>4</version>'),
                    SCAN.replace('rule_SV-2', 'rule_SV-X')]
        for scan in variants:
            with self.subTest(scan=scan[-90:]), self.assertRaises((EvidenceError, PolicyError)):
                self.make(scan=scan)

    def test_reject_duplicate_results_and_missing_benchmark_rules(self):
        scan = SCAN.replace('</TestResult>', '<rule-result idref="xccdf_mil.disa.stig_rule_SV-1"><result>pass</result></rule-result></TestResult>')
        with self.assertRaises(EvidenceError):
            self.make(scan=scan)
        with self.assertRaises(PolicyError):
            self.make(scan=SCAN, p={**policy(), 'benchmark_version': '2'})

    def test_reject_xccdf_override_even_when_final_result_is_pass(self):
        scan = SCAN.replace('<result>pass</result>',
            '<result>pass</result><override authority="analyst" time="2026-11-06T15:02:00Z">'
            '<old-result>fail</old-result><new-result>pass</new-result><remark>manual</remark></override>')
        with self.assertRaisesRegex(EvidenceError, 'override'):
            self.make(scan=scan)
        with self.assertRaisesRegex(EvidenceError, 'override'):
            self.make(scan=SCAN.replace('<result>pass</result>', '<result>pass</result><override/>'))

    def test_reject_benchmark_group_without_identifier(self):
        invalid = BENCHMARK.replace('id="xccdf_mil.disa.stig_group_V-1"', '')
        with self.assertRaises(PolicyError):
            generate_cklb(policy(), invalid, SCAN, 'host1', now=NOW)

    def test_strict_release_rejects_missing_result_but_allows_explicit_unknown(self):
        with self.assertRaisesRegex(EvidenceError, 'missing scan rule-result.*SV-4'):
            generate_cklb(policy(), BENCHMARK, SCAN, 'host1', now=NOW, require_full_coverage=True)
        full = SCAN.replace('</TestResult>', '<rule-result idref="xccdf_mil.disa.stig_rule_SV-4"><result>unknown</result></rule-result></TestResult>')
        rules = generate_cklb(policy(), BENCHMARK, full, 'host1', now=NOW, strict_release=True)['stigs'][0]['rules']
        self.assertEqual(rules[-1]['status'], 'not_reviewed')

    def test_reject_xml_dtd_in_scan_and_description(self):
        malicious = SCAN.replace('<Benchmark ', '<!DOCTYPE Benchmark [<!ENTITY x "host1">]><Benchmark ', 1).replace('<target>host1</target>', '<target>&x;</target>')
        with self.assertRaises(PolicyError):
            self.make(scan=malicious)
        bad = BENCHMARK.replace('Must secure service', '&lt;!DOCTYPE root [&lt;!ENTITY x "boom"&gt;]&gt;&lt;VulnDiscussion&gt;&amp;x;')
        with self.assertRaises(PolicyError):
            generate_cklb(policy(), bad, SCAN, 'host1', now=NOW)

    def test_reject_missing_ambiguous_scan_identity_and_revision(self):
        variants = [SCAN.replace('<target>host1</target>', '<target>host1</target><target>host2</target>'),
                    SCAN.replace('idref="quarterly-profile"', 'idref="xccdf_mil.disa.stig_profile_MAC-2"'),
                    SCAN.replace('<benchmark id="xccdf_mil.disa.stig_benchmark_RHEL" href="bench.xml"/>', ''),
                    SCAN.replace('<version>3</version>', '<version>3</version><version>4</version>'),
                    SCAN.replace('<result>pass</result>', '<result>pass</result><result>fail</result>'),
                    SCAN.replace('<result>pass</result>', '<result>bogus</result>')]
        for variant in variants:
            with self.subTest(variant=variant[-130:]), self.assertRaises(EvidenceError):
                self.make(scan=variant)

    def test_official_schema_runtime_validation_and_fail_closed(self):
        doc = self.make()
        with tempfile.TemporaryDirectory() as directory:
            schema = Path(directory) / 'schema.json'
            schema.write_bytes(Path('/scratch/iso/quarterly/SV3_CKLB_1_0_JSON_SCHEMA.json').read_bytes())
            self.assertTrue(validate_cklb_schema(doc, schema))
            with self.assertRaises(EvidenceError):
                validate_cklb_schema({**doc, 'cklb_version': '2.0'}, schema)
            schema.write_text(json.dumps({'type': 'object', 'properties': {'cklb_version': {'const': '1.0'}}}))
            with self.assertRaisesRegex(EvidenceError, 'digest'):
                validate_cklb_schema(doc, schema)
            schema.write_text('{')
            with self.assertRaises(EvidenceError):
                validate_cklb_schema(doc, schema)
            schema.write_text(json.dumps({'type': 'invalid-type'}))
            with self.assertRaises(EvidenceError):
                validate_cklb_schema(doc, schema)
            schema.write_text(json.dumps({'$ref': 'https://example.invalid/remote.json'}))
            with self.assertRaises(EvidenceError):
                validate_cklb_schema(doc, schema)
            with patch.dict('sys.modules', {'jsonschema': None}):
                with self.assertRaisesRegex(EvidenceError, 'jsonschema'):
                    validate_cklb_schema(doc, schema)

    def test_scan_release_info_required_and_must_match(self):
        release = '<plain-text id="release-info">Release: 2 Benchmark Date: 2026-01-01</plain-text>'
        for scan in (SCAN.replace(release, ''), SCAN.replace(release, release.replace('Release: 2', 'Release: 99')),
                     SCAN.replace(release, '<plain-text id="other">Release: 2 Benchmark Date: 2026-01-01</plain-text>'),
                     SCAN.replace(release, '<plain-text id="release-info"/>')):
            with self.subTest(scan=scan[:240]), self.assertRaisesRegex(EvidenceError, 'release'):
                self.make(scan=scan)

        with self.assertRaisesRegex(EvidenceError, 'release'):
            generate_cklb(policy(), BENCHMARK.replace(release, '<plain-text id="release-info"/>'),
                          SCAN.replace(release, '<plain-text id="release-info"/>'), 'host1', now=NOW)

    def test_scan_release_info_in_explicit_disa_xccdf_namespace_matches(self):
        release = '<plain-text id="release-info">Release: 2 Benchmark Date: 2026-01-01</plain-text>'
        scan = SCAN.replace(release, release.replace('<plain-text', f'<plain-text xmlns="{X}"'))
        self.assertEqual(self.make(scan=scan)['stigs'][0]['release_info'], 'Release: 2 Benchmark Date: 2026-01-01')

    def test_release_info_identified_among_disa_plain_text_metadata(self):
        release = '<plain-text id="release-info">Release: 2 Benchmark Date: 2026-01-01</plain-text>'
        extra = '<plain-text id="generator">DISA generator</plain-text>'
        benchmark = BENCHMARK.replace(release, extra + release)
        scan = SCAN.replace(release, extra + release)
        doc = generate_cklb(policy(), benchmark, scan, 'host1', now=NOW)
        self.assertEqual(doc['stigs'][0]['release_info'], 'Release: 2 Benchmark Date: 2026-01-01')
        with self.assertRaisesRegex(EvidenceError, 'release'):
            generate_cklb(policy(), benchmark, scan.replace(release, ''), 'host1', now=NOW)

    def test_generation_schema_option_and_ssg_mismatch_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            schema = Path(directory) / 'schema.json'
            schema.write_text(json.dumps({'type': 'object', 'properties': {'cklb_version': {'const': '2.0'}}}))
            with self.assertRaises(EvidenceError):
                generate_cklb(policy(), BENCHMARK, SCAN, 'host1', now=NOW, schema_path=schema)
        ssg = SCAN.replace('xccdf_mil.disa.stig_benchmark_RHEL', 'xccdf_org.ssgproject.content_benchmark_RHEL')
        with self.assertRaisesRegex(EvidenceError, 'benchmark'):
            self.make(scan=ssg)


if __name__ == '__main__':
    unittest.main()
