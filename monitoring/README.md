# Monitoring your own node

Alert rules and a Grafana dashboard for one Seismic node, to load into a
Prometheus and Grafana you run. They cover every series
[`docs/METRICS.md`](../docs/METRICS.md) describes, with the healthy ranges
stated there as thresholds.

| File                           | What                                                                              |
| ------------------------------ | --------------------------------------------------------------------------------- |
| `alerts/seismic-node.yml`      | Prometheus alert rules, one `severity` label each (`critical`, `warning`, `none`) |
| `alerts/seismic-node.test.yml` | `promtool` unit tests for the rules                                               |
| `dashboards/seismic-node.json` | Grafana dashboard, filtered by a `node` variable                                  |

These are the per-node rules Seismic operations run centrally, minus the fleet
layer: Seismic pages when at least K validators are affected at once, which only
a fleet view can ask. For your node every signal is the whole story, so each
alert fires on its own, and where it goes (page, chat, mail) is your
Alertmanager's routing decision; the rules carry no routing labels. Seismic
monitors validators only, so for an observer these files are the only alerts
there are.

## Where the samples come from

The rules and dashboard select on the labels the node's Prometheus Agent stamps
on every sample (runbook [3.12](../docs/RUNBOOK.md#312-monitoring)): `job` is
`summit/<node>`, `reth/<node>`, `custodian/<node>`, `checkpointer/<node>` or
`prometheus-agent/<node>`, and `node` is `MONITORING_NODE_NAME`.

- `MONITORING_MODE=push` with `MONITORING_OPERATOR_REMOTE_WRITE_URL` set (the
  default for a validator that wants its own view; the only option for an
  observer that runs the agent): your Prometheus receives the samples with those
  labels already on them. Nothing to configure beyond
  `--web.enable-remote-write-receiver`.
- `MONITORING_MODE=off` with your own scraper against the loopback endpoints
  (over an SSH tunnel, a WireGuard link, or a scraper on the node itself):
  assign the same labels yourself, or the rules will not match. One job per
  service:

  ```yaml
  scrape_configs:
    - job_name: summit/node-0
      metrics_path: / # Summit and Reth serve on /, not /metrics
      static_configs:
        - targets: ["127.0.0.1:9090"]
          labels: { node: node-0, instance: node-0, role: validator }
    - job_name: reth/node-0
      metrics_path: /
      static_configs:
        - targets: ["127.0.0.1:9001"]
          labels: { node: node-0, instance: node-0, role: validator }
    - job_name: custodian/node-0
      metrics_path: /metrics
      static_configs:
        - targets: ["127.0.0.1:7876"] # CUSTODIAN_COUNCIL_PORT
          labels: { node: node-0, instance: node-0, role: validator }
    - job_name: checkpointer/node-0 # only with CHECKPOINTER_ENABLED=true
      metrics_path: /metrics
      static_configs:
        - targets: ["127.0.0.1:42069"]
          labels: { node: node-0, instance: node-0, role: validator }
  ```

  The two `Agent…` alerts and the dashboard's last row need the agent's own
  series and stay empty in this mode.

## Loading the rules

```yaml
# prometheus.yml
rule_files:
  - /etc/prometheus/rules/seismic-node.yml
```

```bash
promtool check rules monitoring/alerts/seismic-node.yml
(cd monitoring/alerts && promtool test rules seismic-node.test.yml)
```

The thresholds are the central defaults: scrape failures after 2m, no block or
no head movement for 2m (about 60 leader timeouts at
`leader_timeout_ms = 2000`), checkpoints older than 1h, disk below 15% on
filesystems over 8 GiB for 15m, no execution peers for 5m, remote-write lag over
2m for 5m. Edit them in place if your network's parameters differ; the unit
tests state the expected behaviour at the defaults.

`Watchdog` always fires. Route it to a dead man's switch (for example
healthchecks.io) so you learn when the alerting pipeline itself stops.

### A node that stops sending

The `…Down` alerts need the agent to deliver a failed scrape (`up == 0`). In
push mode a dead host, a stopped agent, or lost remote-write connectivity
delivers nothing, and after a few minutes those alerts simply have no series to
evaluate. `NodeTelemetryMissing` covers that case: it fires when a stream that
has been arriving stops for 2m, continuously for 2m, the same thresholds Seismic
uses centrally.

Seismic renders its expected streams from a node inventory. These rules have no
inventory to render from, so `monitoring:expected_push_stream` builds its own:
every `summit/<node>`, `reth/<node>`, `custodian/<node>` and
`prometheus-agent/<node>` stream joins on its first sample and stays because the
rule re-reads its own last value. The checkpointer is not expected, since it is
optional and can be switched off later. Consequences:

- Nothing to configure for a new node; it is expected from its first sample.
- A retired node keeps alerting until you delete its
  `monitoring:expected_push_stream{node="<node>"}` series (Prometheus admin API,
  `--web.enable-admin-api`) or keep Prometheus stopped for 5m; nodes that are
  still live re-register on their next sample.
- With your own scraper instead of push mode (`MONITORING_MODE=off` above), a
  target you remove from `scrape_configs` is reported missing in the same way
  until retired.

## Loading the dashboard

Import `dashboards/seismic-node.json` in Grafana (Dashboards → New → Import) and
pick your Prometheus when asked; the dashboard's `datasource` variable holds the
choice. Or provision it:

```yaml
# /etc/grafana/provisioning/dashboards/seismic.yml
apiVersion: 1
providers:
  - name: seismic
    folder: Seismic
    type: file
    options:
      path: /var/lib/grafana/dashboards
```

with the file copied to `/var/lib/grafana/dashboards/`; the variable then
defaults to the first Prometheus datasource. The `node` variable lists every
`node` label the datasource has seen; with several nodes reporting to the same
Prometheus, select one or all.

Each panel's description names the alert that covers it and the healthy range
from `METRICS.md`. The dashboard shows the series an operator acts on; Summit's
Commonware engine internals (`summit_cw_node_…`) are not on it.

## Keeping these in step with Seismic's rules

The per-node expressions here are the same as in Seismic's central rules
(`deploy/monitoring/templates/alert_rules.yml`), with that file's configuration
placeholders replaced by the defaults above. A test in the `deploy` repository
compares the two files when both checkouts are present, so a threshold change on
one side is caught on the other.
