import copy
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from quarterly.policy import PolicyError, generate_tailoring, load_policy, validate_policy


BENCHMARK = '''<Benchmark xmlns="http://checklists.nist.gov/xccdf/1.2" id="xccdf_mil.disa.stig_benchmark_RHEL">
<title>RHEL STIG</title><version>3</version>
<Profile id="xccdf_mil.disa.stig_profile_MAC-2"><title>MAC II</title></Profile>
<Value id="xccdf_mil.disa.stig_value_timeout"><value>15</value></Value>
<Group id="xccdf_mil.disa.stig_group_V-1"><Rule id="xccdf_mil.disa.stig_rule_SV-1" severity="high"><title>One</title></Rule></Group>
<Group id="xccdf_mil.disa.stig_group_V-2"><Rule id="xccdf_mil.disa.stig_rule_SV-2" severity="medium"><title>Two</title></Rule></Group>
</Benchmark>'''


def policy():
    return {
        'policy_version': 1,
        'benchmark_id': 'xccdf_mil.disa.stig_benchmark_RHEL',
        'benchmark_version': '3',
        'profile_id': 'xccdf_mil.disa.stig_profile_MAC-2',
        'selections': {'xccdf_mil.disa.stig_rule_SV-2': False},
        'values': {'xccdf_mil.disa.stig_value_timeout': '10'},
        'exceptions': [{
            'rule_id': 'xccdf_mil.disa.stig_rule_SV-1',
            'disposition': 'documented_only', 'rationale': 'Legacy dependency',
            'comments': 'Repair scheduled', 'approver': 'ISSO Person',
            'approval_id': 'EX-123', 'expires': '2030-01-01',
            'external_verification': 'Verify approval in external authorization system',
        }],
    }


class PolicyTests(unittest.TestCase):
    def test_tailoring_is_xccdf12_and_contains_policy_selections_values(self):
        xml = generate_tailoring(policy(), BENCHMARK, 'file:///bench.xml', now=datetime(2029, 1, 1, tzinfo=timezone.utc))
        root = ET.fromstring(xml)
        ns = {'x': 'http://checklists.nist.gov/xccdf/1.2'}
        self.assertEqual(root.tag, '{http://checklists.nist.gov/xccdf/1.2}Tailoring')
        self.assertEqual(root.find('x:benchmark', ns).get('href'), 'file:///bench.xml')
        profile = root.find('x:Profile', ns)
        self.assertEqual(profile.get('extends'), policy()['profile_id'])
        self.assertEqual(profile.find('x:select', ns).attrib, {'idref': 'xccdf_mil.disa.stig_rule_SV-2', 'selected': 'false'})
        self.assertEqual(profile.find('x:set-value', ns).text, '10')

    def test_expired_approval_fails_closed(self):
        with self.assertRaisesRegex(PolicyError, 'expired'):
            validate_policy(policy(), BENCHMARK, now=datetime(2030, 1, 1, tzinfo=timezone.utc))

    def test_mismatch_and_unknown_tailoring_ids_fail(self):
        for field, value in [('benchmark_id', 'other'), ('benchmark_version', '4'), ('profile_id', 'other')]:
            p = policy(); p[field] = value
            with self.subTest(field=field), self.assertRaises(PolicyError):
                validate_policy(p, BENCHMARK)
        for field in ('selections', 'values'):
            p = policy(); p[field] = {'unknown': True if field == 'selections' else '42'}
            with self.subTest(field=field), self.assertRaises(PolicyError):
                validate_policy(p, BENCHMARK)

    def test_approval_is_metadata_not_a_signature(self):
        for key in ('rationale', 'comments', 'approver', 'approval_id', 'expires', 'external_verification'):
            p = policy(); del p['exceptions'][0][key]
            with self.subTest(key=key), self.assertRaises(PolicyError):
                validate_policy(p, BENCHMARK)
        p = policy(); p['exceptions'][0]['disposition'] = 'not_applicable'
        with self.assertRaisesRegex(PolicyError, 'applicability'):
            validate_policy(p, BENCHMARK)

    def test_load_policy_rejects_duplicates_and_versions(self):
        p = policy(); p['policy_version'] = 2
        with self.assertRaises(PolicyError):
            validate_policy(p, BENCHMARK)
        p = policy(); p['exceptions'].append(copy.deepcopy(p['exceptions'][0]))
        with self.assertRaises(PolicyError):
            validate_policy(p, BENCHMARK)
        self.assertEqual(load_policy(Path(__file__).parent.parent / 'quarterly/policy.example.json')['policy_version'], 1)

    def test_example_references_must_be_replaced_before_use(self):
        p = load_policy(Path(__file__).parent.parent / 'quarterly/policy.example.json')
        with self.assertRaisesRegex(PolicyError, 'placeholder'):
            validate_policy(p, BENCHMARK)

    def test_reject_dtd_and_internal_entity_in_benchmark(self):
        malicious = BENCHMARK.replace('<title>RHEL STIG</title>', '<title>&expanded;</title>').replace(
            '<Benchmark ', '<!DOCTYPE Benchmark [<!ENTITY expanded "RHEL STIG">]><Benchmark ', 1)
        with self.assertRaisesRegex(PolicyError, 'DTD|entity'):
            validate_policy(policy(), malicious)

    def test_reject_benchmark_duplicate_rule_and_missing_profile_ids(self):
        with self.assertRaises(PolicyError):
            validate_policy(policy(), BENCHMARK.replace('stig_rule_SV-2', 'stig_rule_SV-1'))
        with self.assertRaises(PolicyError):
            validate_policy(policy(), BENCHMARK.replace('id="xccdf_mil.disa.stig_profile_MAC-2"', ''))


if __name__ == '__main__':
    unittest.main()
