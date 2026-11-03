#!/usr/bin/env python3

import json
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.request
from collections import namedtuple
from pathlib import Path


WINDOW = 30
EXPOSE_PLAYBOOK = Path(__file__).resolve().parent.parent / "playbooks" / "expose_log_api_plain.yml"
DEDUP_SECONDS = 300
MARK = "LOG_PIPELINE=1"

IMPORTANT = re.compile(
    r"failed password|invalid user|accepted password|authentication failure"
    r"|command=|not in sudoers|failed login|admin login"
    r"|segfault|out of memory|oom-kill|apparmor=\"denied\"|i/o error"
    r"|\[(WRN|ERR)\]|\|(WARN|ERR|EMER)\||health_(warn|err)"
    r"|\b(error|failed|panic|fatal|timeout|refused|degraded|unreachable|traceback)\b",
    re.IGNORECASE,
)

Node = namedtuple("Node", "name host user sudo")

pending = queue.Queue(maxsize=200)


def read_inventory(path):
    section, hosts, controller, sudo = None, {}, None, "sudo"
    for raw in Path(path).read_text().splitlines():
        line = raw.split("#")[0].strip()
        if not line:
            continue
        if line.startswith("["):
            section = line.strip("[]")
            continue
        name, *pairs = line.split()
        if section == "microcloud:vars":
            key, _, value = name.partition("=")
            if key == "ansible_become_exe":
                sudo = value
        elif section == "microcloud":
            options = dict(pair.split("=", 1) for pair in pairs if "=" in pair)
            hosts[name] = (options.get("ansible_host", name), options.get("ansible_user", "root"))
        elif section == "lxd_controller":
            controller = name
    nodes = {name: Node(name, host, user, sudo) for name, (host, user) in hosts.items()}
    return list(nodes.values()), nodes[controller]


def ssh(node, command, timeout=60):
    remote = shlex.join([node.sudo, "-n", "env", MARK] + command)
    args = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new", f"{node.user}@{node.host}", remote,
    ]
    try:
        result = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"{node.name}: {command[0]} timed out", flush=True)
        return None
    if result.returncode not in (0, 124):
        print(f"{node.name}: {' '.join(command[:3])} failed: {result.stderr.strip()[:200]}", flush=True)
        return None
    return result.stdout


def json_objects(text):
    decoder = json.JSONDecoder()
    pos = 0
    while True:
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            return
        try:
            obj, pos = decoder.raw_decode(text, pos)
        except json.JSONDecodeError:
            pos = text.find("\n", pos)
            if pos == -1:
                return
            continue
        if isinstance(obj, dict):
            yield obj


def submit(source, line):
    try:
        pending.put_nowait((source, line[:2000]))
    except queue.Full:
        print(f"queue full, dropped {source}: {line[:80]}", flush=True)


def submit_lxd_events(text, node_name):
    for event in json_objects(text):
        meta = event.get("metadata") or {}
        if MARK in json.dumps(meta):
            continue
        where = event.get("location") or node_name
        when = event.get("timestamp", "")
        if event.get("type") == "logging":
            if meta.get("level") in ("debug", "info"):
                continue
            context = " ".join(f"{k}={v}" for k, v in (meta.get("context") or {}).items())
            submit(f"{where}/lxd", f"{when} {where} lxd {meta.get('level')}: {meta.get('message')} {context}".strip())
        elif event.get("type") == "lifecycle":
            who = meta.get("requestor") or {}
            submit(f"{where}/lxd-lifecycle", (
                f"{when} {where} lxd lifecycle: {meta.get('action')} {meta.get('source')}"
                f" by {who.get('username', '-')} via {who.get('protocol', '-')} from {who.get('address', 'local')}"
            ))


def read_journal(node, prefix, origin, cursors):
    cursor = cursors.get(origin)
    position = f"--after-cursor={cursor}" if cursor else f"--since=-{WINDOW}s"
    text = ssh(node, prefix + ["journalctl", position, "--output=json", "--no-pager"])
    if text is None:
        cursors.pop(origin, None)
        return
    for entry in json_objects(text):
        cursors[origin] = entry.get("__CURSOR", cursors.get(origin))
        message = entry.get("MESSAGE")
        ident = entry.get("SYSLOG_IDENTIFIER", "journal")
        if not isinstance(message, str) or MARK in message or ident.startswith("ansible-"):
            continue
        if entry.get("_SYSTEMD_UNIT") == "snap.lxd.daemon.service":
            continue
        if entry.get("PRIORITY") in ("0", "1", "2", "3") or IMPORTANT.search(message):
            submit(f"{origin}/{ident}", f"{origin} {ident}: {message}")


def watch_node(node):
    cursors = {}
    while True:
        events = ssh(node, ["timeout", str(WINDOW), "lxc", "monitor",
                            "--type=logging", "--type=lifecycle", "--format=json"], timeout=WINDOW + 30)
        if events is None:
            time.sleep(10)
            continue
        submit_lxd_events(events, node.name)
        read_journal(node, [], node.name, cursors)


def watch_containers(controller):
    cursors = {}
    while True:
        started = time.monotonic()
        names = ssh(controller, ["lxc", "list", "status=running", "--format", "csv", "-c", "n"]) or ""
        for name in names.split():
            if name != "log-api":
                read_journal(controller, ["lxc", "exec", name, "--", "env", MARK], name, cursors)
        time.sleep(max(0, WINDOW - (time.monotonic() - started)))


def log_api_url():
    text = EXPOSE_PLAYBOOK.read_text(encoding="utf-8")
    address = re.search(r"^\s*forward_address:\s*(\S+)", text, re.MULTILINE)
    port = re.search(r"^\s*forward_port:\s*(\d+)", text, re.MULTILINE)
    if not address or not port:
        sys.exit(f"forward_address or forward_port is missing from {EXPOSE_PLAYBOOK}")
    url = f"http://{address.group(1)}:{port.group(1)}"
    try:
        urllib.request.urlopen(url + "/health", timeout=10).close()
    except Exception as exc:
        sys.exit(f"log-api is not reachable at {url} ({exc}); run playbooks/expose_log_api_plain.yml first")
    return url + "/analyze"


def send(api_url, source, line):
    body = json.dumps({"source": source, "log_line": line}).encode()
    request = urllib.request.Request(api_url, data=body, headers={"Content-Type": "application/json"})
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.load(response).get("severity")
        except Exception as exc:
            print(f"  attempt {attempt}/3 failed: {exc}", flush=True)
            time.sleep(10)
    return None


def main():
    inventory = sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "inventory.ini"
    nodes, controller = read_inventory(inventory)
    api_url = log_api_url()
    print(f"watching {', '.join(n.name for n in nodes)} and their containers, sending to {api_url}", flush=True)

    threads = [threading.Thread(target=watch_node, args=(node,), daemon=True) for node in nodes]
    threads.append(threading.Thread(target=watch_containers, args=(controller,), daemon=True))
    for thread in threads:
        thread.start()

    recently_sent = {}
    while True:
        source, line = pending.get()
        key = (source, re.sub(r"\d+", "#", line))
        now = time.time()
        if now - recently_sent.get(key, 0) < DEDUP_SECONDS:
            continue
        recently_sent[key] = now
        if len(recently_sent) > 5000:
            recently_sent = {k: t for k, t in recently_sent.items() if now - t < DEDUP_SECONDS}
        severity = send(api_url, source, line)
        print(f"[{severity or 'not sent'}] {source}: {line[:120]}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
