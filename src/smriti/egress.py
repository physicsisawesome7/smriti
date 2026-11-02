"""Egress guard: where user-configured connectors may send requests.

Connectors take URLs from people, so every outbound request (including each
redirect hop) is checked before it leaves:

* always blocked: loopback, link-local (cloud metadata at 169.254.169.254),
  multicast, unspecified and reserved ranges, and the addresses of Smriti's
  own data services (Postgres, Qdrant, model servers), so a "website" cannot
  be pointed at the vector store's admin API;
* private (RFC 1918) networks are allowed by default, because crawling the
  intranet is the point of an on-premise deployment; set
  SMRITI_EGRESS_ALLOW_PRIVATE=0 to block them;
* SMRITI_EGRESS_ALLOWLIST (hosts or CIDRs, comma separated) overrides the
  blocks for specific targets, e.g. a database on localhost.

Name resolution happens at check time; pin outbound traffic through an egress
proxy as well where DNS rebinding is a concern.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

import httpx

DEFAULT_DENY_HOSTS = "postgres,qdrant,llm,embed,rerank,embed-cpu,rerank-cpu"


class EgressDenied(PermissionError):
    pass


def _env_list(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


@lru_cache(maxsize=1)
def _denied_service_ips() -> frozenset[str]:
    ips: set[str] = set()
    for host in _env_list("SMRITI_EGRESS_DENY_HOSTS", DEFAULT_DENY_HOSTS):
        try:
            for info in socket.getaddrinfo(host, None):
                ips.add(str(info[4][0]))
        except OSError:
            continue  # not on this network (e.g. running outside Docker)
    return frozenset(ips)


def _allowed_override(host: str, ips: list[ipaddress._BaseAddress]) -> bool:
    for entry in _env_list("SMRITI_EGRESS_ALLOWLIST"):
        if entry.lower() == host.lower():
            return True
        try:
            net = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            continue
        if ips and all(ip in net for ip in ips):
            return True
    return False


def check_host(host: str, port: int | None = None) -> None:
    if not host:
        raise EgressDenied("no host")
    try:
        infos = socket.getaddrinfo(host, port or 80, proto=socket.IPPROTO_TCP)
    except OSError as e:
        raise EgressDenied(f"cannot resolve {host}: {e}") from e
    ips = [ipaddress.ip_address(str(i[4][0]).split("%")[0]) for i in infos]
    if _allowed_override(host, ips):
        return
    allow_private = os.environ.get("SMRITI_EGRESS_ALLOW_PRIVATE", "1") not in ("0", "false", "no")
    denied_services = _denied_service_ips()
    for ip in ips:
        if str(ip) in denied_services:
            raise EgressDenied(f"{host} is one of Smriti's own data services")
        if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
            raise EgressDenied(f"{host} resolves to a blocked address ({ip}); allow it with SMRITI_EGRESS_ALLOWLIST")
        if ip.is_private and not allow_private:
            raise EgressDenied(f"{host} is on a private network and SMRITI_EGRESS_ALLOW_PRIVATE=0")


SELF_PATHS = ("/demo-site/", "/demo-api/", "/demo-mcp/", "/robots.txt", "/sitemap.xml")


def _is_self(s: Any) -> bool:
    me = urlsplit(os.environ.get("SMRITI_SELF_URL") or os.environ.get("SMRITI_PUBLIC_URL") or "http://127.0.0.1:8000")
    port = s.port or (443 if s.scheme == "https" else 80)
    my_port = me.port or (443 if me.scheme == "https" else 80)
    if port != my_port:
        return False
    hosts = (
        {me.hostname, "127.0.0.1", "localhost", "::1"} if me.hostname in ("127.0.0.1", "localhost") else {me.hostname}
    )
    if (s.hostname or "") in hosts:
        return True
    return bool(_ips(s.hostname or "") & _ips(me.hostname or ""))  # same service reached by IP or alias


def _ips(host: str) -> set[str]:
    try:
        return {str(i[4][0]) for i in socket.getaddrinfo(host, None)}
    except OSError:
        return set()


def check_url(url: str) -> None:
    s = urlsplit(url)
    if s.scheme not in ("http", "https"):
        raise EgressDenied(f"scheme '{s.scheme}' not allowed")
    if _is_self(s):
        # Smriti's own API must never be a source: in demo auth mode a crawler could
        # read documents as another person and re-ingest them under looser access.
        if (s.path or "/").startswith(SELF_PATHS):
            return
        raise EgressDenied("Smriti's own API cannot be used as a source (only its /demo-* sample targets)")
    check_host(s.hostname or "", s.port)


def _hook(request: httpx.Request) -> None:
    check_url(str(request.url))


def guarded_client(**kw: object) -> httpx.Client:
    """An httpx client that checks every request, redirects included."""
    hooks = dict(kw.pop("event_hooks", {}) or {})  # type: ignore[arg-type]
    hooks["request"] = [*hooks.get("request", []), _hook]
    return httpx.Client(event_hooks=hooks, **kw)  # type: ignore[arg-type]
