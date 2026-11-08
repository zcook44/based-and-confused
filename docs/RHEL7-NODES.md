# RHEL 7.9 lab nodes

Two additional **disposable** VMs use the full RHEL 7.9 x86_64 Server DVD directly. They are managed by the same `lab-vms.sh` commands as the existing eight nodes; rebuilding the 8/9/10 menu ISO or extracting a RHEL 7 HTTP tree is unnecessary.

**Navigation:** [Installer lab and node map](README.md#nodes-and-addresses) · [Fresh-install Ansible dependencies](ANSIBLE-KICKSTART-DEPENDENCIES.md) · [Eight-entry menu quickstart](QUICKSTART.md). These two are Bash-bootstrap targets, **not** menu-ISO entries. The DVD is the package source; a menu rebuild would be an unrelated operation.

| VM | Static address | Install role | Security baseline |
| --- | --- | --- | --- |
| `rhel7-node1` | `192.168.150.131` | Minimal/headless | DVD SSG `stig` through RHEL 7 Anaconda `%addon org_fedora_oscap` |
| `rhel7-node2` | `192.168.150.132` | Server with GUI | DVD SSG `stig` via post-install OpenSCAP remediation, tailored to retain Xorg |

Both use `stig-lab`, 64 GiB *new* VirtIO disks, legacy BIOS for new builds, `fips=1` at installer boot and installed bootloader, SELinux enforcing, `openscap-scanner`, `openscap-utils` and `scap-security-guide`. The Kickstart **erases `/dev/vda`**. Do not attach any existing data disk. Neither guest is registered with RHSM or added to an upstream service by this workflow.

**Existing-domain firmware exception:** an earlier-installed `rhel7-node1` used UEFI and initially page-faulted while launching the installed RHEL 7 shim with the host's newer OVMF. Its **existing domain only** now points to `/scratch/vms/firmware/rhel8-ovmf-202411-1/OVMF_CODE.secboot.4m.fd` from a signature-verified Arch `edk2-ovmf-202411-1` package, retaining its original disk, NVRAM and Secure Boot setting; no host-wide firmware downgrade was made. Do not remove that firmware image while this domain exists. A newly created RHEL 7 domain uses the scripted BIOS path instead.

## Media and installation

The approved local DVD is `/scratch/iso/rhel-7.9-x86_64-dvd.iso`. Its SHA-256 is `e373d3efe1a6e3f6d6de105d32c2632ee661363d6fe0c24285379b85f99437cb`, matching the [Red Hat Developer RHEL download page](https://developers.redhat.com/products/rhel/download) for the RHEL 7.9 x86_64 **DVD**, not the boot ISO. Set `LAB_RHEL7_ISO` to override the path. Set `LAB_RHEL7_SSH_KEY` to an authorized ECDSA/RSA `.pub` file; the local default is `~/.ssh/id_ecdsa.pub`. RHEL 7 Kickstart does not accept `sshkey`; the key is installed by a root-owned `%post` into the selected user's `authorized_keys`. Use the corresponding private key when connecting.

```sh
cd /home/zrc/git/based-and-confused
# Existing lab-vms.env must be owner-owned, non-symlinked, mode 0600.
# If absent: install -m 600 lab-vms.env.example lab-vms.env; edit its values locally.
./lab-vms.sh --dry-run up rhel7-node1 rhel7-node2
./lab-vms.sh prepare rhel7-node1 rhel7-node2
# Inspect the private Kickstarts under /scratch/vms, then start deliberately:
./lab-vms.sh up rhel7-node1 rhel7-node2
./lab-vms.sh status rhel7-node1 rhel7-node2
./lab-vms.sh stop rhel7-node1 rhel7-node2
./lab-vms.sh start rhel7-node1 rhel7-node2
```

`prepare` writes only private hashed-password Kickstarts; `--dry-run up` writes nothing. `up` creates missing domains/disks and starts stopped existing domains without reinstalling. `start` only accepts already-defined guests; `stop` requests graceful shutdown and does not delete anything. The user and root password hashes are derived from the same private `lab-vms.env` used by the other nodes; changing that file later does not rotate a guest. `lab-vms.sh status all` now includes ten nodes. Do not run `up all` without checking host memory.

## Security exceptions and guest gates

**GUI and sudo exceptions:** the DVD's `scap-security-guide-0.1.49-13.el7` includes `stig`, but **not** `stig_gui`; its `stig` profile selects `package_xorg-x11-server-common_removed`, which would break a graphical login. `rhel7-node2` uses a local XCCDF tailoring file to deselect exactly that removal rule and runs `oscap xccdf eval --remediate` with the tailored profile after installing the GUI environment. It retains raw results and an HTML report under `/root`. This exception is a finding requiring review, not a claim of full DISA compliance. Headless `node1` uses the unmodified installer profile. Both nodes deliberately give **only `labadmin`** a root-owned, `0440`, post-remediation `/etc/sudoers.d/90-labadmin` rule for passwordless sudo; it violates the STIG password-prompt control and must be recorded as a separate finding. With a custom operator username, the final post-install creates a separate locked-password, key-only `labadmin` instead of granting the operator account this exception. Existing guests installed before this change were repaired in place; a future Kickstart must be regenerated with `./lab-vms.sh prepare rhel7-node1 rhel7-node2` before a new install. Verify on each guest with `sudo -n id -u` as `labadmin`, and through the RHEL 7 EE with `./ansible-ee ansible -i inventory-rhel7.yml rhel7 -b -m ansible.builtin.ping` from `/scratch/ansible`.

**Post-install gates (run before adding them to an upstream patching service):** verify installation completed, static IP/hostname, installed release, GUI login on node2, SSH with the intended key, actual FIPS mode, SELinux state, package/content presence, and a new untailored/tailored SCAP scan. For example, on each guest:

```sh
cat /etc/redhat-release
cat /proc/sys/crypto/fips_enabled
getenforce
rpm -q openscap-scanner scap-security-guide
oscap info /usr/share/xml/scap/ssg/content/ssg-rhel7-ds.xml
```

For node1 assess the packaged `xccdf_org.ssgproject.content_profile_stig`; for node2 assess `xccdf_org.lab_profile_rhel7_gui` with `--tailoring-file /root/rhel7-gui-tailoring.xml`, then compare against an untailored coverage scan. Retain the assessment results and track any failed/manual checks. `ksvalidator`, a dry-run libvirt XML, and even successful provisioning do **not** prove compliance. RHEL 7 is in an extended-support phase; confirm your organization's entitlement, lifecycle and approved content source before connecting either guest to the patching upstream.

**Local connectivity:** After both guests passed first-boot, authenticated SSH, FIPS (`1`), SELinux (`Enforcing`), OpenSCAP package, and graphical-target/GDM checks, `inventory/rhel7.ini` was added with the two static IPs and the ECDSA key. `/etc/hosts` and `~/.ssh/config` contain matching aliases, pinned to the same IP host-key identities. Check without Python on the guests: `ansible -i inventory/rhel7.ini rhel7_nodes -m raw -a 'hostname -f'`. The workstation's controller is ansible-core 2.21, which does **not** support the already-installed guests' Python 2.7. The updated RHEL 7 Kickstart explicitly requests the DVD's Python 3.6 for **future** installs, but 3.6 still falls below the workstation controller's managed-node range. For the **current** guests, the dedicated Docker EE under `/scratch/ansible` runs ansible-core 2.16.20 against `/usr/bin/python` (2.7); both `ping` and `-b ping` have passed on both nodes without changing the workstation's Ansible. Use `cd /scratch/ansible && ./ansible-ee ansible -i inventory-rhel7.yml rhel7 -b -m ansible.builtin.ping` for that path (RHEL 7 is absent from the default inventory), and test the actual playbook modules before relying on them. A future RHEL 7 rebuild still needs an appropriate controller/runtime pairing and fresh verification; installing Python 3.6 alone does not make ansible-core 2.21 compatible. `raw` SSH connectivity by itself does not prove module compatibility.

## Assessment status at initial build

The headless installed `stig` profile was selected in Anaconda; a subsequent non-remediating scan recorded 74 failed and 19 error rules (plus 23 not checked). The GUI tailored `stig` post-install remediation recorded 129 fixed, 4 failed, 9 error and 23 not checked rules; the Xorg removal rule is explicitly `notselected`. These nodes have a DISA-profile baseline applied, **not an attestation of full STIG compliance**. Address or adjudicate residual findings before treating them as compliant or connecting to a production patching upstream. GUI results and report are `/root/stig-install-results.xml` and `/root/stig-install-report.html` on node2; the headless verification scan is `/home/labadmin/stig-verification.xml` on node1.
