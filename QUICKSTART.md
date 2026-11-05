# Fresh-clone quickstart: eight-node RHEL STIG lab

This gets a **new x86_64 Linux host** from a Git clone to a bootable, eight-entry Kickstart menu ISO, then to a first disposable VM. It does **not** create VMs while building the ISO. The lab uses RHEL 8.10 (two test nodes), 9.8 (three nodes), and 10.2 (three nodes); each `node2` is the GUI/STIG-GUI role.

> **Disk warning:** Every Kickstart clears `/dev/vda`. Attach only a **new, disposable VirtIO disk** to each VM. The script will not overwrite an existing ISO; a `--check-only` or `--dry-run` is *not* an installation.

## 1. Prepare the host and clone

Have an x86_64 host with KVM, system libvirt (`qemu:///system`), `virsh`, `virt-install` (for the optional Bash bootstrap), and virt-manager (for a manual walkthrough). Install Python **3.10+**, OpenSSL, `xorriso`, Git, Docker Engine with the Compose plugin, and enough free storage for **three full DVDs, an approximately 12 GiB output ISO, two extracted repository trees, and 64 GiB per VM disk**. The repository container uses `python:3.13-alpine`; on an air-gapped host, preload that image into Docker before `compose up`. The bootstrap currently expects `/usr/bin/python3` and `/usr/sbin/virt-install`; check those paths before using it. Use the host's supported package installation method; this project does not install host dependencies.

Clone from your organization's approved repository URL (not provided by this project). **Maintainers:** commit and push this guide, `setup-lab.sh`, `vendor-checksums.sha256`, `build_kickstart_iso.py`, `compose.repo.yaml`, and the tests before asking another admin to clone; uncommitted worktree files are not in the remote repository.

```sh
REPO_URL='REPLACE_WITH_YOUR_APPROVED_GIT_URL'
mkdir -p "$HOME/git"
git clone "$REPO_URL" "$HOME/git/based-and-confused"
cd "$HOME/git/based-and-confused"
command -v python3 openssl xorriso virsh docker
python3 --version
docker compose version
virsh -c qemu:///system list --all
```

Ensure the operator can use system libvirt and Docker according to local policy. For new hosts, create the paths used by the build and lab (do not change ownership of existing shared paths blindly). Libvirt/QEMU must be able to access the ISO and VM disks under your host's DAC/SELinux policy; **do not disable SELinux** to make it work.

```sh
sudo install -d -o "$USER" -g "$(id -gn)" -m 0755 /scratch/iso /scratch/vms
```

## 2. Obtain the exact Red Hat DVDs

Download the **x86_64 DVD ISO**, *not* the smaller Boot ISO, for **RHEL 8.10, 9.8, and 10.2**. Red Hat lists release-specific Boot and DVD downloads on its [RHEL Developer downloads page](https://developers.redhat.com/products/rhel/download), with a separate [RHEL 8 downloads page](https://developers.redhat.com/products/rhel/8); entitled customers can also use the [Customer Portal RHEL downloads page](https://access.redhat.com/downloads/content/rhel). Sign-in and appropriate subscription or Developer access may be required.[1][2][3]

Place the authorized downloads at the paths below (the on-disk filenames are this project's convention, not a claim about Red Hat's downloaded filenames). Compare each DVD's `sha256sum` with the checksum shown on its **Red Hat download page** before renaming or using it. `setup-lab.sh check` repeats this check against the project-maintained `vendor-checksums.sha256`; the builder also checks `.treeinfo` for the expected minor versions and BaseOS/AppStream. Review checksum updates against Red Hat's page, never a third-party mirror.

| Media | Local path for the commands below |
| --- | --- |
| RHEL 8.10 x86_64 DVD | `/scratch/iso/rhel-8.10-x86_64-dvd.iso` |
| RHEL 9.8 x86_64 DVD | `/scratch/iso/rhel-9.8-x86_64-dvd.iso` |
| RHEL 10.2 x86_64 DVD | `/scratch/iso/rhel-10.2-x86_64-dvd.iso` |

The full RHEL 10.2 DVD becomes the base of the menu ISO. RHEL 8/9 installer kernels are embedded, **but their packages and installer stage2 are not**; those need the HTTP trees. A Boot ISO cannot replace any of these three full DVDs.

## Automated setup (recommended)

After the clone, dependencies, and authorized DVDs are in place, run these phases **in order** from the clone. Set `PUBKEY` to an ECDSA/RSA public key available to the admins who will use the guests; the placeholder is not a real key.

```sh
cd "$HOME/git/based-and-confused"
PUBKEY='/path/to/authorized-team-key.pub'  # replace this path
./setup-lab.sh check
./setup-lab.sh prepare
./setup-lab.sh --ssh-key "$PUBKEY" --generate-credentials build-iso
./setup-lab.sh verify
```

`check` is read-only: it validates dependencies, vendor SHA-256 values, release metadata, free space, and the lab network. `prepare` defines/starts the network only if needed, extracts the RHEL 8/9 DVDs through non-overwriting staging directories, and serves them via Docker. `build-iso` calls the existing Python builder; the default output is `/scratch/iso/rhel-stig-lab-menu-team.iso`, and GUI credentials go into an owner-only file at `/scratch/vms/menu-gui-credentials-team.json`. `verify` checks both HTTP repositories, output SHA-256, eight Kickstarts, three kernel pairs, and both boot menus. The script has `--media-dir`, `--repo-dir`, `--checksums`, `--out`, and `--credentials` overrides; use `./setup-lab.sh --help` for the interface. An existing output ISO or stale `.partial` tree is **never replaced**.

The script does **not** download DVDs, configure the host firewall, create VMs, or upload artifacts. Review `setup-firewall.sh` and apply its bridge-scoped UFW rules if UFW is active; use equivalent rules for another firewall. The next three sections are the **manual alternative and recovery reference**, not additional steps after a successful automated run. Continue at [Install one VM](#6-install-one-vm-then-expand).

## 3. Manual alternative: create and start the private lab network

The project fixes the NAT subnet at `192.168.150.0/24` (bridge `virbr-stig`, gateway `192.168.150.1`); make sure it does not conflict with existing host or VPN routes. DHCP uses `.10`–`.99`, leaving `.100`–`.199` for lab nodes. Define it **once** on a fresh machine:

```sh
sudo virsh -c qemu:///system net-define stig-lab-network.xml
sudo virsh -c qemu:///system net-start stig-lab
sudo virsh -c qemu:///system net-autostart stig-lab
virsh -c qemu:///system net-info stig-lab
```

If `stig-lab` already exists, inspect it with `net-info` rather than redefining it. If host UFW is active, review `setup-firewall.sh` and run `sudo bash setup-firewall.sh`; it allows DHCP/DNS and TCP 8089 **on `virbr-stig` only**. If your host uses a different firewall, configure equivalent bridge-scoped access. A successful host-side `curl` does not prove a guest can get DHCP or reach the repo.

## 4. Manual alternative: extract and serve the RHEL 8/9 installation trees

These are full local DVD trees, not a PXE service. Extraction needs substantial disk space and time. On a fresh machine, create **empty** destinations, then extract; do not merge over an existing repository tree.

```sh
cd "$HOME/git/based-and-confused"
set -e  # stop rather than serve an incomplete installation tree
for major in 8 9; do
  case "$major" in 8) minor=8.10 ;; 9) minor=9.8 ;; esac
  target="/scratch/iso/repo/rhel$major"
  if test -e "$target"; then printf 'Refusing existing tree: %s\n' "$target" >&2; exit 1; fi
  mkdir -p "$target"
  xorriso -osirrox on -indev "/scratch/iso/rhel-$minor-x86_64-dvd.iso" -extract / "$target"
  test -s "$target/.treeinfo"
  test -s "$target/BaseOS/repodata/repomd.xml"
  test -s "$target/AppStream/repodata/repomd.xml"
done
mkdir -p data/menu-ks  # empty on a fresh clone; the ISO embeds its own Kickstarts
docker compose -p rhel-stig-lab-repo -f compose.repo.yaml up -d --build
docker compose -p rhel-stig-lab-repo -f compose.repo.yaml ps
# Retry after startup if the service is still marked "starting".
for major in 8 9; do
  for path in .treeinfo BaseOS/repodata/repomd.xml AppStream/repodata/repomd.xml; do
    curl -fsSI "http://192.168.150.1:8089/repo/rhel$major/$path"
  done
done
```

The container binds `/scratch/iso/repo` read-only and listens on the lab gateway's TCP port 8089. If extraction is interrupted, inspect and remove or rename the *partial new tree* yourself before retrying; the loop deliberately refuses to overwrite a path.

## 5. Manual alternative: build and verify the Kickstart menu ISO

Choose an **ECDSA or RSA public SSH key that the intended admins can use**. A personal key would leave a different admin unable to SSH to headless guests. Set `PUBKEY` to the authorized public-key path before running this example; never place a private key in the ISO or Git.

```sh
cd "$HOME/git/based-and-confused"
PUBKEY='/path/to/authorized-team-key.pub'  # replace with a real ECDSA/RSA .pub path
test -s "$PUBKEY"
python3 build_kickstart_iso.py \
  --rhel8 /scratch/iso/rhel-8.10-x86_64-dvd.iso \
  --rhel9 /scratch/iso/rhel-9.8-x86_64-dvd.iso \
  --rhel10 /scratch/iso/rhel-10.2-x86_64-dvd.iso \
  --ssh-key "$PUBKEY" \
  --gui-credentials /scratch/vms/menu-gui-credentials-team.json \
  --generate-credentials \
  --out /scratch/iso/rhel-stig-lab-menu-team.iso
cd /scratch/iso
sha256sum -c rhel-stig-lab-menu-team.iso.sha256
```

The builder refuses an existing output or credential file. It writes one unique GUI password per `node2` in `/scratch/vms/menu-gui-credentials-team.json` (owner-only `0600`) and embeds **only password hashes** in the ISO. Store the JSON securely and share it separately via your approved secret manager. For a repeat build reusing that file, omit `--generate-credentials` and choose a **new** output filename. `--check-only` checks paths, key type, private credential file, and overwrite safety without creating or validating an ISO; omit `--generate-credentials` when using it.

Run the project tests on the clone:

```sh
cd "$HOME/git/based-and-confused"
PYTHONPATH=. python3 -m unittest discover -s tests -q
```

**Do not commit the DVDs, derived ISO, or credential JSON to Git.** The ISO contains RHEL 10.2 DVD content, so confirm your organization's redistribution rights before publishing it with its checksum to an approved *internal artifact store*. The ISO and checksum go together; the GUI credential JSON does not. This project has no upload command.

## 6. Install one VM, then expand

**Manual walkthrough (virt-manager):** connect to `qemu:///system`, use `/scratch/iso/rhel-stig-lab-menu-team.iso` as the **only CD-ROM**, select network `stig-lab`, and attach a **fresh** 64 GiB or larger VirtIO disk under `/scratch/vms` as `/dev/vda`. Select `rhel10-node1` first if you want to test the embedded DVD without depending on the RHEL 8/9 HTTP trees. For GUI testing choose a `node2` entry and use its separately stored console password. An additional VM needs **both** `lab.ip=` and `lab.hostname=` changed to unused values at the boot menu (`e`, then Ctrl+X). See [README](README.md#install-in-virt-manager) for resources and the full node/IP map.

**Optional Bash bootstrap (different credentials):** the env file is for the bootstrap only—it does **not** alter the menu ISO. This script defaults to the old local ISO filename, so set `LAB_ISO` to your freshly built ISO. It currently uses `/usr/bin/python3` and `/usr/sbin/virt-install`, and creates missing domains/disks only when you run `up` without `--dry-run`.

```sh
cd "$HOME/git/based-and-confused"
if test ! -e lab-vms.env; then install -m 600 lab-vms.env.example lab-vms.env; fi
${EDITOR:-vi} lab-vms.env  # fill LAB_USERNAME, LAB_PASSWORD, LAB_ROOT_PASSWORD
export LAB_ISO=/scratch/iso/rhel-stig-lab-menu-team.iso
./lab-vms.sh --dry-run up rhel10-node1
# When ready to create its VM and disposable disk:
./lab-vms.sh up rhel10-node1
./lab-vms.sh status all
```

The bootstrap's env parser expects raw `KEY=value` lines (no `export`, shell quotes, or interpolation), three distinct values as described in [README](README.md#install-with-the-bash-bootstrap), and mode `0600`. The initial dry run neither installs nor starts a VM. Do not start `all` until the host has enough memory for eight guests. Never attach a disk with data you wish to keep: Kickstart erases `/dev/vda`.

## Troubleshooting boundaries

- If a RHEL 8/9 installer cannot fetch packages, confirm `stig-lab` is active, the container is healthy, and `.treeinfo`/BaseOS/AppStream are reachable at `http://192.168.150.1:8089/repo/rhel8` and `/rhel9`. Then check **guest** DHCP/firewall/bridge traffic. ISO creation alone does not populate those HTTP trees.
- If `virt-install` or QEMU cannot access `/scratch` files, check system libvirt permissions and SELinux labeling on that host; do not disable its security controls.
- The build checks media versions, embedded Kickstarts, menus, and boot metadata; it does **not** certify a completed installation, FIPS state, SSH access, or DISA compliance. Validate those in a disposable guest. RHEL 10.2's bundled `stig_gui` is labeled a Red Hat STIG profile, not verified against a later official DISA revision.

## Sources

[1] https://developers.redhat.com/products/rhel/download — Red Hat Developer RHEL downloads
[2] https://developers.redhat.com/products/rhel/8 — Red Hat Developer RHEL 8 downloads
[3] https://access.redhat.com/downloads/content/rhel — Red Hat Customer Portal RHEL downloads
