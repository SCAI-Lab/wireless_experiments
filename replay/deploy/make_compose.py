#!/usr/bin/env python3
"""
Derive the compose file for the SLURM node from MedWear's
docker-compose.medwear-minimal.yml, matching the benchmark setup
(~hoezer/benchmark: rootless Podman, minimal compose, no CPU limits):

- only the benchmark's services: postgres, influxdb, minio, keycloak,
  med-api, nginx (+ their one-shot setup helpers minio-setup and
  keycloak-db-setup). The frontend is kept too: nginx refuses to start
  when an upstream host does not resolve, and it proxies "/" to it.
- prebuilt images instead of build sections (built by deploy.sh with the
  ETH proxy and the pinned ORM libraries);
- nginx published on 8443 (rootless Podman cannot bind ports < 1024), as
  in the benchmark (API_HOST=https://127.0.0.1:8443);
- the production /data/batch rate limit (300/minute).

Usage: make_compose.py <medwear repo> <output compose file>
"""
import sys

import yaml

KEEP = ["minio", "minio-setup", "influxdb", "postgres", "keycloak-db-setup", "keycloak",
        "med-api", "frontend", "medwear-nginx"]
IMAGES = {  # podman-compose's own naming for project "medwear"
    "med-api": "localhost/medwear_med-api:latest",
    "frontend": "localhost/medwear_frontend:latest",
    "medwear-nginx": "localhost/medwear_medwear-nginx:latest",
}

src, out = sys.argv[1], sys.argv[2]
with open(f"{src}/docker-compose.medwear-minimal.yml") as f:
    d = yaml.safe_load(f)
d.pop("version", None)

services = {}
for name in KEEP:
    s = d["services"][name]
    if name in IMAGES:
        s.pop("build", None)
        s["image"] = IMAGES[name]
    elif "/" not in s["image"].split(":")[0] or "." not in s["image"].split("/")[0]:
        # Podman on RHEL-likes refuses unqualified short names in batch jobs
        img = s["image"]
        s["image"] = f"docker.io/{img}" if "/" in img.split(":")[0] else f"docker.io/library/{img}"
    if isinstance(s.get("depends_on"), dict):     # keep only kept services
        s["depends_on"] = {k: v for k, v in s["depends_on"].items() if k in KEEP}
    elif isinstance(s.get("depends_on"), list):
        s["depends_on"] = [k for k in s["depends_on"] if k in KEEP]
    services[name] = s

# keycloak-db-setup runs psql once and hides its errors. On a fresh volume the
# postgres image first runs a temporary, socket-only server that already
# passes pg_isready (the healthcheck), so the TCP psql fails, the database is
# never created and Keycloak starts without it. Retry until postgres accepts
# TCP connections, then create the database if it is missing.
services["keycloak-db-setup"]["command"] = (
    '/bin/sh -c "export PGPASSWORD=$$POSTGRES_PASSWORD; '
    'until psql -h postgres -U $$POSTGRES_USER -d postgres -Atc \'select 1\' >/dev/null 2>&1; do sleep 1; done; '
    'psql -h postgres -U $$POSTGRES_USER -d postgres -Atc '
    '\\"select 1 from pg_database where datname=\'keycloak\'\\" | grep -q 1 || '
    'psql -h postgres -U $$POSTGRES_USER -d postgres -c \\"CREATE DATABASE keycloak OWNER $$POSTGRES_USER\\""'
)

# minio-setup waits a fixed 5 s and ends every step in "|| true": when MinIO
# is slower to start, the API's MinIO user is never created and med-api's
# health shows minio "InvalidAccessKeyId". Retry until MinIO answers.
ms = services["minio-setup"]
fixed = ms["entrypoint"].replace(
    "sleep 5; mc alias set myminio http://minio:9000 $$MINIO_ROOT_USER $$MINIO_ROOT_PASSWORD;",
    "until mc alias set myminio http://minio:9000 $$MINIO_ROOT_USER $$MINIO_ROOT_PASSWORD; do sleep 1; done;")
assert fixed != ms["entrypoint"], "minio-setup entrypoint changed upstream"
ms["entrypoint"] = fixed

services["medwear-nginx"]["ports"] = ["8443:443"]
env = services["med-api"].setdefault("environment", [])
env[:] = [e for e in env if not str(e).startswith("BATCH_RATE_LIMIT")] + ["BATCH_RATE_LIMIT=300/minute"]

# No container DNS. Rootless Podman serves names on a network with
# aardvark-dns, which systemd starts in the user's slice, outside the SLURM
# job and under the node's shared user.slice CPU cap, where lookups between
# the containers can time out. Instead, each service gets a fixed address and
# every container an /etc/hosts entry for all service and container names;
# the network is created without DNS, so no aardvark-dns runs.
# nginx resolves proxy_pass
# hostnames at request time through its own resolver, which reads
# resolv.conf, not /etc/hosts. So the stack also gets its own DNS server as a
# container (dnsmasq, inside the job like the rest): it answers only the
# stack's names (no upstream servers, so unknown names fail at once), and
# every container uses it.
NET, SUBNET, DNS_IP = "medwear-static", "10.89.250.0/24", "10.89.250.9"
ips = {name: f"10.89.250.{10 + i}" for i, name in enumerate(services)}
aliases = {alias: ips[name] for name, s in services.items()
           for alias in (name, s.get("container_name")) if alias}
hosts = sorted(f"{a}:{ip}" for a, ip in aliases.items())
for name, s in services.items():
    s["networks"] = {NET: {"ipv4_address": ips[name]}}
    s["extra_hosts"] = hosts
    s["dns"] = [DNS_IP]
services["dns"] = {
    "image": "localhost/medwear_dns:latest", "container_name": "medwear-dns",
    "restart": "unless-stopped", "networks": {NET: {"ipv4_address": DNS_IP}},
    "command": ["dnsmasq", "--keep-in-foreground", "--no-resolv", "--no-hosts",
                "--log-facility=-", "--local-ttl=60"]
               + [f"--host-record={a},{ip}" for a, ip in sorted(aliases.items())],
}
dep = services["medwear-nginx"].get("depends_on")
if isinstance(dep, dict):
    dep["dns"] = {"condition": "service_started"}
else:
    services["medwear-nginx"]["depends_on"] = (dep or []) + ["dns"]
d["networks"] = {NET: {"driver": "bridge", "x-podman.disable_dns": True,
                       "ipam": {"config": [{"subnet": SUBNET}]}}}

used_volumes = {str(v).split(":")[0] for s in services.values() for v in s.get("volumes", [])}
d["services"] = services
d["volumes"] = {k: v for k, v in (d.get("volumes") or {}).items() if k in used_volumes}

with open(out, "w") as f:
    f.write("# Generated by wireless_experiments/replay/deploy/make_compose.py — do not edit.\n")
    yaml.safe_dump(d, f, sort_keys=False)
print(f"wrote {out}: {', '.join(services)}")
