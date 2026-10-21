#!/usr/bin/env python3
"""Offline, read-only GenLayer deployment qualification report.

This command performs local file, metadata and environment-presence checks only. It
never contacts RPC/GitHub/registries, imports the GenLayer SDK, creates an account,
or calls the deployment driver. It prints structured JSON and fails closed.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

EXPECTED_SDK = "0.19.0rc2"
EXPECTED_TEST_SUITE = "0.30.0rc2"
EXPECTED_PYTEST = "9.1.1"
EXPECTED_TEST_SUITE_COMMIT = "d84591b58746a6202b56b720199ce7b9f12fd666"
# Current official contract docs use this content-addressed runtime identifier.
# It proves the reference is documented, not that it is supported by a chosen target.
OFFICIAL_IMMUTABLE_RUNTIME = "1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6"
OFFICIAL_RUNTIME_SOURCE_URL = "https://github.com/genlayerlabs/genlayer-docs/blob/7c3ad7c4bc738a7c55d05df780dd76c2cb052db6/pages/developers/intelligent-contracts/first-contract.mdx"
SUPPORTED_NETWORKS = {
    "studionet": (61999, "https://studio.genlayer.com/api"),
    # The SDK preset is not accepted as proof of a GenLayer node RPC. An explicit
    # operator-approved node endpoint and live identity probe remain prerequisites.
    "testnet_bradbury": (4221, None),
    "testnet_asimov": (4221, "https://rpc-asimov.genlayer.com"),
}
SYNTHETIC_SNAPSHOT_ID = "00000000-0000-4000-8000-000000000001"
SYNTHETIC_EVALUATION_ID = "00000000-0000-4000-8000-000000000002"
SYNTHETIC_TENDER_ID = "00000000-0000-4000-8000-000000000003"
SYNTHETIC_REQUIREMENT_ID = "00000000-0000-4000-8000-000000000004"
SYNTHETIC_POLICY = "mandatory_gate_optional_advisory_v1"
VALID_STATUSES = {"verified", "blocked", "unknown", "requires_user_action"}
CONTRACT_PATH = Path("tests/genlayer_direct/contracts/procurement_evaluation.py")
REQUIRED_PROJECT_FILES = (
    Path("requirements-genlayer.in"),
    Path("requirements-genlayer.lock.txt"),
    Path("requirements-genlayer-build.in"),
    Path("requirements-genlayer-build.lock.txt"),
    Path(".devcontainer/setup.sh"),
    Path("backend/evaluation_adapter.py"),
    Path("backend/app.py"),
    Path("backend/genlayer_sdk_driver.py"),
    Path("scripts/genlayer_preflight.py"),
    Path("tests/genlayer_direct/contracts/procurement_evaluation.py"),
    Path("tests/genlayer_direct/test_procurement_evaluation.py"),
    Path("backend/test_api.py"),
    Path("backend/test_genlayer_preflight.py"),
    Path("backend/test_evaluation_adapter.py"),
    Path("backend/test_genlayer_sdk_driver.py"),
    Path("tests/genlayer_direct/test_genlayer_sdk_driver_surface.py"),
    Path(".github/workflows/tenderlens-checks.yml"),
    Path(".devcontainer/Dockerfile"),
    Path("docs/genlayer-milestone6-sdk-driver.md"),
    Path("docs/genlayer-milestone7-qualification.md"),
)


def _installed_version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def _installed_suite_commit() -> str | None:
    try:
        raw = importlib.metadata.distribution("genlayer-test").read_text("direct_url.json")
        parsed = json.loads(raw or "{}")
        commit = parsed.get("vcs_info", {}).get("commit_id")
        return commit if isinstance(commit, str) else None
    except (importlib.metadata.PackageNotFoundError, json.JSONDecodeError, AttributeError, TypeError):
        return None


def _check(identifier: str, status: str, summary: str, evidence: str, resolution: str,
           *, requires_user_action: bool, resolution_scope: str, mandatory: bool = True,
           owner: str | None = None) -> dict[str, Any]:
    if status not in VALID_STATUSES:
        raise ValueError("invalid qualification status")
    return {
        "id": identifier,
        "status": status,
        "summary": summary,
        "evidence": evidence,
        "resolution": resolution,
        "requires_user_action": requires_user_action,
        "owner": owner or ("operator" if requires_user_action else "repository_maintainer"),
        "check_scope": "local_read_only",
        "resolution_scope": resolution_scope,
        "external_interaction_required": resolution_scope in {"external_read_only", "external_action"},
        "external_side_effect_required": resolution_scope == "external_action",
        "mandatory": mandatory,
    }


def _safe_read(root: Path, relative: Path) -> bytes | None:
    try:
        return (root / relative).read_bytes()
    except OSError:
        return None


def _synthetic_snapshot_evidence() -> dict[str, Any]:
    """Build a deterministic, offline-only TenderLens snapshot integrity fixture."""
    base = {
        "schema_version": "tenderlens.evaluation-submission.v1",
        "evaluation_id": SYNTHETIC_EVALUATION_ID,
        "snapshot_id": SYNTHETIC_SNAPSHOT_ID,
        "tender_id": SYNTHETIC_TENDER_ID,
        "aggregation_policy": SYNTHETIC_POLICY,
        "requirements": [{
            "id": SYNTHETIC_REQUIREMENT_ID,
            "code": "OFFLINE-PREFLIGHT-SYNTHETIC",
            "text": "Synthetic placeholder for offline integrity checking only; not a real tender requirement.",
            "category": "eligibility",
            "mandatory": True,
            "kind": "judgment",
        }],
        "evidence": [],
    }
    canonical_base = json.dumps(base, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    integrity_sha256 = hashlib.sha256(canonical_base.encode("utf-8")).hexdigest()
    submission = {**base, "snapshot_sha256": integrity_sha256}
    exact_contract_input = json.dumps(submission, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {
        "status": "offline_synthetic_not_a_real_tender",
        "snapshot_id": SYNTHETIC_SNAPSHOT_ID,
        "integrity_sha256": integrity_sha256,
        "contract_input_sha256": hashlib.sha256(exact_contract_input.encode("utf-8")).hexdigest(),
        "contract_input_bytes": len(exact_contract_input.encode("utf-8")),
        "payload_json": exact_contract_input,
        "binding": "integrity_sha256 is SHA-256 of canonical JSON payload excluding snapshot_sha256; contract_input_sha256 hashes the exact submitted JSON string.",
        "live_snapshot_evidence": False,
    }


def _local_fee_evidence(root: Path, env: dict[str, str], network: str | None) -> dict[str, Any]:
    profile_name = env.get("TENDERLENS_GENLAYER_FEE_PROFILE", "")
    declared_digest = env.get("TENDERLENS_GENLAYER_FEE_PROFILE_SHA256", "")
    profile = None
    local_digest = None
    profile_status = "unknown"
    measured_at = None
    valid_until = None
    profile_network = None

    def parse_aware(value: Any):
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)

    def valid_uint(value: Any, *, positive: bool = False) -> bool:
        if type(value) is int:
            number = value
        elif isinstance(value, str) and len(value) <= 78 and re.fullmatch(r"[0-9]+", value):
            number = int(value)
        else:
            return False
        return (number > 0 if positive else number >= 0) and number <= 2**256 - 1

    if profile_name:
        try:
            profile_path = Path(profile_name).expanduser()
            if not profile_path.is_absolute():
                profile_path = root / profile_path
            raw = profile_path.read_bytes()
            if len(raw) > 1_000_000:
                profile_status = "blocked"
            else:
                local_digest = hashlib.sha256(raw).hexdigest()
                profile = json.loads(raw.decode("utf-8"))
                if not isinstance(profile, dict) or type(profile.get("version")) is not int or profile["version"] != 1:
                    profile_status = "blocked"
                elif not declared_digest or not re.fullmatch(r"[0-9a-f]{64}", declared_digest):
                    profile_status = "requires_user_action"
                elif local_digest != declared_digest:
                    profile_status = "blocked"
                else:
                    profile_network = profile.get("network")
                    measured_at = profile.get("measuredAt")
                    metadata = profile.get("tenderlens")
                    valid_until = metadata.get("validUntil") if isinstance(metadata, dict) else None
                    measured = parse_aware(measured_at)
                    expiry = parse_aware(valid_until)
                    contract_bytes = _safe_read(root, CONTRACT_PATH)
                    contract_text = contract_bytes.decode("utf-8", "replace") if contract_bytes is not None else ""
                    contract_hash = hashlib.sha256(contract_bytes).hexdigest() if contract_bytes is not None else None
                    runtime = _runtime_dependency(contract_text)
                    methods = profile.get("methods")
                    evaluate = methods.get("evaluate") if isinstance(methods, dict) else None
                    deploy = profile.get("deploy")
                    gas = metadata.get("maxEvmGas") if isinstance(metadata, dict) else None
                    fields_valid = (
                        profile.get("network") == network
                        and measured is not None and measured <= datetime.now(timezone.utc)
                        and measured is not None and datetime.now(timezone.utc) - measured <= timedelta(days=7)
                        and expiry is not None and expiry > datetime.now(timezone.utc)
                        and measured is not None and expiry is not None and expiry > measured
                        and isinstance(metadata, dict)
                        and metadata.get("sdkVersion") == EXPECTED_SDK
                        and metadata.get("contractSha256") == contract_hash
                        and metadata.get("runtime") == runtime
                        and type(metadata.get("maxSubmissionBytes")) is int
                        and 1 <= metadata["maxSubmissionBytes"] <= 250_000
                        and type(metadata.get("appealRounds")) is int
                        and 0 <= metadata["appealRounds"] <= 2
                        and isinstance(gas, dict)
                        and all(valid_uint(gas.get(stage), positive=True) for stage in ("deploy", "evaluate"))
                        and isinstance(deploy, dict) and isinstance(evaluate, dict)
                        and all(
                            valid_uint(entry.get(field), positive=field != "totalMessageFees")
                            for entry in (deploy, evaluate)
                            for field in ("leaderTimeunitsAllocation", "validatorTimeunitsAllocation",
                                          "executionBudgetPerRound", "totalMessageFees", "rotationsPerRound")
                        )
                    )
                    if not network:
                        profile_status = "requires_user_action"
                    else:
                        profile_status = "verified" if fields_valid else "blocked"
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            profile_status = "blocked"

    raw_cap = env.get("TENDERLENS_GENLAYER_MAX_TOTAL_COST_WEI", "")
    cap = int(raw_cap) if len(raw_cap) <= 78 and re.fullmatch(r"[0-9]+", raw_cap) and int(raw_cap) > 0 else None
    return {
        "profile_file_status": profile_status,
        "profile_sha256_local": local_digest,
        "profile_sha256_configured": declared_digest if re.fullmatch(r"[0-9a-f]{64}", declared_digest) else None,
        "profile_network_declared": profile_network,
        "profile_measured_at_declared": measured_at if isinstance(measured_at, str) else None,
        "profile_valid_until_declared": valid_until if isinstance(valid_until, str) else None,
        "profile_measurement_evidence": "local file digest, freshness, runtime, and schema checks only; not independently measured or target-qualified by this command",
        "live_fee_quote_status": "unknown_not_queried",
        "expected_total_cost_wei": None,
        "cost_cap_wei": str(cap) if cap is not None else None,
        "cost_cap_status": "configured_locally_not_authorized" if cap is not None else "missing_or_invalid",
        "cost_cap_covers_expected_cost": None,
        "full_cost_check_status": "unknown_no_live_fee_quote_or_balance_read",
        "live_fee_values_invented": False,
    }


def _driver_runtime_path_consistent(driver_source: str) -> bool:
    return bool(re.search(
        r'DEFAULT_CONTRACT\s*=\s*REPOSITORY_ROOT\s*/\s*"tests/genlayer_direct/contracts/procurement_evaluation\.py"',
        driver_source,
    ))


def _runtime_dependency(contract_text: str) -> str | None:
    # Official GenLayer contract docs require the Depends directive on line one.
    first_line = contract_text.splitlines()[0] if contract_text.splitlines() else ""
    match = re.fullmatch(r'#\s*\{\s*"Depends"\s*:\s*"py-genlayer:([^"]+)"\s*\}', first_line)
    return match.group(1).strip() if match else None


def _preflight_is_local_only(source_text: str) -> bool:
    try:
        tree = ast.parse(source_text)
    except SyntaxError:
        return False
    forbidden_import_roots = {"urllib", "urllib3", "socket", "http", "requests", "httpx", "aiohttp", "websocket", "websockets", "subprocess", "web3", "genlayer", "genlayer_py"}
    forbidden_calls = {"urlopen", "create_connection", "connect", "import_module", "Popen", "system", "popen", "run", "create_client", "deploy_contract", "write_contract", "send_transaction", "sign_transaction"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] in forbidden_import_roots for alias in node.names):
                return False
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in forbidden_import_roots:
                return False
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            if name in forbidden_calls:
                return False
    return True


def collect_report(root: Path, env: dict[str, str], *, versions: dict[str, str | None] | None = None,
                   suite_commit: str | None = None, python_version: tuple[int, int] | None = None) -> dict[str, Any]:
    """Collect only local evidence. Injectable metadata exists for deterministic tests."""
    root = Path(root)
    if versions is None:
        versions = {name: _installed_version(name) for name in ("genlayer-py", "genlayer-test", "pytest")}
    if suite_commit is None:
        suite_commit = _installed_suite_commit()
    py = python_version or sys.version_info[:2]
    checks: list[dict[str, Any]] = []

    checks.append(_check(
        "python.runtime", "verified" if py >= (3, 12) else "blocked",
        f"Python {py[0]}.{py[1]} {'meets' if py >= (3, 12) else 'does not meet'} the SDK minimum 3.12.",
        "Local interpreter version_info; no interpreter was installed or changed.",
        "Run ordinary checks in the repository's locked Python 3.12 environment; do not change the SDK pin to accommodate an older interpreter.",
        requires_user_action=False, resolution_scope="local_action",
    ))

    requirements = _safe_read(root, Path("requirements-genlayer.in"))
    lock = _safe_read(root, Path("requirements-genlayer.lock.txt"))
    build_requirements = _safe_read(root, Path("requirements-genlayer-build.in"))
    build_lock = _safe_read(root, Path("requirements-genlayer-build.lock.txt"))
    setup_script = _safe_read(root, Path(".devcontainer/setup.sh"))
    requirements_text = requirements.decode("utf-8", "replace") if requirements is not None else ""
    lock_text = lock.decode("utf-8", "replace") if lock is not None else ""
    build_requirements_text = build_requirements.decode("utf-8", "replace") if build_requirements is not None else ""
    build_lock_text = build_lock.decode("utf-8", "replace") if build_lock is not None else ""
    setup_text = setup_script.decode("utf-8", "replace") if setup_script is not None else ""
    pin_consistent = bool(
        re.search(r"(?m)^genlayer-py==0\.19\.0rc2\s*$", requirements_text)
        and re.search(r"(?m)^pytest==9\.1\.1\s*$", requirements_text)
        and EXPECTED_TEST_SUITE_COMMIT in requirements_text
        and re.search(r"(?m)^genlayer-py==0\.19\.0rc2\s*\\$", lock_text)
        and re.search(r"(?m)^pytest==9\.1\.1\s*\\$", lock_text)
        and EXPECTED_TEST_SUITE_COMMIT in lock_text
        and re.search(r"(?m)^setuptools==84\.2\.0\s*$", build_requirements_text)
        and re.search(r"(?m)^setuptools==84\.2\.0\s*\\$", build_lock_text)
        and "requirements-genlayer-build.lock.txt" in setup_text
    )
    checks.append(_check(
        "dependencies.lock_consistency", "verified" if pin_consistent else "blocked",
        "Pinned SDK, Direct Mode source commit, pytest root, and isolated build backend " + ("match" if pin_consistent else "do not match") + " their locks and setup references.",
        "Local runtime/build root files, generated locks, and setup script; lock hashes do not qualify a GenVM runtime.",
        "Update roots and regenerate/verify the Python 3.12 lock only with an evidenced compatible dependency change.",
        requires_user_action=False, resolution_scope="local_action",
    ))

    for check_id, package, expected in (
        ("dependencies.sdk_installed", "genlayer-py", EXPECTED_SDK),
        ("dependencies.direct_mode_suite", "genlayer-test", EXPECTED_TEST_SUITE),
        ("dependencies.pytest_installed", "pytest", EXPECTED_PYTEST),
    ):
        actual = versions.get(package)
        status = "verified" if actual == expected else "blocked"
        if check_id == "dependencies.direct_mode_suite" and status == "verified" and suite_commit != EXPECTED_TEST_SUITE_COMMIT:
            status = "blocked"
        detail = f"Installed {package} version matches the repository pin." if status == "verified" else f"Installed {package} version is missing or does not match the repository pin."
        if package == "genlayer-test" and status != "verified":
            detail += " The required source commit is also checked without contacting GitHub."
        checks.append(_check(
            check_id, status, detail,
            "Local importlib.metadata only; no package install, index lookup, or source checkout was attempted.",
            "Use the already-pinned Python 3.12 CI environment; this qualification command does not install dependencies.",
            requires_user_action=False, resolution_scope="local_action",
        ))
    suite_check = next(check for check in checks if check["id"] == "dependencies.direct_mode_suite")
    if versions.get("genlayer-test") == EXPECTED_TEST_SUITE and suite_commit == EXPECTED_TEST_SUITE_COMMIT:
        suite_check["evidence"] = f"Local package metadata reports the pinned version and source commit {EXPECTED_TEST_SUITE_COMMIT}; no network access."
    elif versions.get("genlayer-test") == EXPECTED_TEST_SUITE:
        suite_check["evidence"] = "Local package metadata version matches, but the expected source commit is not established by direct_url metadata."

    contract_bytes = _safe_read(root, CONTRACT_PATH)
    driver_bytes = _safe_read(root, Path("backend/genlayer_sdk_driver.py"))
    contract_text = contract_bytes.decode("utf-8", "replace") if contract_bytes is not None else ""
    driver_text = driver_bytes.decode("utf-8", "replace") if driver_bytes is not None else ""
    dependency = _runtime_dependency(contract_text)
    if dependency is None:
        runtime_status = "blocked"
        runtime_detail = "The contract runtime dependency directive is absent, malformed, or not on line one."
        runtime_resolution = "Restore the first-line Depends directive from the reviewed contract source; do not guess a runtime reference."
    elif dependency.lower() in {"latest", "stable", "main", "master", ""}:
        runtime_status = "blocked"
        runtime_detail = f"The configured py-genlayer reference is the mutable alias `{dependency}`; no immutable artifact is pinned."
        runtime_resolution = "Use an immutable runtime identifier documented by GenLayer, then independently establish target availability and compatibility. Do not guess an artifact ID."
    elif dependency == OFFICIAL_IMMUTABLE_RUNTIME:
        runtime_status = "verified"
        runtime_detail = "The contract reference matches the exact content-addressed runtime identifier published in current official contract documentation. This verifies the documented immutable reference only, not runtime availability or compatibility with a selected network."
        runtime_resolution = "Keep the documented immutable reference pinned; independently verify the selected target's supported runner and exact contract compatibility before deployment."
    else:
        runtime_status = "unknown"
        runtime_detail = "A non-alias runtime string is present, but it does not match the official immutable reference recorded by this repository; no compatibility or immutability is inferred."
        runtime_resolution = "Compare the reference against current official GenLayer docs and verify availability/compatibility for the selected target; do not infer it from tag shape."
    checks.append(_check(
        "runtime.artifact_immutability", runtime_status, runtime_detail,
        f"Local contract directive: {('py-genlayer:' + dependency) if dependency is not None else 'not found'}. Official source: {OFFICIAL_RUNTIME_SOURCE_URL}. No registry was contacted and no image was pulled.",
        runtime_resolution,
        requires_user_action=True, resolution_scope="external_read_only",
    ))
    checks.append(_check(
        "runtime.target_compatibility", "unknown",
        "No evidence establishes that the exact contract and runtime execute in the intended target GenVM/WASM environment.",
        "The repository's Direct Mode tests use the pinned Python test runner and mocked nondeterministic calls; the locked SDK source/API check is not a GenVM/WASM compile or execution test.",
        "After the target and immutable runner are authoritatively selected, maintainer must run a documented target-supported compile/execute compatibility test; no equivalent offline substitute is accepted.",
        requires_user_action=True, resolution_scope="external_action",
    ))

    docker_bytes = _safe_read(root, Path(".devcontainer/Dockerfile"))
    docker_text = docker_bytes.decode("utf-8", "replace") if docker_bytes is not None else ""
    from_line = next((line.strip() for line in docker_text.splitlines() if line.strip().upper().startswith("FROM ")), None)
    docker_digest_pinned = bool(from_line and "@sha256:" in from_line)
    checks.append(_check(
        "runtime.devcontainer_image_provenance", "verified" if from_line else "blocked",
        "The Dev Container base reference is present, but it is a Python development image, not the GenLayer contract runtime; its reference is " + ("digest-pinned." if docker_digest_pinned else "not digest-pinned."),
        f"Local .devcontainer/Dockerfile FROM line: {'present (reference value intentionally omitted)' if from_line else 'missing'}. No Docker command ran and no image was pulled.",
        "Do not use this development image as evidence of contract runtime compatibility. If immutable development-container reproduction is required, obtain the publisher's canonical digest separately.",
        requires_user_action=False, resolution_scope="external_read_only", mandatory=False,
    ))

    try:
        contract_ast = ast.parse(contract_text) if contract_bytes is not None else None
        driver_ast = ast.parse(driver_text) if driver_bytes is not None else None
    except SyntaxError:
        contract_ast = driver_ast = None
    contract_class = next((node for node in contract_ast.body if isinstance(node, ast.ClassDef) and node.name == "ProcurementEvaluation"), None) if contract_ast else None
    methods = {node.name for node in contract_class.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))} if contract_class else set()
    required_methods_present = {"__init__", "evaluate", "get_result"}.issubset(methods)
    config_consistent = bool(
        contract_ast and driver_ast and required_methods_present
        and _driver_runtime_path_consistent(driver_text)
    )
    checks.append(_check(
        "contract.configuration_consistency", "verified" if config_consistent else "blocked",
        "Local contract syntax, expected constructor/evaluate/result entry points, and driver's default source path " + ("are consistent." if config_consistent else "are missing, malformed, or inconsistent."),
        "Python AST parsing and local path/string comparison only; this is not contract compilation or runtime qualification.",
        "Repair the local source/path configuration and rerun ordinary tests; obtain separate target-runtime evidence before any deployment claim.",
        requires_user_action=False, resolution_scope="local_action",
    ))

    project_missing = [str(path) for path in REQUIRED_PROJECT_FILES if _safe_read(root, path) is None]
    checks.append(_check(
        "project.required_files", "verified" if not project_missing else "blocked",
        "Required contract, driver, adapter, locks, tests, documentation, and workflow files " + ("are present." if not project_missing else "are missing."),
        "Locally checked required paths: " + (", ".join(str(path) for path in REQUIRED_PROJECT_FILES) if not project_missing else ", ".join(project_missing)),
        "Restore missing repository-owned files from reviewed source control; no remote fetch is performed by preflight.",
        requires_user_action=False, resolution_scope="local_action",
    ))
    checks.append(_check(
        "project.source_digest_provenance", "unknown",
        "This report does not calculate a local source digest; no signed/authoritative manifest anchors these source bytes here.",
        "Use the repository's reviewed Git history and a separately trusted release/signature process; do not infer authenticity from a digest alone.",
        "If source provenance is needed, use the repository's reviewed Git history and a separately trusted release/signature process; do not infer authenticity from a digest alone.",
        requires_user_action=True, resolution_scope="external_read_only", mandatory=False,
    ))

    source_script = _safe_read(root, Path("scripts/genlayer_preflight.py"))
    local_only = bool(source_script and _preflight_is_local_only(source_script.decode("utf-8", "replace")))
    checks.append(_check(
        "preflight.side_effect_boundary", "verified" if local_only else "blocked",
        "Preflight implementation " + ("contains only local metadata/file/environment checks and no network, SDK client, signing, or transaction call path." if local_only else "does not satisfy the static local-only import/call allowlist."),
        "AST inspection of this script; only local files, interpreter metadata, and environment-variable presence are read.",
        "Keep all RPC, wallet, image-pull, signing, deployment, and submission operations outside this report; add an explicit review and authorization process before future external checks.",
        requires_user_action=False, resolution_scope="local_action",
    ))

    network = env.get("TENDERLENS_GENLAYER_NETWORK", "")
    if not network:
        network_status, network_summary = "requires_user_action", "No target network has been selected."
        network_evidence = "TENDERLENS_GENLAYER_NETWORK is absent; the variable name is reported, never its value."
    elif network not in SUPPORTED_NETWORKS:
        network_status, network_summary = "blocked", "The configured target name is not one of the SDK's locally listed network descriptors."
        network_evidence = "TENDERLENS_GENLAYER_NETWORK is present but its value is intentionally not printed."
    else:
        network_status, network_summary = "verified", "An explicit network name is configured and matches a locally listed SDK descriptor; endpoint identity is not verified."
        network_evidence = "TENDERLENS_GENLAYER_NETWORK is present and recognized by the local descriptor table; value omitted."
    checks.append(_check(
        "target.network_selection", network_status, network_summary, network_evidence,
        "User/maintainer must select the intended supported target; do not interpret this local name match as proof of the remote network.",
        requires_user_action=network_status != "verified", resolution_scope="local_action",
    ))

    rpc_value = env.get("TENDERLENS_GENLAYER_RPC_URL", "")
    if network == "testnet_bradbury":
        rpc_status = "requires_user_action"
        rpc_summary = "No qualified Bradbury GenLayer node RPC is established locally; the SDK preset and underlying EVM RPC are not accepted as node identity evidence."
        rpc_evidence = "No endpoint was contacted or printed; an operator-approved GenLayer node URL and documented identity procedure are absent from offline evidence."
    elif network in SUPPORTED_NETWORKS:
        expected_rpc = SUPPORTED_NETWORKS[network][1]
        if not rpc_value or rpc_value == expected_rpc:
            rpc_status = "verified"
            rpc_summary = "The configured endpoint string matches the local SDK descriptor (or no override is configured). This does not establish that the remote service is a GenLayer node or belongs to the selected network."
            rpc_evidence = "Local SDK endpoint mapping comparison only; endpoint value omitted and no request made."
        else:
            rpc_status = "blocked"
            rpc_summary = "Configured RPC override differs from the selected SDK descriptor; arbitrary endpoint overrides are rejected by this local policy."
            rpc_evidence = "String equality check only; URL value omitted to avoid exposing embedded credentials."
    else:
        rpc_status = "requires_user_action"
        rpc_summary = "An endpoint cannot be resolved until a supported target is selected."
        rpc_evidence = "No network selection was established; RPC URL value is not printed."
    checks.append(_check(
        "target.endpoint_configuration", rpc_status, rpc_summary, rpc_evidence,
        "Select the target first; if an override is supplied, it must match the pinned SDK descriptor. A future network identity check requires separate explicit authorization and is not part of this preflight.",
        requires_user_action=rpc_status != "verified", resolution_scope="external_read_only",
    ))
    checks.append(_check(
        "target.remote_identity", "unknown",
        "The remote chain/network identity is not verified by a local name, endpoint string, or chain ID. Asimov and Bradbury share chain ID 4221 in the pinned SDK.",
        "No RPC request or network probe was performed; no existing local evidence authenticates the remote endpoint.",
        "After explicit authorization, obtain authoritative target identity evidence using the operator-approved read-only procedure; chain ID alone is insufficient for the two 4221 descriptors.",
        requires_user_action=True, resolution_scope="external_read_only",
    ))

    key = env.get("TENDERLENS_GENLAYER_PRIVATE_KEY", "")
    if not key:
        account_status, account_summary = "requires_user_action", "No deployment signer is configured."
        account_evidence = "TENDERLENS_GENLAYER_PRIVATE_KEY is absent; no secret value is printed or requested."
    elif re.fullmatch(r"(?:0x)?[0-9a-fA-F]{64}", key):
        account_status, account_summary = "verified", "A private-key environment value is present and has the expected hexadecimal length only; ownership, account derivation, authorization, and usable balance are not checked."
        account_evidence = "Presence and shape were checked in memory; the value and derived address are not output. No account was created or unlocked."
    else:
        account_status, account_summary = "blocked", "Configured signer value does not have the expected private-key text shape; the value is not displayed."
        account_evidence = "Only a boolean and shape check was performed; the value is not output."
    checks.append(_check(
        "account.configuration_presence", account_status, account_summary, account_evidence,
        "If a deployment signer is needed later, configure it through the approved secret manager; never put a key in chat, logs, or repository files.",
        requires_user_action=account_status != "verified", resolution_scope="external_action",
    ))
    checks.append(_check(
        "account.authorization", "unknown",
        "Signer ownership and target-level deployment authorization cannot be established without a protected, explicitly authorized process.",
        "No key was loaded into an SDK account, no signature was requested, and no RPC call was made.",
        "Authorized operator/maintainer must establish signer ownership and target permissions out of band; keep keys out of this report.",
        requires_user_action=True, resolution_scope="external_action",
    ))

    broadcast_opt_in = env.get("TENDERLENS_GENLAYER_BROADCAST_AUTHORIZED", "") == "1"
    checks.append(_check(
        "broadcast.explicit_opt_in", "verified" if broadcast_opt_in else "requires_user_action",
        "The explicit broadcast opt-in flag is " + ("configured." if broadcast_opt_in else "not configured; it is false by default."),
        "Only comparison with the literal value `1`; environment values are never included in the report.",
        "A future deployment requires separate explicit authorization after all qualification blockers are resolved; this report never broadcasts even if the flag is set.",
        requires_user_action=not broadcast_opt_in, resolution_scope="external_action",
    ))

    driver_source = driver_text
    app_bytes = _safe_read(root, Path("backend/app.py"))
    app_source = app_bytes.decode("utf-8", "replace") if app_bytes else ""
    has_identity_gate = (
        "_verify_builtin_network_identity" in driver_source
        and "network_identity_not_verified" in driver_source
        and "network_chain_id_mismatch" in driver_source
    )
    checks.append(_check(
        "target.identity_verifier_gate_present", "verified" if has_identity_gate else "blocked",
        "The driver fails closed unless a built-in target check or explicit identity verifier accepts the connected SDK client." if has_identity_gate else "The driver does not fail closed on missing target-identity verification.",
        "Local driver source inspection only; no identity check was invoked and no endpoint was contacted.",
        "Keep identity verification mandatory for read and write contexts; a local gate does not authenticate a remote endpoint.",
        requires_user_action=False, resolution_scope="local_action",
    ))
    identity_provider_wired = (
        "GenLayerSDKDriver" in app_source and "_verify_builtin_network_identity" in driver_source
    )
    checks.append(_check(
        "target.identity_verifier_availability", "verified" if identity_provider_wired else "blocked",
        "The application wires the driver, which implements built-in chain/genesis/consensus identity checks; no live endpoint identity is established here." if identity_provider_wired else "A target identity-check implementation is not wired into the application.",
        "Static inspection of backend/app.py and the driver only; local implementation availability is not live identity evidence.",
        "After explicit authorization, run the documented identity procedure against the operator-approved node; retain remote identity as unknown until then.",
        requires_user_action=True, resolution_scope="external_read_only", owner="repository_maintainer",
    ))
    has_runtime_gate = "def _qualify_runtime" in driver_source and "runtime_not_qualified" in driver_source
    checks.append(_check(
        "runtime.qualification_gate_present", "verified" if has_runtime_gate else "blocked",
        "The driver has a fail-closed no-broadcast target-runtime qualification path." if has_runtime_gate else "The driver's runtime-qualification gate is absent.",
        "Local driver source inspection only; no `gen_call` was made and no runtime artifact was qualified.",
        "Keep target execution fail-closed; local provider code does not establish live runtime compatibility.",
        requires_user_action=False, resolution_scope="local_action",
    ))
    runtime_provider_wired = (
        "GenLayerSDKDriver" in app_source and "def _qualify_runtime" in driver_source
        and 'method="gen_call"' in driver_source
    )
    checks.append(_check(
        "runtime.qualification_verifier_availability", "verified" if runtime_provider_wired else "blocked",
        "The default driver implements a no-broadcast GenLayer node runtime probe; this command did not invoke it." if runtime_provider_wired else "A target runtime-probe implementation is not wired into the application.",
        "Static inspection only; the node endpoint, contract runner, and exact input remain unqualified until a separately authorized live read-only probe.",
        "After target identity and exact immutable runtime are established, run the no-broadcast probe under explicit operator authorization and retain its report.",
        requires_user_action=True, resolution_scope="external_read_only", owner="repository_maintainer",
    ))
    has_funding_gate = (
        "def _verify_funding_and_quote" in driver_source
        and "approved_cost_cap_exceeded" in driver_source
        and "insufficient_wallet_balance" in driver_source
    )
    checks.append(_check(
        "funding.verifier_gate_present", "verified" if has_funding_gate else "blocked",
        "The default path requires nonzero fee quotes, a full-cost cap, and an observed balance before it can prepare a broadcast-capable runtime." if has_funding_gate else "The driver's measured-cost/funding gate is absent.",
        "Local driver source inspection only; no balance, fee, estimate, or RPC was queried.",
        "Keep unknown cost/balance fail-closed; implementation presence does not establish actual funding sufficiency.",
        requires_user_action=False, resolution_scope="local_action",
    ))
    funding_provider_wired = (
        "GenLayerSDKDriver" in app_source and "def _verify_funding_and_quote" in driver_source
        and "estimate_transaction_fees" in driver_source
    )
    checks.append(_check(
        "funding.verifier_availability", "verified" if funding_provider_wired else "blocked",
        "The default driver implements measured-profile fee quotes and a balance/cap gate; no fee or funding evidence is read by this command." if funding_provider_wired else "A measured-fee/funding implementation is not wired into the application.",
        "Static inspection of backend/app.py and the driver; the provider was not invoked, the profile is not live-qualified, and no balance was queried.",
        "After target/runtime qualification, an authorized operator must provide a valid measured profile and obtain current fee/balance evidence; none is established by this local implementation check.",
        requires_user_action=True, resolution_scope="external_read_only", owner="repository_maintainer",
    ))
    checks.append(_check(
        "funding.confirmation", "unknown",
        "No target-specific fee budget or funded-account balance has been confirmed.",
        "The read-only qualification process deliberately does not invoke any funding verifier or query a wallet/RPC.",
        "After the target and fee mechanism are established, an authorized operator must perform the approved funding/fee check; this preflight must remain non-mutating.",
        requires_user_action=True, resolution_scope="external_read_only",
    ))

    workflow_bytes = _safe_read(root, Path(".github/workflows/tenderlens-checks.yml"))
    workflow_text = workflow_bytes.decode("utf-8", "replace") if workflow_bytes else ""
    api_tests = _safe_read(root, Path("backend/test_api.py"))
    adapter_tests = _safe_read(root, Path("backend/test_evaluation_adapter.py"))
    direct_contract_tests = _safe_read(root, Path("tests/genlayer_direct/test_procurement_evaluation.py"))
    driver_tests = _safe_read(root, Path("backend/test_genlayer_sdk_driver.py"))
    preflight_tests = _safe_read(root, Path("backend/test_genlayer_preflight.py"))
    workflow_has_backend = "backend.test_evaluation_adapter" in workflow_text and "backend.test_genlayer_sdk_driver" in workflow_text
    workflow_has_preflight = "backend.test_genlayer_preflight" in workflow_text
    workflow_has_direct = "pytest tests/genlayer_direct" in workflow_text
    lifecycle_tests_present = bool(driver_tests and b"test_reconcile_polls_deployment_but_does_not_implicitly_submit_evaluation" in driver_tests and b"test_uncertain_poll_timeout_never_proceeds_to_evaluation" in driver_tests)
    adapter_tests_present = bool(adapter_tests and b"test_result_must_match_request_snapshot_policy_and_actual_recorded_refs" in adapter_tests and b"test_tx_hash_database_write_failure_leaves_unknown_and_never_retries" in adapter_tests)
    preflight_tests_present = bool(preflight_tests and b"test_preflight_is_local_only_and_makes_no_network_or_broadcast_calls" in preflight_tests and b"test_network_probe_flag_is_not_available" in preflight_tests)
    binding_tests_present = bool(driver_tests and b"test_driver_rejects_caller_supplied_raw_payload" in driver_tests and b"test_broadcast_requires_explicit_authorization_before_client_or_rpc" in driver_tests and b"test_exact_snapshot_authorization_is_checked_before_sdk_and_again_before_submission" in driver_tests)
    transaction_safety_tests_present = bool(
        driver_tests
        and b"test_measured_profile_quotes_both_stages_and_checks_full_cost_against_balance" in driver_tests
        and b"test_cost_ceiling_applies_to_genlayer_deposits_and_all_outer_evm_gas" in driver_tests
        and b"test_unknown_fee_or_evm_cost_fails_closed_instead_of_becoming_zero" in driver_tests
        and b"test_fee_profile_quotes_are_passed_to_sdk_and_prepared_transaction_is_guarded_pre_send" in driver_tests
        and b"test_unjournaled_send_methods_are_rejected_before_provider_can_broadcast" in driver_tests
        and b"test_insufficient_or_unavailable_balance_is_not_treated_as_zero_cost_or_funding" in driver_tests
        and b"test_missing_invalid_and_expired_authorization_never_initialize_sdk" in driver_tests
        and b"test_missing_network_or_rpc_and_unpinned_endpoints_fail_before_sdk" in driver_tests
        and b"test_wrong_sdk_chain_id_and_wrong_consensus_addresses_are_not_accepted" in driver_tests
        and b"test_preflight_runs_exact_snapshot_no_broadcast_and_without_signing_capability" in driver_tests
    )
    snapshot_integrity_tests_present = bool(adapter_tests and b"test_payload_digest_relationship_is_checked_before_constructor_input" in adapter_tests and b"test_policy_mismatch_unsupported_schema_and_malformed_snapshot_are_rejected" in adapter_tests and b"test_modified_row_with_invalid_hmac_fails_closed" in adapter_tests)
    readonly_recovery_test_present = bool(driver_tests and b"test_readonly_recovery_context_needs_no_broadcast_or_funding_gate" in driver_tests)
    identity_and_result_tests_present = bool(
        api_tests and b"test_json_request_rejects_duplicate_object_keys" in api_tests
        and b"test_factory_uses_only_server_environment_and_injected_submitter_for_broadcast_gate" in api_tests
        and b"test_submit_route_keeps_authentication_and_maps_request_conflicts" in api_tests
        and b"test_invalid_finalized_result_is_durable_and_never_reported_as_unknown_or_completed" in api_tests
        and direct_contract_tests and b"test_utf8_encoded_submission_size_limit_is_enforced" in direct_contract_tests
        and b"test_oversized_result_fails_without_committing_a_partial_evaluation" in direct_contract_tests
        and driver_tests and b"test_query_only_connection_requires_target_identity_verifier" in driver_tests
        and b"test_false_or_non_boolean_runtime_qualification_never_reaches_sdk" in driver_tests
        and b"test_false_or_non_boolean_identity_verifier_results_never_authorize_writes" in driver_tests
        and b"test_mismatched_or_missing_transaction_reference_fails_closed" in driver_tests
        and b"test_mismatched_finalized_receipt_is_not_used_for_contract_address" in driver_tests
        and b"test_finalized_receipt_must_report_successful_execution" in driver_tests
        and b"test_finalized_transaction_timeout_and_undetermined_states_are_not_success" in driver_tests
        and b"test_prepared_runtime_cannot_be_mutated_or_reused_after_config_replacement" in driver_tests
        and b"test_contract_source_change_after_preflight_blocks_deployment" in driver_tests
        and adapter_tests and b"test_result_reason_must_be_bounded_and_cannot_carry_uncited_evidence_ids" in adapter_tests
    )
    tests_covered = (workflow_has_backend and workflow_has_preflight and workflow_has_direct and lifecycle_tests_present
                     and adapter_tests_present and preflight_tests_present and binding_tests_present
                     and transaction_safety_tests_present and snapshot_integrity_tests_present
                     and readonly_recovery_test_present
                     and identity_and_result_tests_present)
    checks.append(_check(
        "tests.ci_coverage_configuration", "verified" if tests_covered else "blocked",
        "Local CI configuration includes backend/adapter/preflight/driver tests plus offline cases for snapshot/capability binding, fee-profile staleness, full-cost caps, balance/authorization gates, network identity, exact-snapshot preflight without signing/broadcast, lifecycle recovery, and Direct Mode." if tests_covered else "One or more relevant backend, binding, transaction-safety, lifecycle, recovery, preflight, or Direct Mode tests are absent from the local CI workflow.",
        "Read-only inspection of the workflow and test names; this preflight does not run pytest/unittest or query GitHub Actions status.",
        "Run the repository CI workflow for the current commit and retain results; Direct Mode is not GenVM/WASM compatibility evidence.",
        requires_user_action=False, resolution_scope="external_read_only",
    ))
    checks.append(_check(
        "recovery.uncertain_transaction_safety", "verified" if lifecycle_tests_present and "def reconcile(" in driver_source and "resume: bool = False" in driver_source else "blocked",
        "Persisted-hash reconciliation defaults to query-only; resuming a not-yet-attempted next phase is explicitly gated." if lifecycle_tests_present else "Required reconciliation tests or explicit default gating are missing.",
        "Local driver and offline fake-client tests inspect the default `resume=False` and do not submit real transactions.",
        "Keep uncertain/hashless states operator-visible and never retry them automatically; no external action is taken by this check.",
        requires_user_action=False, resolution_scope="local_action",
    ))

    if not _preflight_is_local_only(Path(__file__).read_text(encoding="utf-8")):
        # This self-check describes the deployed script itself and cannot make the
        # report ready if the implementation has acquired a network/broadcast path.
        side_effect_check = next(item for item in checks if item["id"] == "preflight.side_effect_boundary")
        side_effect_check["status"] = "blocked"
        side_effect_check["summary"] = "The checked-in preflight implementation does not satisfy its local-only static allowlist."

    mandatory_checks = [item for item in checks if item["mandatory"]]
    ready = bool(mandatory_checks) and all(item["status"] == "verified" for item in mandatory_checks)
    synthetic_snapshot = _synthetic_snapshot_evidence()
    local_contract_sha256 = hashlib.sha256(contract_bytes).hexdigest() if contract_bytes is not None else None
    fee_evidence = _local_fee_evidence(root, env, network if network in SUPPORTED_NETWORKS else None)
    selected_network = network if network in SUPPORTED_NETWORKS else None
    descriptor_chain_id = SUPPORTED_NETWORKS[selected_network][0] if selected_network else None
    authorized_scope_names = (
        "TENDERLENS_GENLAYER_AUTHORIZATION_EXPIRES_AT",
        "TENDERLENS_GENLAYER_AUTHORIZED_NETWORK",
        "TENDERLENS_GENLAYER_AUTHORIZED_RPC_ENDPOINT_SHA256",
        "TENDERLENS_GENLAYER_AUTHORIZED_SIGNER_ADDRESS",
        "TENDERLENS_GENLAYER_AUTHORIZED_CONTRACT_SHA256",
        "TENDERLENS_GENLAYER_AUTHORIZED_FEE_PROFILE_SHA256",
        "TENDERLENS_GENLAYER_AUTHORIZED_MAX_TOTAL_COST_WEI",
        "TENDERLENS_GENLAYER_AUTHORIZED_SNAPSHOT_ID",
        "TENDERLENS_GENLAYER_AUTHORIZED_SNAPSHOT_SHA256",
    )
    authorization_scope_complete = all(env.get(name) for name in authorized_scope_names)
    authorization_expiry = env.get("TENDERLENS_GENLAYER_AUTHORIZATION_EXPIRES_AT")
    if not authorization_expiry:
        authorization_expiry_status = "requires_user_action"
    else:
        try:
            parsed_expiry = datetime.fromisoformat(authorization_expiry.replace("Z", "+00:00"))
            if parsed_expiry.tzinfo is None or parsed_expiry.utcoffset() is None:
                authorization_expiry_status = "blocked"
            elif parsed_expiry.astimezone(timezone.utc) <= datetime.now(timezone.utc):
                authorization_expiry_status = "blocked"
            else:
                authorization_expiry_status = "verified"
        except ValueError:
            authorization_expiry_status = "blocked"
    report = {
        "schema_version": "tenderlens.deployment-qualification.v2",
        "ready_to_deploy": ready,
        "read_only": True,
        "external_side_effects_performed": [],
        "secret_values_included": False,
        "offline_evidence": {
            "target": {
                "selected_network_name": selected_network,
                "target_selection_check_status": network_status,
                "sdk_descriptor_chain_id": descriptor_chain_id,
                "remote_chain_id_observed": None,
                "remote_chain_identity_status": "unknown_no_rpc_request",
                "node_rpc_identity_status": "unknown_not_probed",
                "node_rpc_identity_qualified": False,
                "rpc_endpoint_configured": bool(rpc_value),
                "rpc_endpoint_value_reported": False,
                "endpoint_configuration_check_status": rpc_status,
            },
            "contract": {
                "local_source_sha256": local_contract_sha256,
                "runtime_reference_from_local_source": dependency,
                "local_source_present": contract_bytes is not None,
                "deployment_status": "not_attempted_by_offline_preflight",
                "deployed_contract_address": None,
                "deployment_receipt_or_finalized_status": "unknown_no_deployment_evidence",
                "deployed_source_hash_match": "unknown_not_deployed_or_queried",
            },
            "synthetic_snapshot": synthetic_snapshot,
            "signer_and_funding": {
                "private_key_presence_shape_check": account_status,
                "signer_address": None,
                "signer_address_status": "unknown_not_derived",
                "private_key_loaded_into_sdk_account": False,
                "funding_status": "unknown_not_queried",
                "balance_wei": None,
                "funding_evidence_source": "none_offline_only",
            },
            "measured_fee_profile_and_cost": fee_evidence,
            "authorization": {
                "broadcast_flag_enabled_in_environment": broadcast_opt_in,
                "scoped_authorization_fields_all_present": authorization_scope_complete,
                "expiry_local_format_and_time_status": authorization_expiry_status,
                "expiry_value_reported": False,
                "live_authorization_validated": False,
                "live_submission_authorized": False,
            },
            "signing_and_broadcast": {
                "signing_capability_available_to_this_command": False,
                "broadcast_capability_available_to_this_command": False,
                "signing_performed": False,
                "broadcast_performed": False,
                "signed_transactions": 0,
                "broadcast_transactions": 0,
            },
            "check_status": {
                "ready_to_deploy": ready,
                "mandatory_check_count": len(mandatory_checks),
                "mandatory_nonverified_check_ids": [
                    item["id"] for item in mandatory_checks if item["status"] != "verified"
                ],
                "live_qualification_complete": False,
            },
            "live_qualification_gaps": [
                "authoritatively identify the intended network and qualified GenLayer node RPC",
                "verify the node's chain identity and target-specific consensus contracts",
                "qualify the exact immutable contract runtime and exact submission with no-broadcast execution",
                "obtain the exact live snapshot from the authenticated backend and verify its persisted integrity binding",
                "establish signer ownership and explicit, unexpired authorization scoped to network, signer, contract, profile, cap, and exact snapshot",
                "read current target-account balance and measured fee quotes; calculate full deploy plus evaluation GenLayer fees and EVM gas against the cap",
                "if separately authorized later, verify finalized deployment and deployed source hash before any evaluation write",
            ],
        },
        "checks": checks,
        "summary": {
            "total": len(checks),
            "verified": sum(item["status"] == "verified" for item in checks),
            "blocked": sum(item["status"] == "blocked" for item in checks),
            "unknown": sum(item["status"] == "unknown" for item in checks),
            "requires_user_action": sum(item["status"] == "requires_user_action" for item in checks),
        },
    }
    return report


def format_report(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    report = collect_report(args.root, dict(os.environ))
    print(format_report(report))
    return 0 if report["ready_to_deploy"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
