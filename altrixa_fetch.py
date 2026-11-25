#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Altrixa / Tytan VPN — VLESS Reality fetcher.

API quirks:
  - /api/servers?platform=android returns different type=free/premium sets
    depending on client IP (Termux home IP ≠ GitHub Actions US IP).
  - 403 premium_required → paid node for this account/IP
  - 500 → flaky VPS
  - 429 → rate limit

Strategy:
  - collect ALL online primary (+ fallback) IPs
  - register-client each; skip premium/5xx
  - normalize URI: fp=random, fragment=country name
  - Gist auto if GIST_TOKEN + GIST_ID set (or --gist)

Usage:
  python3 altrixa_fetch.py -o tytan.txt
  python3 altrixa_fetch.py --free-only
  python3 altrixa_fetch.py --no-fallback
  GIST_TOKEN=... GIST_ID=... python3 altrixa_fetch.py
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlunparse

import requests

BASE = "https://panel.altrixa.space"
DEFAULT_USER = os.getenv("ALTRIXA_USERNAME", "test123")
GIST_TOKEN = os.getenv("GIST_TOKEN")
GIST_ID = os.getenv("GIST_ID")
GIST_FILENAME = os.getenv("GIST_FILENAME", "tytan.txt")

HEADERS = {
    "User-Agent": "Dart/3.7 (dart:io)",
    "Accept": "application/json",
    "Content-Type": "application/json",
}


def normalize_vless_uri(uri: str, country: str) -> str:
    """Force fp=random and fragment = country name."""
    if not uri.startswith("vless://"):
        return uri
    try:
        p = urlparse(uri)
        qs = parse_qs(p.query, keep_blank_values=True)
        flat: dict[str, str] = {k: (vals[0] if vals else "") for k, vals in qs.items()}
        flat["fp"] = "random"
        flat.setdefault("encryption", "none")
        flat.setdefault("flow", "xtls-rprx-vision")
        flat.setdefault("security", "reality")
        flat.setdefault("type", "tcp")
        flat.setdefault("headerType", "none")
        order = [
            "encryption", "flow", "security", "sni", "fp",
            "pbk", "sid", "type", "headerType",
        ]
        items = [(k, flat[k]) for k in order if k in flat]
        for k, v in flat.items():
            if k not in order:
                items.append((k, v))
        query = urlencode(items, doseq=False, safe="")
        name = (country or p.fragment or p.hostname or "Altrixa").strip()
        return urlunparse(("vless", p.netloc, "", "", query, quote(name, safe="")))
    except Exception:
        import re
        base = uri.split("#", 1)[0]
        if "fp=" in base:
            base = re.sub(r"([?&])fp=[^&]*", r"\1fp=random", base)
        else:
            base += ("&" if "?" in base else "?") + "fp=random"
        return f"{base}#{quote(country or 'Altrixa', safe='')}"


def build_uri_from_data(info: dict[str, Any], country: str) -> str | None:
    required = ("uuid", "server_ip", "public_key", "short_id")
    if not all(info.get(k) for k in required):
        return None
    sni = info.get("sni") or "www.apple.com"
    port = info.get("port") or 443
    items = [
        ("encryption", "none"),
        ("flow", "xtls-rprx-vision"),
        ("security", "reality"),
        ("sni", str(sni)),
        ("fp", "random"),
        ("pbk", str(info["public_key"])),
        ("sid", str(info["short_id"])),
        ("type", "tcp"),
        ("headerType", "none"),
    ]
    q = urlencode(items, safe="")
    name = quote((country or str(info["server_ip"])).strip(), safe="")
    return f"vless://{info['uuid']}@{info['server_ip']}:{port}?{q}#{name}"


def get_targets(
    session: requests.Session,
    free_only: bool,
    include_fallback: bool,
) -> list[dict[str, Any]]:
    """
    Unique VPS targets. free_only uses API type field (geo-dependent!).
    Prefer trying all and skipping premium at register time for max coverage on GHA.
    """
    r = session.get(f"{BASE}/api/servers?platform=android", timeout=30)
    r.raise_for_status()
    data = r.json()
    servers = data.get("servers") or []

    seen: set[str] = set()
    targets: list[dict[str, Any]] = []

    for srv in servers:
        if srv.get("status") is not True:
            continue
        api_type = (srv.get("type") or "").lower()
        if free_only and api_type != "free":
            continue
        country = (srv.get("name") or "?").strip()

        for sub in srv.get("sub_servers") or []:
            if sub.get("status") is not True:
                continue
            vps = sub.get("vps_server") or {}
            if vps.get("status") is True and vps.get("ip_address"):
                ip = str(vps["ip_address"])
                if ip not in seen:
                    seen.add(ip)
                    targets.append({
                        "ip": ip,
                        "country": country,
                        "kind": "primary",
                        "api_type": api_type or "?",
                    })
            if include_fallback:
                for fb in sub.get("fallback_vps_servers") or []:
                    if fb.get("status") is True and fb.get("ip_address"):
                        ip = str(fb["ip_address"])
                        if ip not in seen:
                            seen.add(ip)
                            fb_name = (fb.get("name") or ip).strip()
                            targets.append({
                                "ip": ip,
                                "country": f"{country} ({fb_name})",
                                "kind": "fallback",
                                "api_type": api_type or "?",
                            })
    return targets


def register_client(
    session: requests.Session,
    username: str,
    ip: str,
    country: str,
    retries: int = 2,
) -> tuple[str | None, str]:
    payload = {"username": username, "ip": ip}
    delay = 1.5

    for attempt in range(retries + 1):
        try:
            r = session.post(
                f"{BASE}/api/vpn/register-client",
                json=payload,
                timeout=25,
            )
        except requests.RequestException as e:
            if attempt < retries:
                time.sleep(delay)
                delay *= 1.5
                continue
            return None, f"network:{e}"

        if r.status_code == 429:
            if attempt < retries:
                time.sleep(max(delay * 2, 5.0))
                delay *= 2
                continue
            return None, "rate_limit"

        try:
            data = r.json()
        except ValueError:
            return None, f"http_{r.status_code}"

        if r.status_code == 403 or data.get("reason") == "premium_required":
            return None, "premium"

        if r.status_code >= 500 or data.get("status") is not True:
            if attempt < retries and r.status_code >= 500:
                time.sleep(delay)
                delay *= 1.5
                continue
            msg = data.get("message") or data.get("reason") or f"http_{r.status_code}"
            return None, f"server_error:{msg}"

        info = data.get("data") or {}
        ready = info.get("vless_url")
        if ready and str(ready).startswith("vless://"):
            return normalize_vless_uri(str(ready), country), "ok"

        built = build_uri_from_data(info, country)
        if built:
            return built, "ok"
        return None, "bad_payload"

    return None, "server_error"


def update_gist(content: str) -> None:
    if not GIST_TOKEN or not GIST_ID:
        raise RuntimeError("GIST_TOKEN / GIST_ID not set")
    headers = {
        "Authorization": f"Bearer {GIST_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {"files": {GIST_FILENAME: {"content": content}}}
    r = requests.patch(
        f"https://api.github.com/gists/{GIST_ID}",
        headers=headers,
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    print("[GIST] updated")


def main() -> int:
    ap = argparse.ArgumentParser(description="Altrixa VLESS Reality fetcher")
    ap.add_argument("-o", "--output", default="tytan.txt")
    ap.add_argument("-u", "--username", default=DEFAULT_USER)
    ap.add_argument(
        "--free-only",
        action="store_true",
        help="Only API type=free (geo-dependent; fewer nodes on GitHub US IP)",
    )
    ap.add_argument(
        "--no-fallback",
        action="store_true",
        help="Do not try fallback_vps_servers",
    )
    ap.add_argument(
        "--gist",
        action="store_true",
        help="Force Gist update (also auto if GIST_TOKEN+GIST_ID set)",
    )
    ap.add_argument("--no-gist", action="store_true", help="Disable Gist even if env set")
    ap.add_argument("--sleep", type=float, default=1.0, help="Delay between register calls")
    args = ap.parse_args()

    do_gist = (args.gist or (GIST_TOKEN and GIST_ID)) and not args.no_gist

    print("=" * 45)
    print("Altrixa VPN — VLESS Reality")
    print("=" * 45)

    session = requests.Session()
    session.headers.update(HEADERS)

    mode = "free-only" if args.free_only else "all (skip premium at register)"
    print(f"[1/3] servers ({mode}, fallback={not args.no_fallback})...")
    targets = get_targets(session, args.free_only, include_fallback=not args.no_fallback)
    if not targets:
        print("[FATAL] no servers in API response")
        return 1
    print(f"  unique IPs: {len(targets)}")
    for t in targets:
        print(f"    {t['ip']:18} {t['country']:28} [{t['kind']}] api_type={t['api_type']}")

    print(f"\n[2/3] register-client as {args.username!r}...")
    configs: list[str] = []
    stats = {"ok": 0, "premium": 0, "server_error": 0, "rate_limit": 0, "other": 0}

    for i, t in enumerate(targets, 1):
        ip, country = t["ip"], t["country"]
        print(f"[{i}/{len(targets)}] {ip} ({country})", end="", flush=True)
        uri, tag = register_client(session, args.username, ip, country)
        if uri:
            configs.append(uri)
            stats["ok"] += 1
            print("  OK")
        else:
            key = tag.split(":", 1)[0]
            if key in stats:
                stats[key] += 1
            else:
                stats["other"] += 1
            print(f"  SKIP ({tag})")
        time.sleep(args.sleep)

    if not configs:
        print("\n[FATAL] zero configs — not writing file / gist")
        return 2

    seen: set[str] = set()
    uniq: list[str] = []
    for u in configs:
        if u not in seen:
            seen.add(u)
            uniq.append(u)

    content = "\n".join(uniq) + "\n"
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(content)

    print("\n[3/3] done")
    print(
        f"  targets: {len(targets)}\n"
        f"  ok: {stats['ok']}  premium: {stats['premium']}  "
        f"5xx: {stats['server_error']}  429: {stats['rate_limit']}  other: {stats['other']}\n"
        f"  unique URIs: {len(uniq)} → {args.output}"
    )
    for u in uniq:
        print(f"  {u}")

    if do_gist:
        try:
            update_gist(content)
        except Exception as e:
            print(f"[GIST] failed: {e}")
            return 3
    else:
        print("[GIST] skipped (set GIST_TOKEN+GIST_ID or pass --gist)")

    print("=" * 45)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"\n[FATAL] {e}")
        sys.exit(1)
