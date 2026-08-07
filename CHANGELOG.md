# Changelog

## 0.4.0

Additive: every existing call signature still works, all new parameters default to
the previous behaviour, and the two new error classes subclass `RuntimeError` so
existing `except RuntimeError` handlers keep working.

**This is also the first release with a test suite** — 188 tests where there were
none. The 0.3.2 release was a one-line fix for a `NameError` on `destroy_workflow`'s
happy path that shipped in 0.3.0 and went undetected for two tags; that class of
defect is now caught.

### Added — connection lifecycle

- `disconnect(si)`, `close_connection(conn)`, and `Connection.close()`. There was
  previously **no way to close a connection at all**.
- `Connection` is a context manager.
- `open_connection(..., register_atexit=False)` opts out of the process-exit hook.
  With it on, `atexit.register(Disconnect, si)` retained every session ever opened
  for the process lifetime — a long-lived process opening one connection per batch
  hits the host's per-host session cap and keeps credentials in memory.
  `disconnect` unregisters exactly its own hook (a shared callable would make
  closing one session strip every other session's).

### Added — bulk

- `plan_capacity(content, datastore, specs, ...) -> CapacityPlan` sums **all N**
  VMDKs against free space once and reports CPU/RAM headroom.
  `validate_disk_usage` is per-call TOCTOU: N concurrent clones each see the same
  free space, each individually "fits", and collectively they fill the datastore.
  Nothing read host CPU/RAM at all before this, so overcommit was unchecked.
  Distinct bases are measured once each. `validate_disk_usage` is unchanged.
- Discovery: `list_datastores`, `list_networks`, `list_vms`, `list_base_vms`,
  `get_host_capacity`. Nothing could be enumerated before — a caller could only
  fetch a datastore by exact name, so a typo became a mid-clone failure.
- `ProgressEvent.context` and `progress_context=` on every workflow. The progress
  key was derived from the step label alone, so `task:vmdk-copy` was byte-identical
  for every VM in a batch and consumers could not demultiplex concurrent clones.
  The context is also prefixed onto `key`, so consumers that group by `key` alone
  stay correct.
- Overall workflow progress: a `workflow:clone` step counter with `unit="steps"`.
  Consumers previously hardcoded vmkit's internal step count and derived the total
  from their own request flags, so any change to the step sequence silently broke
  their progress bar.
- `wait_for_task(..., timeout_s=, should_cancel=)`, issuing `CancelTask` and raising
  `TaskTimeoutError` / `TaskCancelledError`; threaded through all three workflows.
  The wait loop was previously unbounded with no cancellation, so a hung
  `CopyVirtualDisk_Task` held a worker slot forever.
- `cleanup_partial_clone(conn, name, datastore)`. If `register_vm` failed after the
  VMDK/NVRAM/ISO/VMX were uploaded, the datastore directory and its contents were
  orphaned with no rollback. Refuses to run against a registered VM.
- `random_mac(existing=None, attempts=100)` avoids addresses already in use and
  raises rather than returning a known duplicate. Only 22 bits are random, so
  collisions are not remote at batch scale.
- `used_macs=` on `clone_workflow`.

### Fixed

- **`ethernet0.networkName` was hardcoded to `"VM Network"`.** On any target
  without that port group the VM registered with a dead NIC — silent, and only
  discovered when first boot never phoned home. `render_vmx` and both workflows now
  take `network=` (default `"VM Network"`, so behaviour is unchanged).
  `update_workflow` defaults to preserving the VM's *current* port group, read from
  its VMX, so an update never silently moves a VM.
- **A raising progress callback aborted the operation it was reporting on.** The
  callback is now invoked inside try/except and logged. A network blip in a
  consumer's event publisher could previously kill a clone mid-flight.
- `wait_for_task` reads `task.info` **once** per poll instead of two or three
  times. Each read is a live SOAP fetch, and reading `.state` and `.progress`
  separately could observe two different snapshots of the same task.
- `urllib3.disable_warnings()` no longer runs as an import-time side effect in
  `datastore.py`, where it silenced insecure-request warnings for the entire host
  application. It now runs only when a request is actually made unverified.
- `Connection.password` is excluded from the dataclass repr, keeping the ESXi
  password out of tracebacks and frame-locals captures.

### Added — errors

- `TaskFailedError(VmkitError, RuntimeError)` from `wait_for_task`, carrying
  `.label` and `.fault`. **The most common real failure — a failed vSphere task —
  previously escaped the typed hierarchy entirely as a bare `RuntimeError`.**
- `NotFoundError(VmkitError, RuntimeError)`, with `VmNotFoundError`,
  `DatastoreNotFoundError` and `DatacenterNotFoundError` under it.
- `ConnectionFailedError` subclasses `HostUnreachableError`, `TlsError` and
  `PermissionDeniedError`. `connect` previously caught bare `Exception`, collapsing
  everything non-login into one type — which is why consumers walked the
  `__cause__`/`__context__` chain by hand looking for `vim.fault.NoPermission`.
  vmkit now walks it for them.
- `TaskTimeoutError`, `TaskCancelledError`.

### Changed

- Loggers are `logging.getLogger(__name__)` per module, not a single hardcoded
  `"deploy-vm"`. `logging.getLogger("vmkit")` now catches everything and callers can
  route by module. `setup_logging` configures the `vmkit` package logger and is
  documented as CLI-only — it adds handlers unconditionally and `mkdir`s a relative
  `logs/`, so a server must not call it.
- `verify_tls=` on `connect` / `open_connection` and the datastore transfers,
  defaulting to `False` (unchanged behaviour).
- `urllib3` is declared as a dependency. It was imported directly by
  `datastore.py` but present only transitively via `requests`.
- `pytest` is declared in `[dependency-groups] dev`, with `pythonpath` configured.

### Documented, not fixed

`get_datacenter` returns the first datacenter and `register_vm` picks the first
host and its resource pool. Correct and forced on standalone ESXi;
**nondeterministic against vCenter**. Targeting vCenter needs explicit
datacenter/host/pool parameters, which is a larger change than everything else
here combined.

## 0.3.2

- Fix a `NameError` on `destroy_workflow`'s happy path (missing `wait_for_task`
  import), shipped in 0.3.0 and undetected for two tags.

## 0.3.1

- Tolerate concurrently-deleted VMs during inventory enumeration.

## 0.3.0

- Guest-OS detection from the base VMX; `update_workflow`; `destroy_workflow`.
