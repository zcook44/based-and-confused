# Ansible prerequisites in fresh-install Kickstarts

These source changes affect **future installations only**. Existing VMs and the already-built menu ISO are not modified by a source edit. The private RHEL 7 `/scratch/vms/*.ks` files were separately regenerated for the two nodes after the sudo repair; regenerate any other private Kickstart before using it for a new installation. Rebuild the menu ISO to embed updated 8/9/10 templates when a future replacement is deliberately scheduled; building it here would change the currently deployed lab artifact.

**Navigation:** [Installer lab](README.md) · [Menu-ISO quickstart](QUICKSTART.md) · [Direct-DVD RHEL 7 nodes](RHEL7-NODES.md) · [Separate quarterly workflow](QUARTERLY-WORKFLOW.md). Package selection is a prerequisite, not a successful playbook in formalwear.

The authoritative Kickstart sources are `lab.py` (single-DVD RHEL 8/9/10), `menu_lab.py` (eight-node menu), `rhel7_lab.py` (two RHEL 7 DVD nodes), `quarterly/vm.py` (eight clean quarterly bases), and the checked-in reference templates `data/ks/9.ks` and `data/ks/10.ks`. The eight ignored `data/menu-ks/*.ks` snapshots also had their package stanzas refreshed locally, but are not source-of-truth or embedded into the existing ISO; a fresh clone obtains the generator, not those ignored files.

## Fresh-install package map

| Fresh target | Explicit installed interpreter | Ansible setting | Other explicit packages |
| --- | --- | --- | --- |
| RHEL 8.10 | `python3.11` (`/usr/bin/python3.11`) | `ansible_python_interpreter=/usr/bin/python3.11` | `openssh-server`, `sudo`, `shadow-utils` |
| RHEL 9.8 | `python3` (3.9) | `/usr/bin/python3` | Above, plus `python3-dnf`, `python3-libselinux` |
| RHEL 10.2 | `python3` (3.12) | `/usr/bin/python3` | Above, plus `python3-dnf`, `python3-libselinux` |
| RHEL 7.9 | `python3` (3.6) | **Not compatible with ansible-core 2.21** | `openssh-server`, `sudo`, `shadow-utils` |

The menu's RHEL 9/10 controller roles additionally install `openssh-clients` and `ansible-core`. The quarterly controller roles install `openssh-clients`; the quarterly build is a clean target image, not an installed Ansible controller. All roles retain their existing SSH access, FIPS/SELinux and STIG or clean-baseline semantics. These package names were checked against the local full DVDs and the extracted RHEL 8/9 installation trees.

## Compatibility is a separate gate

**Compatibility boundaries:** Ansible's [support matrix](https://docs.ansible.com/projects/ansible/latest/reference_appendices/release_and_maintenance.html#ansible-core-support-matrix) lists managed-node Python 3.9–3.14 for ansible-core 2.21. RHEL 8.10's DVD has `python3.11` but no standalone `python3` RPM; do not point Ansible at `/usr/libexec/platform-python` (3.6), or change the system Python symlink. Red Hat's [RHEL 8 Python guidance](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/8/html/configuring_basic_system_settings/installing-and-using-dynamic-programming-languages_configuring-basic-system-settings) says the 3.11 suite lacks the system DNF/RPM/SELinux bindings of the base 3.6 environment. Thus Python 3.11 can run core Ansible modules, but the DVD alone does **not** make arbitrary RHEL 8 DNF/SELinux modules work with that interpreter; validate the intended collections and arrange approved bindings/content before claiming those tasks work. RHEL 7's DVD includes Python 2.7 and a Python 3 package providing 3.6; adding 3.6 does **not** make modern ansible-core 2.21 modules compatible. The current guests have Python 2.7, and both normal and become `ping` have passed through the dedicated ansible-core 2.16.20 Docker EE in `/scratch/ansible`. Use that isolated, risk-reviewed controller for these guests and test each intended module; future installs need a fresh interpreter/controller compatibility check. Do not silently downgrade the workstation controller or weaken FIPS/SELinux to work around this.

For a **new disposable** guest, verify the installed interpreter and basic connection with an inventory setting matching the table, then test the exact playbook modules to be used. `ksvalidator` and RPM presence checks are source validation, not a new-VM boot test or proof of a functioning Ansible playbook.

For `rhel8-node2`, a planned or ongoing rebuild cannot be counted as completed merely because the source now requests `python3.11`. Verify its actual install, GUI, FIPS/SELinux, SSH and chosen interpreter, then run representative modules against **that rebuilt guest**. Do not silently transfer an older `node1` result, or assume a source edit updated a previously built ISO.
