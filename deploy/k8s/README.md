# Kubernetes: a weekly report that waits for a human

```
kubectl kustomize deploy/k8s            # render (CI does this on every push)
kubectl apply -k deploy/k8s             # namespace, PVC, ConfigMap, CronJob, approver
```

Rendered in CI on every push, and **verified once on a local kind cluster (30 Sep 2026)**: the
CronJob was triggered by hand, logged `PAUSED thread=sales_mbr-… (publish?)`, the approve ran from the
approver pod as `REPORT_APPROVER=hemant`, `/data/reports/sales_mbr/2026-09-30.md` appeared on the PVC,
and `--list` showed one `approved by hemant` audit row. It has not run on a shared or managed cluster. The image
is `agentic-reporting:0.1.0` built from the root `Dockerfile`; push it to your registry and
change the `image:` lines (or add a kustomize `images:` override).

## What runs

| Object | What it does |
|---|---|
| `CronJob agentic-reporting-sales-mbr` | Mondays 06:00: runs `specs/sales_mbr.yaml` up to the human gate, logs `PAUSED thread=<t> (publish?)` with a preview, exits 0. |
| `Deployment agentic-reporting-approver` | One idle pod (`sleep infinity`) on the same volume. Humans `kubectl exec` into it to list and answer paused runs. No Service, no port. |
| `PersistentVolumeClaim agentic-reporting-data` | `/data/state/checkpoints.sqlite` (run state + `audit` table) and `/data/reports/`. |
| `ConfigMap agentic-reporting-config` | Provider choice, Ollama host, approver default. |
| `secret.example.yaml` | An example only. Not applied. Create the real Secret out of band. |

Both pods: non-root (uid 10001), read-only root filesystem, all capabilities dropped,
`RuntimeDefault` seccomp, CPU and memory limits.

## Approving a run

```
kubectl -n agentic-reporting logs job/<job-name>              # shows PAUSED thread=... and the preview
kubectl -n agentic-reporting exec deploy/agentic-reporting-approver -- \
  python -m agentic_reporting.run --list                      # audit rows + open threads
kubectl -n agentic-reporting exec deploy/agentic-reporting-approver -- \
  env REPORT_APPROVER=alice python -m agentic_reporting.run --resume --thread <t> --decision approve
```

The approve step renders and writes `/data/reports/sales_mbr/<YYYY-MM-DD>.md` and one audit
row with `decided_by=alice`. `--decision reject` writes the audit row and no file.

## The trade-off: unattended vs gated

An unattended job and a human gate pull in opposite directions. This deployment picks the gate.

* **Why not `--no-gate` in the CronJob?** It would auto-publish text no person has read.
  The checker stops invented numbers, but it does not judge tone, emphasis or a misleading
  sentence built from true numbers. That judgement is the reviewer's job.
* **What it costs.** A report is not "done" when the CronJob finishes. It is done when
  someone approves it. Unanswered runs stay paused in the checkpoint file indefinitely and
  show up under "open threads" in `--list`.
* **What is missing for production.** No notification when a run pauses (wire the job log
  to Slack/Teams/email). No auth or RBAC on who may approve beyond Kubernetes
  `pods/exec` permission; `REPORT_APPROVER` is self-declared. A web approval UI with SSO
  would replace the `kubectl exec` step.
* **Storage.** SQLite on a PVC keeps the state portable and inspectable. A multi-node
  cluster needs a ReadWriteMany class (or node pinning) so both pods see the same file. A
  Postgres checkpointer would remove that constraint; it is not implemented here.
* **Data source.** The CronJob reads the synthetic `samples/sales.sqlite` baked into the
  image. For real data, mount a read-only volume (or a snapshot) and pass it with `--db`.
  The pipeline opens it read-only either way.
