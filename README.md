# RHEL STIG pipeline lab

Eight disposable VMs on one libvirt network, built from the RHEL 8.10, 9.8, and 10.2 DVDs. Use them to develop Ansible automation against a STIG-oriented starting point. Nothing in this repo creates a VM until you run `./lab-vms.sh up ...` or create one in virt-manager.

## Pick an install path

- **Want to watch the installer in virt-manager?** Boot the [menu ISO](#install-in-virt-manager). Pick the OS/role and optionally change its IP and hostname at the boot menu. The ISO has a fixed `labadmin` account; the Bash env file does not change it.
- **Want repeatable VMs with your own username and passwords?** Use the [Bash bootstrap](#install-with-the-bash-bootstrap). It selects the right Kickstart for each VM, creates its disk, and starts the install. It also starts and stops existing VMs.

The current ISO is:

```text
/scratch/iso/rhel-stig-lab-menu-8.10-9.8-10.2-gui-login.iso
```

Do not use the older 9.7/10.1 menu ISO for new installs. Its RHEL 9 entry points at the now-replaced 9.7 package repository.

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

Manual ISO installs use `labadmin` with the workstation's ECDSA SSH public key. For example, after a headless VM finishes installing, try `ssh -i ~/.ssh/id_ecdsa labadmin@192.168.150.121`. Headless accounts have **no console password** in the menu ISO; if your Ansible work needs password-based `become`, prefer the Bash bootstrap below. Each GUI `node2` has a console password in the owner-only file `/scratch/vms/stig-lab-gui-credentials-8.10-9.8-10.2.json`. Read it locally; do not copy it into chat, tickets, or the repo. The ISO contains hashes, not plaintext passwords.

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

`up` creates missing domains and disks, injects a private per-node Kickstart with password **hashes** into the installer, and starts the installation. It will not overwrite an orphan disk or reinstall an existing VM. The selected user gets the ECDSA SSH key and a password suitable for managed `sudo`/Ansible `become`; root gets a password, though the STIG profile may still restrict root SSH. Changing the env file later does not change an installed VM. No plaintext password goes into the ISO, Kickstart, or `virt-install` command line.

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
docker compose -p rhel-stig-lab-repo -f compose.repo.yaml ps
curl -f http://192.168.150.1:8089/repo/rhel8/.treeinfo
curl -f http://192.168.150.1:8089/repo/rhel9/.treeinfo
```

If the network is inactive, run `virsh -c qemu:///system net-start stig-lab`. If the container stopped, run `docker compose -p rhel-stig-lab-repo -f compose.repo.yaml up -d --build`. If the host firewall changed, review `setup-firewall.sh` and reapply its bridge-scoped UFW rules with `sudo bash setup-firewall.sh`. Host-side HTTP success alone does **not** prove guest connectivity; check DHCP, the `virbr-stig` bridge, and repository access from the installer when debugging a guest.

The two extracted installation trees live at `/scratch/iso/repo/rhel8` and `/scratch/iso/repo/rhel9`. They came from the full DVDs, including BaseOS and AppStream. Keep the version-matched trees and the container available until installation completes.

## STIG baseline and verification limits

- RHEL 8.10 and 9.8 use the packaged `stig` profile for headless nodes or `stig_gui` for GUI nodes through the Anaconda OpenSCAP addon. Their bundled GUI profile titles are "DISA STIG with GUI."
- RHEL 10.2 has no Anaconda OpenSCAP addon. Its Kickstart attempts remediation after package installation with the DVD-matched `stig` or `stig_gui` profile and retains its report/results under `/root`. The bundled profile is titled "Red Hat STIG with GUI," **not proof** of alignment with an official DISA RHEL 10 revision.
- FIPS is requested at installer boot and in the installed bootloader. Check the installed guest's FIPS state, SSH access, disk layout, and any remaining manual or failed findings before calling it compliant. A password set for root does not imply root SSH login is permitted.

The eight Kickstarts passed `ksvalidator`; the repository trees answered HTTP requests; all 30 unit tests passed; the menu booted under BIOS and UEFI; and the eight `virt-install` configurations passed dry runs. **No guest was installed from this ISO in these checks.** Your installation walkthrough is the end-to-end test. No persistent VMs were created for validation.

## Rebuild the menu ISO

Only rebuild when you change the media, SSH key, or templates. Choose a **new output filename**; the builder refuses to overwrite an existing ISO. The original Red Hat DVDs remain untouched.

```sh
cd /home/zrc/git/based-and-confused
python menu_lab.py \
  --source /scratch/iso/rhel-10.2-x86_64-dvd.iso \
  --rhel8 /scratch/iso/rhel-8.10-x86_64-dvd.iso \
  --rhel9 /scratch/iso/rhel-9.8-x86_64-dvd.iso \
  --ssh-key ~/.ssh/id_ecdsa.pub \
  --gui-credentials /scratch/vms/stig-lab-gui-credentials-8.10-9.8-10.2.json \
  --out /scratch/iso/rhel-stig-lab-menu-next.iso
PYTHONPATH=. python -m unittest discover -s tests -q
# Example syntax check; use RHEL9/RHEL10 for the other generated files:
uvx --from pykickstart ksvalidator -v RHEL8 data/menu-ks/rhel8-node1.ks
```

The older single-version `lab.py`, `compose.yaml`, and `data/ks/` examples remain in the project for reference; they are not the current eight-node installation path.
