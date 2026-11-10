# Quarterly lab → hash-only asset handoff

This is the **operator sequence**, not a one-command deployment. Run from the repository root with `/usr/bin/python3`. Use the same sequence for each quarterly cycle and each asset. The first cycle must build and prove clean bases before clones are possible. See [QUARTERLY-LAB.md](QUARTERLY-LAB.md) for design, safety constraints, and validation limits.

**Current readiness:** the CLI can render plans and private Kickstarts, make a verified full disk copy from an externally sealed base, and export a small asset record. It does **not** seal a base, define/start/tear down domains, assess or remediate guests, generate CKLBs, or approve evidence. No new quarterly guest or real pre-scan has been verified here. The separate [burden-of-proof](https://github.com/zcook44/burden-of-proof) Go converter produces CKLB review candidates from independently saved scans and official manual benchmarks; Checkmate edits selected existing CKLBs. Policy examples are not approval, and formal review, signing and delivery remain external authorized steps.

## 0. Choose scope and gather inputs

- Use the dedicated `stigqa-lab` / `virbr-stigqa` / `192.168.164.0/24` network and the paths declared in `quarterly/lab.json`. Keep its guests, disks, network, and installer inputs scoped to this lab.
- Use authorized, release-matched full RHEL 8.10, 9.8, and 10.2 DVDs under `/scratch/iso`; inspect and pin their checksums and the extracted kernel/initrd bytes against the chosen DVDs before installation. The CLI currently checks DVD volume ID and cache presence, **not** byte provenance of the cache.
- Arrange authorized pre-remediation scan content and protected host storage for its raw results; do not fabricate an assessment. Official manual benchmarks and optional reviewed per-OS checklist policy are converter inputs in [burden-of-proof](https://github.com/zcook44/burden-of-proof), not parts of this lab; approval and evidence release are separate authorized processes.
- Choose an asset from `quarterly/lab.json`. This walkthrough uses `qa-rhel9-node1.qa.stig.test` (`192.168.164.102`, headless). Repeat for all required nodes; `node2` for each release and both controllers have GUI roles. **Every node gets its own fresh Kickstart installation and build/base disk each cycle**, even when release and role match. Never reuse an existing build disk.

```bash
cd /home/zrc/git/based-and-confused
/usr/bin/python3 -m quarterly.cli inventory
virsh -c qemu:///system net-list --all
virsh -c qemu:///system list --all
/usr/bin/python3 -m quarterly.cli preflight
```

**Gate:** resolve any unexpected route, network, domain, or disk collision before proceeding. Preflight is a read-only *pre-creation* check; once this new network/domains/disks exist, it will intentionally report them as collisions. Do not remove them to make the check green.

## 1. Define and start only the isolated network

Create a reviewed XML file, validate it, inspect it for `stigqa-lab` and `192.168.164.0/24`, then explicitly define/start it. These `virsh` commands **change host state**; use the host's approved libvirt privileges. Scope all changes to this lab's network.

```bash
install -d -m 0700 /scratch/vms/quarterly
/usr/bin/python3 -m quarterly.cli network-xml > /scratch/vms/quarterly/stigqa-network.xml
virt-xml-validate /scratch/vms/quarterly/stigqa-network.xml network
virsh -c qemu:///system net-define /scratch/vms/quarterly/stigqa-network.xml
virsh -c qemu:///system net-start stigqa-lab
virsh -c qemu:///system net-info stigqa-lab
```

**Gate:** confirm the bridge, route, guest DNS and required host firewall policy with only the egress this lab needs. This network XML has no DHCP pool; installed guests use manifest static IPs. Network creation does not create guests.

## 2. Create private credentials and a disposable build plan

Create only the new quarterly directories. Verify ownership and `0700` on `private` and `clones`; the asset-record output parent must be an existing owner-only directory. Prepare a nonsymlinked `0600` `/scratch/vms/quarterly/private/quarterly.env` with exactly these literal `KEY=value` lines; edit it securely, not with a shell `source`, command-line secret argument, or committed file:

```text
QUARTERLY_USERNAME=<chosen-operator-login>
QUARTERLY_PASSWORD=<unique-operator-password>
QUARTERLY_ROOT_PASSWORD=<unique-root-password>
QUARTERLY_LABADMIN_PUBLIC_KEY=<one-FIPS-compatible-ECDSA-or-RSA-public-key-line>
```

The CLI hashes passwords via stdin and creates a no-overwrite private Kickstart. `labadmin` is a separate key-enabled account with **passwordless sudo**, a deliberately noncompliant scored exception to record in policy and scans. Do not distribute the Kickstart: it contains credential hashes.

```bash
install -d -m 0700 /scratch/vms/quarterly/private /scratch/vms/quarterly/build /scratch/vms/quarterly/bases /scratch/vms/quarterly/clones
/usr/bin/python3 -m quarterly.cli kickstart --node qa-rhel9-node1 \
  --env /scratch/vms/quarterly/private/quarterly.env \
  --out /scratch/vms/quarterly/private/qa-rhel9-node1.ks
/usr/bin/python3 -m quarterly.cli install-plan --node qa-rhel9-node1 \
  --kickstart /scratch/vms/quarterly/private/qa-rhel9-node1.ks \
  --confirm build-qa-rhel9-node1
```

**Gate before the destructive install:** `install-plan` only **prints** a `virt-install` argv. Inspect its new `/scratch/vms/quarterly/build/qa-rhel9-node1.qcow2` target, DVD, release-matched kernel/initrd, `inst.ks`, `inst.stage2`, `fips=1`, and new network. The Kickstart limits `clearpart` to `/dev/vda`; verify that the **only** `/dev/vda` attached to the new build VM is its disposable disk. Validate the Kickstart with the matching `ksvalidator -v RHEL9` where available. Add `--dry-run --print-xml` to the reviewed `virt-install` argv, validate both install and disk-boot domain XML phases with `virt-xml-validate`, and only then execute that reviewed argv manually. **Do not pipe the printed plan into a shell.** Dry-run XML validation is not Anaconda proof.

## 3. Prove and seal the node-specific clean base (no scrub)

Boot the **new** `build-qa-rhel9-node1` guest and verify installer completion, release/hostname/static IP, record its SSH host-key fingerprints and verify key login, package source, FIPS (`fips-mode-setup --check`, `/proc/sys/crypto/fips_enabled`, installed boot arguments), SELinux enforcing/labels, and `labadmin`'s `sudo -n`. For a GUI role, verify graphical target and an actual password-based desktop login. Run a complete untailored pre-remediation scan on this clean build guest, verify its target/profile/rules, and export raw results and installation logs to protected host storage **before shutdown**. The asset handoff pre-scan refers to this captured assessment, not to an unscanned clone. If an assessor requires a scan of the disposable copy as well, keep that as separately labeled supplemental evidence; never mislabel it as the original pre-scan. **Registration gate:** the clean build/base must not carry RHSM entitlements, registration identity, private enrollment tokens, or other service credentials unless the subscription/security owner explicitly approves reuse for this *same logical node* and documents how portal-side records and non-concurrent use are managed. Prefer authorized local/DVD content for the build and register only the disposable working copy when required. If registration was necessary on the build and reuse is not approved, stop—do not seal a credential-bearing image or silently run sysprep on the primary path.

**Stop gate — manual sealing, not automated:** shut down the exact build domain and verify `shut off`, its disk path, and that no other domain attaches the disk (inspect active and inactive domain XML). Never copy a running or attached-for-write image. Retain the build domain **off** after sealing; do not boot the base. The base is a full standalone copy of this *same node's* unmodified clean build, including its identity—not a generalized golden image. Check available space and `qemu-img info --output=json` for qcow2 format, no backing file/data file or internal snapshots. Example, after reviewing the paths against `quarterly/lab.json`:

```bash
(
set -euo pipefail
node=qa-rhel9-node1
build=/scratch/vms/quarterly/build/$node.qcow2
base=/scratch/vms/quarterly/bases/$node-baseline.qcow2
test "$(virsh -c qemu:///system domstate build-$node)" = 'shut off'
virsh -c qemu:///system domblklist build-$node --details  # verify exact build disk
check_image() {
  /usr/bin/python3 -c '
import json, subprocess, sys
i = json.loads(subprocess.check_output(["qemu-img", "info", "--output=json", sys.argv[1]]))
d = (i.get("format-specific") or {}).get("data") or {}
c = i.get("children", [])
own_file = (len(c) == 1 and c[0].get("name") == "file" and
            c[0].get("info", {}).get("format") == "file" and
            not c[0].get("info", {}).get("children"))
if (i.get("format") != "qcow2" or i.get("snapshots") or
    any(i.get(k) for k in ("backing-filename", "full-backing-filename",
                            "backing-filename-format", "backing-image")) or
    "data-file" in i or (c and not own_file) or
    d.get("has-backing-file") or any(k in d for k in ("backing-filename", "data-file"))):
    sys.exit("not a standalone, snapshot-free qcow2")
' "$1"
}
check_image "$build"
test -f "$build" && test ! -L "$build" && test ! -e "$base" && test ! -L "$base"
source_sha=$(sha256sum "$build" | cut -d ' ' -f1)
stage=$(mktemp -d /scratch/vms/quarterly/bases/.seal-XXXXXX)
qemu-img convert -f qcow2 -O qcow2 "$build" "$stage/image.qcow2"
check_image "$stage/image.qcow2"
test "$(sha256sum "$build" | cut -d ' ' -f1)" = "$source_sha"
test "$(virsh -c qemu:///system domstate build-$node)" = 'shut off'
chmod 0400 "$stage/image.qcow2"
ln -T -- "$stage/image.qcow2" "$base"                # fails if base exists; never replace it
rm -f -- "$stage/image.qcow2" && rmdir -- "$stage"
sha256sum "$base"                                     # pin in protected cycle manifest
)
```

Do not run the example unless the XML/attachment and image checks passed and you have an **exclusive operator window** for this node: prohibit other operators/automation from starting, attaching, or modifying the build disk until the seal and hash record finish. `domblklist` is displayed for human review, not asserted by the snippet; the last `domstate` check alone cannot close a concurrent-start race. This is a manual procedure, **not an atomic safety guarantee**. On failure inspect/remove only the unlinked private staging directory after confirming no process uses it; never remove a published base to retry. Store the SHA-256, node, cycle, source provenance and media/scan references in an independently protected, write-once cycle record; recheck the digest before each clone. Mode `0400` alone is not protection against a privileged operator: enforce immutable storage/retention policy externally. The manifest's fixed paths cannot hold another cycle simultaneously. **Archive the old cycle and its hash/evidence under unique cycle-specific paths and approved retention controls; inspect live and inactive XML, shut down and retire every prior build/disposable domain definition that references those paths, then provision fresh manifest-target paths**. A retained `build-NAME` definition will both block the next install name and may point to a newly reused path. Do not repoint or undefine any domain until its exact ownership, disks, snapshots and retention have been reviewed and teardown approved. Stop if any old domain reference or build/base/clone path remains. Never silently overwrite or repoint a sealed prior cycle. Maintain a separately identified hardened reference after remediation for drift testing; do not harden the clean base.

Optional **experimental identity-generalization only**: [candidate sysprep](QUARTERLY-SYSPREP.md) and [clone personalization](QUARTERLY-PERSONALIZE.md) require separate approval and boot proofs. They are not required or authorized steps in this primary node-specific path.

## 4. Clone and boot the assessed asset

With the node-specific base externally sealed and its exact digest recorded and rechecked, run the plan, then the explicit no-overwrite **full copy**. Replace `<recorded-64-hex-base-sha256>` with the actual value; do not make up a digest. `clone` copies **only the disk** and refuses an existing destination; it does not define or boot the guest. Keep only one disposable copy for a node active at a time.

```bash
/usr/bin/python3 -m quarterly.cli clone-plan --node qa-rhel9-node1 \
  --base-sha256 '<recorded-64-hex-base-sha256>' --confirm qa-rhel9-node1
/usr/bin/python3 -m quarterly.cli clone --node qa-rhel9-node1 \
  --base-sha256 '<recorded-64-hex-base-sha256>' --confirm qa-rhel9-node1
```

**Manual domain gate:** inspect the copy's ownership and standalone chain. The CLI clone is **already node-specific**; do not sysprep or personalize it on this path. Define/import a disposable `qa-rhel9-node1` domain referencing **only** `/scratch/vms/quarterly/clones/qa-rhel9-node1.qcow2` on `stigqa-lab`, with a reviewed MAC. Use the same logical node identity as the stopped build domain (or a distinct libvirt domain name if both definitions are retained). Dry-run/inspect domain XML before definition. Verify `build-qa-rhel9-node1` remains off and no other domain for this node is running **before boot and throughout assessment**; never boot build, base and clone together. The copy deliberately carries the *same* machine ID, SSH host keys, hostname and static IP as its source. Verify the pinned SSH fingerprint against the recorded build fingerprint and the base/clone provenance; do **not** claim it is unique from earlier same-node runs or disable host-key checking. Verify actual IP/FQDN, key login, FIPS/SELinux, GUI where applicable, and that registration/entitlement state matches the approved build-time decision; an unregistered build must not yield enrollment material in its clone. If a name or disk exists, stop and investigate rather than overwrite it. Domain creation, shutdown and teardown are manual, not CLI features.

## 5. Export the hash-only asset record and hand off

Only after separate manual verification of the build, assessment, seal and pinned digest, use the node-owned baseline and exported raw pre-scan file. `asset-record` uses only this repository's canonical tracked `quarterly/lab.json`; unlike inert planning, an alternate `--manifest` cannot export a record. Its no-follow manifest bytes must match committed HEAD in the clean-worktree gate. The command hashes both owner-private regular files in bounded chunks (checking the supplied base digest), requires a read-only baseline, inspects its descriptor-pinned qcow2 format for snapshots/external dependencies, rejects symlinks and unsafe paths, and creates a new owner-only `0600` JSON file under an existing owned, nonsymlinked `0700` parent. It never boots a VM, seals a base, validates the scan content/target, or verifies guest assessment; it cannot establish that the image was assessed or sealed merely from hashes and format. It never overwrites an output. Retain the raw scan and separate seal/assessment evidence in protected storage; only file digests enter the record.

```bash
/usr/bin/python3 -m quarterly.cli asset-record --node qa-rhel9-node1 \
  --cycle-id Q4-2026 --base-sha256 '<recorded-64-hex-base-sha256>' \
  --pre /scratch/vms/quarterly/private/qa-rhel9-node1-pre.xml \
  --out /scratch/vms/quarterly/private/qa-rhel9-node1-asset.json
```

The v1 record contains exactly `schema_version`, `cycle_id`, `asset_fqdn`, `os_release` (manifest OS variant), `role`, `baseline_state` (`clean`), `baseline_sha256`, `pre_scan_sha256`, and `lab_commit`. `baseline_state` is a declared manifest label, **not** independent proof of a clean or assessed guest. `lab_commit` is this repository's Git HEAD, recorded **only if the checkout has no staged, unstaged or untracked changes**; Git environment overrides are ignored. Review and commit lab changes before a real handoff. Also verify that no tracked files have `assume-unchanged` or `skip-worktree` index flags: Git status may conceal edits to those paths. Preserve the raw scan, base digest and manual seal/assessment record under approved retention outside the guest. Pass the JSON and independently retained evidence to the [private burden-of-proof repository](https://github.com/zcook44/burden-of-proof) for independent asset-record checking and optional saved-scan-to-CKLB review conversion. A separately supplied Checkmate can edit selected existing CKLBs; neither tool approves findings or publishes a signed release. This lab does not certify compliance or sign an ISSO acceptance.
