# vmkit

ESXi/vCenter VM automation library — the reusable core behind the VM-Setup-Scripts CLIs, the
PQC-Playground backend, and bulk provisioning.

Modules:
- `connect`/`disconnect`, inventory lookups, discovery, register, power on/off, task waiting (`esxi`)
- server-side datastore file/disk copy, HTTPS upload/download, size queries (`datastore`)
- VMX template rendering, guest-OS and port-group detection, random VMware MAC (`vmx`)
- input validators for argparse `type=` (`validate`)
- structured progress events + logging helpers (`progress`)
- clone/update/destroy workflows, batch capacity planning, partial-clone cleanup (`workflows`)

Library code takes parameters (including credentials) and raises exceptions; it does not
prompt, print, or call `sys.exit` — those are the caller's concern. Long-running steps
emit `progress.ProgressEvent`s through an optional `progress` callback (pass `None` to stay
silent), so the caller decides how to render them, and the lib ships no renderer or `tqdm`
dependency.

## Multi-target use

Every function takes an explicit `Connection`, so one process can drive as many ESXi hosts as
it likes. In a **long-lived process** the default `register_atexit=True` is wrong: it holds a
process-exit hook on every session ever opened, which runs into the host's per-host session
cap and keeps credentials in memory. Pass `register_atexit=False` and close the connection:

```python
with open_connection(host, user, pw, register_atexit=False) as conn:
    for spec in batch:
        clone_workflow(conn, name=spec.name, ..., progress_context=spec.name)
```

## Bulk provisioning

```python
# Validate the whole batch against the datastore ONCE, before starting.
plan = plan_capacity(conn.content, "datastore1", specs)
plan.raise_if_insufficient()
for warning in plan.warnings:          # CPU/RAM overcommit is reported, not enforced
    log.warning(warning)

# Pre-validate names against the target instead of failing mid-clone.
valid_networks = {n.name for n in list_networks(conn.content)}
valid_bases = {vm.name for vm in list_base_vms(conn.content)}
```

- `plan_capacity` sums **all N** VMDKs against free space once. `validate_disk_usage` answers
  "does one more fit", which N concurrent callers all answer yes to while collectively filling
  the datastore.
- `progress_context=` (typically the VM name) is carried on every `ProgressEvent` and prefixed
  onto its `key`, so concurrent clones can be told apart. Without it, `task:vmdk-copy` is
  byte-identical for every VM in the batch.
- Each workflow also emits an overall `workflow:clone` step counter, so a consumer does not
  have to hardcode vmkit's internal step count.
- `should_cancel=` and `timeout_s=` bound and interrupt each vSphere task — a batch needs a
  stop button, and an unbounded wait means a hung disk copy holds a worker slot forever.
- `cleanup_partial_clone(conn, name, datastore)` removes the datastore directory left behind
  when a clone fails after the uploads but before registration.
- `random_mac(existing)` avoids addresses already assigned; only 22 bits are random, so
  collisions are not remote at batch scale.

## Errors

All errors derive from `VmkitError`. `TaskFailedError` and `NotFoundError` also derive from
`RuntimeError`, which is what the corresponding failures used to raise, so existing
`except RuntimeError` handlers keep working.

| Error | Meaning |
|---|---|
| `AuthenticationError` | Bad username or password |
| `ConnectionFailedError` | Could not connect — see the three subclasses below |
| ├ `HostUnreachableError` | DNS failure, refused connection, timeout |
| ├ `TlsError` | Certificate rejected or protocol mismatch |
| └ `PermissionDeniedError` | Authenticated but not privileged (`vim.fault.NoPermission`) |
| `NotFoundError` | `VmNotFoundError`, `DatastoreNotFoundError`, `DatacenterNotFoundError` |
| `VmExistsError` | Name already in inventory |
| `InsufficientSpaceError` | Would exceed the datastore free-space limit |
| `TaskFailedError` | A vSphere task ended non-success; carries `.label` and `.fault` |
| ├ `TaskTimeoutError` | Exceeded `timeout_s`; cancellation requested (best-effort) |
| `TaskCancelledError` | `should_cancel` returned true — not a failure |

## Security notes

- **The config ISO is a plaintext credential store.** It carries the agent's bearer token and,
  for a domain controller, an Administrator password, and it stays attached for the VM's whole
  life. Call `update_workflow(..., remove_iso=True)` once first boot has completed.
- `verify_tls` defaults to `False` (historical behaviour). urllib3's insecure-request warning
  is now suppressed only on the unverified path, not as an import-time global side effect.
- `Connection.password` is excluded from the dataclass repr.

## Known limitation: vCenter

`get_datacenter` returns the **first** datacenter and `register_vm` picks the **first** host
and that host's resource pool. A standalone ESXi host has exactly one of each, so the choice
is forced and correct. Against vCenter it is arbitrary and nondeterministic — targeting
vCenter needs explicit datacenter/host/pool parameters first.

## Development

```sh
uv sync
uv run pytest
```

Tests use `MagicMock(spec=vim.X)` fakes (see `tests/fakes.py`) rather than a live host: real
pyVmomi managed objects resolve every property through a SOAP stub and cannot be constructed
offline, but a spec'd mock satisfies the `isinstance` checks the library relies on. Enum
values and fault classes come from the real library. No test touches the network.

## Consumers

- `PQC-Playground` (backend) — pinned by tag.
- `VM-Setup-Scripts` — consumes it as a git submodule.

Both pin a tag; see `CHANGELOG.md` before bumping.
