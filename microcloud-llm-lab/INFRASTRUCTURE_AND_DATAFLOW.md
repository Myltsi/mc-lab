# MicroCloud LLM Lab Infrastructure and Dataflow

This document describes the infrastructure and application dataflow implemented by
the Ansible project in this directory. It documents the current setup only; it
does not propose or apply configuration changes.

## 1. Scope and architecture

The lab consists of:

- One Ansible control host, referred to as the controller.
- Four Ubuntu servers forming the MicroCloud/LXD cluster.
- Three LXD containers distributed across the cluster:
  - `ollama`: local large-language-model service.
  - `log-api`: client-facing log-analysis API.
  - `log-db`: Flask API backed by SQLite.

At the application level, the services are connected as follows:

```text
Client
  |
  | HTTP :8080
  v
log-api
  | \
  |  | HTTP :5000
  |  +--------------------> log-db
  |
  | HTTP :11434
  +-----------------------> ollama
```

The container-to-container name resolution and network reachability shown above
are treated as an established prerequisite based on testing in the physical
environment.

## 2. Physical and cluster infrastructure

The Ansible inventory is defined in [inventory.ini](./inventory.ini).

| Inventory host | Address | SSH user | Role |
|---|---:|---|---|
| `mc-node-1` | `192.168.1.11` | `mc` | MicroCloud node and Ansible/LXD controller |
| `mc-node-2` | `192.168.1.12` | `mc` | MicroCloud node |
| `mc-node-3` | `192.168.1.13` | `mc` | MicroCloud node |
| `mc-node-4` | `192.168.1.14` | `mc` | MicroCloud node |

The inventory group `[microcloud]` contains all four nodes. The
`[lxd_controller]` group contains only `mc-node-1`; all deployment playbooks
execute against this host.

Privilege escalation is enabled for the MicroCloud hosts with:

```ini
ansible_become=true
ansible_become_method=sudo
ansible_become_exe=/usr/bin/sudo.ws
```

The Ansible defaults are defined in [ansible.cfg](./ansible.cfg). The project
uses [inventory.ini](./inventory.ini) as its inventory and loads roles from the
local `roles` directory. SSH host-key checking is disabled.

## 3. LXD container placement

The intended container definitions are in
[group_vars/all.yml](./group_vars/all.yml):

| Container | Intended target | Image | Main service |
|---|---|---|---|
| `ollama` | `mc1` | `ubuntu:24.04` | Ollama API on port `11434` |
| `log-api` | `mc2` | `ubuntu:24.04` | Flask API on port `8080` |
| `log-db` | `mc3` | `ubuntu:24.04` | Flask/SQLite API on port `5000` |

The container role is implemented in
[roles/lxd_containers/tasks/main.yml](./roles/lxd_containers/tasks/main.yml).
It runs on the LXD controller and:

1. Lists existing LXD instances.
2. Launches each missing container with `lxc launch`.
3. Uses `--target` to place a container on its selected cluster member.
4. Waits for each container to be running and to have an IPv4 address.
5. Prints the resulting LXD instance list.

The values `mc1`, `mc2`, and `mc3` are LXD target names configured by the
project. They are separate from the Ansible inventory names `mc-node-1`,
`mc-node-2`, and `mc-node-3`.

## 4. Deployment and operational playbooks

The playbooks are executed from the project directory using the inventory and
defaults in [ansible.cfg](./ansible.cfg).

### Cluster observation

[playbooks/check_cluster.yml](./playbooks/check_cluster.yml) runs on the LXD
controller and displays:

- LXD cluster members using `lxc cluster list`.
- LXD storage pools using `lxc storage list`.
- LXD networks using `lxc network list`.

This playbook reports the current state and does not modify the cluster.

### Deployment order

The application deployment is split into four playbooks:

```text
playbooks/deploy_containers.yml
        |
        +--> playbooks/deploy_ollama.yml
        |
        +--> playbooks/deploy_log_db.yml
        |
        +--> playbooks/deploy_log_api.yml
```

The practical order is:

1. `deploy_containers.yml`
2. `deploy_ollama.yml`
3. `deploy_log_db.yml`
4. `deploy_log_api.yml`

The application playbooks assume that the required containers already exist.

### Teardown

[playbooks/teardown.yml](./playbooks/teardown.yml) removes the lab containers:

```text
log-api
log-db
ollama
```

It lists existing instances, deletes matching containers with `lxc delete
--force`, lists the remaining instances, and verifies that the selected
containers are gone. Forced deletion permanently removes data stored inside
those containers.

## 5. Service responsibilities

### Ollama

The role in
[roles/ollama/tasks/main.yml](./roles/ollama/tasks/main.yml):

1. Updates the container package index.
2. Installs `curl`, CA certificates, and GnuPG.
3. Installs Ollama using the upstream installation script.
4. Configures Ollama to listen on `0.0.0.0:11434`.
5. Restarts the Ollama systemd service.
6. Waits for the local Ollama API to respond.
7. Pulls the configured model, currently `qwen2.5:0.5b`.
8. Displays the installed model list.

The model is used by `log-api` to generate a natural-language analysis of a
submitted log line.

### Log database service

The role in
[roles/log_db/tasks/main.yml](./roles/log_db/tasks/main.yml) deploys a Flask
application inside `log-db`.

The service:

- Runs on `0.0.0.0:5000`.
- Stores records in `/opt/log-db/logs.db`.
- Uses SQLite rather than PostgreSQL.
- Starts through the `log-db.service` systemd unit.
- Restarts automatically through systemd if the process exits.

Endpoints:

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/health` | Returns service health |
| `POST` | `/records` | Stores an analysis record |
| `GET` | `/records` | Returns recent analysis records |

These endpoints are internal to the MicroCloud. Clients should use the
`log-api` address instead of connecting to `log-db` directly.

Each stored record contains an ID, creation timestamp, source, original log
line, generated analysis, and severity.

### Log analysis API

The role in
[roles/log_api/tasks/main.yml](./roles/log_api/tasks/main.yml) deploys a Flask
application inside `log-api`.

The service:

- Runs on `0.0.0.0:8080`.
- Starts through the `log-api.service` systemd unit.
- Uses `ollama` at `http://ollama:11434`.
- Uses `log-db` at `http://log-db:5000`.
- Uses model `qwen2.5:0.5b`.

Endpoints:

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/health` | Returns service and dependency configuration |
| `POST` | `/analyze` | Classifies, analyzes, and attempts to store a log |
| `GET` | `/records` | Proxies record retrieval from `log-db` |

When the MicroCloud is established, this is the client-facing service. All
client requests use the `log-api` address and port `8080`:

| Method | Client URL | Purpose |
|---|---|---|
| `GET` | `http://<log-api-address>:8080/health` | Returns service and dependency configuration |
| `POST` | `http://<log-api-address>:8080/analyze` | Analyzes and attempts to store a log |
| `GET` | `http://<log-api-address>:8080/records` | Returns records through `log-api` |

The `ollama:11434` and `log-db:5000` addresses are internal service addresses
used by `log-api`; they are not required by the client.

Before calling Ollama, the API performs a local severity classification:

- `high`: matches patterns such as root login, sudo, segfault, kernel panic,
  privilege escalation, or authentication failure.
- `medium`: matches patterns such as failed password, invalid user, error,
  denied, unauthorized, or warning.
- `low`: no configured high- or medium-severity pattern matches.

## 6. End-to-end dataflow

### Log analysis request

1. A client sends a `POST /analyze` request to
  `http://<log-api-address>:8080`.
2. `log-api` reads the `log_line` and optional `source` fields.
3. `log-api` performs local severity classification.
4. `log-api` builds a defensive analysis prompt containing:
   - The source.
   - The submitted log line.
   - Instructions to return event type, severity, reason, and recommended
     defensive action.
5. `log-api` sends the prompt to
   `ollama:11434/api/generate`.
6. Ollama runs the configured model and returns generated text.
7. `log-api` sends a `POST /records` request to `log-db:5000`.
8. `log-db` inserts the source, log line, analysis, severity, and timestamp into
   SQLite.
9. `log-api` returns the original input, local severity, generated analysis, and
   storage result to the client.

```text
Client
  |
  | POST /analyze
  v
log-api:8080
  |
  | 1. Local severity classification
  |
  | POST /api/generate
  v
ollama:11434
  |
  | Generated analysis
  v
log-api:8080
  |
  | POST /records
  v
log-db:5000
  |
  | INSERT
  v
/opt/log-db/logs.db
```

### Record retrieval

1. A client sends `GET /records` to
  `http://<log-api-address>:8080/records`.
2. `log-api` forwards the request to `log-db:5000/records`.
3. `log-db` queries SQLite, ordering records by newest ID first.
4. The records are returned through `log-api` to the client.

## 7. Persistence and lifecycle

The SQLite database is stored inside the `log-db` container at:

```text
/opt/log-db/logs.db
```

The application files and systemd unit files are also stored inside their
respective containers under `/opt` and `/etc/systemd/system`.

Because the teardown playbook uses forced container deletion, the database and
all other data inside the three lab containers are removed when teardown is
executed. The current setup does not define external storage or a backup
workflow for the SQLite database.

## 8. Current configuration notes

The following variables are present in `group_vars/all.yml`:

```yaml
lxd_controller_host
lxd_storage_pool
lxd_network
ollama_port
log_api_port
log_db_port
```

The current roles use several fixed command arguments and URLs instead of
reading all of these variables. In particular, the database implementation
uses SQLite and port `5000`; the `log_db_port: 5432` value does not represent a
running PostgreSQL service.

This document records that behavior as implemented and does not change it.
