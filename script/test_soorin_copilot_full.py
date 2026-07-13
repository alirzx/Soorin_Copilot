#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = REPO_ROOT / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from src.config.settings import get_settings  # noqa: E402


@dataclass(frozen=True)
class Scenario:
    name: str
    session_id: str
    message: str
    selected_ip: str | None = None


class Tee:
    def __init__(self, *streams: TextIO) -> None:
        self.streams = streams

    def write(self, data: str) -> int:
        for stream in self.streams:
            stream.write(data)
            stream.flush()
        return len(data)

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()


def section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def safe_command(command: list[str], timeout: int = 15) -> None:
    print("+", " ".join(command))
    try:
        result = subprocess.run(
            command,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        print(f"command_not_found={command[0]}")
        return
    except subprocess.TimeoutExpired:
        print("command_timed_out=True")
        return

    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip())
    print(f"exit_code={result.returncode}")


def check_tcp(host: str, port: int, timeout: float = 5.0) -> bool:
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            ok = True
            error = ""
    except OSError as exc:
        ok = False
        error = f"{type(exc).__name__}: {exc}"

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    print(f"tcp_target={host}:{port}")
    print(f"tcp_reachable={ok}")
    print(f"tcp_latency_ms={elapsed_ms}")
    if error:
        print(f"tcp_error={error}")
    return ok


def safe_settings_check(settings: Any) -> bool:
    checks = {
        "product_api_base_url_present": bool(settings.product_api_base_url),
        "product_login_path_present": bool(settings.product_login_path),
        "product_username_present": bool(settings.product_username),
        "product_password_present": bool(settings.product_password),
        "product_hwid_present": bool(settings.product_hwid),
        "product_captcha_bypass_present": bool(settings.product_captcha_bypass),
        "bootstrap_token_present": bool(settings.product_api_token),
        "refresh_seconds": settings.product_token_refresh_seconds,
    }
    for key, value in checks.items():
        print(f"{key}={value}")

    required = all(
        checks[key]
        for key in (
            "product_api_base_url_present",
            "product_login_path_present",
            "product_username_present",
            "product_password_present",
            "product_hwid_present",
            "product_captcha_bypass_present",
        )
    )
    print(f"login_configuration_complete={required}")
    return bool(required)


def login_preflight(settings: Any, timeout: float) -> bool:
    url = f"{settings.product_api_base_url.rstrip('/')}{settings.product_login_path}"
    started = time.perf_counter()
    try:
        response = requests.post(
            url,
            headers={
                "x-hwid": settings.product_hwid,
                "x-captcha-bypass": settings.product_captcha_bypass,
                "Content-Type": "application/json",
            },
            json={
                "username": settings.product_username,
                "password": settings.product_password,
            },
            timeout=timeout,
        )
    except requests.RequestException as exc:
        print("login_request_ok=False")
        print(f"login_error_type={type(exc).__name__}")
        print(f"login_error={exc}")
        return False

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    print("login_request_ok=True")
    print(f"login_status_code={response.status_code}")
    print(f"login_latency_ms={elapsed_ms}")

    try:
        payload = response.json()
    except ValueError:
        print("login_json_response=False")
        print("access_token_present=False")
        return False

    access_token_present = bool(payload.get("accessToken"))
    print("login_json_response=True")
    print(f"access_token_present={access_token_present}")

    # Never print or persist the token. Copilot must authenticate independently.
    return response.status_code in {200, 201} and access_token_present


def api_health(api_base: str, timeout: float) -> bool:
    url = f"{api_base.rstrip('/')}/health"
    started = time.perf_counter()
    try:
        response = requests.get(url, timeout=timeout)
    except requests.RequestException as exc:
        print("health_request_ok=False")
        print(f"health_error_type={type(exc).__name__}")
        print(f"health_error={exc}")
        return False

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    print("health_request_ok=True")
    print(f"health_status_code={response.status_code}")
    print(f"health_latency_ms={elapsed_ms}")
    try:
        body = response.json()
    except ValueError:
        print(response.text)
    else:
        print(json.dumps(body, indent=2, ensure_ascii=False))
    return response.ok


def run_chat(api_base: str, scenario: Scenario, timeout: float) -> bool:
    section(f"SCENARIO: {scenario.name}")
    print(f"session_id={scenario.session_id}")
    print(f"message={scenario.message}")
    print(f"ui_selected_ip={scenario.selected_ip or '<none>'}")

    payload: dict[str, Any] = {
        "session_id": scenario.session_id,
        "message": scenario.message,
    }
    if scenario.selected_ip:
        payload["ui_context"] = {"selected_ip": scenario.selected_ip}

    started = time.perf_counter()
    try:
        response = requests.post(
            f"{api_base.rstrip('/')}/chat",
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        print("chat_request_ok=False")
        print(f"chat_error_type={type(exc).__name__}")
        print(f"chat_error={exc}")
        return False

    elapsed = time.perf_counter() - started
    print("chat_request_ok=True")
    print(f"http_status={response.status_code}")
    print(f"elapsed_seconds={elapsed:.3f}")
    try:
        body = response.json()
    except ValueError:
        print("json_response=False")
        print(response.text)
    else:
        print("json_response=True")
        print(json.dumps(body, indent=2, ensure_ascii=False))
    return response.ok


def build_scenarios(stamp: str, ui_ip: str, asset_ip: str, second_ip: str) -> list[Scenario]:
    ui_session = f"ui-{stamp}"
    asset_session = f"asset-{stamp}"
    detection_session = f"detection-{stamp}"
    pair_session = f"pair-{stamp}"
    general_session = f"general-{stamp}"

    return [
        Scenario("UI selected ambiguous asset question", ui_session, "is this any kind of animal?", ui_ip),
        Scenario("UI selected identification follow-up", ui_session, "give me more identification information about it"),
        Scenario("UI selected graph-only follow-up", ui_session, "show all of its inbound and outbound connections"),
        Scenario("Explicit IP asset summary", asset_session, f"Tell me about {asset_ip}."),
        Scenario(
            "Detection-only detailed classification",
            detection_session,
            f"Why was {asset_ip} classified this way? Show the matched rules, evidence, and conflicts.",
        ),
        Scenario("Detection active-single follow-up", detection_session, "Show me more detection evidence."),
        Scenario("Pure graph outbound neighbors", asset_session, f"List all outbound connections of {asset_ip}."),
        Scenario(
            "Combined detection and topology",
            asset_session,
            "Does its detected role agree with its network topology and behavior?",
        ),
        Scenario(
            "Two-entity comparison",
            pair_session,
            f"Compare {asset_ip} and {second_ip}, including shared peers and network reach.",
        ),
        Scenario("Active-pair path follow-up", pair_session, "Find the shortest path between them."),
        Scenario("General knowledge with no providers", general_session, "What is Kerberos?"),
        Scenario("UI topic detachment", ui_session, "Not about this asset; explain Kerberos."),
        Scenario(
            "Explicit IP overrides UI selection",
            f"authority-{stamp}",
            f"Tell me about {asset_ip}.",
            ui_ip,
        ),
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run real Soorin Copilot integration and routing scenarios.")
    parser.add_argument("--api-base", default=os.getenv("API_BASE", "http://127.0.0.1:6998"))
    parser.add_argument("--ui-ip", default=os.getenv("UI_IP", "192.168.21.164"))
    parser.add_argument("--asset-ip", default=os.getenv("ASSET_IP", "192.168.30.111"))
    parser.add_argument("--second-ip", default=os.getenv("SECOND_IP", "192.168.30.115"))
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(os.getenv("OUT_DIR", str(REPO_ROOT / "copilot_test_results"))),
    )
    parser.add_argument("--product-timeout", type=float, default=20.0)
    parser.add_argument("--chat-timeout", type=float, default=180.0)
    parser.add_argument(
        "--continue-on-preflight-failure",
        action="store_true",
        help="Run scenarios even if product login preflight fails.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = args.output_dir / f"copilot_full_test_{stamp}.txt"

    with output_file.open("w", encoding="utf-8") as handle:
        tee = Tee(sys.stdout, handle)
        with redirect_stdout(tee), redirect_stderr(tee):
            section("SOORIN COPILOT FULL TEST")
            print(f"timestamp={datetime.now().astimezone().isoformat()}")
            print(f"repo_root={REPO_ROOT}")
            print(f"api_base={args.api_base}")
            print(f"ui_ip={args.ui_ip}")
            print(f"asset_ip={args.asset_ip}")
            print(f"second_ip={args.second_ip}")
            print(f"output_file={output_file}")

            settings = get_settings()
            parsed = requests.utils.urlparse(settings.product_api_base_url)
            product_host = parsed.hostname
            product_port = parsed.port or 80

            section("1. NETWORK PREFLIGHT")
            if product_host:
                safe_command(["ip", "route", "get", product_host])
                safe_command(["ping", "-c", "2", "-W", "2", product_host])
                product_reachable = check_tcp(product_host, int(product_port), timeout=5.0)
            else:
                print("product_url_parse_ok=False")
                product_reachable = False

            section("2. SAFE SETTINGS CHECK")
            configuration_ok = safe_settings_check(settings)

            section("3. LOGIN PREFLIGHT")
            if product_reachable and configuration_ok:
                login_ok = login_preflight(settings, timeout=args.product_timeout)
            else:
                login_ok = False
                print("login_preflight_skipped=True")
                print("login_preflight_reason=product_unreachable_or_configuration_incomplete")

            section("4. COPILOT API HEALTH")
            health_ok = api_health(args.api_base, timeout=20.0)
            if not health_ok:
                section("TEST ABORTED")
                print("reason=copilot_api_unavailable")
                print(f"result_file={output_file}")
                return 2

            if not login_ok and not args.continue_on_preflight_failure:
                section("TEST ABORTED")
                print("reason=product_login_preflight_failed")
                print("Use --continue-on-preflight-failure only for intentional partial-provider testing.")
                print(f"result_file={output_file}")
                return 3

            section("5. COPILOT ROUTING SCENARIOS")
            scenarios = build_scenarios(stamp, args.ui_ip, args.asset_ip, args.second_ip)
            passed = 0
            failed = 0
            for scenario in scenarios:
                if run_chat(args.api_base, scenario, timeout=args.chat_timeout):
                    passed += 1
                else:
                    failed += 1

            section("TEST SUMMARY")
            print(f"scenario_count={len(scenarios)}")
            print(f"scenario_http_successes={passed}")
            print(f"scenario_http_failures={failed}")
            print(f"product_tcp_reachable={product_reachable}")
            print(f"login_preflight_ok={login_ok}")
            print(f"copilot_health_ok={health_ok}")
            print(f"result_file={output_file}")
            return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
