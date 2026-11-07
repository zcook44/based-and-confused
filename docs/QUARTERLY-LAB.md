# Quarterly STIG evidence lab (feature branch)

For the operator's ordered procedure—from network startup through hash-only asset handoff—use [QUARTERLY-WORKFLOW.md](QUARTERLY-WORKFLOW.md). This document records the implementation boundaries and validation evidence.

**Status:** isolated lab configuration, private Kickstart rendering, direct-DVD install plans, full-copy clone planning/execution, and hash-only asset-record export are implemented. Offline sysprep/personalization scripts are optional experimental identity-generalization tools, not the primary path. The CLI checks owner-private files, the baseline's qcow2 format/chain, digests and a clean Git checkout; it does not prove a manual seal or real assessment, create or boot a domain, scan, remediate, or issue CKLB evidence. No real RHEL installation or assessment has been demonstrated. Policy and release operations are in [burden-of-proof](https://github.com/zcook44/burden-of-proof).

## Isolation and safety

The new lab is `stigqa-lab`, bridge `virbr-stigqa`, `192.168.164.0/24`, gateway `.1`, domain `qa.stig.test`. The eight nodes are `qa-rhel8-node1` `.121`, `qa-rhel8-node2` `.122`, `qa-rhel9-rheladm` `.101`, `qa-rhel9-node1` `.102`, `qa-rhel9-node2` `.103`, `qa-rhel10-rheladm` `.111`, `qa-rhel10-node1` `.112`, and `qa-rhel10-node2` `.113`. `node2` for each release and both controllers have GUI roles. Each node, including nodes with the same release/role, gets its **own fresh Kickstart installation** each quarter, `/scratch/vms/quarterly/build/{node-name}.qcow2`, `/scratch/vms/quarterly/bases/{node-name}-baseline.qcow2`, and one full disposable `/scratch/vms/quarterly/clones/{node-name}.qcow2` copy. All new disks and installer caches live under `/scratch/vms/quarterly`; authorized vendor DVDs remain under `/scratch/iso`. The existing `stig-lab` and its `192.168.150.0/24` guests, rules, disks, and ISO are not modified by these commands.

Run from the repository with **system Python** (`/usr/bin/python3`); the terminal's default virtualenv cannot import `gi` for `virt-install`:

```bash
mkdir -p -m 0755 /scratch/vms/quarterly
/usr/bin/python3 -m quarterly.cli inventory
/usr/bin/python3 -m quarterly.cli preflight
/usr/bin/python3 -m quarterly.cli network-xml > /scratch/vms/quarterly/stigqa-network.xml
virt-xml-validate /scratch/vms/quarterly/stigqa-network.xml network
```

Preflight is a *before-creation* collision check. It deliberately reports the new network as a collision after the network is defined; that is not a request to delete it. Review the XML and existing routes, then explicitly define/start **only** the new `stigqa-lab` network if proceeding with a real installation. Network creation and any UFW forwarding or SSH-host trust changes are not automatic. The code never deletes an existing domain/disk; a Kickstart still erases `/dev/vda` in its **new disposable build guest**, so verify the install plan's exact disk before execution.

## Clean image preparation

The manifest is `quarterly/lab.json`. Its three release-matched full DVDs have verified volume IDs; their kernels/initrds are expected at `/scratch/vms/quarterly/installers/<release>/`. On this workstation those six files were extracted from the corresponding DVDs without overwriting existing files. On another host, extract `/images/pxeboot/vmlinuz` and `/images/pxeboot/initrd.img` with `xorriso -osirrox on -indev <matching-vendor-DVD> -extract <ISO-path> <new-cache-path>`, verify the exact DVD volume ID, and do not reuse an older minor release's cache.

Create an owner-only literal `KEY=value` file **outside Git**, for example `/scratch/vms/quarterly/private/quarterly.env` (parent directory mode `0700`, file `0600`, not a symlink):

```text
QUARTERLY_USERNAME=<your-operator-username>
QUARTERLY_PASSWORD=<your-unique-operator-password>
QUARTERLY_ROOT_PASSWORD=<your-unique-root-password>
QUARTERLY_LABADMIN_PUBLIC_KEY=<your-ECDSA-or-RSA-public-key-line>
```

Replace placeholders locally. **Do not commit, paste, shell-source, or publish this file.** The CLI parses values literally, hashes passwords through `openssl passwd -6 -stdin`, and writes only hashes into a private, no-overwrite Kickstart. A separate key-only `labadmin` receives passwordless sudo on all new guests. That is an **explicit scored STIG exception**, not compliance. FIPS is requested at installer boot and installed bootloader, SELinux enforcing is set, and no STIG remediation is baked into the clean baseline. FIPS operational state and SSH key/sudo/GUI login still need guest verification.

```bash
/usr/bin/python3 -m quarterly.cli kickstart --node qa-rhel9-node2 \
  --env /scratch/vms/quarterly/private/quarterly.env \
  --out /scratch/vms/quarterly/private/qa-rhel9-node2.ks
/usr/bin/python3 -m quarterly.cli install-plan --node qa-rhel9-node2 \
  --kickstart /scratch/vms/quarterly/private/qa-rhel9-node2.ks \
  --confirm build-qa-rhel9-node2
```

`install-plan` **prints** an inert `virt-install` command. It does not run it. Do not feed that output directly to a shell without review. The example targets a separate, new 40-GB `/scratch/vms/quarterly/build/qa-rhel9-node2.qcow2` and the matching DVD; an earlier `virt-install --dry-run --print-xml` produced valid install- and final-boot libvirt domain XML for the RHEL 9 GUI role. It did not boot a guest. `ksvalidator` is not installed here; syntax and Anaconda execution remain unproven.

For **each node**, boot the fresh build guest, verify package source/install completion, FIPS (`fips-mode-setup --check` and boot arguments), SELinux labels, manifest hostname/IP, GUI login where applicable, SSH-key login and `sudo -n`. Record the host-key fingerprint and capture/export a complete pre-remediation scan and installer proof outside the guest. Keep the clean build/base unregistered and free of entitlement or other service credentials when possible; if build-time registration is necessary, require an explicit subscription/security-owner decision authorizing reuse for this same non-concurrent logical node and retaining its portal-side record before sealing. Otherwise stop rather than copying the registration into a base or silently scrubbing it. Shut down the build domain, verify it is off and its disk is unattached elsewhere, then manually seal a standalone full qcow2 copy to that **node's** manifest base path: private staging on the same filesystem, inspect chain and snapshots, atomic no-overwrite publication, mode `0400`, SHA-256 pinned in a protected write-once cycle record. No seal command is provided by the CLI. Keep the build domain off and **never boot the base**. `clone-plan --node <exact-node> --base-sha256 <64-hex-digest> --confirm <exact-node>` returns inert JSON metadata after validating the sealed base. `clone` with the same arguments rechecks the base at execution and copies it into an owner-only `/scratch/vms/quarterly/clones` directory, publishing the new full disk copy atomically without overwriting an existing entry; it does **not** create or boot a domain. The clone deliberately keeps the **same node identity**, including machine ID, SSH host keys, static IP and FQDN. Define/import it manually only when the build domain is off; never boot build, base and clone simultaneously or run two copies with duplicate identity. Verify SSH against the fingerprint pinned at build time and provenance; do not require new host keys or disable host-key checking. Scan/remediate/re-scan the disposable clone and maintain a separate hardened reference for drift tests. The clean base is not proof of hardening. The [sysprep](QUARTERLY-SYSPREP.md) and [personalization](QUARTERLY-PERSONALIZE.md) scripts belong only to a separately approved **experimental identity-generalization path**, not this default procedure.

Manifest build/base/clone paths are fixed and cannot hold multiple cycles at once. Archive each prior cycle's disks and hash/evidence record under **unique cycle-specific paths** with approved retention, or perform explicitly approved cleanup after retention obligations are satisfied, before reusing manifest target paths for a fresh cycle. Inspect live/inactive XML, shut down and retire prior build/disposable domain definitions that reference those paths after ownership/snapshot/retention review; a retained `build-NAME` definition will block the next install and may point to a reused path. Stop on any old domain reference or existing target; never overwrite a base or silently relabel an old image as a new cycle. Domain definition/teardown, scanning and sealing remain manual gates.

## Asset handoff boundary

The lab ends after exporting the private v1 hash-only asset JSON from the selected node-owned baseline and pre-scan file. See [the workflow handoff](QUARTERLY-WORKFLOW.md#5-export-the-hash-only-asset-record-and-hand-off) for the implemented command, manual seal/assessment gates and retained scan requirements. The CLI cannot determine whether these files actually came from an assessed, sealed clean guest; independently verify and retain that evidence. `asset-record` accepts only this repository's exact canonical, tracked `quarterly/lab.json` (default manifest); `--manifest` with a different path remains useful for planning but cannot export an asset record. The no-follow manifest bytes must match committed HEAD in the clean-worktree provenance gate before use. `lab_commit` is this repository's Git HEAD only when staged, unstaged and untracked changes are absent; Git environment overrides are ignored. Policy, tailoring, scan/remediation coordination, approvals, CKLB release and ISSO review belong to the [private burden-of-proof repository](https://github.com/zcook44/burden-of-proof). No live VM run or production compliance assertion is implied.
