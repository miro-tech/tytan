
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Altrixa VPN — VLESS Reality Fetcher
"""

import os
import sys
import requests
from urllib.parse import quote

BASE = "https://panel.altrixa.space"
USERNAME = os.getenv("ALTRIXA_USERNAME", "test123")
OUTPUT = "altrixa_vless.txt"

GIST_TOKEN = os.getenv("GIST_TOKEN")
GIST_ID = os.getenv("GIST_ID")
GIST_FILENAME = "altrixa_vless.txt"

HEADERS = {
    "User-Agent": "Dart/3.7 (dart:io)",
    "Accept": "application/json",
}

session = requests.Session()
session.headers.update(HEADERS)


def get_servers():
    print("[1/3] Получение актуальных серверов...")

    response = session.get(
        f"{BASE}/api/servers?platform=android",
        timeout=30
    )
    response.raise_for_status()

    data = response.json()
    servers = set()

    for server in data.get("servers", []):
        if server.get("status") is not True:
            continue

        for sub in server.get("sub_servers", []):
            if sub.get("status") is not True:
                continue

            vps = sub.get("vps_server") or {}

            if vps.get("status") is not True:
                continue

            ip = vps.get("ip_address")

            if ip:
                servers.add(str(ip))

    result = sorted(servers)

    if not result:
        raise RuntimeError("Список активных серверов пуст")

    print(f"Найдено серверов: {len(result)}")
    return result


def register_client(ip):
    payload = {
        "username": USERNAME,
        "ip": ip
    }

    try:
        response = session.post(
            f"{BASE}/api/vpn/register-client",
            json=payload,
            timeout=20
        )

        response.raise_for_status()
        data = response.json()

        if data.get("status") is not True:
            return None

        info = data.get("data") or {}

        required = [
            "uuid",
            "server_ip",
            "public_key",
            "short_id"
        ]

        if not all(info.get(key) for key in required):
            return None

        uuid = info["uuid"]
        server_ip = info["server_ip"]
        public_key = info["public_key"]
        short_id = info["short_id"]

        config = (
            f"vless://{uuid}@{server_ip}:443"
            f"?encryption=none"
            f"&flow=xtls-rprx-vision"
            f"&security=reality"
            f"&sni=www.apple.com"
            f"&fp=chrome"
            f"&pbk={quote(str(public_key), safe='')}"
            f"&sid={quote(str(short_id), safe='')}"
            f"&type=tcp"
            f"&headerType=none"
            f"#{quote(str(server_ip), safe='')}"
        )

        return config

    except (requests.RequestException, ValueError, TypeError) as error:
        print(f"  Ошибка запроса: {error}")
        return None


def update_gist(content):
    if not GIST_TOKEN or not GIST_ID:
        raise RuntimeError(
            "Не заданы GIST_TOKEN или GIST_ID"
        )

    print("[GIST] Обновление GitHub Gist...")

    headers = {
        "Authorization": f"Bearer {GIST_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    payload = {
        "files": {
            GIST_FILENAME: {
                "content": content
            }
        }
    }

    response = requests.patch(
        f"https://api.github.com/gists/{GIST_ID}",
        headers=headers,
        json=payload,
        timeout=30
    )

    response.raise_for_status()

    print("[GIST] Gist успешно обновлён")


def main():
    print("=" * 45)
    print("Altrixa VPN — VLESS Reality Fetcher")
    print("=" * 45)

    servers = get_servers()

    print("\n[2/3] Получение VLESS-конфигов...")

    configs = []
    failed = 0

    for index, ip in enumerate(servers, 1):
        print(f"[{index}/{len(servers)}] {ip}")

        config = register_client(ip)

        if config:
            configs.append(config)
            print("  OK")
        else:
            failed += 1
            print("  ERROR")

    if not configs:
        raise RuntimeError(
            "Не получено ни одного VLESS-конфига. "
            "Gist не будет изменён."
        )

    content = "\n".join(configs) + "\n"

    with open(OUTPUT, "w", encoding="utf-8") as file:
        file.write(content)

    print("\n[3/3] Готово.")
    print(f"Серверов: {len(servers)}")
    print(f"Успешно: {len(configs)}")
    print(f"Ошибок: {failed}")
    print(f"Файл: {OUTPUT}")

    update_gist(content)

    print("=" * 45)
    print("ОБНОВЛЕНИЕ ЗАВЕРШЕНО")
    print("=" * 45)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"\n[FATAL] {error}")
        sys.exit(1)
