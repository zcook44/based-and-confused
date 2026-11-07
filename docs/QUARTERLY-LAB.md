# Quarterly STIG evidence lab (feature branch)

For the operator's ordered procedure—from network startup through per-host CKLB and ISSO review artifacts—use [QUARTERLY-WORKFLOW.md](QUARTERLY-WORKFLOW.md). This document records the implementation boundaries and validation evidence.

**Status:** isolated lab configuration, private Kickstart rendering, direct-DVD install plans, full-copy clone planning/execution, offline candidate and per-node image personalization scripts, policy-to-tailoring generation, and signed-approval/scan-to-CKLB evidence gating are implemented. The CLI does **not** create, boot, remediate, destroy, or scan a guest; the offline scripts do not promote a base or prove real guest behavior. No real RHEL installation, STIG Viewer import, or ISSO acceptance has been demonstrated. Do not send the generated CKLB to an ISSO as a validated production checklist until the gates below are satisfied.

## Isolation and safety

The new lab is `stigqa-lab`, bridge `virbr-stigqa`, `192.168.164.0/24`, gateway `.1`, domain `qa.stig.test`. The eight nodes are `qa-rhel8-node1` `.121`, `qa-rhel8-node2` `.122`, `qa-rhel9-rheladm` `.101`, `qa-rhel9-node1` `.102`, `qa-rhel9-node2` `.103`, `qa-rhel10-rheladm` `.111`, `qa-rhel10-node1` `.112`, and `qa-rhel10-node2` `.113`. `node2` for each release and both controllers have GUI roles. All new disks and installer caches live under `/scratch/vms/quarterly`; authorized vendor DVDs remain under `/scratch/iso`. The existing `stig-lab` and its `192.168.150.0/24` guests, rules, disks, and ISO are not modified by these commands.

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

`install-plan` **prints** an inert `virt-install` command. It does not run it. Do not feed that output directly to a shell without review. The example uses a separate, new 40-GB qcow2 build disk and the matching DVD; `virt-install --dry-run --print-xml` produced valid install- and final-boot libvirt domain XML for the RHEL 9 GUI role. It did not boot a guest. `ksvalidator` is not installed here; syntax and Anaconda execution remain unproven.

Before promoting any installed build disk to a clean base: boot the guest, confirm package source/install completion, FIPS (`fips-mode-setup --check` and boot arguments), SELinux labels, unique hostname/IP, GUI login where applicable, key login and `sudo -n`, and capture a pre-remediation scan. Shut it down. The [sysprep script](QUARTERLY-SYSPREP.md) prepares only a new **unbootable candidate copy** with approved operations; it does not seal a base. Two separately personalized disposable canaries must prove distinct identity, network reachability, access, and FIPS/SELinux/GUI behavior before an operator independently promotes a standalone qcow2 base with no overwrite, read-only permissions, and a recorded/rechecked SHA-256. No automated canary/sealing or identity-safe guest boot is implemented; do not simply copy an installed disk and start duplicate identities. `clone-plan --node <exact-node> --base-sha256 <64-hex-digest> --confirm <exact-node>` returns inert JSON metadata after validating the sealed base. `clone` with the same arguments rechecks the base at execution and copies it into an owner-only `/scratch/vms/quarterly/clones` directory, publishing the new disk atomically without overwriting an existing entry; it does **not** create or boot a domain. The CLI clone is unpersonalized and must not be booted: [offline personalization](QUARTERLY-PERSONALIZE.md) publishes a separate verified image with manifest FQDN/static network for manual domain definition. A separately built hardened reference is required for drift tests; the clean image is for testing remediation, not proof of hardening.

## Master policy and evidence

`quarterly/policy.example.json` describes the **shape**, not a usable approved RHEL policy. Replace every example benchmark/profile/rule ID and approval placeholder with the matching **official DISA STIG revision** and actual externally approved exceptions. For an N/A exception, use an `applicability_by_host` object keyed by exact asset FQDN; never use a global free-text assertion to mark every VM N/A. A real failure stays `open`, even with an approval. Passing scans become `not_a_finding`; unknown/manual/error results stay `not_reviewed`. Missing coverage blocks release.

The official DISA STIG Viewer 3 CKLB 1.0 JSON schema is available from the [DISA SRG/STIG Tools page](https://www.cyber.mil/stigs/srg-stig-tools/) at [SV3_CKLB_1_0_JSON_SCHEMA.zip](https://dl.dod.cyber.mil/wp-content/uploads/stigs/zip/SV3_CKLB_1_0_JSON_SCHEMA.zip). The ZIP downloaded on this workstation had SHA-256 `4e017289df9d80f0976e1549ba4e31b7d653eba34ecb8364388bdb8fb3c2a430`; the contained JSON had SHA-256 `8b88205de07662006e1660b1decad677d344828d6687b1dd26bca15105b7bb37` and is stored at `/scratch/iso/quarterly/SV3_CKLB_1_0_JSON_SCHEMA.json`. Obtain/pin the schema and benchmark through authorized channels on other hosts. The JSON schema verifies shape, not STIG Viewer behavior or the authenticity of a supplied benchmark.

Generate the tailoring file against the **same official benchmark file** used at release:

```bash
/usr/bin/python3 -m quarterly.cli tailor --policy /path/to/approved-policy.json \
  --benchmark /path/to/official-benchmark.xml \
  --benchmark-href official-benchmark.xml \
  --out /scratch/vms/quarterly/private/rhel9-tailoring.xml
```

The policy example intentionally fails approval validation until real references replace its placeholders. The CLI release gate requires a separate JSON **approval ledger** with `schema: 1`, `policy_sha256`, `benchmark_sha256`, and an `approvals` list. Every exception's `approval_id`, `rule_id`, `approver`, `disposition`, and `expires` must match its signed ledger entry. A trusted ISSO/approval-system key must be pinned independently; the ledger must be signed with a detached SHA-256 signature verifiable by `openssl dgst -sha256 -verify <trusted-public-key> -signature <signature> <ledger.json>`. A signature created by the automation itself would not constitute independent approval. The ledger signature approves the policy revision, **not** the per-host CKLB; reviewer signoff remains an external step.

Capture raw per-host XCCDF results from the *same benchmark and revision*: a complete base-profile **pre** assessment, a tailored-profile **post** assessment, and a complete untailored base-profile **coverage** assessment after post. The XCCDF result targets must exactly equal the manifest FQDN; pre must precede post, coverage must not precede post, and post/coverage must be fresh. The supported converter requires DISA rule IDs in the scan itself; a standard SSG result with different IDs/benchmark cannot be treated as the official DISA checklist. A verified SSG→DISA mapping or an actually scannable matching DISA XCCDF is still required for real unattended issuance. Do not invent a mapping or turn manual controls green.

```bash
/usr/bin/python3 -m quarterly.cli cklb \
  --host qa-rhel9-node1.qa.stig.test \
  --policy /path/to/approved-policy.json \
  --benchmark /path/to/official-benchmark.xml \
  --tailoring /scratch/vms/quarterly/private/rhel9-tailoring.xml \
  --pre /path/to/qa-rhel9-node1-pre.xml \
  --post /path/to/qa-rhel9-node1-post.xml \
  --coverage /path/to/qa-rhel9-node1-coverage.xml \
  --schema /scratch/iso/quarterly/SV3_CKLB_1_0_JSON_SCHEMA.json \
  --approval-ledger /path/to/approved-ledger.json \
  --approval-signature /path/to/approved-ledger.sig \
  --trusted-key /path/to/independently-pinned-approver.pub \
  --evidence-dir /scratch/vms/quarterly/evidence/qa-rhel9-node1-run-001
```

The destination parent must exist, not be a symlink, and not be group/world writable. The new directory is `0700`, each file `0600`; an existing run is never overwritten. The CLI snapshots inputs into an owner-only temporary directory and validates **those exact bytes** before release. The bundle retains the CKLB, raw assessments, policy, benchmark, tailoring, pinned schema, signed approval ledger, detached signature, trusted **public** key, SHA-256 manifest and a `pending ISSO STIG Viewer import and signed per-asset acceptance` review state. Evidence stays outside disposable guest disks. Restrict ISSO distribution and retention appropriately. This pipeline does **not** perform remediation or acquire an ISSO signature, and it does not automatically validate package/media/datastream provenance or STIG Viewer import/export.

## Verified here / remaining release gates

- Full Python suite passes; read-only preflight found no current collision on the new subnet; network XML passed libvirt's `virt-xml-validate`.
- RHEL 8.10/9.8/10.2 matching DVD volume IDs and installer payloads were inspected; a direct-DVD plan was generated for RHEL 8 minimal, RHEL 9 GUI, and RHEL 10 controller. RHEL 9 GUI `virt-install --dry-run --print-xml` produced two libvirt-valid XML phases, with DVD and owned disk paths verified. No new VM or network was defined or started.
- A synthetic complete four-rule CKLB passed the **downloaded official DISA CKLB 1.0 schema**. Tests use synthetic XCCDF fixtures, not a real host scan; no STIG Viewer round trip or ISSO ingest was performed.
- Still required for a real quarterly cycle: official revision-matched DISA benchmark and an attested rule mapping/automated scan path, signed approval ledger from a trusted external authority, vendor DVD and extracted installer-cache checksum provenance (current planning verifies volume ID and cache presence, not byte identity), offline sysprep/sealing with boot proof, remediation runner and hardened reference, explicit safe **domain** creation/teardown, GUI/FIPS/SSH verification in installed guests, and STIG Viewer/ISSO acceptance. Do not represent the partial implementation as a compliance certificate.
