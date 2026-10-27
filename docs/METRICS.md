# Seismic node metrics

What a node exports, what each series means, what a healthy node looks like, and
which of these Seismic operations already alert on. Seismic monitors validators
only; observers serve the same series on loopback for the operator's own
pipeline, and the "Seismic alert" column does not apply to them. This is the
reference for reading your own Grafana (runbook
[3.12, Seeing your own metrics](RUNBOOK.md#seeing-your-own-metrics)) and for
writing your own alert rules.

Metric names and label values below are taken from the service sources at the
pinned `*_VERSION` releases. Healthy ranges are stated in network units (epochs,
blocks, leader timeouts) so they hold on any deployment.

## Where the series come from

Every service serves Prometheus text on loopback (runbook 3.12). In
`MONITORING_MODE=push` the agent scrapes them every 15 s and stamps each sample
with:

| Label      | Value                                                                                                |
| ---------- | ---------------------------------------------------------------------------------------------------- |
| `node`     | `MONITORING_NODE_NAME`                                                                               |
| `instance` | `MONITORING_NODE_NAME`                                                                               |
| `role`     | `validator` or `observer`                                                                            |
| `job`      | `summit/<node>`, `reth/<node>`, `custodian/<node>`, `checkpointer/<node>`, `prometheus-agent/<node>` |

In `pull` mode the scraper assigns the labels; Seismic operations use the same
scheme. Queries in this document assume it. To look at a single service without
Prometheus:

```bash
curl -s 127.0.0.1:9090/ | grep -v '^#' | head          # Summit
curl -s 127.0.0.1:9001/ | grep '^reth_'                # Reth
curl -s 127.0.0.1:$CUSTODIAN_COUNCIL_PORT/metrics       # Custodian
curl -s 127.0.0.1:42069/metrics                        # checkpointer, if enabled
```

Summit also exports several hundred Commonware consensus-engine and runtime
series (`summit_cw_node_…`, `runtime_…`). They are engine internals, used by
Seismic's own dashboards, and are not described here.

## Network parameters

Healthy ranges below are expressed in epochs and leader timeouts. The network's
values are in its genesis file; the bounds Summit accepts are from
`types/src/protocol_params.rs` and `types/src/genesis.rs`.

| Parameter           | Accepted by Summit                                              |
| ------------------- | --------------------------------------------------------------- |
| `blocks_per_epoch`  | 10 to 1,814,400 blocks                                          |
| `leader_timeout_ms` | greater than 0 and strictly less than `notarization_timeout_ms` |

## Liveness and progress

| Series                                                                              | Healthy    | Seismic alert                                                                                                                                                             |
| ----------------------------------------------------------------------------------- | ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `up{job=~"summit/.*"}`                                                              | 1          | `SummitDown` after `BLACKBOX_FAILURE_DURATION` (2m); counts toward fleet paging                                                                                           |
| `up{job=~"reth/.*"}`                                                                | 1          | `RethDown`; counts toward fleet paging                                                                                                                                    |
| `up{job=~"custodian/.*"}`                                                           | 1          | `CustodianDown`; counts toward fleet paging                                                                                                                               |
| `up{job=~"checkpointer/.*"}`                                                        | 1          | `CheckpointerDown` (warning)                                                                                                                                              |
| any expected stream absent                                                          | present    | `NodeTelemetryMissing` after 2m + 2m (push mode); the operator rules learn the expected streams from arriving samples, see `monitoring/README.md`                         |
| `summit_blocks_committed_total`                                                     | increasing | `SummitStalled` when flat for `PROGRESS_STALL_WINDOW` (2m); counts toward fleet paging                                                                                    |
| `reth_blockchain_tree_canonical_chain_height`                                       | increasing | `RethStalled` when unchanged for `PROGRESS_STALL_WINDOW`; counts toward fleet paging                                                                                      |
| `reth_seismic_rotation_pending_unfetched_epochs`                                    | 0          | `RethMissingRotationKeys` on any non-zero value; counts toward fleet paging. Absent means Reth is not being scraped, not that no rotation happened                        |
| `custodian_socket_requests_total{response=~"epoch_key_unavailable\|denied\|error"}` | flat       | `CustodianSocketFailures` on any increase within `PROGRESS_STALL_WINDOW`; counts toward fleet paging                                                                      |
| `checkpointer_last_checkpoint_timestamp_seconds`                                    | recent     | `CheckpointerStale` (warning) when older than `CHECKPOINT_MAX_AGE` (1h); `CheckpointerNoCheckpoint` (warning) when a running checkpointer has produced none for that long |
| `custodian_council_requests_total{outcome=~"rejected\|observer_rejected"}`          | flat       | `CustodianCouncilRejections` (warning) on any increase within `PROGRESS_STALL_WINDOW`                                                                                     |

`up == 0` proves the metrics endpoint is unreachable, not that the process is
down; `systemctl status 'seismic-*'` tells the difference. The progress and
key-delivery rules catch the opposite case: a process that answers scrapes and
reports no error while the chain is not moving on this validator, or while it
cannot obtain the purpose keys the chain requires.

## Summit

| Series                                                                                                               | Type            | Meaning                                                                                                                                                                         | Healthy                                                                                                                                                                                          |
| -------------------------------------------------------------------------------------------------------------------- | --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `summit_critical_errors_total{reason}`                                                                               | counter         | Errors requiring immediate attention, by reason (below)                                                                                                                         | Never increases                                                                                                                                                                                  |
| `summit_warning_errors_total{reason}`                                                                                | counter         | Errors not requiring paging                                                                                                                                                     | Rarely increases                                                                                                                                                                                 |
| `summit_blocks_committed_total`                                                                                      | counter         | Blocks the finalizer committed                                                                                                                                                  | Increasing at all times; `increase(summit_blocks_committed_total[1m]) == 0` means this node is not following consensus. The expected rate follows from the network's consensus timing parameters |
| `summit_tx_committed_total`                                                                                          | counter         | Transactions in committed blocks                                                                                                                                                | Tracks network load; zero is fine on a quiet network                                                                                                                                             |
| `summit_finalizer_epochs_completed_total`                                                                            | counter         | Epoch transitions processed **since the process started**                                                                                                                       | Increments once per `blocks_per_epoch` blocks. Not the epoch number: it counts only the transitions this Summit process has seen, so it starts again at 0 whenever the Summit process restarts   |
| `summit_disk_used_bytes{mountpoint}` `summit_disk_total_bytes{mountpoint}` `summit_disk_available_bytes{mountpoint}` | gauge           | Usage of every filesystem on a block device, one series per mount point (Summit reads `/proc/mounts`). The one holding your node state is the one to watch, wherever you put it | `available / total > 0.15`                                                                                                                                                                       |
| `summit_process_cpu_seconds_total` `summit_process_start_time_seconds`                                               | counter / gauge | Process CPU time and start time                                                                                                                                                 | `start_time` only changes on restart; `changes(summit_process_start_time_seconds[1h]) > 0` means a restart happened                                                                              |

### Critical error reasons

| `reason`                                                                                                                                                                | Meaning                                                                                                                 |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `equivocation_notarize` `equivocation_finalize` `nullify_finalize`                                                                                                      | Conflicting votes observed: evidence of double-signing. Pages Seismic immediately regardless of which node reports it   |
| `engine_client_error`                                                                                                                                                   | Reth's Engine API returned an error; Summit cannot execute blocks                                                       |
| `fatal_db_error`                                                                                                                                                        | Summit's database failed                                                                                                |
| `epoch_genesis_unavailable`                                                                                                                                             | The genesis for a new epoch could not be loaded                                                                         |
| `pending_notarized_cap`                                                                                                                                                 | Too many notarized blocks waiting for finalization (backpressure)                                                       |
| `conflicting_finalization`                                                                                                                                              | Two finalizations for the same height                                                                                   |
| `finalized_block_invalid` `finalized_block_non_canonical` `finalized_wrong_parent` `finalized_certificate_mismatch` `finalized_forkchoice_invalid` `invalid_forkchoice` | A finalized block failed a consistency check against Reth or the certificate; the node's view diverges from the network |

Any of these on your node: read `journalctl -u seismic-summit -p err` and the
`critical/` log files under `/persistence/summit` before restarting anything.

## Reth

| Series                                           | Type  | Meaning                                                                                                                          | Healthy                                                                                                                                                                                                                                                                                                                                                          |
| ------------------------------------------------ | ----- | -------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `reth_blockchain_tree_canonical_chain_height`    | gauge | Head block number of the canonical chain                                                                                         | Increasing in step with `summit_blocks_committed_total`; a flat line while Summit commits means Reth is not executing                                                                                                                                                                                                                                            |
| `reth_network_connected_peers`                   | gauge | Execution-layer P2P peers                                                                                                        | ≥ 1; expect roughly the number of other nodes in the network                                                                                                                                                                                                                                                                                                     |
| `reth_network_tracked_peers`                     | gauge | Peers known to the discovery table                                                                                               | ≥ `connected_peers`                                                                                                                                                                                                                                                                                                                                              |
| `reth_sync_execution_gas_per_second`             | gauge | Execution throughput                                                                                                             | Load dependent; a sudden drop to 0 with blocks still committing points at execution                                                                                                                                                                                                                                                                              |
| `reth_seismic_rotation_pending_unfetched_epochs` | gauge | Rotation epochs announced in the on-chain registry whose purpose keys Reth has requested from the Custodian and not yet received | 0. Non-zero for longer than a fetch retry means the Custodian was not delivered that epoch's root key. Reth's rotation watcher sets the gauge every second from startup, rotation or not, so the series exists (at 0) on every scraped node; absent means the scrape is failing or the node is not running v0.1.1 with `--seismic.purpose-keys-source custodian` |

## Custodian

Every node runs one. A validator's Custodian takes part in the council that
delivers the root key for each purpose-key epoch and serves purpose keys to Reth
and to its observers; an observer's fetches the root key from its parent and
serves Reth.

The Custodian's epoch is the **purpose-key rotation epoch**, not Summit's
consensus epoch. Rotations are announced on chain in the key-rotation registry
(`announceRotation(activationBlock)` by the admin; see seismic-reth
`docs/design/purpose-key-rotation.md`); epoch 0 is derived from the local
keyfile, and each announced epoch `n` needs its root key delivered by the
council before Reth executes the first block at or after its activation height.
There is no fixed cadence: on a chain with no announcement every block is
key-epoch 0 while the consensus epoch keeps advancing. The authoritative "is
this node keyed for the chain" signal is therefore on the Reth side,
`reth_seismic_rotation_pending_unfetched_epochs` (see [Reth](#reth)), not a
comparison of epoch numbers.

| Series                                              | Type    | Meaning                                                                                             | Healthy                                                                                                                                                                                                            |
| --------------------------------------------------- | ------- | --------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `custodian_delivered_epoch`                         | gauge   | Highest rotation epoch whose root key this Custodian holds (0 = none delivered; epoch 0 needs none) | Equals the highest epoch announced in the registry once its root key is delivered; advances only when a rotation is announced. `seismic_getKeyEpochInfo` on Reth's RPC reports the active epoch to compare against |
| `custodian_socket_requests_total{request,response}` | counter | Reth's requests over the Unix socket, by request and response kind                                  | Rate of `response` in `epoch_key_unavailable`, `denied`, `error` is 0. Any sustained rate means Reth cannot obtain purpose keys and execution of key-dependent transactions fails                                  |
| `custodian_council_requests_total{outcome}`         | counter | Council HTTP requests, by response kind                                                             | `delivered` ticks once per announced rotation; `rejected` and `observer_rejected` stay at 0. A steady `observer_rejected` rate from a validator means one of its observers has a wrong assignment or key           |
| `custodian_council_http_errors_total{status}`       | counter | Requests refused before parsing (400, 404, 405, 413, 415, 500)                                      | 4xx are noise from the public route; `500` should never increase                                                                                                                                                   |
| `custodian_build_info{version,role}`                | gauge   | Always 1                                                                                            | `role` matches `SEISMIC_ROLE`; `version` matches `CUSTODIAN_VERSION`                                                                                                                                               |

## Checkpointer

Only on nodes with `CHECKPOINTER_ENABLED=true`.

| Series                                              | Type      | Meaning                                                                                       | Healthy                                                                                           |
| --------------------------------------------------- | --------- | --------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `checkpointer_last_checkpoint_epoch`                | gauge     | Consensus epoch (Summit's, `blocks_per_epoch` blocks each) of the newest completed checkpoint | The consensus epoch Summit is in, or that minus one while the current checkpoint is being written |
| `checkpointer_last_checkpoint_block`                | gauge     | Reth block number of the newest checkpoint                                                    | Within one epoch of `reth_blockchain_tree_canonical_chain_height`                                 |
| `checkpointer_last_checkpoint_timestamp_seconds`    | gauge     | Unix time the newest checkpoint completed                                                     | `time() - value` below two epochs of wall-clock time at the network's block rate                  |
| `checkpointer_checkpoints_total{result}`            | counter   | Attempts by `ok` / `error`                                                                    | `increase(...{result="error"}[1h]) == 0`                                                          |
| `checkpointer_checkpoint_duration_seconds`          | histogram | Wall-clock time of successful checkpoint creation                                             | Stable; a steady rise tracks database growth                                                      |
| `checkpointer_monitor_errors_total{kind}`           | counter   | Failures of the loop that watches Summit and Reth (`rpc`, `http`, `other`)                    | Rate 0                                                                                            |
| `checkpointer_snapshot_requests_total{kind,status}` | counter   | Snapshot downloads served (`manifest`, `archive`) by HTTP status                              | 5xx rate 0; 401/403 are clients with bad tokens                                                   |
| `checkpointer_build_info{version}`                  | gauge     | Always 1                                                                                      | `version` matches `CHECKPOINTER_VERSION`                                                          |

## Prometheus Agent (push mode)

Standard Prometheus self-metrics on `127.0.0.1:9091`, forwarded as
`prometheus-agent/<node>`. The ones that matter:

| Series                                                                                                                               | Healthy                                                                                            |
| ------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------- |
| `prometheus_remote_storage_samples_failed_total{url}`                                                                                | Rate 0 per destination                                                                             |
| `prometheus_remote_storage_samples_pending{url}`                                                                                     | Near 0; growing means the destination is unreachable and the WAL is filling (it holds at most 6 h) |
| `prometheus_remote_storage_highest_timestamp_in_seconds` minus `prometheus_remote_storage_queue_highest_sent_timestamp_seconds{url}` | A few seconds; this is the delivery lag per destination                                            |

## What Seismic operations watch

Seismic's central rules cover the `up` heartbeats, Summit committing blocks and
Reth's head advancing, purpose-key delivery (Reth missing an announced rotation
epoch, Custodian socket failures), Summit critical and warning errors, disk
space, checkpointer staleness, Custodian council rejections, and missing push
telemetry, and page on Byzantine faults or when at least K validators are
affected at once by any of the critical ones. The disk rule covers every
filesystem Summit reports, wherever the node state lives.

The per-node half of those rules, with the fleet layer removed and the healthy
ranges above as thresholds, is in
[`monitoring/alerts/seismic-node.yml`](../monitoring/alerts/seismic-node.yml)
for your own Prometheus, with a matching Grafana dashboard in
`monitoring/dashboards/`; [`monitoring/README.md`](../monitoring/README.md)
explains how to load them and which labels they expect.

## Items to confirm against a running network

- `reth_seismic_rotation_pending_unfetched_epochs` has not been observed yet
  because the internal testnet is not scraped. Reth v0.1.1 creates it at startup
  (`fetch_missing_epochs` sets the gauge on every one-second poll, with or
  without announced rotations) under the global `reth` prefix, so it should
  appear at 0 on the first scrape of a v0.1.1 node. The behaviour (going
  non-zero when a Custodian lacks an announced epoch's root key) is best
  confirmed by announcing a rotation on a devnet.
- The internal testnet validators still run Custodian 0.1.0 and a checkpointer
  without `/metrics`; both endpoints appear with the `v0.1.1` releases pinned in
  `node.env.example`, and until a node runs them its `custodian/<node>` and
  `checkpointer/<node>` scrapes fail (`CustodianDown`, `CheckpointerDown`,
  `NodeTelemetryMissing`).
