# Quarterly lab → per-host CKLB → review package

This is the **operator sequence**, not a one-command deployment. Run from the repository root on the `feature/quarterly-stig-evidence-lab` branch with `/usr/bin/python3`. Use the same sequence for each quarterly cycle and each asset. The first cycle must build and prove clean bases before clones are possible. See [QUARTERLY-LAB.md](QUARTERLY-LAB.md) for design, safety constraints, and validation limits.

**Current readiness:** the CLI can render plans/Kickstarts and release a schema-validated CKLB from supplied evidence. It does **not** define or boot VMs, seal/sysprep images, remediate guests, perform assessments, obtain approvals, or sign an ISSO review. On this workstation the matching DVDs, extracted installer kernels/initrds, and pinned DISA CKLB schema exist, but **no new lab network, clean base, or guest scan has been established**. `ksvalidator` and host-side `oscap` are not installed. Do not treat the steps below as a completed run.

## 0. Choose scope and gather inputs

- Work only with `stigqa-lab` / `virbr-stigqa` / `192.168.164.0/24`. The existing `stig-lab` (`192.168.150.0/24`) is unrelated. Do not re-use its guests, disks, network, or installer ISO.
- Use authorized, release-matched full RHEL 8.10, 9.8, and 10.2 DVDs under `/scratch/iso`; inspect and pin their checksums and the extracted kernel/initrd bytes against the chosen DVDs before installation. The CLI currently checks DVD volume ID and cache presence, **not** byte provenance of the cache.
- Obtain the applicable **official DISA XCCDF 1.2 Benchmark**, its exact revision and complete rule set, an independently pinned approval authority public key, and a detached-signature approval ledger. Obtain the matching automation/scanner content and prove its rule IDs map to that benchmark; stock SSG rule IDs alone are not sufficient for this converter. Keep licensed media and nonpublic evidence in authorized storage.
- Choose an asset from `quarterly/lab.json`. This walkthrough uses `qa-rhel9-node1.qa.stig.test` (`192.168.164.102`, headless). Repeat for all required nodes; `node2` for each release and both controllers have GUI roles. Different roles have **different build/base disks**. Never reuse a build disk that already exists.

```bash
cd /home/zrc/git/based-and-confused
/usr/bin/python3 -m quarterly.cli inventory
virsh -c qemu:///system net-list --all
virsh -c qemu:///system list --all
/usr/bin/python3 -m quarterly.cli preflight
```

**Gate:** resolve any unexpected route, network, domain, or disk collision before proceeding. Preflight is a read-only *pre-creation* check; once this new network/domains/disks exist, it will intentionally report them as collisions. Do not remove them to make the check green.

## 1. Define and start only the isolated network

Create a reviewed XML file, validate it, inspect it for `stigqa-lab` and `192.168.164.0/24`, then explicitly define/start it. These `virsh` commands **change host state**; use the host's approved libvirt privileges. No old-lab network is modified.

```bash
install -d -m 0700 /scratch/vms/quarterly
/usr/bin/python3 -m quarterly.cli network-xml > /scratch/vms/quarterly/stigqa-network.xml
virt-xml-validate /scratch/vms/quarterly/stigqa-network.xml network
virsh -c qemu:///system net-define /scratch/vms/quarterly/stigqa-network.xml
virsh -c qemu:///system net-start stigqa-lab
virsh -c qemu:///system net-info stigqa-lab
```

**Gate:** confirm the bridge, route, guest DNS and required host firewall policy without broadening the existing lab's egress. This network XML has no DHCP pool; installed guests use manifest static IPs. Network creation does not create guests.

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

**Gate before the destructive install:** `install-plan` only **prints** a `virt-install` argv. Inspect its new `/scratch/vms/quarterly/build/clean-rhel9.qcow2` target, DVD, release-matched kernel/initrd, `inst.ks`, `inst.stage2`, `fips=1`, and new network. The Kickstart limits `clearpart` to `/dev/vda`; verify that the **only** `/dev/vda` attached to the new build VM is its disposable disk. Validate the Kickstart with the matching `ksvalidator -v RHEL9` where available. Add `--dry-run --print-xml` to the reviewed `virt-install` argv, validate both install and disk-boot domain XML phases with `virt-xml-validate`, and only then execute that reviewed argv manually. **Do not pipe the printed plan into a shell.** Dry-run XML validation is not Anaconda proof.

## 3. Prove, scrub, and seal the clean base

Boot the **new** `build-qa-rhel9-node1` guest and verify installer completion, release/hostname/static IP, SSH host-key identity and key login, package source, FIPS (`fips-mode-setup --check`, `/proc/sys/crypto/fips_enabled`, installed boot arguments), SELinux enforcing/labels, and `labadmin`'s `sudo -n`. For a GUI role, verify graphical target and an actual password-based desktop login. Keep installation logs and a pre-remediation assessment outside the guest.

**Stop gate — not implemented by the CLI:** shut down the build VM, remove instance-specific machine ID, SSH *host* keys, registration entitlements, logs and other instance secrets with an approved **offline** sysprep procedure, then boot a disposable test copy to prove fresh unique identity and reachable access. Preserve a separate **clean pre-remediation** base and separately create a **hardened reference** for drift tests. Only after these checks may the operator place a standalone qcow2 at `/scratch/vms/quarterly/bases/clean-rhel9.qcow2`, make it read-only, and record its SHA-256 in the cycle manifest. Do **not** merely copy a live installed disk or start two guests with duplicate IDs/keys. Base creation, safe domain teardown, and sysprep are not provided here.

## 4. Clone and boot the assessed asset

With the base externally sealed and its exact digest recorded, run the plan, then the explicit no-overwrite copy. Replace `<recorded-64-hex-base-sha256>` with the actual value; do not make up a digest. `clone` copies **only the disk** and refuses an existing destination; it does not define or boot the guest.

```bash
/usr/bin/python3 -m quarterly.cli clone-plan --node qa-rhel9-node1 \
  --base-sha256 '<recorded-64-hex-base-sha256>' --confirm qa-rhel9-node1
/usr/bin/python3 -m quarterly.cli clone --node qa-rhel9-node1 \
  --base-sha256 '<recorded-64-hex-base-sha256>' --confirm qa-rhel9-node1
```

**Manual domain gate:** inspect the resulting clone's ownership and image chain; create a **new** `qa-rhel9-node1` libvirt domain referencing only `/scratch/vms/quarterly/clones/qa-rhel9-node1.qcow2` on `stigqa-lab`. Dry-run and inspect the domain XML before `virt-install --import`/definition. Boot it, verify its unique machine ID/SSH host keys, static IP/FQDN, access, and no copied registration secret. Do not attach the sealed base or original-lab disk to a running domain. If a clone name or disk already exists, stop and investigate rather than overwriting it.

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

Run a **complete untailored base-profile pre-scan** on the clean disposable guest; apply the approved remediation separately and log its playbook/revision/change record; run the **tailored post-scan** and a **complete untailored coverage scan** afterward. Use the actual matching DISA XCCDF/scanner whose result embeds the official benchmark ID/version/release-info, exact DISA rule IDs, original profile ID (pre and coverage), generated `quarterly-profile` (post), and the asset FQDN as target. For example, on a scanner with `oscap` and the matching content installed, the command shapes are:

```bash
oscap xccdf eval --profile '<official-base-profile-id>' \
  --results qa-rhel9-node1-pre.xml --report qa-rhel9-node1-pre.html \
  rhel9-official-benchmark.xml
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

Keep additional human-readable SCAP reports, installation/sysprep proof, base/media/datastream checksums, remediation logs, exception decisions, and change tickets in a separate protected supplemental set with its own checksums and retention record. The CLI does **not** collect or hash those supplemental files automatically. Do not add them into the generated run directory and imply they are covered by its manifest.

## 8. Review, sign off, retain, reset

1. Import the generated CKLB into **STIG Viewer 3**, verify a representative import/export round trip and every official rule/status/comment, especially Open exceptions, asset-specific N/A, and Not Reviewed manual checks. Schema validation alone is insufficient.
2. Give the reviewer the unchanged per-host bundle and separately hashed supplemental evidence, plus the cycle manifest (vendor media, base, datastream, policy, tailoring, scanner, remediation revision and dates). Verify detached approval signature against the independently pinned key. The ledger authorizes the policy; it does **not** sign the per-host CKLB.
3. Obtain separate **per-asset ISSO acceptance/signature** or return findings for correction. Until then, the manifest correctly says `pending ISSO STIG Viewer import and signed per-asset acceptance`; do not call the guest or checklist STIG-compliant.
4. Retain the protected evidence **outside** guest disks per policy. For the next cycle, rebake clean bases from pinned media, repeat the identity-safe boot proof, and use fresh, uniquely named clones and scans. Before any teardown, inspect the exact libvirt domain XML, attached disk and base-image relationship; remove only explicitly approved disposable domains/disks. This repository does **not** automate teardown.

**Stop conditions:** missing authorized benchmark/scanner mapping, no verified sealed base, no independent signed approval, incomplete or stale raw scans, or failed STIG Viewer/ISSO review. Never replace those gates with synthetic tests or a plausible-looking checklist.
