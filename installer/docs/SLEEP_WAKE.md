# Sleep and wake

Every installation gets two AWS Step Functions state machines: **`<name>-sleep`** puts the environment to sleep and **`<name>-wake`**
wakes it up. While it sleeps, its EC2 nodes and its Aurora database are stopped, so they cost nothing; DIAL is unavailable until it is woken up.
Optionally, EventBridge Scheduler runs them on a schedule (for example: sleep at 19:30, wake at 08:40, Sunday to Thursday).

```
./dial-install sleep     # put it to sleep now and wait (up to about an hour)
./dial-install wake      # wake it up now and wait until every service answers
```

You can also start the state machines yourself in the AWS console (Step Functions > `<name>-sleep` or `<name>-wake` > Start execution).

## The schedule

`./dial-install init` asks whether to sleep and wake automatically, and if so the times, the days and the time zone. In `dial.yaml`:

```
power:
  schedule:
    sleep: "19:30"            # HH:MM
    wake: "08:40"
    days: SUN-THU             # cron style: SUN-THU, MON-FRI, MON,WED,FRI   (default SUN-THU)
    timezone: Asia/Jerusalem  # IANA time zone                              (default Asia/Jerusalem)
```

Without `schedule` the state machines exist and are started only by hand. To change or remove the schedule, edit `dial.yaml` and run
`./dial-install apply` (or `upgrade`). `power.enabled: false` creates no sleep / wake control at all.

## What sleeping and waking do

Sleep: stops Knative and the Cluster Autoscaler, cordons and empties every node group (the platform's and those of add-ons) and scales it to
zero, then stops the Aurora database. It refuses to sleep while something it does not know runs in the cluster (for example a workload you
added by hand in another namespace): a sleeping cluster would silently stop it.

Wake: starts the database, brings the node groups back to their size (system nodes first), restores the Cluster Autoscaler and Knative,
and finishes only when every Deployment and StatefulSet is ready and every public address answers through the load balancer
(`core`, `chat`, `admin`, `themes`, `flows`, `flows-core`, and `superset` and `grafana` with analytics). A lock (DynamoDB) makes sure
two executions never act at the same time; a newer request supersedes an older one.

Still billed while asleep: ElastiCache Serverless, the load balancer, the NAT gateway, the EKS control plane and storage.
Note: AWS starts a stopped Aurora cluster again by itself after seven days.

## How it is built

Two CloudFormation stacks per installation, created at the end of `apply` and refreshed by `upgrade` and by every add-on change:

- `<name>-sleep-wake-artifacts`: a private S3 bucket with the worker code, the state machine definition and the environment configuration.
- `<name>-sleep-wake`: the worker Lambda (Python, in the cluster's private subnets), the two state machines, the DynamoDB lock table,
  the worker's EKS access entry and, with a schedule, the EventBridge Scheduler group, schedules and dead-letter queue.

The environment configuration (node groups, namespaces, workloads, endpoints) is built from the live installation each time and handed
to the worker with its own refresh protocol, which checks the new configuration while holding the lock. That is why `apply`, `upgrade` and
`add-ons` need the installation to be awake; they say so when it is not.

In the cluster the worker may: read and cordon nodes, read workloads, delete pods when it empties a node, scale the Cluster Autoscaler and
Knative, read the istiod endpoints (RBAC bound to the group `<name>-sleep-wake-controller`).

### Where the worker runs (every kind of network)

One worker, two ways to run it, chosen by `power.lambda_network` (default `auto`):

| Mode | When `auto` picks it | What it needs | Wake checks the public addresses |
|---|---|---|---|
| `vpc` (preferred) | The private subnets can reach AWS: the network created by the installer (NAT), `network.create_vpc_endpoints: true` (the installer adds endpoints for DynamoDB, RDS, Auto Scaling, EKS, S3), or an existing VPC with a default route or with its own endpoints for those services | Nothing more; works with a private cluster API (`kubernetes.public_endpoint: false`) and an internal load balancer | Yes, through the load balancer |
| `public` | Only when the VPC cannot reach AWS and the cluster API is public | `kubernetes.public_endpoint: true` | Only with an internet-facing load balancer open to everyone; otherwise only the workloads in the cluster are checked |

`./dial-install check` says which mode will be used and why, and fails when neither can work (a private cluster API in a VPC without a
way to AWS): then set `network.create_vpc_endpoints: true` or give the private subnets a route to NAT.

## Removal

`./dial-install uninstall` removes both stacks and the bucket. An installation that is asleep can be uninstalled: the database is started
first, because AWS cannot delete a stopped database.
