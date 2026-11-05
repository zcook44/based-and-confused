#!/usr/bin/env python3
"""Generate deliberately disk-scoped RHEL lab Kickstarts and remaster vendor DVDs."""
import argparse
import configparser
import pathlib
import re
import subprocess
import sys
import tempfile

PROFILE = 'xccdf_org.ssgproject.content_profile_stig'


def render_kickstart(version: str, disk: str, key: str) -> str:
    if version not in ('8', '9', '10'):
        raise ValueError('only RHEL 8, 9 and 10 are supported')
    if not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]*', disk):
        raise ValueError('disk must be a bare device name, e.g. vda; never a path')
    key = key.strip()
    if not re.fullmatch(r'(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp\d+) [A-Za-z0-9+/=]+(?: [A-Za-z0-9@._+-]+)?', key):
        raise ValueError('expected one SSH public key (no newlines or quotes)')
    hardening = (f'''%addon com_redhat_oscap
content-type = scap-security-guide
profile = {PROFILE}
%end''' if version in ('8', '9') else f'''# RHEL 10 removed oscap-anaconda-addon. This is post-install remediation,
# not equivalent to a vendor-generated, reviewed install-time STIG Kickstart.
%post --log=/root/ks-stig-remediation.log
oscap xccdf eval --remediate --profile {PROFILE} \\
  --results /root/stig-install-results.xml \\
  --report /root/stig-install-report.html \\
  /usr/share/xml/scap/ssg/content/ssg-rhel10-ds.xml
result=$?
# OpenSCAP exit 2 means rules remain noncompliant; retain evidence for review.
if [ "$result" -ne 0 ] && [ "$result" -ne 2 ]; then exit "$result"; fi
%end''')
    return f'''# RHEL {version} lab STIG baseline; destructive ONLY on /dev/{disk}.
# Review vendor profile and update the ISO to your intended minor release.
text
lang en_US.UTF-8
keyboard --vckeymap=us --xlayouts='us'
timezone UTC --utc
network --bootproto=dhcp --device=link --activate
cdrom
rootpw --lock
user --name=labadmin --groups=wheel --lock
sshkey --username=labadmin "{key}"
selinux --enforcing
firewall --enabled --service=ssh
services --enabled=sshd,chronyd,auditd
bootloader --append="fips=1"
ignoredisk --only-use={disk}
zerombr
clearpart --all --initlabel --drives={disk}
reqpart --add-boot
part pv.01 --fstype=lvmpv --size=1 --grow --ondisk={disk}
volgroup vg_lab pv.01
logvol / --vgname=vg_lab --name=root --fstype=xfs --size=16384 --grow
logvol /home --vgname=vg_lab --name=home --fstype=xfs --size=4096
logvol /tmp --vgname=vg_lab --name=tmp --fstype=xfs --size=4096
logvol /var --vgname=vg_lab --name=var --fstype=xfs --size=8192
logvol /var/tmp --vgname=vg_lab --name=var_tmp --fstype=xfs --size=4096
logvol /var/log --vgname=vg_lab --name=var_log --fstype=xfs --size=4096
logvol /var/log/audit --vgname=vg_lab --name=audit --fstype=xfs --size=4096
logvol swap --vgname=vg_lab --name=swap --fstype=swap --size=2048
%packages
@^minimal-environment
openscap-scanner
scap-security-guide
openscap-utils
aide
audit
chrony
%end
{hardening}
# Expected datastream on the installed guest: ssg-rhel{version}-ds.xml
reboot --eject
'''


def inspect_treeinfo(text: str, version: str) -> None:
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_string(text)
    general = cfg['general']
    if 'Red Hat Enterprise Linux' not in general.get('family', ''):
        raise ValueError('not a Red Hat Enterprise Linux installer ISO')
    if not re.match(r'^' + re.escape(version) + r'\.', general.get('version', '')):
        raise ValueError('installer minor/major version does not match requested RHEL major')
    for repo in ('BaseOS', 'AppStream'):
        if not any(s.lower() in (f'repository-{repo.lower()}', f'variant-{repo.lower()}')
                   and cfg[s].get('repository', repo).lower() == repo.lower()
                   for s in cfg.sections()):
            raise ValueError(f'full installation DVD with {repo} repo required (not boot.iso)')


def patch_boot_config(text: str, kind: str) -> str:
    if kind not in ('bios', 'efi'):
        raise ValueError('unknown boot configuration')
    def edit(match):
        line = match.group(0)
        if 'inst.stage2=' not in line or 'inst.ks=' in line or 'inst.rescue' in line:
            return line
        additions = ' inst.ks=cdrom:/ks.cfg'
        if 'fips=1' not in line:
            additions += ' fips=1'
        return line.rstrip() + additions
    pattern = r'(?m)^\s*append\s+[^\n]*' if kind == 'bios' else r'(?m)^\s*(?:linux|linuxefi)\s+[^\n]*'
    updated = re.sub(pattern, edit, text)
    if 'inst.ks=cdrom:/ks.cfg' not in updated:
        raise ValueError(f'no recognized {kind} installation entry with inst.stage2=; refusing ISO')
    if kind == 'efi':
        updated = re.sub(r'(?m)^(\s*set default=)["\']?\d+["\']?', r'\g<1>"0"', updated, count=1)
    elif re.search(r'(?m)^label linux\s*$', updated):
        updated = re.sub(r'(?m)^\s*menu default\s*\n', '', updated)
        updated = re.sub(r'(?m)^(label linux\s*\n[^\n]*menu label[^\n]*\n)',
                         r'\1menu default\n', updated, count=1)
        if 'menu default' not in updated.split('label check')[0]:
            raise ValueError('unable to select BIOS install entry as default')
    return updated


def xorriso(*args):
    return subprocess.run(['xorriso', *map(str, args)], check=True, capture_output=True, text=True).stdout


def extract(iso, source, dest):
    xorriso('-osirrox', 'on', '-indev', iso, '-extract', source, dest)
    return pathlib.Path(dest).read_bytes()


def make_iso(version: str, source: pathlib.Path, ks: pathlib.Path, output: pathlib.Path) -> None:
    source, ks, output = source.resolve(), ks.resolve(), output.resolve()
    if source == output:
        raise ValueError('output cannot overwrite the vendor installer')
    if output.exists():
        raise ValueError('output already exists (no overwrite)')
    boot_report = xorriso('-indev', source, '-report_el_torito', 'plain')
    if 'El Torito' not in boot_report:
        raise ValueError('source ISO has no El Torito boot information')
    grub2_bios = 'El Torito img path :   1  /images/eltorito.img' in boot_report
    with tempfile.TemporaryDirectory(prefix='rhel-ks-') as tmp:
        tmp = pathlib.Path(tmp)
        inspect_treeinfo(extract(source, '/.treeinfo', tmp / 'treeinfo').decode(), version)
        configs = [('efi', ('/EFI/BOOT/grub.cfg', '/EFI/BOOT/GRUB.CFG'))]
        if grub2_bios:
            configs.insert(0, ('efi', ('/boot/grub2/grub.cfg',)))
        else:
            configs.insert(0, ('bios', ('/isolinux/isolinux.cfg',)))
        mapping = []
        verify = []
        for kind, choices in configs:
            data = None
            for path in choices:
                try:
                    data = extract(source, path, tmp / f'{kind}.orig').decode()
                    break
                except (subprocess.CalledProcessError, UnicodeDecodeError):
                    continue
            if data is None:
                raise ValueError(f'{kind} boot config not found; ISO layout needs review')
            patched = tmp / ('patched-' + path.strip('/').replace('/', '-'))
            patched.write_text(patch_boot_config(data, kind))
            mapping += ['-map', patched, path]
            verify.append((path, patched.read_bytes()))
        # A temporary output beside the destination makes publication atomic.
        output.parent.mkdir(parents=True, exist_ok=True)
        temp_output = output.with_name(output.name + '.partial')
        if temp_output.exists():
            raise ValueError(f'stale temporary ISO: {temp_output}')
        try:
            xorriso('-indev', source, '-outdev', temp_output, '-overwrite', 'on',
                    '-map', ks, '/ks.cfg', *mapping, '-boot_image', 'any', 'replay', '-commit')
            for path, expected in [('/ks.cfg', ks.read_bytes()), *verify]:
                if extract(temp_output, path, tmp / ('verify-' + path.split('/')[-1])) != expected:
                    raise ValueError(f'ISO verification failed for {path}')
            if 'El Torito' not in xorriso('-indev', temp_output, '-report_el_torito', 'plain'):
                raise ValueError('generated ISO lost boot metadata')
            temp_output.rename(output)
        finally:
            temp_output.unlink(missing_ok=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    render = sub.add_parser('render', help='render disk-scoped lab Kickstart')
    render.add_argument('--version', choices=['8', '9', '10'], required=True)
    render.add_argument('--disk', required=True, help='VM-only target device, e.g. vda')
    render.add_argument('--ssh-key', type=pathlib.Path, required=True)
    render.add_argument('--out', type=pathlib.Path, required=True)
    iso = sub.add_parser('iso', help='embed Kickstart in full RHEL DVD (BIOS/UEFI)')
    iso.add_argument('--version', choices=['8', '9', '10'], required=True)
    iso.add_argument('--source', type=pathlib.Path, required=True)
    iso.add_argument('--ks', type=pathlib.Path, required=True)
    iso.add_argument('--out', type=pathlib.Path, required=True)
    a = p.parse_args()
    try:
        if a.command == 'render':
            if a.out.exists():
                raise ValueError('output already exists (no overwrite)')
            data = render_kickstart(a.version, a.disk, a.ssh_key.read_text())
            a.out.parent.mkdir(parents=True, exist_ok=True)
            a.out.write_text(data)
        else:
            make_iso(a.version, a.source, a.ks, a.out)
        print(a.out.resolve())
    except (ValueError, OSError, subprocess.CalledProcessError) as e:
        p.exit(1, f'error: {e}\n')


if __name__ == '__main__':
    main()
