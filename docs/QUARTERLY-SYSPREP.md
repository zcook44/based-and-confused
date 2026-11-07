# Experimental offline build-image sysprep (not the quarterly default)

The [primary operator workflow](QUARTERLY-WORKFLOW.md) freshly installs **each node** every cycle and seals a same-node baseline **without scrubbing or personalization**. This guide is retained only for an **optional experimental identity-generalization study**, with separate review/authorization; it is not a quarterly release gate. **Do not run a scrub merely because this guide exists.** `scripts/quarterly-sysprep.sh` inspects an installed, **shut-down quarterly build** image and, only on explicit execution, copies it to a new owner-only candidate before running `virt-sysprep`. It neither boots a guest nor publishes a clean base. This is a proposed, gated procedure—not authorization to use the scrub profile on real images.

## Preconditions and scope

- Obtain local approval for the exact operations below, capture required installer and pre-remediation evidence **before** log removal, and validate RHSM cleanup requirements with the subscription administrator. Offline cleanup does not unregister an asset in the portal.
- Read `quarterly/lab.json` for the node, role, canonical `/scratch/vms/quarterly/build/{node-name}.qcow2` and `/scratch/vms/quarterly/bases/{node-name}-baseline.qcow2` paths. The source must be a standalone, snapshot-free qcow2 owned by the operator. The corresponding `build-NAME` domain must be shut off with exactly the expected disk; no other domain may attach the source. The script reads but must not scrub the original build disk; never point it at a running image, a base, or a clone outside the quarterly manifest. Do not disturb a sealed primary-path base or archived cycle.
- Create `/scratch/vms/quarterly/candidates` as an owner-only (`0700`) directory under the approved quarterly disk root. The source build directory and disk must also be owner-only (`0700` directory, `0600` qcow2). If the build directory is not owner-only, inspection refuses it; verify ownership and access needs before explicitly tightening that *quarterly build directory*. Do not change permissions of another lab's directory. The final candidate path must **not** exist, including as a symlink. The script refuses to create or replace it.
- Install `virsh`, `qemu-img`, and `virt-sysprep` in the operator environment and confirm access to `qemu:///system`. `virt-sysprep --list-operations` on **this host** must include every requested operation. The script fails closed when any tool or operation is missing. Do not rely on the sysprep default operation set.

The explicit allowlist is `machine-id,ssh-hostkeys,rh-subscription-manager,net-nmconn,net-hwaddr,dhcp-client-state,logfiles`. It intentionally excludes account/user SSH-directory deletion, certificates, firewall rules, UUID changes, arbitrary scripts and customization. `labadmin` and its authorized **public** key are intended to survive; verify that outcome in a guest. Clearing NetworkManager profiles leaves the candidate **unbootable on a static-only network** until separate offline personalization. `/etc/hostname` may persist; do not boot the candidate directly.

## Commands

Run from the repository root, using one manifest build role at a time:

```bash
cd /home/zrc/git/based-and-confused
install -d -m 0700 /scratch/vms/quarterly/candidates
scripts/quarterly-sysprep.sh --check --node qa-rhel9-node1
# Only after approval and reviewing the exact paths, source digest, and operation list:
scripts/quarterly-sysprep.sh --execute --node qa-rhel9-node1 \
  --confirm build-qa-rhel9-node1
```

`--check` reads the build image, libvirt domain definitions and installed operation list, then prints a JSON plan with a source SHA-256; it does not copy or scrub. `--execute` repeats checks, converts the source to a private temporary standalone qcow2, scrubs **that copy**, verifies it, and publishes `/scratch/vms/quarterly/candidates/qa-rhel9-node1-node-candidate.qcow2` without replacing an existing path (the actual path is in the JSON plan). It prints a JSON report with source/candidate SHA-256 and `bootable: false`. Other nodes derive different candidate paths. A failed operation must not leave a final candidate; inspect private leftovers after any host crash. Do not paste private Kickstarts, credentials or guest logs into reports.

## Required gate after the script

The candidate is not a base. For **this experimental generalized-image path only**, designate **two disposable, independently offline-personalized copies** with different static addresses/FQDNs before first boot; do not start a copy of the old build identity. Verify distinct machine IDs and regenerated FIPS-compatible SSH **host** keys between those *different-identity canaries*, original operator public-key access, FIPS/SELinux enforcement, network/GUI where applicable, no carried registration secrets, and real scans. This is **not** a requirement to rotate host keys when a primary-path clone represents the **same node**. Preserve the clean pre-remediation baseline separately from a hardened reference. Only after independent review may an operator publish a new standalone qcow2 to the manifest's base path from private same-filesystem staging with atomic no-overwrite publication, make it read-only and record/recheck its SHA-256 in a protected write-once cycle record. Never copy a running disk or boot a sealed base. **That promotion/canary pipeline is not implemented by this script.** In particular, [the clone personalizer](QUARTERLY-PERSONALIZE.md) operates on a manifest clone *after* base promotion; it does not make this candidate a canary automatically. Do not use the CLI `clone` command until a sealed base has passed the gate. Fixed manifest paths require cycle-specific archival/retention or explicitly approved cleanup before any subsequent cycle; no overwrite is permissible.

## Verification limits

Synthetic qcow2/stub tests check refusal and no-overwrite behavior, not whether the actual installed libguestfs build scrubs these RHEL versions correctly. The real host currently lacks `virt-sysprep`; no real image was processed or canary booted. Tool availability and authorized execution must be verified on the intended hypervisor.
