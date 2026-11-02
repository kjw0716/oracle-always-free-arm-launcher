# Usage guide

[English README](../README.md) · [中文指南](USAGE.zh-CN.md)

## Configure the account

Run `python grab.py --setup`, or choose **1** in the launcher menu. Enter keeps a default; `Ctrl+C` cancels. The wizard supports pasting Oracle's **Configuration file preview** (finish with `.` on a new line), importing `~/.oci/config` with a selected Profile, or manual entry.

| Field | Source |
| --- | --- |
| User, tenancy, fingerprint, region | Oracle user settings → Tokens and keys → API keys → Configuration file preview |
| API private key path | Local RSA PEM key matching the registered public key; at least 2048 bits |
| SSH public key path | A `.pub` key for instance login, separate from the API signing key |
| Subnet | Select an existing public subnet in the root compartment, or enter its OCID |

The wizard checks the signing key's fingerprint and can read availability domains and subnets. Saving configuration does not create resources. Run `--check` before starting.

`config.ini` stores the private key's path, not its contents. For encrypted keys, provide `OCI_KEY_PASSPHRASE` in the process environment. Do not save passwords in the configuration or Git. Importing a profile does not copy its `pass_phrase` field.

References: [Oracle signing keys](https://docs.oracle.com/en-us/iaas/Content/API/Concepts/apisigningkey.htm), [OCI configuration fields](https://docs.oracle.com/en-us/iaas/Content/API/Concepts/sdkconfig.htm).

## Timing

```ini
[retry]
interval_minutes = 2.5
max_minutes = 120
jitter_seconds = 30
max_backoff_seconds = 900
check_capacity_first = false
```

| Setting | Meaning |
| --- | --- |
| `interval_minutes` | Wait after each round; 1–60 minutes, including decimals |
| `max_minutes` | Duration of this run; 0 is unlimited, otherwise at least 1 minute |
| `jitter_seconds` | Add 0 to this many seconds randomly; default 30, set 0 to disable |
| `max_backoff_seconds` | Throttling/transient-failure backoff ceiling before jitter, never below the normal interval |
| `check_capacity_first` | Optional capacity report before launch; default false |

API calls take time. Throttling and transient failures extend the wait. The duration limit and stop command wait for the current round to return and preserve its state. They do not delete accepted instances. Each new run starts a new duration budget.

Legacy `interval_seconds` remains supported; `interval_minutes` takes precedence when both exist. Menu **6** changes timing without repeating credential setup. Stop before editing. Timing edits preserve the state fingerprint; changing the account or target instance may conflict with saved state and will be rejected.

## Commands

Install dependencies with `python -m pip install -r requirements.txt`, or use the launcher-managed `.venv`.

```sh
python grab.py --menu
python grab.py --setup
python grab.py --check
python grab.py --resume --interval-minutes 3 --max-minutes 180
python grab.py --status
python grab.py --stop
```

Replace `python grab.py` with `start.cmd` or `sh start.sh` to use the launcher environment.

- `--config PATH` selects another config; its parent directory holds runtime state.
- `--resume` clears a stop marker. Without it, an existing stop request is respected.
- `--once` runs at most one round. **It can create an instance.**
- Timing flags override this run without rewriting configuration.

Exit codes: `0` for completion, validation or orderly stop; `2` for errors requiring attention; `3` for a single round without completion; `130` for keyboard interruption. Exit code 0 alone does not mean an instance was created: inspect `state.json` or the status output.

## Background operation

Complete setup and read-only validation first. Keep the host running and awake.

Linux/macOS:

```sh
mkdir -p runtime
nohup .venv/bin/python grab.py --resume > runtime/console.log 2>&1 &
```

Windows PowerShell, in the project directory after the launcher has installed dependencies:

```powershell
Start-Process -FilePath "$PWD\.venv\Scripts\python.exe" -ArgumentList 'grab.py','--resume' -WorkingDirectory $PWD -WindowStyle Hidden
```

Use another terminal for `--status` or `--stop`. This is a normal Python process; it does not install automatic restart. For persistent hosting, use your operating system's service manager. The included GitHub Actions workflow only runs tests.

## Status and recovery

| File | Contents |
| --- | --- |
| `runtime/grab.log` | Rotating log of outcomes and waits |
| `runtime/run.json` | Process activity, update time and next expected attempt |
| `runtime/state.json` | Attempts, pending request/token, instance ID and public IP |
| `runtime/needs-attention.json` | Error requiring account or configuration attention |

`--status` checks the OS lock rather than trusting a stale PID or old log line. After interruption, start again to reconcile cloud state and reuse unconfirmed request tokens. **Do not delete `state.json` to bypass errors.** Requests approaching token expiry stop for manual reconciliation.

Only one process can use a configuration directory at a time. The lock cannot coordinate different machines or separate folders; do not run multiple independent launchers against the same account.

## Scope and limits

- The selected region must be the tenancy's home region. One availability domain is selected; there is no automatic rotation.
- A1 only: default 2 OCPU / 12 GB, or 1 OCPU / 6 GB in the wizard. Boot disk defaults to 50 GB; configuration accepts 47–50 GB.
- Uses a compatible official Oracle Linux 9 platform image. SSH user is normally `opc`.
- Uses an existing root-compartment public subnet, without creating networks or changing security rules. Public subnet flags alone do not guarantee SSH connectivity; routes and security rules still apply.
- Checks regional instances and disk usage across compartments. Existing other A1 instances, total disks exceeding 200 GB after creation, failed queries or insufficient permissions stop the task.
- Does not upgrade accounts or delete resources. A capacity report is not a reservation; creation can still fail after an available report.

[Oracle's Always Free documentation](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm), checked on 2026-11-02, describes 1,500 OCPU hours and 9,000 GB hours monthly for A1, equivalent to 2 OCPU / 12 GB for Always Free tenancies. Account terms and actual billing take precedence. Checks do not cover previously consumed monthly hours, other programs creating resources concurrently or future pricing changes. Availability is not guaranteed.

## Development and releases

```sh
python -m unittest discover -s tests -v
python tools/build_release.py
```

The release is `dist/oci-a1-grabber.zip`. An explicit allowlist includes source, docs, images, blank example config and tests. The builder scans for accidental real OCIDs and private-key material. `.gitignore` excludes local configs, common key files, runtime data and virtual environments.

Publish the source release, not a personal working directory. Documentation images are previews rendered from actual CLI output with demonstration inputs, without account data.

CI covers Windows/Ubuntu and Python 3.10/3.13 using mocked APIs; it does not use real credentials or launch instances. [License](../LICENSE) · [Distribution notices](../NOTICE.txt).
