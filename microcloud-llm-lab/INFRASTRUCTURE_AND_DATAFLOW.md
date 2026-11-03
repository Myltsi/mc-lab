# MicroCloud LLM Lab Infrastructure and Dataflow

This document describes the infrastructure and application dataflow implemented by
the Ansible project in this directory. It documents the current setup, including
the log API-to-database authorization flow and the Ansible Vault deployment
requirements.

## 1. Scope and architecture

The lab consists of:

- One Ansible control host, referred to as the controller.
- Four Ubuntu servers forming the MicroCloud/LXD cluster.
- Four LXD containers distributed across the cluster:
  - `ollama`: local large-language-model service.
  - `log-api`: client-facing log-analysis API.
  - `log-db`: Flask API backed by SQLite.
  - `log-ui`: Flask web interface for the log-analysis API.

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

log-ui
  | HTTP :8081
  +-----------------------> log-api
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
| `log-ui` | `mc4` | `ubuntu:24.04` | Flask web interface on port `8081` |

The container role is implemented in
[roles/lxd_containers/tasks/main.yml](./roles/lxd_containers/tasks/main.yml).
It runs on the LXD controller and:

1. Lists existing LXD instances.
2. Launches each missing container with `lxc launch`.
3. Uses `--target` to place a container on its selected cluster member.
4. Waits for each container to be running and to have an IPv4 address.
5. Prints the resulting LXD instance list.

The values `mc1`, `mc2`, `mc3`, and `mc4` are LXD target names configured by
the project. They are separate from the Ansible inventory names `mc-node-1`,
`mc-node-2`, `mc-node-3`, and `mc-node-4`.

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

The application deployment is split into five playbooks:

```text
playbooks/deploy_containers.yml
        |
        +--> playbooks/deploy_ollama.yml
        |
        +--> playbooks/deploy_log_db.yml
        |
        +--> playbooks/deploy_log_api.yml
        |
        +--> playbooks/deploy_log_ui.yml
```

The practical order is:

1. `deploy_containers.yml`
2. `deploy_ollama.yml`
3. `deploy_log_db.yml`
4. `deploy_log_api.yml`
5. `deploy_log_ui.yml`

The application playbooks assume that the required containers already exist.
`deploy_log_ui.yml` can be omitted when only the backend services are needed.

### Ansible Vault and deployment credentials

The log API and log database share a secret named `vault_log_db_token`. Store it
in an encrypted Vault variables file, such as `group_vars/vault.yml`:

```yaml
vault_log_db_token: "replace-with-a-random-token"
```

Create or encrypt the file with:

```bash
ansible-vault encrypt group_vars/vault.yml
```

Do not store the token in `group_vars/all.yml`, a playbook, or a committed
deployment script. The encrypted file must be available on the Ansible control
host before deploying either service.

With the current repository configuration, provide the Vault password when
running the playbooks:

```bash
ansible-playbook --ask-vault-pass playbooks/deploy_log_db.yml
ansible-playbook --ask-vault-pass playbooks/deploy_log_api.yml
```

The database must be deployed before the API so both services are configured
with the same token. If unattended deployment is needed, use an external
password file outside Git with `--vault-password-file`; do not put the Vault
password directly in a repository script.

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
line, generated analysis, and severity. The `/records` endpoints require the
`X-Log-DB-Token` request header; `/health` remains public for deployment health
checks.

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

`log-api` authenticates to `log-db` by sending:

```http
X-Log-DB-Token: <vault_log_db_token>
```

The header is sent for both the internal `POST /records` request made during
analysis and the internal `GET /records` request. `log-db` compares the header
with its `LOG_DB_TOKEN` environment variable and returns HTTP `401` when the
header is missing or incorrect. This protects the internal API-to-database
connection; the public `log-api` endpoints do not currently authenticate their
clients.

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
8. `log-api` includes the `X-Log-DB-Token` header from its systemd environment.
9. `log-db` validates the header and returns `401` if it is missing or incorrect.
10. `log-db` inserts the source, log line, analysis, severity, and timestamp into
   SQLite.
11. `log-api` returns the original input, local severity, generated analysis, and
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
  | X-Log-DB-Token
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
2. `log-api` forwards the request to `log-db:5000/records` with the
   `X-Log-DB-Token` header.
3. `log-db` validates the header and returns `401` if it is missing or
   incorrect.
4. `log-db` queries SQLite, ordering records by newest ID first.
5. The records are returned through `log-api` to the client.

## 7. Persistence and lifecycle

The SQLite database is stored inside the `log-db` container at:

```text
/opt/log-db/logs.db
```

The application files and systemd unit files are also stored inside their
respective containers under `/opt` and `/etc/systemd/system`.

Because the teardown playbook uses forced container deletion, the database and
all other data inside the four lab containers are removed when teardown is
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
log_ui_port
```

The current roles use several fixed command arguments and URLs instead of
reading all of these variables. In particular, the database implementation
uses SQLite and port `5000`; the `log_db_port: 5432` value does not represent a
running PostgreSQL service. Likewise, the roles currently use the fixed service
ports `11434`, `8080`, `5000`, and `8081` in their commands and systemd units.

The `log-ui` container is defined in `group_vars/all.yml` and deployed by
`playbooks/deploy_log_ui.yml`. The current `playbooks/teardown.yml` removes
`ollama`, `log-api`, and `log-db`, but does not remove `log-ui`; delete
`log-ui` separately with LXC if a complete lab teardown is required.
