# Offline quarterly clone personalization

`scripts/quarterly-personalize.sh` prepares a **separate** per-node qcow2 from the exact unbooted clone produced by `quarterly.cli clone`. It injects the manifest FQDN and static NetworkManager connection **offline before first boot**, clears inherited network and host identity in the private copy, verifies its contents, and atomically publishes a new image. The original CLI clone is never replaced. Read the [quarterly operator workflow](QUARTERLY-WORKFLOW.md) and [candidate sysprep guide](QUARTERLY-SYSPREP.md) first.

## Preconditions

- A clean base must already have been independently approved, canary-booted, sealed and SHA-256 pinned. This script **cannot** prepare the two pre-promotion canaries from a sysprep candidate and does not promote any base. If the clean-base gate is unmet, stop.
- `quarterly/lab.json` must name the exact asset, its static IPv4/FQDN, and `/scratch/vms/quarterly/clones/NAME.qcow2`. `quarterly.cli clone` creates that source disk; it does not boot or define a domain. Both it and the future `NAME.personalized.qcow2` must be detached from all libvirt domains, and the output must not already exist. The owned clones directory must be `0700`; source must be a nonsymlinked `0600` regular, standalone, snapshot-free qcow2.
- The operator needs `virsh` on `qemu:///system`, `qemu-img`, `virt-customize`, `virt-cat`, `guestfish` and sufficient space for a full private copy. Offline libguestfs support for **each** RHEL release and SELinux labeling must be proven on the execution host. No guest or host network access is requested by `virt-customize` (`--no-network`). The current host lacks `virt-customize`; no live clone has been modified.
- Obtain approval for offline cleanup, especially deleting inherited private SSH user keys. The script does **not** set or rotate user/root passwords, create users, write a private SSH key, register RHSM, or establish that a previous image contains no other application secrets. The intended `labadmin` public authorized key is preserved but must be verified by real guest login. Avoid baking any private keys/passwords into images or evidence.

## Commands

Run from the repository root after approved base promotion and clone publication:

```bash
cd /home/zrc/git/based-and-confused
/usr/bin/python3 -m quarterly.cli clone-plan --node qa-rhel9-node1 \
  --base-sha256 '<recorded-64-hex-base-sha256>' --confirm qa-rhel9-node1
/usr/bin/python3 -m quarterly.cli clone --node qa-rhel9-node1 \
  --base-sha256 '<recorded-64-hex-base-sha256>' --confirm qa-rhel9-node1
scripts/quarterly-personalize.sh --check --node qa-rhel9-node1
# After reviewing source, target, hostname and address against quarterly/lab.json:
scripts/quarterly-personalize.sh --execute --node qa-rhel9-node1 \
  --confirm qa-rhel9-node1
```

The first two CLI commands create **only** an unpersonalized disk. `--check` validates paths, ownership, detachment, format and tool availability without writing an image. `--execute` creates a private copy under the clones directory; removes old NetworkManager/legacy `ifcfg-*` profiles, inherited SSH host/private user keys, machine ID and sibling `/etc/hosts` entries; writes a root-owned mode-`0600` static NM profile using manifest IP/prefix/gateway/DNS and hostname; invokes SELinux relabel; reads back the hostname, NM file, guest permissions and removed host keys; then publishes `/scratch/vms/quarterly/clones/qa-rhel9-node1.personalized.qcow2` without overwrite. Failed validation leaves no final output. It does not update the manifest's `disk` field or define a VM.

**Boot only the `.personalized.qcow2` path.** Define the disposable VM manually on `stigqa-lab` with a distinct MAC; inspect its XML to prove it references that path, not the unpersonalized clone or sealed base. Do not boot the original clone. Check actual static address/FQDN, a new machine ID and unique host-key fingerprints against another clone, expected SSH public-key access with host-key verification enabled, FIPS/SELinux enforcement, absence of inherited registration, GUI login where applicable, and guest assessment. A successful offline readback is not a boot or STIG validation. Retain the source clone safely until accepted; do not overwrite or delete it on a retry. If the output exists, choose a new approved clone/run rather than bypassing refusal.

## Limits

The network file targets a single NetworkManager Ethernet connection with no fixed MAC and disables IPv6 in the guest; review that profile against each release's installer state and security requirements. The script does not check unique fingerprints or access over the actual lab network. Synthetic tests use tiny qcow2 images plus command stubs; they do not prove libguestfs compatibility, SELinux contexts on real RHEL, first-boot key regeneration, or successful GUI/FIPS operation. No automatic guest teardown, base sealing, policy approval or CKLB release is included.
