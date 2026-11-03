#!/usr/bin/env python3
"""Välittää MicroCloudin lokit jatkuvasti log-api:n /analyze-rajapintaan.

Käyttö: forward_logs.py [inventory.ini]

Ohjelma ajetaan Ansible-ohjauskoneella, ei klusterissa. Se ottaa nodeihin
yhteyden SSH:lla käyttäen Ansiblen inventaarion osoitteita ja käyttäjiä.
Nodeille ei kopioida mitään: niillä ajetaan vain komentoja.

Toimintaperiaate:
  * jokainen node ajaa silmukassa `timeout 30 lxc monitor` (LXD-tapahtumat)
    ja lukee sen jälkeen journalistaan uudet rivit
  * ohjausnode lukee jokaisen käynnissä olevan kontin uudet journal-rivit
  * tärkeät rivit lähetetään log-api:lle, jonka playbooks/expose_log_api.yml
    on julkaissut kiinteään osoitteeseen (LXD network forward)
"""

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

# --- Asetukset -------------------------------------------------------------

WINDOW = 30            # kuinka monta sekuntia yksi lxc monitor -ajo kuuntelee
# Playbook, jonka vars-osiossa log-api:n julkinen osoite ja portti on
# määritelty (forward_address ja forward_port).
EXPOSE_PLAYBOOK = Path(__file__).resolve().parent.parent / "playbooks" / "expose_log_api.yml"
DEDUP_SECONDS = 300    # sama rivi lähetetään korkeintaan kerran 5 minuutissa
# Tunniste, joka lisätään jokaiseen ohjelman ajamaan komentoon. Sen avulla
# ohjelma tunnistaa omat jälkensä lokeista eikä lähetä niitä analysoitavaksi.
MARK = "LOG_PIPELINE=1"

# Avainsanat, joiden perusteella journal-rivi katsotaan tärkeäksi.
IMPORTANT = re.compile(
    # kirjautumiset ja pääkäyttäjäoikeuksien käyttö (sshd, sudo, webshop)
    r"failed password|invalid user|accepted password|authentication failure"
    r"|command=|not in sudoers|failed login|admin login"
    # kernelin ongelmat
    r"|segfault|out of memory|oom-kill|apparmor=\"denied\"|i/o error"
    # Cephin ja OVN:n varoitukset ja virheet sekä yleiset virhesanat
    r"|\[(WRN|ERR)\]|\|(WARN|ERR|EMER)\||health_(warn|err)"
    r"|\b(error|failed|panic|fatal|timeout|refused|degraded|unreachable|traceback)\b",
    re.IGNORECASE,
)

# Yhden noden tiedot: nimi, IP-osoite, SSH-käyttäjä ja sudo-ohjelman polku.
Node = namedtuple("Node", "name host user sudo")

# Jono keräyssäikeiden ja lähettäjän välissä. Keräys lisää jonoon rivejä,
# lähettäjä ottaa niitä yksi kerrallaan. Enintään 200 riviä odottaa kerralla.
pending = queue.Queue(maxsize=200)


# --- Inventaario ja SSH ----------------------------------------------------

def read_inventory(path):
    """Lukee inventory.ini:stä [microcloud]-nodet ja [lxd_controller]-noden."""
    section, hosts, controller, sudo = None, {}, None, "sudo"
    for raw in Path(path).read_text().splitlines():
        line = raw.split("#")[0].strip()  # poistetaan kommentit ja tyhjät
        if not line:
            continue
        if line.startswith("["):          # uusi osio, esim. [microcloud]
            section = line.strip("[]")
            continue
        name, *pairs = line.split()
        if section == "microcloud:vars":
            # Esim. ansible_become_exe=/usr/bin/sudo.ws: sama sudo kuin Ansiblella.
            key, _, value = name.partition("=")
            if key == "ansible_become_exe":
                sudo = value
        elif section == "microcloud":
            # Esim. "mc-node-1 ansible_host=192.168.1.11 ansible_user=mc"
            options = dict(pair.split("=", 1) for pair in pairs if "=" in pair)
            hosts[name] = (options.get("ansible_host", name), options.get("ansible_user", "root"))
        elif section == "lxd_controller":
            controller = name
    nodes = {name: Node(name, host, user, sudo) for name, (host, user) in hosts.items()}
    return list(nodes.values()), nodes[controller]


def ssh(node, command, timeout=60):
    """Ajaa komennon nodella pääkäyttäjänä ja palauttaa sen tulosteen.

    Palauttaa None, jos yhteys tai komento epäonnistui.
    """
    # Komento ajetaan muodossa: sudo -n env LOG_PIPELINE=1 <komento>
    # -n = sudo ei kysy salasanaa, vaan epäonnistuu heti, jos sitä tarvittaisiin.
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
    # Paluukoodi 124 tarkoittaa, että timeout pysäytti komennon. Se on
    # odotettu tulos lxc monitorille, joka ei muuten pysähtyisi koskaan.
    if result.returncode not in (0, 124):
        print(f"{node.name}: {' '.join(command[:3])} failed: {result.stderr.strip()[:200]}", flush=True)
        return None
    return result.stdout


def json_objects(text):
    """Pilkkoo tekstin JSON-objekteiksi.

    Toimii sekä yksi objekti per rivi -muodolle (journalctl) että usealle
    riville jaetuille objekteille (lxc monitor).
    """
    decoder = json.JSONDecoder()
    pos = 0
    while True:
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            return
        try:
            obj, pos = decoder.raw_decode(text, pos)  # lukee yhden objektin
        except json.JSONDecodeError:
            # Rikkinäinen kohta (esim. timeout katkaisi viimeisen tapahtuman):
            # hypätään seuraavalle riville sen sijaan, että kaaduttaisiin.
            pos = text.find("\n", pos)
            if pos == -1:
                return
            continue
        if isinstance(obj, dict):
            yield obj


# --- Lokien käsittely ------------------------------------------------------

def submit(source, line):
    """Lisää rivin lähetysjonoon. Jos jono on täynnä, rivi pudotetaan."""
    try:
        pending.put_nowait((source, line[:2000]))  # pitkät rivit katkaistaan
    except queue.Full:
        print(f"queue full, dropped {source}: {line[:80]}", flush=True)


def submit_lxd_events(text, node_name):
    """Poimii lxc monitorin tulosteesta tärkeät LXD-tapahtumat jonoon."""
    for event in json_objects(text):
        meta = event.get("metadata") or {}
        if MARK in json.dumps(meta):
            continue  # ohjelman oma lxc exec konttiin, ei analysoida
        where = event.get("location") or node_name  # klusterin jäsen, esim. mc2
        when = event.get("timestamp", "")
        if event.get("type") == "logging":
            # LXD-palvelun omat lokiviestit: vain varoitukset ja virheet.
            if meta.get("level") in ("debug", "info"):
                continue
            context = " ".join(f"{k}={v}" for k, v in (meta.get("context") or {}).items())
            submit(f"{where}/lxd", f"{when} {where} lxd {meta.get('level')}: {meta.get('message')} {context}".strip())
        elif event.get("type") == "lifecycle":
            # Klusterissa tehdyt toimenpiteet (kontin luonti, poisto, exec,
            # asetusmuutokset...) ja tieto siitä, kuka ne teki.
            who = meta.get("requestor") or {}
            submit(f"{where}/lxd-lifecycle", (
                f"{when} {where} lxd lifecycle: {meta.get('action')} {meta.get('source')}"
                f" by {who.get('username', '-')} via {who.get('protocol', '-')} from {who.get('address', 'local')}"
            ))


def read_journal(node, prefix, origin, cursors):
    """Lukee journalin uudet rivit ja lisää tärkeät jonoon.

    prefix on tyhjä noden omalle journalille ja "lxc exec <kontti> --"
    kontin journalille. cursors muistaa, mihin asti kukin journal on luettu.
    """
    # Kursori on journalin "kirjanmerkki". Kun luku jatkuu sen jälkeen,
    # yksikään rivi ei jää väliin eikä tule kahdesti. Ensimmäisellä kerralla
    # kursoria ei vielä ole, joten luetaan viimeiset 30 sekuntia.
    cursor = cursors.get(origin)
    position = f"--after-cursor={cursor}" if cursor else f"--since=-{WINDOW}s"
    text = ssh(node, prefix + ["journalctl", position, "--output=json", "--no-pager"])
    if text is None:
        # Esim. uudelleen luotu kontti ei tunne vanhaa kursoria: aloitetaan alusta.
        cursors.pop(origin, None)
        return
    for entry in json_objects(text):
        cursors[origin] = entry.get("__CURSOR", cursors.get(origin))
        message = entry.get("MESSAGE")
        ident = entry.get("SYSLOG_IDENTIFIER", "journal")  # ohjelma, esim. sshd
        # Ohitetaan ohjelman omat sudo-rivit (MARK) ja Ansiblen moduulilokit.
        if not isinstance(message, str) or MARK in message or ident.startswith("ansible-"):
            continue
        if entry.get("_SYSTEMD_UNIT") == "snap.lxd.daemon.service":
            continue  # lxc monitor raportoi jo LXD:n omat lokit
        # Mukaan, jos prioriteetti on err tai vakavampi (0-3) tai rivi
        # sisältää jonkin tärkeän avainsanan.
        if entry.get("PRIORITY") in ("0", "1", "2", "3") or IMPORTANT.search(message):
            submit(f"{origin}/{ident}", f"{origin} {ident}: {message}")


# --- Keräyssäikeet ---------------------------------------------------------

def watch_node(node):
    """Yhden noden vahti: kuuntelee LXD-tapahtumia ja lukee journalia ikuisesti."""
    cursors = {}
    while True:
        # 1) Kuunnellaan LXD-tapahtumia 30 sekuntia. Komento palaa vasta,
        #    kun timeout pysäyttää sen.
        events = ssh(node, ["timeout", str(WINDOW), "lxc", "monitor",
                            "--type=logging", "--type=lifecycle", "--format=json"], timeout=WINDOW + 30)
        if events is None:
            time.sleep(10)  # node ei vastaa: odotetaan hetki ja yritetään uudelleen
            continue
        submit_lxd_events(events, node.name)
        # 2) Luetaan noden journalista sillä välin tulleet rivit.
        read_journal(node, [], node.name, cursors)


def watch_containers(controller):
    """Konttien vahti: lukee 30 sekunnin välein jokaisen kontin uudet journal-rivit."""
    cursors = {}
    while True:
        started = time.monotonic()
        # Konttilista haetaan joka kierroksella, joten uudet kontit tulevat mukaan.
        names = ssh(controller, ["lxc", "list", "status=running", "--format", "csv", "-c", "n"]) or ""
        for name in names.split():
            if name != "log-api":  # log-api:n omat lokit syöttäisivät itseään takaisin
                # MARK myös kontin sisällä, jotta oma lxc exec tunnistetaan LXD-tapahtumista.
                read_journal(controller, ["lxc", "exec", name, "--", "env", MARK], name, cursors)
        time.sleep(max(0, WINDOW - (time.monotonic() - started)))


# --- log-api ---------------------------------------------------------------

def log_api_url():
    """Lukee log-api:n julkisen osoitteen ja tarkistaa, että se vastaa.

    Osoite ja portti luetaan expose_log_api.yml:stä, joten niitä ei tarvitse
    kirjoittaa tähän erikseen.
    """
    text = EXPOSE_PLAYBOOK.read_text(encoding="utf-8")
    address = re.search(r"^\s*forward_address:\s*(\S+)", text, re.MULTILINE)
    port = re.search(r"^\s*forward_port:\s*(\d+)", text, re.MULTILINE)
    if not address or not port:
        sys.exit(f"forward_address or forward_port is missing from {EXPOSE_PLAYBOOK}")
    url = f"http://{address.group(1)}:{port.group(1)}"
    try:
        urllib.request.urlopen(url + "/health", timeout=10).close()
    except Exception as exc:
        sys.exit(f"log-api is not reachable at {url} ({exc}); run playbooks/expose_log_api.yml first")
    return url + "/analyze"


def send(api_url, source, line):
    """Lähettää yhden rivin log-api:lle ja palauttaa sen vakavuusarvion.

    Yrittää kolme kertaa. Palauttaa None, jos kaikki yritykset epäonnistuvat.
    """
    body = json.dumps({"source": source, "log_line": line}).encode()
    request = urllib.request.Request(api_url, data=body, headers={"Content-Type": "application/json"})
    for attempt in range(1, 4):
        try:
            # 120 s, koska log-api odottaa Ollaman vastausta jopa 90 s.
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.load(response).get("severity")
        except Exception as exc:
            print(f"  attempt {attempt}/3 failed: {exc}", flush=True)
            time.sleep(10)
    return None


# --- Pääohjelma ------------------------------------------------------------

def main():
    # inventory.ini haetaan oletuksena projektin juuresta (scripts/-kansion yläpuolelta).
    inventory = sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "inventory.ini"
    nodes, controller = read_inventory(inventory)
    api_url = log_api_url()
    print(f"watching {', '.join(n.name for n in nodes)} and their containers, sending to {api_url}", flush=True)

    # Käynnistetään vahdit: yksi jokaiselle nodelle ja yksi konteille.
    # daemon=True: säikeet loppuvat, kun pääohjelma lopetetaan (Ctrl+C).
    threads = [threading.Thread(target=watch_node, args=(node,), daemon=True) for node in nodes]
    threads.append(threading.Thread(target=watch_containers, args=(controller,), daemon=True))
    for thread in threads:
        thread.start()

    # Lähettäjä: ottaa jonosta rivin kerrallaan ja lähettää sen log-api:lle.
    # Keräys jatkuu taustalla sillä aikaa, kun Ollama analysoi.
    recently_sent = {}
    while True:
        source, line = pending.get()
        # Toistojen tunnistus: numerot (PID:t, portit, kellonajat) korvataan
        # #-merkillä, jolloin samanlaiset rivit saavat saman avaimen.
        key = (source, re.sub(r"\d+", "#", line))
        now = time.time()
        if now - recently_sent.get(key, 0) < DEDUP_SECONDS:
            continue  # sama rivi lähetetty alle 5 minuuttia sitten
        recently_sent[key] = now
        if len(recently_sent) > 5000:
            # Siivotaan vanhat avaimet, ettei muistinkäyttö kasva rajatta.
            recently_sent = {k: t for k, t in recently_sent.items() if now - t < DEDUP_SECONDS}
        severity = send(api_url, source, line)
        print(f"[{severity or 'not sent'}] {source}: {line[:120]}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass  # Ctrl+C lopettaa ohjelman siististi
