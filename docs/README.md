# RHEL STIG lab

## Repository boundaries

This repository contains the existing menu-ISO/Bash installer lab and the separate quarterly clean-baseline lab. For the existing installer lab, use this guide and [QUICKSTART.md](QUICKSTART.md). For the quarterly node-specific clean image, safe plans, clone and hash-only asset-record handoff, use [QUARTERLY-LAB.md](QUARTERLY-LAB.md) and [QUARTERLY-WORKFLOW.md](QUARTERLY-WORKFLOW.md). Seal and assessment evidence must be verified separately. Optional offline identity-generalization scripts are described in [QUARTERLY-SYSPREP.md](QUARTERLY-SYSPREP.md) and [QUARTERLY-PERSONALIZE.md](QUARTERLY-PERSONALIZE.md).

Policy, tailoring, remediation/assessment coordination, approvals, CKLB conversion and release belong to the separate [private burden-of-proof repository](https://github.com/zcook44/burden-of-proof), not this lab. Do not treat either lab's synthetic tests or asset handoff as compliance acceptance.

Eight disposable VMs on one libvirt network, built from the RHEL 8.10, 9.8, and 10.2 DVDs. Use them to develop Ansible automation against a STIG-oriented starting point. Nothing in this repo creates a VM until you run `./lab-vms.sh up ...` or create one in virt-manager.

**New machine?** Start with the [fresh-clone quickstart](QUICKSTART.md). Its `setup-lab.sh` runs separate `check`, `prepare`, `build-iso`, and `verify` phases; the guide has official DVD links and the first-VM walkthrough.

## Pick an install path

- **Want to watch the installer in virt-manager?** Boot the [menu ISO](#install-in-virt-manager). Pick the OS/role and optionally change its IP and hostname at the boot menu. The ISO has a fixed `labadmin` account; the Bash env file does not change it.
- **Want repeatable VMs with your own username and passwords?** Use the [Bash bootstrap](#install-with-the-bash-bootstrap). It selects the right Kickstart for each VM, creates its disk, and starts the install. It also starts and stops existing VMs.

The current ISO is:

```text
/scratch/iso/rhel-stig-lab-menu-8.10-9.8-10.2-labadmin-sudo-final.iso
```

This ISO embeds a final `%post` that repairs SSH key ownership/permissions and persistent SELinux file labels after STIG/site changes, checks FIPS boot arguments, and gives **only `labadmin`** passwordless sudo via a validated `/etc/sudoers.d/90-labadmin` rule. It explicitly installs `sudo` and `shadow-utils` on all eight roles. The previous `ssh-post-v2` ISO lacks that sudo rule. Do not use the older 9.7/10.1 menu ISO for new installs: its RHEL 9 entry points at the now-replaced 9.7 package repository.

## Nodes and addresses

| Menu entry / VM name | Installed IP | Purpose |
| --- | --- | --- |
| `rhel8-node1` | `192.168.150.121` | Headless STIG test node |
| `rhel8-node2` | `192.168.150.122` | GUI and STIG-with-GUI test node |
| `rhel9-rheladm` | `192.168.150.101` | Ansible control server (`ansible-core`) |
| `rhel9-node1` | `192.168.150.102` | Headless STIG test node |
| `rhel9-node2` | `192.168.150.103` | GUI and STIG-with-GUI test node |
| `rhel10-rheladm` | `192.168.150.111` | Ansible control server (`ansible-core`) |
| `rhel10-node1` | `192.168.150.112` | Headless STIG test node |
| `rhel10-node2` | `192.168.150.113` | GUI and STIG-with-GUI test node |

All names end in `.lab.test`. There is deliberately **no RHEL 8 Ansible server**. Every VM uses `stig-lab` (`192.168.150.0/24`, gateway and DNS `192.168.150.1`). The installer initially uses DHCP; Kickstart then configures the listed static address. The Kickstarts add the eight default names to `/etc/hosts`.

RHEL 8/9 installs fetch packages from the local read-only repository at `http://192.168.150.1:8089/repo/rhel8` or `/repo/rhel9`. RHEL 10 packages are on the menu ISO. Keep the repository service running while installing RHEL 8 or 9. This setup does not provide PXE.

## Install in virt-manager

1. Connect to **`qemu:///system`**. Create a new VM using the menu ISO above as its **only** optical drive. Select the **`stig-lab`** network, not `default`.
2. Give it a **new disposable VirtIO disk** under `/scratch/vms`, at least **64 GiB**, presented to the guest as `/dev/vda`. The Kickstart runs `clearpart --all --initlabel --drives=vda`: **it erases `/dev/vda` without asking**. Never attach an existing disk you care about.
3. Allocate at least 6 GiB RAM / 2 vCPUs for a headless node; 8 GiB / 4 vCPUs is preferable for a GUI node. BIOS and UEFI menus both work. Choose the entry matching your OS and role.
4. For another instance of a template, highlight its entry and press `e`. Change **both** `lab.ip=` and `lab.hostname=` on the kernel line, then press Ctrl+X. Use an unused IP in `192.168.150.100`–`.199`. There is no separate GUI form for these fields. Update the other guests' `/etc/hosts` entries if you change a default peer's identity.

Manual ISO installs use `labadmin` with the workstation's ECDSA SSH public key. For example, after a headless VM finishes installing, try `ssh -i ~/.ssh/id_ecdsa labadmin@192.168.150.121`, then `sudo -n id` on the guest. Headless accounts have **no console password** in the menu ISO; `labadmin` uses passwordless sudo on all eight roles. Each GUI `node2` has a console password in the owner-only file `/scratch/vms/stig-lab-gui-credentials-8.10-9.8-10.2.json`. Read it locally; do not copy it into chat, tickets, or the repo. The ISO contains hashes, not plaintext passwords.

## Install with the Bash bootstrap

From the project directory, create a private env file and edit the three values yourself:

```sh
cd /home/zrc/git/based-and-confused
install -m 600 lab-vms.env.example lab-vms.env
${EDITOR:-vi} lab-vms.env
./lab-vms.sh --dry-run up rhel8-node1 rhel8-node2
./lab-vms.sh up rhel8-node1 rhel8-node2
```

The file contains raw `LAB_USERNAME=`, `LAB_PASSWORD=`, and `LAB_ROOT_PASSWORD=` lines. Do **not** use `export`, shell quotes, or interpolation. Passwords must differ and each have at least 15 characters. The script rejects a symlink, an unowned file, or permissions other than `0600`; `lab-vms.env` is gitignored. To use different passwords per VM, set `LAB_ENV_FILE=/path/to/private-node.env` on a single-node `up` command.

`up` creates missing domains and disks, injects a private per-node Kickstart with password **hashes** into the installer, and starts the installation. It will not overwrite an orphan disk or reinstall an existing VM. The selected user gets the ECDSA SSH key and a password suitable for managed `sudo`/Ansible `become`; root gets a password, though the STIG profile may still restrict root SSH. If your selected username differs from `labadmin`, the final postinstall also creates a **separate, key-only, password-locked `labadmin`** with the same authorized key and passwordless sudo; your selected user retains password-required sudo. If you select `labadmin`, it is the one account and receives the configured password as well as passwordless sudo. Changing the env file later does not change an installed VM. No plaintext password goes into the ISO, Kickstart, or `virt-install` command line.

| Command | What it does |
| --- | --- |
| `./lab-vms.sh --dry-run up all` | Show planned installs; no disks or VMs created. Requires a valid env file. |
| `./lab-vms.sh up rhel9-rheladm rhel10-node2` | Install missing selected VMs; start selected VMs that are shut off. |
| `./lab-vms.sh up all` | Same for all eight. Consider host RAM before starting eight installers together. |
| `./lab-vms.sh prepare all` | Write private Kickstarts under `/scratch/vms` without creating VMs. |
| `./lab-vms.sh status all` | Show present or absent domains; no env file needed. |
| `./lab-vms.sh start rhel8-node1` | Start an existing VM only; never creates or reinstalls one. |
| `./lab-vms.sh stop all` | Request graceful shutdown; does not delete domains or disks. |

## If an install cannot reach its packages

Check the lab network and the two HTTP installation trees from the host:

```sh
virsh -c qemu:///system net-info stig-lab
ENGINE=docker  # set to podman if that was your selected engine
"$ENGINE" compose -p rhel-stig-lab-repo -f compose.repo.yaml ps
curl -f http://192.168.150.1:8089/repo/rhel8/.treeinfo
curl -f http://192.168.150.1:8089/repo/rhel9/.treeinfo
```

If the network is inactive, run `virsh -c qemu:///system net-start stig-lab`. If the container stopped, rerun `./setup-lab.sh prepare` (auto-selects a usable engine) or use your chosen engine's `compose -p rhel-stig-lab-repo -f compose.repo.yaml up -d --build` command (`docker` or `podman`). Do not start a copy under the other engine while port 8089 is occupied. If the host firewall changed, review `setup-firewall.sh` and reapply its bridge-scoped UFW rules with `sudo bash setup-firewall.sh`. Host-side HTTP success alone does **not** prove guest connectivity; check DHCP, the `virbr-stig` bridge, and repository access from the installer when debugging a guest.

The two extracted installation trees live at `/scratch/iso/repo/rhel8` and `/scratch/iso/repo/rhel9`. They came from the full DVDs, including BaseOS and AppStream. Keep the version-matched trees and the container available until installation completes.

## STIG baseline and verification limits

- RHEL 8.10 and 9.8 use the packaged `stig` profile for headless nodes or `stig_gui` for GUI nodes through the Anaconda OpenSCAP addon. Their bundled GUI profile titles are "DISA STIG with GUI."
- RHEL 10.2 has no Anaconda OpenSCAP addon. Its Kickstart attempts remediation after package installation with the DVD-matched `stig` or `stig_gui` profile and retains its report/results under `/root`. The bundled profile is titled "Red Hat STIG with GUI," **not proof** of alignment with an official DISA RHEL 10 revision.
- FIPS is requested at installer boot and in the installed bootloader. Check the installed guest's FIPS state, SSH access, disk layout, and any remaining manual or failed findings before calling it compliant. A password set for root does not imply root SSH login is permitted.
- **Deliberate security exception:** `labadmin ALL=(ALL) NOPASSWD: ALL` is installed *after* the profile remediation on every node. It violates the STIG password-required privilege escalation control. These guests are disposable automation labs, **not STIG-compliant systems**; do not copy this sudo rule into a production baseline. Verify the exception after installation with `sudo -n id` as `labadmin` and record it as a finding.

The new ISO's SHA-256, eight embedded Kickstarts (including the final SSH/FIPS/SELinux repair, explicit sudo packages, and labadmin sudo rule), kernel/initrd pairs, BIOS/UEFI menus and boot metadata were checked; the RHEL 8/9 repository trees answered HTTP requests; and all 59 unit tests passed. Earlier menu versions had BIOS/UEFI boot and `virt-install` dry-run checks, but **no guest has been installed from this rebuilt ISO**. Your installation walkthrough must verify SSH key login, `sudo -n id`, FIPS state, SELinux labels, and remaining SCAP findings; passwordless sudo remains an explicit noncompliant exception.

## Build the Kickstart menu ISO on another admin workstation

The **build script is in this repo; the licensed vendor DVDs and private credentials are not**. Obtain the official full RHEL 8.10, 9.8, and 10.2 x86_64 DVDs through your organization's Red Hat entitlement. Install Python 3, OpenSSL, and `xorriso` locally. Leave enough free space for the source media, another ~12 GiB output ISO, and temporary files. The script works offline and creates no VMs. It requires a new output filename and never modifies a vendor DVD.

Use a FIPS-compatible **ECDSA or RSA public key that the intended admins can actually use**. If you embed only your personal public key, a different admin cannot SSH into headless guests. GUI passwords are generated separately, one per `node2`, into an owner-only file; share them through your approved secret manager, **never** alongside the ISO or in Git.

```sh
cd /home/zrc/git/based-and-confused  # or your own clone
python build_kickstart_iso.py \
  --rhel8 /scratch/iso/rhel-8.10-x86_64-dvd.iso \
  --rhel9 /scratch/iso/rhel-9.8-x86_64-dvd.iso \
  --rhel10 /scratch/iso/rhel-10.2-x86_64-dvd.iso \
  --ssh-key /path/to/authorized-team-key.pub \
  --gui-credentials /scratch/vms/menu-gui-credentials.json \
  --generate-credentials \
  --out /scratch/iso/rhel-stig-lab-menu-team.iso
cd /scratch/iso && sha256sum -c rhel-stig-lab-menu-team.iso.sha256
```

`--generate-credentials` refuses an existing file; for a repeat build with the **same** credentials, omit that flag and provide the existing mode-`0600` file. `--check-only` (also omit `--generate-credentials`) checks file paths, ownership, password hashes, key type, and output safety without writing or inspecting the DVD contents. For a full build, the wrapper runs `menu_lab.py`, which checks the base DVD and BIOS/UEFI boot metadata, embeds all eight Kickstarts and the three installer kernel/initrd pairs, and reads back the boot menus and Kickstarts. A `.sha256` sidecar is generated after the ISO completes. Run `PYTHONPATH=. python -m unittest discover -s tests -q` to check the source; booting/installing a disposable guest remains the end-to-end verification.

**Distribution:** This ISO contains the RHEL 10.2 DVD's packages, password *hashes*, the selected public key, and fixed lab network settings. The RHEL 8/9 DVDs are **not** embedded; the target environment must serve matching full installation trees at `192.168.150.1:8089` on `stig-lab` (see above). Do not commit the ISO or credential JSON to Git; `.gitignore` excludes `*.iso`. Even Git LFS is not a substitute for confirming your organization's Red Hat redistribution rights. Publish the ISO and checksum only to an approved internal artifact repository, after checking access controls and license terms. Distribute GUI credentials separately. This script does not upload anything.

The older single-version `lab.py`, `compose.yaml`, and `data/ks/` examples remain for reference; they are not the current eight-node installation path.
