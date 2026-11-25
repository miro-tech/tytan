#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Altrixa / Tytan VPN — VLESS Reality fetcher (free nodes only).

  403 → premium_required (type=premium)
  500 → broken free VPS
  429 → rate limit

Fixes vs old script:
  - only type == "free"
  - unique IPs (+ optional fallbacks)
  - fp=random, fragment = country name
  - prefer API vless_url, then normalize
  - retry 500, back off 429
  - optional Gist

Usage:
  python3 altrixa_fetch.py -o tytan.txt
  ALTRIXA_USERNAME=myuser python3 altrixa_fetch.py -o tytan.txt
  python3 altrixa_fetch.py --include-fallback
  GIST_TOKEN=... GIST_ID=... python3 altrixa_fetch.py --gist
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
    """
    Force fingerprint=random and set fragment to country name.
    Keeps host/uuid/reality params from panel.
    """
    if not uri.startswith("vless://"):
        return uri

    # vless://uuid@host:port?query#fragment
    try:
        # urlparse handles vless:// as scheme
        p = urlparse(uri)
        # netloc = uuid@host:port
        qs = parse_qs(p.query, keep_blank_values=True)
        # parse_qs values are lists; flatten to single
        flat: dict[str, str] = {}
        for k, vals in qs.items():
            if not vals:
                flat[k] = ""
            else:
                flat[k] = vals[0]

        flat["fp"] = "random"
        # ensure common defaults if panel omitted them
        flat.setdefault("encryption", "none")
        flat.setdefault("flow", "xtls-rprx-vision")
        flat.setdefault("security", "reality")
        flat.setdefault("type", "tcp")
        flat.setdefault("headerType", "none")

        # stable query order for readability
        order = [
            "encryption",
            "flow",
            "security",
            "sni",
            "fp",
            "pbk",
            "sid",
            "type",
            "headerType",
        ]
        items = []
        for key in order:
            if key in flat and flat[key] is not None:
                items.append((key, flat[key]))
        for key, val in flat.items():
            if key not in order:
                items.append((key, val))

        query = urlencode(items, doseq=False, safe="")
        name = (country or p.fragment or p.hostname or "Altrixa").strip()
        return urlunparse(("vless", p.netloc, "", "", query, quote(name, safe="")))
    except Exception:
        # fallback: append fp + rename fragment roughly
        base = uri.split("#", 1)[0]
        if "fp=" in base:
            import re

            base = re.sub(r"([?&])fp=[^&]*", r"\1fp=random", base)
        else:
            base = base + ("&" if "?" in base else "?") + "fp=random"
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


def get_free_targets(session: requests.Session, include_fallback: bool) -> list[dict[str, Any]]:
    """Unique free-tier VPS targets: {ip, country, kind}."""
    r = session.get(f"{BASE}/api/servers?platform=android", timeout=30)
    r.raise_for_status()
    data = r.json()
    servers = data.get("servers") or []

    seen: set[str] = set()
    targets: list[dict[str, Any]] = []

    for srv in servers:
        if srv.get("status") is not True:
            continue
        if (srv.get("type") or "").lower() != "free":
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
                    targets.append({"ip": ip, "country": country, "kind": "primary"})
            if include_fallback:
                for fb in sub.get("fallback_vps_servers") or []:
                    if fb.get("status") is True and fb.get("ip_address"):
                        ip = str(fb["ip_address"])
                        if ip not in seen:
                            seen.add(ip)
                            fb_name = (fb.get("name") or ip).strip()
                            targets.append(
                                {
                                    "ip": ip,
                                    "country": f"{country} ({fb_name})",
                                    "kind": "fallback",
                                }
                            )
    return targets


def register_client(
    session: requests.Session,
    username: str,
    ip: str,
    country: str,
    retries: int = 2,
) -> tuple[str | None, str]:
    """
    Returns (normalized_vless_uri | None, status_tag).
    """
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
                time.sleep(delay * 2)
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
    ap = argparse.ArgumentParser(description="Altrixa free VLESS Reality fetcher")
    ap.add_argument("-o", "--output", default="tytan.txt")
    ap.add_argument("-u", "--username", default=DEFAULT_USER)
    ap.add_argument("--include-fallback", action="store_true")
    ap.add_argument("--gist", action="store_true", help="Push to GitHub Gist if env set")
    ap.add_argument("--sleep", type=float, default=0.8, help="Delay between register calls")
    args = ap.parse_args()

    print("=" * 45)
    print("Altrixa VPN — free VLESS Reality")
    print("=" * 45)

    session = requests.Session()
    session.headers.update(HEADERS)

    print("[1/3] servers (type=free only)...")
    targets = get_free_targets(session, args.include_fallback)
    if not targets:
        print("[FATAL] no free servers in API response")
        return 1
    print(f"  unique free IPs: {len(targets)}")
    for t in targets:
        print(f"    {t['ip']:18} {t['country']} [{t['kind']}]")

    print(f"\n[2/3] register-client as {args.username!r}...")
    configs: list[str] = []
    stats = {"ok": 0, "premium": 0, "server_error": 0, "rate_limit": 0, "other": 0}

    for i, t in enumerate(targets, 1):
        ip = t["ip"]
        country = t["country"]
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
        f"  free targets: {len(targets)}\n"
        f"  ok: {stats['ok']}  premium_skipped: {stats['premium']}  "
        f"5xx: {stats['server_error']}  429: {stats['rate_limit']}  other: {stats['other']}\n"
        f"  unique URIs: {len(uniq)} → {args.output}"
    )
    for u in uniq:
        print(f"  {u}")

    if args.gist:
        try:
            update_gist(content)
        except Exception as e:
            print(f"[GIST] failed: {e}")
            return 3

    print("=" * 45)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"\n[FATAL] {e}")
        sys.exit(1)
