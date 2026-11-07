"""Quarterly STIG lab: safe plans and fail-closed, per-asset evidence release.

This CLI never runs virt-install, defines a network, or deletes guest resources.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile

from . import vm
from .policy import PolicyError, X, _xml, load_policy, generate_tailoring
from .cklb import EvidenceError, generate_cklb


class ReleaseError(ValueError):
    """Evidence/approval gate failure; nothing was published."""


_REQUIRED_CREDENTIALS = frozenset({'QUARTERLY_USERNAME', 'QUARTERLY_PASSWORD',
                                   'QUARTERLY_ROOT_PASSWORD', 'QUARTERLY_LABADMIN_PUBLIC_KEY'})


def load_credentials(path):
    """Parse an owner-only literal KEY=value file; never source shell code."""
    path = Path(path)
    if (path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid()
            or path.stat().st_mode & 0o077):
        raise ReleaseError('private credentials file must be owned and mode 0600')
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except (OSError, UnicodeError) as exc:
        raise ReleaseError('unable to read private credentials') from exc
    result = {}
    for line in lines:
        if not line or line.lstrip().startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep or key not in _REQUIRED_CREDENTIALS or key in result or not value or '\x00' in value:
            raise ReleaseError('invalid, duplicate or unknown credential entry')
        result[key] = value
    if result.keys() != _REQUIRED_CREDENTIALS:
        raise ReleaseError('missing credential entry')
    return result


def hash_password(password):
    """SHA512-crypt through stdin; plaintext is never placed in argv or logs."""
    if not password or '\n' in password or '\x00' in password:
        raise ReleaseError('invalid password input')
    try:
        proc = subprocess.run(['openssl', 'passwd', '-6', '-stdin'], input=password + '\n',
                              capture_output=True, text=True, timeout=20, check=True)
        hashed = proc.stdout.strip()
        if not hashed.startswith('$6$'):
            raise ReleaseError('unexpected password hash output')
        return hashed
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleaseError('password hashing unavailable') from exc


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def verify_approvals(exceptions, policy, benchmark, ledger, signature, trusted_key):
    """Check detached signature and bind every exception to exact policy/benchmark.

    Trust in the public key's owner must be established out of band by the ISSO.
    """
    try:
        ledger_bytes = Path(ledger).read_bytes()
        # OpenSSL requires paths for the signature and key; pin their exact bytes
        # in a private directory so a later replacement of either path is inert.
        with tempfile.TemporaryDirectory(prefix='quarterly-approval-') as td:
            key_copy = Path(td) / 'trusted.pub'
            signature_copy = Path(td) / 'approval.sig'
            _create_private(key_copy, Path(trusted_key).read_bytes())
            _create_private(signature_copy, Path(signature).read_bytes())
            result = subprocess.run(['openssl', 'dgst', '-sha256', '-verify', str(key_copy),
                                     '-signature', str(signature_copy)],
                                    input=ledger_bytes, capture_output=True, timeout=20)
        if result.returncode or result.stdout.strip() != b'Verified OK':
            raise ReleaseError('approval ledger signature failed verification')
        data = json.loads(ledger_bytes.decode('utf-8'))
        if (data.get('schema') != 1 or data.get('policy_sha256') != digest_file(policy)
                or data.get('benchmark_sha256') != digest_file(benchmark)):
            raise ReleaseError('signed ledger is not bound to the exact policy and benchmark files')
        entries = data.get('approvals')
        if not isinstance(entries, list):
            raise ReleaseError('signed approval list missing')
        indexed = {}
        for entry in entries:
            identifier = entry.get('approval_id')
            if not identifier or identifier in indexed:
                raise ReleaseError('missing or duplicate approval ID')
            indexed[identifier] = entry
        for item in exceptions:
            entry = indexed.get(item['approval_id'])
            if entry is None or any(entry.get(k) != item[k] for k in ('rule_id', 'approver', 'disposition', 'expires')):
                raise ReleaseError('signed approval does not match policy exception')
        return hashlib.sha256(ledger_bytes).hexdigest()
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError, KeyError, TypeError) as exc:
        if isinstance(exc, ReleaseError):
            raise
        raise ReleaseError('approval verification could not complete') from exc


def _create_private(path, data):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        Path(path).unlink(missing_ok=True)
        raise


_RELEASE_INPUTS = ('policy', 'benchmark', 'pre', 'post', 'coverage', 'tailoring',
                   'schema', 'approval_ledger', 'approval_signature', 'trusted_key')


def _snapshot_inputs(args, directory):
    """Capture release inputs once in an owner-only directory before any gates."""
    paths = {}
    for name in _RELEASE_INPUTS:
        source = getattr(args, name)
        try:
            fd = os.open(source, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ReleaseError('invalid release input')
                content = stream.read()
            target = Path(directory) / (name + source.suffix)
            _create_private(target, content)
            paths[name] = target
        except OSError as exc:
            raise ReleaseError('unable to snapshot release input') from exc
    return paths


def write_evidence(destination, host, cklb, inputs, metadata):
    """Create a new, owner-only evidence directory; never replace existing output."""
    root = Path(destination)
    if not host or '/' in host or host in ('.', '..') or not all(c.isalnum() or c in '-._' for c in host):
        raise ReleaseError('invalid asset hostname')
    if root.exists() or root.is_symlink():
        raise ReleaseError('evidence destination exists: choose a new run directory')
    if not root.parent.is_dir() or root.parent.is_symlink() or root.parent.stat().st_mode & 0o022:
        raise ReleaseError('evidence parent must exist, not be a symlink and not be group/world writable')
    sources = {}
    for name, source in sorted(inputs.items()):
        path = Path(source)
        if not name.isidentifier() or not path.is_file() or path.is_symlink():
            raise ReleaseError('invalid evidence source')
        sources[name] = (path.suffix, path.read_bytes())
    root.mkdir(mode=0o700)
    try:
        records = {}
        for name, (suffix, content) in sources.items():
            copy = root / (name + suffix)
            _create_private(copy, content)
            records[name] = {'file': copy.name, 'sha256': digest_file(copy)}
        checklist = root / (host + '.cklb')
        _create_private(checklist, (json.dumps(cklb, indent=2, sort_keys=True) + '\n').encode())
        manifest = {'host': host, 'cklb': {'file': checklist.name, 'sha256': digest_file(checklist)},
                    'inputs': records, 'metadata': metadata,
                    'created_utc': datetime.now(timezone.utc).isoformat(),
                    'review_state': 'pending ISSO STIG Viewer import and signed per-asset acceptance'}
        _create_private(root / 'manifest.json', (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode())
        return checklist
    except BaseException:
        # Never purge a partial evidence directory: preserve it for forensic inspection.
        raise


def _release_cklb(args, paths):
    """Validate and publish from the same private snapshot, not mutable sources."""
    policy = load_policy(paths['policy'])
    lab = vm.load_manifest(args.manifest)
    matching = [n for n in lab['nodes'] if args.host in (n['name'], n['fqdn'])]
    if len(matching) != 1 or args.host != matching[0]['fqdn']:
        raise ReleaseError('host must match an exact manifest FQDN')
    signed = verify_approvals(policy.get('exceptions', []), paths['policy'], paths['benchmark'],
                              paths['approval_ledger'], paths['approval_signature'], paths['trusted_key'])
    tailoring_root = _xml(paths['tailoring'])
    hrefs = tailoring_root.findall(f'{{{X}}}benchmark')
    href = hrefs[0].get('href') if len(hrefs) == 1 else None
    if not href or Path(href).name != args.benchmark.name:
        raise ReleaseError('tailoring benchmark reference differs from pinned input')
    from xml.etree import ElementTree as ET
    expected = _xml(generate_tailoring(policy, paths['benchmark'], href))
    if ET.canonicalize(ET.tostring(tailoring_root, encoding='unicode'), strip_text=True) != ET.canonicalize(ET.tostring(expected, encoding='unicode'), strip_text=True):
        raise ReleaseError('tailoring differs from master policy')
    # The untailored coverage scan determines every rule; a tailored post-scan
    # remains separately retained for comparison and human review.
    doc = generate_cklb(policy, paths['benchmark'], paths['coverage'], args.host,
                        require_full_coverage=True, scan_profile=policy['profile_id'],
                        schema_path=paths['schema'])
    generate_cklb(policy, paths['benchmark'], paths['pre'], args.host,
                  require_full_coverage=True, scan_profile=policy['profile_id'],
                  max_age_hours=72)
    post_doc = generate_cklb(policy, paths['benchmark'], paths['post'], args.host)
    post_root = _xml(paths['post'])
    post_result = post_root.find(f'{{{X}}}TestResult')
    if post_result is None:
        raise ReleaseError('tailored post-scan has no TestResult')
    post_ids = {item.get('idref') for item in post_result.findall(f'{{{X}}}rule-result')}
    coverage_rules = {r['rule_id_src']: r['status'] for r in doc['stigs'][0]['rules']}
    required_post = {rid for rid in coverage_rules if policy.get('selections', {}).get(rid) is not False}
    if required_post - post_ids:
        raise ReleaseError('selected post-scan rule missing: ' + ', '.join(sorted(required_post - post_ids)))
    for rule in post_doc['stigs'][0]['rules']:
        rid, observed = rule['rule_id_src'], rule['status']
        if rid in required_post and coverage_rules[rid] != observed:
            raise ReleaseError('tailored post-scan disagrees with coverage scan: ' + rid)
    def scan_end(path):
        result = _xml(path).find(f'{{{X}}}TestResult')
        if result is None:
            raise ReleaseError('scan has no TestResult')
        return datetime.fromisoformat(result.attrib['end-time'].replace('Z', '+00:00'))
    if not scan_end(paths['pre']) < scan_end(paths['post']) <= scan_end(paths['coverage']):
        raise ReleaseError('scan chronology must be pre < post <= coverage')
    metadata = {'approval_ledger_sha256': signed, 'schema_sha256': digest_file(paths['schema']),
                'trusted_key_sha256': digest_file(paths['trusted_key']),
                'review_state': 'unsigned: pending independent ISSO signoff'}
    return write_evidence(args.evidence_dir, args.host, doc, paths, metadata)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Isolated quarterly RHEL STIG lab and evidence')
    parser.add_argument('--manifest', type=Path, default=vm.MANIFEST)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('inventory')
    sub.add_parser('network-xml')
    sub.add_parser('preflight')
    ks = sub.add_parser('kickstart')
    ks.add_argument('--node', required=True)
    ks.add_argument('--env', type=Path, required=True)
    ks.add_argument('--out', type=Path, required=True)
    install = sub.add_parser('install-plan', help='print command only; no VM is created')
    install.add_argument('--node', required=True)
    install.add_argument('--kickstart', type=Path, required=True)
    install.add_argument('--confirm', required=True)
    clone = sub.add_parser('clone-plan', help='print inert clone metadata only')
    clone.add_argument('--node', required=True)
    clone.add_argument('--base-sha256', required=True)
    clone.add_argument('--confirm', required=True)
    create_clone = sub.add_parser('clone', help='atomically copy a verified sealed base disk, but do not boot it')
    create_clone.add_argument('--node', required=True)
    create_clone.add_argument('--base-sha256', required=True)
    create_clone.add_argument('--confirm', required=True)
    tailor = sub.add_parser('tailor')
    tailor.add_argument('--policy', type=Path, required=True)
    tailor.add_argument('--benchmark', type=Path, required=True)
    tailor.add_argument('--benchmark-href', required=True)
    tailor.add_argument('--out', type=Path, required=True)
    evidence = sub.add_parser('cklb')
    for arg in ('policy', 'benchmark', 'pre', 'post', 'coverage', 'tailoring', 'schema', 'approval-ledger', 'approval-signature', 'trusted-key', 'evidence-dir', 'host'):
        evidence.add_argument('--' + arg, required=True, type=str if arg == 'host' else Path)
    try:
        args = parser.parse_args(argv)
        if args.command == 'inventory':
            print(json.dumps(vm.load_manifest(args.manifest)['nodes'], indent=2))
        elif args.command == 'network-xml':
            print(vm.render_network_xml(vm.load_manifest(args.manifest)))
        elif args.command == 'preflight':
            problems = vm.preflight(vm.load_manifest(args.manifest))
            if problems:
                raise ReleaseError('; '.join(problems))
            print('Preflight: no currently detectable route/network/domain/clone-disk collision')
        elif args.command == 'kickstart':
            env = load_credentials(args.env)
            hashed = hash_password(env['QUARTERLY_PASSWORD'])
            root_hash = hash_password(env['QUARTERLY_ROOT_PASSWORD'])
            rendered = vm.render_kickstart(vm.load_manifest(args.manifest), args.node,
                username=env['QUARTERLY_USERNAME'], password_hash=hashed,
                root_password_hash=root_hash,
                ssh_public_key=env['QUARTERLY_LABADMIN_PUBLIC_KEY'],
                labadmin_ssh_public_key=env['QUARTERLY_LABADMIN_PUBLIC_KEY'])
            _create_private(args.out, rendered.encode())
            print(args.out)
        elif args.command == 'install-plan':
            command = vm.plan_install(vm.load_manifest(args.manifest), args.node,
                                      kickstart=args.kickstart, confirm=args.confirm)
            print(shlex.join(command))
        elif args.command == 'clone-plan':
            plan = vm.plan_clone(vm.load_manifest(args.manifest), args.node,
                                 base_sha256=args.base_sha256, confirm=args.confirm)
            print(json.dumps(plan, indent=2))
        elif args.command == 'clone':
            path = vm.execute_clone(vm.load_manifest(args.manifest), args.node,
                                    base_sha256=args.base_sha256, confirm=args.confirm)
            print(path)
        elif args.command == 'tailor':
            policy = load_policy(args.policy)
            xml = generate_tailoring(policy, args.benchmark, args.benchmark_href)
            _create_private(args.out, xml.encode())
            print(args.out)
        elif args.command == 'cklb':
            with tempfile.TemporaryDirectory(prefix='quarterly-input-') as td:
                paths = _snapshot_inputs(args, td)
                print(_release_cklb(args, paths))
        return 0
    except (ReleaseError, PolicyError, EvidenceError, vm.PreflightError, OSError, ValueError,
            subprocess.SubprocessError) as exc:
        print(f'quarterly: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
