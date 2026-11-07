"""Versioned XCCDF policy and tailoring. Approval fields are references, not signatures."""

import json
from datetime import date, datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.parsers import expat

X = 'http://checklists.nist.gov/xccdf/1.2'
ET.register_namespace('', X)


class PolicyError(ValueError):
    """Policy, benchmark or approval is unsafe or inconsistent."""


def _xml(source):
    try:
        data = source if isinstance(source, (bytes, str)) else Path(source).read_bytes()
        # ElementTree expands internal entities; reject declarations before parsing.
        parser = expat.ParserCreate()
        def reject_doctype(*_args):
            raise PolicyError('XML DTD/entity declarations are forbidden')
        parser.StartDoctypeDeclHandler = reject_doctype
        parser.EntityDeclHandler = reject_doctype
        parser.Parse(data, True)
        return ET.fromstring(data)
    except (ET.ParseError, expat.ExpatError, OSError) as exc:
        raise PolicyError(f'invalid XCCDF: {exc}') from exc


def load_policy(path):
    """Read one versioned JSON policy; validation requires the benchmark."""
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise PolicyError(f'invalid policy JSON: {exc}') from exc


def validate_policy(policy, benchmark_xml, *, now=None):
    """Validate policy identifiers, expiry and metadata against official benchmark."""
    if not isinstance(policy, dict) or type(policy.get('policy_version')) is not int or policy['policy_version'] != 1:
        raise PolicyError('unsupported policy_version (expected 1)')
    root = _xml(benchmark_xml)
    if root.tag != f'{{{X}}}Benchmark':
        raise PolicyError('expected XCCDF 1.2 Benchmark')
    if policy.get('benchmark_id') != root.get('id') or not root.get('id'):
        raise PolicyError('benchmark_id mismatch')
    version = root.findtext(f'{{{X}}}version')
    if not version or str(policy.get('benchmark_version')) != version:
        raise PolicyError('benchmark_version mismatch')
    profile_ids = [e.get('id') for e in root.findall(f'{{{X}}}Profile')]
    profiles = set(profile_ids)
    if not profile_ids or None in profiles or len(profiles) != len(profile_ids):
        raise PolicyError('benchmark has missing or duplicate profile identifiers')
    if policy.get('profile_id') not in profiles:
        raise PolicyError('profile_id mismatch')
    rule_ids = [e.get('id') for e in root.iter(f'{{{X}}}Rule')]
    rules = set(rule_ids)
    if not rule_ids or None in rules or len(rules) != len(rule_ids):
        raise PolicyError('benchmark has missing or duplicate rule identifiers')
    values = {e.get('id') for e in root.iter(f'{{{X}}}Value')}
    selections = policy.get('selections', {})
    settings = policy.get('values', {})
    if not isinstance(selections, dict) or any(k not in rules or type(v) is not bool for k, v in selections.items()):
        raise PolicyError('unknown rule or invalid selection')
    if not isinstance(settings, dict) or any(k not in values or not isinstance(v, str) or not v for k, v in settings.items()):
        raise PolicyError('unknown value or invalid value setting')
    exceptions = policy.get('exceptions', [])
    if not isinstance(exceptions, list):
        raise PolicyError('exceptions must be a list')
    today = (now or datetime.now(timezone.utc)).date()
    seen = set()
    for item in exceptions:
        if not isinstance(item, dict) or item.get('rule_id') not in rules or item['rule_id'] in seen:
            raise PolicyError('unknown or duplicate exception rule')
        seen.add(item['rule_id'])
        if item.get('disposition') not in ('documented_only', 'not_applicable'):
            raise PolicyError('invalid exception disposition')
        for field in ('rationale', 'comments', 'approver', 'approval_id', 'expires', 'external_verification'):
            if not isinstance(item.get(field), str) or not item[field].strip():
                raise PolicyError(f'exception missing {field}')
        if any('EXAMPLE' in item[field].upper() or 'REPLACE' in item[field].upper()
               for field in ('approver', 'approval_id', 'external_verification')):
            raise PolicyError('placeholder approval metadata must be replaced')
        if item['disposition'] == 'not_applicable':
            assets = item.get('applicability_by_host')
            if not isinstance(assets, dict) or not assets or any(
                    not isinstance(host, str) or not host.strip()
                    or not isinstance(reason, str) or not reason.strip()
                    for host, reason in assets.items()):
                raise PolicyError('not_applicable requires exact host-to-applicability evidence')
        try:
            expiry = date.fromisoformat(item['expires'])
        except ValueError as exc:
            raise PolicyError('invalid approval expiry') from exc
        if expiry <= today:
            raise PolicyError(f'expired approval {item["approval_id"]}')
    return root


def generate_tailoring(policy, benchmark_xml, benchmark_href, *, now=None):
    """Produce XCCDF 1.2 Tailoring XML; use returned profile id for oscap scan."""
    validate_policy(policy, benchmark_xml, now=now)
    if not isinstance(benchmark_href, str) or not benchmark_href.strip():
        raise PolicyError('benchmark_href required')
    root = ET.Element(f'{{{X}}}Tailoring', {'id': 'quarterly-tailoring'})
    ET.SubElement(root, f'{{{X}}}benchmark', {'href': benchmark_href})
    profile = ET.SubElement(root, f'{{{X}}}Profile', {'id': 'quarterly-profile', 'extends': policy['profile_id']})
    for rule_id, selected in sorted(policy.get('selections', {}).items()):
        ET.SubElement(profile, f'{{{X}}}select', {'idref': rule_id, 'selected': str(selected).lower()})
    for value_id, value in sorted(policy.get('values', {}).items()):
        ET.SubElement(profile, f'{{{X}}}set-value', {'idref': value_id}).text = value
    return ET.tostring(root, encoding='unicode', xml_declaration=True)
