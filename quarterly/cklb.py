"""Conservative STIG Viewer 3 CKLB export from an official XCCDF and fresh scan.

CKLB 1.0 fields/statuses follow DISA's SV3_CKLB_1_0_JSON_SCHEMA.zip.
The stdlib structural guard is a subset, not a complete JSON Schema validator.
"""

from datetime import datetime, timezone, timedelta
from hashlib import sha256
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
from xml.etree import ElementTree as ET

from .policy import PolicyError, X, _xml, validate_policy

# DISA source: https://dl.dod.cyber.mil/wp-content/uploads/stigs/zip/SV3_CKLB_1_0_JSON_SCHEMA.zip
# Raw JSON bytes extracted from ZIP SHA-256:
# 4e017289df9d80f0976e1549ba4e31b7d653eba34ecb8364388bdb8fb3c2a430.
OFFICIAL_CKLB_SCHEMA_SHA256 = '8b88205de07662006e1660b1decad677d344828d6687b1dd26bca15105b7bb37'


class EvidenceError(ValueError):
    """The scan is stale, incomplete in identity, or inconsistent with the benchmark."""


def _text(node, path, default=''):
    found = node.find(path)
    return ''.join(found.itertext()).strip() if found is not None else default


def _date(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('timezone required')
        return parsed.astimezone(timezone.utc)
    except (AttributeError, ValueError) as exc:
        raise EvidenceError('invalid scan timestamp or missing timezone') from exc


def _scan_results(root, policy, host_name, now, max_age_hours, bench_root, scan_profile):
    versions = root.findall(f'{{{X}}}version')
    if (root.tag != f'{{{X}}}Benchmark' or root.get('id') != policy['benchmark_id']
            or len(versions) != 1 or _text(root, f'{{{X}}}version') != str(policy['benchmark_version'])):
        raise EvidenceError('scan benchmark ID/version mismatch')
    official_release = [node for node in bench_root.findall(f'{{{X}}}plain-text') if node.get('id') == 'release-info']
    scan_release = [node for node in root.findall(f'{{{X}}}plain-text') if node.get('id') == 'release-info']
    if (len(official_release) > 1 or len(scan_release) > 1
            or len(scan_release) != len(official_release)
            or (official_release and (not ''.join(official_release[0].itertext()).strip()
                or ''.join(scan_release[0].itertext()).strip()
                != ''.join(official_release[0].itertext()).strip()))):
        raise EvidenceError('scan benchmark release mismatch')
    results = root.findall(f'{{{X}}}TestResult')
    if len(results) != 1:
        raise EvidenceError('exactly one XCCDF TestResult required per host')
    result = results[0]
    if next(result.iter(f'{{{X}}}override'), None) is not None:
        raise EvidenceError('XCCDF result overrides are not independent scan evidence')
    bench = result.find(f'{{{X}}}benchmark')
    if len(result.findall(f'{{{X}}}benchmark')) != 1 or bench.get('id') != policy['benchmark_id']:
        raise EvidenceError('scan result benchmark mismatch')
    profile = result.find(f'{{{X}}}profile')
    if len(result.findall(f'{{{X}}}profile')) != 1 or profile.get('idref') != scan_profile:
        raise EvidenceError('scan profile mismatch')
    if len(result.findall(f'{{{X}}}target')) != 1 or _text(result, f'{{{X}}}target') != host_name:
        raise EvidenceError('scan target mismatch')
    started, ended = _date(result.get('start-time')), _date(result.get('end-time'))
    if started > ended or ended > now or now - ended > timedelta(hours=max_age_hours):
        raise EvidenceError('stale or future scan result')
    mapped = {}
    for item in result.findall(f'{{{X}}}rule-result'):
        key = item.get('idref')
        if not key or key in mapped:
            raise EvidenceError('duplicate or empty scan rule idref')
        observations = item.findall(f'{{{X}}}result')
        if len(observations) != 1 or (observations[0].text or '').strip().lower() not in (
                'pass', 'fail', 'error', 'unknown', 'notapplicable', 'notchecked',
                'notselected', 'informational', 'fixed'):
            raise EvidenceError('invalid or ambiguous scan rule result')
        mapped[key] = item
    return mapped, ended


def _description(rule, name):
    raw = _text(rule, f'{{{X}}}description')
    try:
        content = _xml(f'<root>{raw}</root>')
        node = content.find(name)
        return ''.join(node.itertext()).strip() if node is not None else ''
    except ET.ParseError:
        return ''


def validate_cklb_schema(doc, schema_path):
    """Validate against the byte-pinned official DISA CKLB 1.0 JSON schema."""
    try:
        import jsonschema
        from jsonschema.validators import validator_for
        from jsonschema.exceptions import SchemaError, ValidationError
    except ImportError as exc:
        raise EvidenceError('jsonschema is required for CKLB release validation') from exc
    try:
        raw_schema = Path(schema_path).read_bytes()
        if sha256(raw_schema).hexdigest() != OFFICIAL_CKLB_SCHEMA_SHA256:
            raise EvidenceError('CKLB schema digest mismatch (expected official DISA 1.0 schema)')
        schema = json.loads(raw_schema)
        if not isinstance(schema, dict) or not schema:
            raise ValueError('schema must be a nonempty JSON object')
        def check_refs(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in ('$ref', '$dynamicRef') and (not isinstance(value, str) or not value.startswith('#')):
                        raise EvidenceError('external schema references are forbidden')
                    check_refs(value)
            elif isinstance(node, list):
                for value in node:
                    check_refs(value)
        check_refs(schema)
        validator_class = validator_for(schema)
        validator_class.check_schema(schema)
        validator_class(schema).validate(doc)
    except (OSError, ValueError, TypeError, SchemaError, ValidationError) as exc:
        raise EvidenceError(f'CKLB schema validation failed: {exc}') from exc
    return True


def validate_cklb_structure(doc):
    """Offline subset of the official CKLB 1.0 schema; not full schema validation."""
    if not isinstance(doc, dict) or set(doc) - {'title', 'id', 'cklb_version', 'target_data', 'stigs'}:
        raise EvidenceError('unexpected checklist fields')
    if not isinstance(doc.get('title'), str) or not isinstance(doc.get('id'), str) or doc.get('cklb_version') != '1.0':
        raise EvidenceError('invalid checklist identity/version')
    if not isinstance(doc.get('target_data'), dict) or not isinstance(doc.get('stigs'), list) or not doc['stigs']:
        raise EvidenceError('missing target or STIG')
    for stig in doc['stigs']:
        if any(not isinstance(stig.get(k), str) for k in ('stig_name', 'display_name', 'stig_id', 'release_info', 'uuid')):
            raise EvidenceError('invalid STIG identity')
        if type(stig.get('size')) is not int or len(stig.get('rules', [])) != stig['size']:
            raise EvidenceError('incomplete STIG rule inventory')
        for rule in stig['rules']:
            if rule.get('severity') not in ('unknown', 'low', 'medium', 'high') or rule.get('status') not in ('not_reviewed', 'not_applicable', 'open', 'not_a_finding'):
                raise EvidenceError('invalid rule severity/status')
            if not all(isinstance(rule.get(k), str) for k in ('rule_id_src', 'group_id_src', 'finding_details', 'comments')):
                raise EvidenceError('invalid rule fields')
    return True


def generate_cklb(policy, benchmark_xml, results_xml, host_name, *, now=None,
                  max_age_hours=24, strict_release=False, schema_path=None,
                  require_full_coverage=False, scan_profile='quarterly-profile'):
    """Return one per-host CKLB dict. Caller serializes JSON; no file/guest side effects.

    Pass full official XCCDF Benchmark and OpenSCAP XCCDF results XML.
    require_full_coverage (or strict_release) rejects absent rule-results,
    not explicit manual/unknown results.
    schema_path enables full validation with the byte-pinned official DISA schema.
    Only an observed notapplicable AND asset-specific approved applicability
    evidence become not_applicable. Exceptions never convert failures to passes.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not isinstance(host_name, str) or not host_name.strip() or max_age_hours <= 0:
        raise EvidenceError('valid host_name and positive max_age_hours required')
    bench = validate_policy(policy, benchmark_xml, now=now)
    if scan_profile not in ('quarterly-profile', policy['profile_id']):
        raise EvidenceError('scan profile must be the generated or original profile')
    result_map, ended = _scan_results(_xml(results_xml), policy, host_name, now, max_age_hours, bench, scan_profile)
    stig_id = bench.get('id')
    groups = bench.findall(f'.//{{{X}}}Group')
    inventory = [(group, rule) for group in groups for rule in group.findall(f'{{{X}}}Rule')]
    rule_ids = [rule.get('id') for _, rule in inventory]
    if (not inventory or any(not rid for rid in rule_ids)
            or any(not group.get('id') for group, _ in inventory)
            or len(rule_ids) != len(set(rule_ids))):
        raise PolicyError('benchmark has missing or duplicate group/rule identifiers')
    if set(result_map) - set(rule_ids):
        raise EvidenceError('scan contains rules absent from official benchmark')
    if (strict_release or require_full_coverage) and set(rule_ids) - set(result_map):
        raise EvidenceError('missing scan rule-result: ' + ', '.join(sorted(set(rule_ids) - set(result_map))))
    exceptions = {e['rule_id']: e for e in policy.get('exceptions', [])}
    rules = []
    stig_uuid = str(uuid5(NAMESPACE_URL, stig_id + ':' + str(policy['benchmark_version'])))
    for group, rule in inventory:
        rid = rule.get('id')
        group_id = group.get('id')
        assert rid is not None and group_id is not None  # checked above
        scan = result_map.get(rid)
        observed = _text(scan, f'{{{X}}}result').lower() if scan is not None else 'missing'
        exception = exceptions.get(rid)
        if observed == 'fail':
            status = 'open'
        elif observed == 'pass':
            status = 'not_a_finding'
        elif (observed == 'notapplicable' and exception and exception['disposition'] == 'not_applicable'
              and host_name in exception['applicability_by_host']):
            status = 'not_applicable'
        else:
            status = 'not_reviewed'
        if policy.get('selections', {}).get(rid) is False and status != 'open':
            status = 'not_reviewed'
        evidence = [f'OpenSCAP XCCDF result: {observed}; scan ended: {ended.isoformat()}; target: {host_name}']
        if scan is not None:
            for field in ('message', 'check'):
                for node in scan.findall(f'{{{X}}}{field}'):
                    detail = ' '.join(''.join(node.itertext()).split())
                    if detail:
                        evidence.append(f'{field}: {detail}')
        comments = ''
        if exception:
            comments = (f"External approval reference {exception['approval_id']} (approver: {exception['approver']}; "
                        f"expires: {exception['expires']}; disposition: {exception['disposition']}). "
                        f"Rationale: {exception['rationale']}. Comments: {exception['comments']}. "
                        f"External verification required: {exception['external_verification']}. "
                        'Policy metadata is not a cryptographic signature.')
            if status == 'not_applicable':
                evidence.append('Asset-specific applicability: ' + exception['applicability_by_host'][host_name])
        ref = rule.find(f'{{{X}}}reference/{{{X}}}identifier')
        ref_id = ref.text.strip() if ref is not None and ref.text else None
        check = rule.find(f'{{{X}}}check/{{{X}}}check-content-ref')
        ccis = []
        for ident in rule.findall(f'{{{X}}}ident'):
            if ident.text and ident.text.strip().startswith('CCI-'):
                ccis.append(ident.text.strip())
        raw = _text(rule, f'{{{X}}}description')
        import re
        ccis.extend(re.findall(r'CCI-\d+', raw))
        rules.append({
            'uuid': str(uuid5(NAMESPACE_URL, stig_uuid + rid)), 'stig_uuid': stig_uuid,
            'group_id': group_id.removeprefix('xccdf_mil.disa.stig_group_'), 'group_id_src': group_id,
            'rule_id': rid.removeprefix('xccdf_mil.disa.stig_rule_'), 'rule_id_src': rid,
            'severity': rule.get('severity', 'unknown'), 'weight': rule.get('weight', ''),
            'rule_version': _text(rule, f'{{{X}}}version'), 'rule_title': _text(rule, f'{{{X}}}title'),
            'group_title': _text(group, f'{{{X}}}title'), 'discussion': _description(rule, 'VulnDiscussion'),
            'false_positives': _description(rule, 'FalsePositives'),
            'fix_text': _text(rule, f'{{{X}}}fixtext'), 'check_content': _text(rule, f'{{{X}}}check/{{{X}}}check-content'),
            'check_content_ref': {'name': check.get('name', ''), 'href': check.get('href', '')} if check is not None else None,
            'reference_identifier': ref_id, 'ccis': list(dict.fromkeys(ccis)),
            'status': status, 'finding_details': '\n'.join(evidence), 'comments': comments,
        })
    doc = {
        'title': f'{host_name} - {_text(bench, f"{{{X}}}title")}',
        'id': str(uuid5(NAMESPACE_URL, host_name + ':' + stig_uuid + ':' + ended.isoformat())),
        'cklb_version': '1.0', 'target_data': {'host_name': host_name},
        'stigs': [{
            'stig_name': _text(bench, f'{{{X}}}title'), 'display_name': _text(bench, f'{{{X}}}title'),
            'stig_id': stig_id, 'release_info': _text(bench, f"{{{X}}}plain-text[@id='release-info']"),
            'uuid': stig_uuid, 'reference_identifier': rules[0]['reference_identifier'],
            'size': len(rules), 'rules': rules,
        }],
    }
    validate_cklb_structure(doc)
    if schema_path is not None:
        validate_cklb_schema(doc, schema_path)
    return doc
