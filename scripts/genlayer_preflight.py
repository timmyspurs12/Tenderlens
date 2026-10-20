#!/usr/bin/env python3
"""Read-only deployment readiness preflight; never deploys or prints secret values."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

EXPECTED_SDK = "0.18.0"
EXPECTED_TEST_SUITE = "0.29.2"
EXPECTED_PYTEST = "9.1.1"
EXPECTED_TEST_SUITE_COMMIT = "343e3a358f9e235a93b49c60721ce7676585ff07"
NETWORKS = {
    "studionet": (61999, "https://studio.genlayer.com/api"),
    "testnet_bradbury": (4221, "https://rpc-bradbury.genlayer.com"),
    "testnet_asimov": (4221, "https://rpc-asimov.genlayer.com"),
}


def _version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _suite_commit() -> str | None:
    try:
        raw = importlib.metadata.distribution("genlayer-test").read_text("direct_url.json")
        parsed = json.loads(raw or "{}")
        return parsed.get("vcs_info", {}).get("commit_id")
    except (importlib.metadata.PackageNotFoundError, json.JSONDecodeError, AttributeError, TypeError):
        return None


def _check(level: str, name: str, detail: str) -> dict[str, str]:
    return {"level": level, "name": name, "detail": detail}


def _rpc_probe(url: str, expected_chain_id: int) -> tuple[bool, str]:
    request = urllib.request.Request(
        url,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            body = json.loads(response.read(65536))
        chain_id = int(body["result"], 16)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return False, f"RPC probe failed ({type(exc).__name__}); check endpoint availability and network access."
    if chain_id != expected_chain_id:
        return False, f"RPC returned chain ID {chain_id}, expected {expected_chain_id}."
    return True, f"RPC answered with expected chain ID {expected_chain_id}."


def collect_checks(root: Path, env: dict[str, str], versions: dict[str, str | None] | None = None,
                   suite_commit: str | None = None, rpc_probe=None) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    versions = versions or {name: _version(name) for name in ("genlayer-py", "genlayer-test", "pytest")}
    suite_commit = _suite_commit() if suite_commit is None else suite_commit

    if sys.version_info >= (3, 12):
        checks.append(_check("PASS", "Python", f"{sys.version_info.major}.{sys.version_info.minor} meets SDK minimum 3.12."))
    else:
        checks.append(_check("BLOCK", "Python", "GenLayer Python SDK requires Python 3.12 or newer."))

    requirements = (root / "requirements-genlayer.in").read_text(encoding="utf-8")
    if f"genlayer-py=={EXPECTED_SDK}" in requirements:
        checks.append(_check("PASS", "SDK requirements", f"Input requirements pin genlayer-py=={EXPECTED_SDK}."))
    else:
        checks.append(_check("BLOCK", "SDK requirements", f"Pin genlayer-py=={EXPECTED_SDK} or re-audit the SDK change."))
    lock_path = root / "requirements-genlayer.lock.txt"
    lock_text = lock_path.read_text(encoding="utf-8") if lock_path.exists() else ""
    suite_reference = f"genlayer-testing-suite.git@{EXPECTED_TEST_SUITE_COMMIT}"
    if f"genlayer-py=={EXPECTED_SDK}" in lock_text and suite_reference in lock_text and f"pytest=={EXPECTED_PYTEST}" in lock_text:
        checks.append(_check("PASS", "GenLayer dependency lock", "SDK, Direct Mode source commit, and pytest pins are present in the generated lock."))
    else:
        checks.append(_check("BLOCK", "GenLayer dependency lock", "Regenerate and verify the lock containing the audited SDK, test-suite commit, and pytest pins."))
    if versions.get("genlayer-py") == EXPECTED_SDK:
        checks.append(_check("PASS", "Installed SDK", f"genlayer-py=={EXPECTED_SDK} is installed."))
    else:
        checks.append(_check("BLOCK", "Installed SDK", f"Expected genlayer-py=={EXPECTED_SDK}; installed value is {versions.get('genlayer-py') or 'missing'}."))
    if versions.get("genlayer-test") == EXPECTED_TEST_SUITE and suite_commit == EXPECTED_TEST_SUITE_COMMIT:
        checks.append(_check("PASS", "Direct Mode test pin", "Pinned test suite version and source commit are installed."))
    else:
        checks.append(_check("BLOCK", "Direct Mode test pin", "Install the repository-locked genlayer-test 0.29.2 source commit before relying on Direct Mode checks."))
    if versions.get("pytest") == EXPECTED_PYTEST:
        checks.append(_check("PASS", "Pytest pin", f"pytest=={EXPECTED_PYTEST} is installed."))
    else:
        checks.append(_check("BLOCK", "Pytest pin", f"Expected pytest=={EXPECTED_PYTEST}; installed value is {versions.get('pytest') or 'missing'}."))

    contract_path = root / "tests/genlayer_direct/contracts/procurement_evaluation.py"
    source = contract_path.read_bytes()
    source_text = source.decode("utf-8")
    marker = re.search(r'^#\s*\{\s*"Depends"\s*:\s*"([^"]+)"\s*\}', source_text, re.MULTILINE)
    source_hash = hashlib.sha256(source).hexdigest()
    checks.append(_check("INFO", "Contract source", f"{len(source)} bytes; SHA-256 {source_hash}. No maximum source-size limit is asserted here."))
    runner_hash = r"[0-9abcdefghjkmnpqrstvwxyz]{52}"
    if marker is None or not re.fullmatch(r"py-genlayer:" + runner_hash, marker.group(1)):
        checks.append(_check("BLOCK", "Contract runner pin", "The contract does not name a 256-bit GVM32 py-genlayer content hash. Pin the exact runner hash verified on the target network."))
    else:
        checks.append(_check("INFO", "Contract runner pin", "A GVM32-shaped py-genlayer reference is present; shape alone does not establish that the artifact exists or is supported by the target."))
    checks.append(_check("BLOCK", "WASM compatibility", "Direct Mode is ordinary Python execution; no matching GenVM/WASM compile or deployed-runtime smoke test is recorded."))

    network = env.get("TENDERLENS_GENLAYER_NETWORK", "")
    if network in NETWORKS:
        chain_id, default_rpc = NETWORKS[network]
        checks.append(_check("PASS", "Target network", f"Selected {network} (SDK chain ID {chain_id})."))
        rpc_url = env.get("TENDERLENS_GENLAYER_RPC_URL") or default_rpc
        if rpc_probe is None:
            checks.append(_check("BLOCK", "RPC", "Target selection is not endpoint verification. Run --probe-rpc for a read-only chain-ID check."))
        else:
            ok, detail = rpc_probe(rpc_url, chain_id)
            checks.append(_check("PASS" if ok else "BLOCK", "RPC", detail))
    else:
        checks.append(_check("BLOCK", "Target network", "Set TENDERLENS_GENLAYER_NETWORK to studionet, testnet_bradbury, or testnet_asimov."))
        if rpc_probe is not None:
            checks.append(_check("BLOCK", "RPC", "Cannot probe without a supported target network."))

    private_key = env.get("TENDERLENS_GENLAYER_PRIVATE_KEY", "")
    if not private_key:
        checks.append(_check("BLOCK", "Deployment account", "TENDERLENS_GENLAYER_PRIVATE_KEY is not configured; no deployment will be attempted."))
    elif re.fullmatch(r"(?:0x)?[0-9a-fA-F]{64}", private_key):
        checks.append(_check("PASS", "Deployment account", "A private-key value is configured and has the expected shape; its value is not displayed or used to sign."))
    else:
        checks.append(_check("BLOCK", "Deployment account", "Configured private-key value has an invalid shape; value is not displayed."))
    checks.append(_check("BLOCK", "Funding", "No fee estimate or funded-wallet balance was verified; SDK/network fee requirements must be checked before an authorized deployment."))
    checks.append(_check("INFO", "Deployment path", "A fail-closed pinned-SDK driver is implemented but not configured or runtime-qualified; no production submitter or CLI pin is active."))
    return checks


def format_report(checks: list[dict[str, str]]) -> str:
    blocked = sum(item["level"] == "BLOCK" for item in checks)
    lines = [f"Deployment preflight: {'BLOCKED' if blocked else 'READY'} ({blocked} blocker(s))"]
    lines.extend(f"[{item['level']}] {item['name']}: {item['detail']}" for item in checks)
    lines.append("No deployment transaction was submitted.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--probe-rpc", action="store_true", help="make one read-only eth_chainId request to the selected network")
    args = parser.parse_args(argv)
    checks = collect_checks(args.root, dict(os.environ), rpc_probe=_rpc_probe if args.probe_rpc else None)
    print(format_report(checks))
    return 2 if any(item["level"] == "BLOCK" for item in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
