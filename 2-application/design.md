# Log analysis pipeline

The pipeline reads logs from the MicroCloud nodes and containers and sends the important lines to log-api, which analyses them with Ollama and saves the results to log-db.

Python script `forward_logs.py` runs on the Ansible control node and connects to the nodes over SSH. On every node, it runs `timeout 30 lxc monitor`, which saves 30 seconds of LXD events like errors and container changes. Because lxc monitor doesn't stop on its own, timeout stops it and the script starts it again, over and over. The script also reads the node's journal and the journals of the containers with `lxc exec <container> -- journalctl`. Only errors, warnings, failed logins, sudo commands and LXD events are sent to log-api. No code or files are saved on MicroCloud, the nodes only run the commands.

The control node can't reach the containers directly, so the Ansible playbook `expose_log_api.yml` creates a network forward with the lxc CLI. After that log-api can be reached at `192.168.1.130:8080`, and the forward can be seen in the web GUI under Networks > default > Forwards. `teardown_modified_part.yml` deletes the forward, so the cluster goes back to how it was.
