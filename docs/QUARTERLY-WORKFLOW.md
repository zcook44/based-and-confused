# Quarterly lab → per-host CKLB → review package

This is the **operator sequence**, not a one-command deployment. Run from the repository root on the `feature/quarterly-stig-evidence-lab` branch with `/usr/bin/python3`. Use the same sequence for each quarterly cycle and each asset. The first cycle must build and prove clean bases before clones are possible. See [QUARTERLY-LAB.md](QUARTERLY-LAB.md) for design, safety constraints, and validation limits.

**Current readiness:** the CLI can render plans/Kickstarts, make a verified full disk copy from an externally sealed base, and release a schema-validated CKLB from supplied evidence. It does **not** seal a base, define/start/tear down domains, remediate guests, perform assessments, obtain approvals, or sign an ISSO review. On this workstation the matching DVDs, extracted installer kernels/initrds, and pinned DISA CKLB schema exist, but **no new lab network, clean base, or guest scan has been established**. `ksvalidator` and host-side `oscap` are not installed. The optional identity-generalization scripts also require unavailable `virt-sysprep`/`virt-customize`; they are not prerequisites here. Do not treat the steps below as a completed run.

## 0. Choose scope and gather inputs

- Use the dedicated `stigqa-lab` / `virbr-stigqa` / `192.168.164.0/24` network and the paths declared in `quarterly/lab.json`. Keep its guests, disks, network, and installer inputs scoped to this lab.
- Use authorized, release-matched full RHEL 8.10, 9.8, and 10.2 DVDs under `/scratch/iso`; inspect and pin their checksums and the extracted kernel/initrd bytes against the chosen DVDs before installation. The CLI currently checks DVD volume ID and cache presence, **not** byte provenance of the cache.
- Obtain the applicable **official DISA XCCDF 1.2 Benchmark**, its exact revision and complete rule set, an independently pinned approval authority public key, and a detached-signature approval ledger. Obtain the matching automation/scanner content and prove its rule IDs map to that benchmark; stock SSG rule IDs alone are not sufficient for this converter. Keep licensed media and nonpublic evidence in authorized storage.
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

Create only the new quarterly directories. Verify ownership and `0700` on `private`, `clones`, and `evidence`; the eventual evidence **parent** must not be group/world writable. Prepare a nonsymlinked `0600` `/scratch/vms/quarterly/private/quarterly.env` with exactly these literal `KEY=value` lines; edit it securely, not with a shell `source`, command-line secret argument, or committed file:

```text
QUARTERLY_USERNAME=<chosen-operator-login>
QUARTERLY_PASSWORD=<unique-operator-password>
QUARTERLY_ROOT_PASSWORD=<unique-root-password>
QUARTERLY_LABADMIN_PUBLIC_KEY=<one-FIPS-compatible-ECDSA-or-RSA-public-key-line>
```

The CLI hashes passwords via stdin and creates a no-overwrite private Kickstart. `labadmin` is a separate key-enabled account with **passwordless sudo**, a deliberately noncompliant scored exception to record in policy and scans. Do not distribute the Kickstart: it contains credential hashes.

```bash
install -d -m 0700 /scratch/vms/quarterly/private /scratch/vms/quarterly/build /scratch/vms/quarterly/bases /scratch/vms/quarterly/clones /scratch/vms/quarterly/evidence
/usr/bin/python3 -m quarterly.cli kickstart --node qa-rhel9-node1 \
  --env /scratch/vms/quarterly/private/quarterly.env \
  --out /scratch/vms/quarterly/private/qa-rhel9-node1.ks
/usr/bin/python3 -m quarterly.cli install-plan --node qa-rhel9-node1 \
  --kickstart /scratch/vms/quarterly/private/qa-rhel9-node1.ks \
  --confirm build-qa-rhel9-node1
```

**Gate before the destructive install:** `install-plan` only **prints** a `virt-install` argv. Inspect its new `/scratch/vms/quarterly/build/qa-rhel9-node1.qcow2` target, DVD, release-matched kernel/initrd, `inst.ks`, `inst.stage2`, `fips=1`, and new network. The Kickstart limits `clearpart` to `/dev/vda`; verify that the **only** `/dev/vda` attached to the new build VM is its disposable disk. Validate the Kickstart with the matching `ksvalidator -v RHEL9` where available. Add `--dry-run --print-xml` to the reviewed `virt-install` argv, validate both install and disk-boot domain XML phases with `virt-xml-validate`, and only then execute that reviewed argv manually. **Do not pipe the printed plan into a shell.** Dry-run XML validation is not Anaconda proof.

## 3. Prove and seal the node-specific clean base (no scrub)

Boot the **new** `build-qa-rhel9-node1` guest and verify installer completion, release/hostname/static IP, record its SSH host-key fingerprints and verify key login, package source, FIPS (`fips-mode-setup --check`, `/proc/sys/crypto/fips_enabled`, installed boot arguments), SELinux enforcing/labels, and `labadmin`'s `sudo -n`. For a GUI role, verify graphical target and an actual password-based desktop login. Run a complete untailored pre-remediation scan on this clean build guest, verify its target/profile/rules, and export raw results and installation logs to protected host storage **before shutdown**. The pre-scan in §6 refers to this captured assessment, not to an unscanned clone. If an assessor requires a scan of the disposable copy as well, keep that as separately labeled supplemental evidence; never mislabel it as the original pre-scan. **Registration gate:** the clean build/base must not carry RHSM entitlements, registration identity, private enrollment tokens, or other service credentials unless the subscription/security owner explicitly approves reuse for this *same logical node* and documents how portal-side records and non-concurrent use are managed. Prefer authorized local/DVD content for the build and register only the disposable working copy when required. If registration was necessary on the build and reuse is not approved, stop—do not seal a credential-bearing image or silently run sysprep on the primary path.

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

## 5. Prepare the versioned policy, tailoring, and independent approval

Put the real official benchmark and approved policy in protected, versioned storage. `quarterly/policy.example.json` is **illustrative only**: replace benchmark/profile/rule/value IDs, placeholder approval metadata, expiry and host-specific applicability evidence. Record the `labadmin` passwordless-sudo exception as a scored finding where applicable; an approved failure remains **Open**. A genuine Not Applicable needs evidence keyed to the exact asset. Keep the master policy free of per-host scan results.

Generate tailoring against the **same benchmark bytes** that later go into the release bundle. Its `--benchmark-href` must name that benchmark file; put a copy with the same basename next to the tailoring in the guest scanner's working directory. The generated tailored profile ID is `quarterly-profile`.

```bash
/usr/bin/python3 -m quarterly.cli tailor \
  --policy /scratch/vms/quarterly/private/rhel9-policy.json \
  --benchmark /scratch/iso/quarterly/rhel9-official-benchmark.xml \
  --benchmark-href rhel9-official-benchmark.xml \
  --out /scratch/vms/quarterly/private/rhel9-tailoring.xml
```

**Approval gate:** obtain the external JSON ledger with `schema: 1`, SHA-256 bindings to these exact policy and benchmark files, and an `approvals` list matching each exception's ID, rule, approver, disposition, and expiry. Obtain its detached signature and the authority's public key through **independent** trust channels; check that key's fingerprint out of band. Do not sign an approval yourself and call it ISSO authorization. Keep ledger/signature/key with the evidence inputs.

## 6. Assess, remediate, and reassess each guest

Use the **complete untailored base-profile pre-scan captured on the clean build guest in §3**, before any remediation; apply approved remediation to the disposable clone separately and log its playbook/revision/change record; run the **tailored post-scan** and a **complete untailored coverage scan** afterward on that clone. Use the actual matching DISA XCCDF/scanner whose result embeds the official benchmark ID/version/release-info, exact DISA rule IDs, original profile ID (pre and coverage), generated `quarterly-profile` (post), and the asset FQDN as target. For example, on a scanner with `oscap` and the matching content installed, the command shapes are:

```bash
# On clean build guest BEFORE shutdown/seal (section 3):
oscap xccdf eval --profile '<official-base-profile-id>' \
  --results qa-rhel9-node1-pre.xml --report qa-rhel9-node1-pre.html \
  rhel9-official-benchmark.xml
# On disposable clone AFTER approved remediation:
# Apply and record the separately approved remediation here; never assume a scan remediates.
oscap xccdf eval --tailoring rhel9-tailoring.xml --profile quarterly-profile \
  --results qa-rhel9-node1-post.xml --report qa-rhel9-node1-post.html \
  rhel9-official-benchmark.xml
oscap xccdf eval --profile '<official-base-profile-id>' \
  --results qa-rhel9-node1-coverage.xml --report qa-rhel9-node1-coverage.html \
  rhel9-official-benchmark.xml
```

These are **command shapes, not a proven real DISA scan path**. Confirm that the official benchmark is executable by the installed scanner and that its checks really ran; if using SSG, first establish a reviewed SSG→DISA mapping—the converter will not invent one. Run with the privilege needed for a complete assessment; a findings exit code is not permission to discard results. Copy raw XML and optional HTML reports to protected host storage outside the disposable disk. Check `TestResult` target, profile, timestamps, release-info, full official-rule coverage, and pre < post ≤ coverage. Any override, stale/missing rule, revision mismatch, or tailored/coverage disagreement blocks CKLB release. Manual or unsupported checks remain **Not Reviewed**, not passed.

## 7. Generate and verify the per-host review package

The official DISA CKLB 1.0 schema is pinned by digest in `quarterly/cklb.py`; on this workstation its JSON is `/scratch/iso/quarterly/SV3_CKLB_1_0_JSON_SCHEMA.json`. Use a **new** evidence run directory under an owned, nonsymlinked, owner-only parent. Give each host/run a unique destination. The ledger, signature and public key must match the exact policy/benchmark supplied here.

```bash
/usr/bin/python3 -m quarterly.cli cklb \
  --host qa-rhel9-node1.qa.stig.test \
  --policy /scratch/vms/quarterly/private/rhel9-policy.json \
  --benchmark /scratch/iso/quarterly/rhel9-official-benchmark.xml \
  --tailoring /scratch/vms/quarterly/private/rhel9-tailoring.xml \
  --pre /scratch/vms/quarterly/private/qa-rhel9-node1-pre.xml \
  --post /scratch/vms/quarterly/private/qa-rhel9-node1-post.xml \
  --coverage /scratch/vms/quarterly/private/qa-rhel9-node1-coverage.xml \
  --schema /scratch/iso/quarterly/SV3_CKLB_1_0_JSON_SCHEMA.json \
  --approval-ledger /scratch/vms/quarterly/private/rhel9-approvals.json \
  --approval-signature /scratch/vms/quarterly/private/rhel9-approvals.sig \
  --trusted-key /scratch/vms/quarterly/private/approval-authority.pub \
  --evidence-dir /scratch/vms/quarterly/evidence/qa-rhel9-node1-run-001
```

A successful command prints the `.cklb` path. It creates a `0700` run directory containing a `0600` host CKLB, `manifest.json`, and hashed copies of **policy, benchmark, pre/post/coverage XML, tailoring, pinned schema, signed approval ledger, detached signature, and trusted public key**. It does not include a private signing key. An existing run is never overwritten. Inspect the manifest's `host`, file hashes, and `review_state`; preserve it unchanged. If validation fails, fix the source evidence and use a **new** run ID; do not hand-edit a CKLB to force release. An interrupted write may leave a partial directory for forensic inspection—do not treat that directory as a released artifact.

Keep additional human-readable SCAP reports, installation and no-scrub seal proof, base/media/datastream checksums, remediation logs, exception decisions, and change tickets in a separate protected supplemental set with its own checksums and retention record. The CLI does **not** collect or hash those supplemental files automatically. Do not add them into the generated run directory and imply they are covered by its manifest.

## 8. Review, sign off, retain, reset

1. Import the generated CKLB into **STIG Viewer 3**, verify a representative import/export round trip and every official rule/status/comment, especially Open exceptions, asset-specific N/A, and Not Reviewed manual checks. Schema validation alone is insufficient.
2. Give the reviewer the unchanged per-host bundle and separately hashed supplemental evidence, plus the cycle manifest (vendor media, base, datastream, policy, tailoring, scanner, remediation revision and dates). Verify detached approval signature against the independently pinned key. The ledger authorizes the policy; it does **not** sign the per-host CKLB.
3. Obtain separate **per-asset ISSO acceptance/signature** or return findings for correction. Until then, the manifest correctly says `pending ISSO STIG Viewer import and signed per-asset acceptance`; do not call the guest or checklist STIG-compliant.
4. Retain the protected evidence **outside** guest disks per policy. For the next cycle, perform a fresh Kickstart installation **per node** from pinned media, archive prior-cycle disks/hash records under unique cycle paths, retire the inspected old build/disposable domain definitions after approval, and use new manifest-target base and disposable disks only after collision checks. Before any teardown, shut down the clone and inspect exact libvirt domain XML and attached disk; remove only explicitly approved disposable domains/disks, never an archived base or evidence. This repository does **not** automate teardown.

**Stop conditions:** missing authorized benchmark/scanner mapping, no verified sealed base, no independent signed approval, incomplete or stale raw scans, or failed STIG Viewer/ISSO review. Never replace those gates with synthetic tests or a plausible-looking checklist.
