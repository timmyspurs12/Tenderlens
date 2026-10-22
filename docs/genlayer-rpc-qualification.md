# Bradbury GenLayer RPC qualification

## Findings and decision

`https://rpc-bradbury.genlayer.com` is present in the official [`genlayer-py` Bradbury chain descriptor](https://github.com/genlayerlabs/genlayer-py/blob/dd25ef7f43e99a14b8fe42a64e01374845ad4d2d/genlayer_py/chains/testnet_bradbury.py), and the upstream [Bradbury smoke tests](https://github.com/genlayerlabs/genlayer-py/blob/dd25ef7f43e99a14b8fe42a64e01374845ad4d2d/tests/unit/smoke/test_bradbury_smoke.py) use that preset. This makes it an official SDK candidate, **not evidence that the endpoint is currently reachable, serves a GenLayer node, or supports TenderLens' required calls**.

The current official [Networks guide](https://github.com/genlayerlabs/genlayer-docs/blob/c4a09c85efe8b1209642e46817c97cd0f1c772eb/pages/developers/networks.mdx) does not list that hostname. It distinguishes the GenLayer node RPC from the underlying chain RPC (`https://rpc.testnet-chain.genlayer.com`), requires a Phase 2-compatible node RPC, and specifies Bradbury chain ID `4221`, genesis hash `0x17d89764d4f4f963158fc4c5e8f2cb670c2837740b35f37081c12860c3df97e4`, and AddressManager `0x3F71AE080BD6270A91dBcc7960d88797828538E0`.

There is also a version drift to guard against: the pinned `genlayer-py==0.19.0rc2` descriptor and its upstream smoke test use older static ConsensusMain/ConsensusData addresses, while the current [Bradbury protocol manifest](https://github.com/genlayerlabs/genlayer-docs/blob/8e570073bd04b1450045dd2433e70383e5971655/content/validators/bradbury.protocol-manifest.json) lists a current AddressManager registry and different contract addresses. The driver now resolves the documented `ConsensusMain`, `ConsensusData`, and `Staking` keys from the live AddressManager and checks current manifest values and manager links; it does not treat the old SDK addresses as current identity evidence.

The driver previously rejected `rpc-bradbury.genlayer.com` during URL validation, before SDK client creation or any RPC request. That was a **configuration-time rejection**, not a live capability-test failure. The candidate now proceeds to the driver's live identity/runtime gates. It is not marked qualified by configuration alone. The underlying chain RPC remains explicitly rejected for GenLayer node operations.

## Read-only RPC probe

`scripts/genlayer_rpc_qualification.py` is a reproducible read-only probe for an authorized network runner. It checks:

- `eth_chainId` and `eth_getBlockByNumber` block zero against the current Bradbury identity;
- non-empty AddressManager and current registry contract code, dynamic AddressManager mappings, and ConsensusMain/ConsensusData links back to the manager;
- JSON-RPC dispatch for `gen_call` with `type: "read"` to the zero address, plus `gen_getTransactionLifecycle` and `gen_getContractCode` sentinel lookups.

The probe uses positive JSON-RPC request IDs, makes no retries, and does not follow redirects. Its `gen_*` errors are included in the JSON report. An explicit method-not-found response is reported as unsupported; a structured application/parameter error means the method name was recognized but does **not** prove a successful application call. The dummy `gen_call` only checks read-call dispatch; it does not execute TenderLens' contract or establish GenVM/runtime compatibility.

It does not create/load a wallet, read balances or fees, sign, broadcast, deploy, or contact a faucet. It never calls an `eth_send*` or deploy-type method. Its report always leaves deployment readiness unestablished.

### Exact command for an authorized runner

From the repository root, with Python 3.12 available and network access explicitly authorized:

```bash
python3.12 -m pip install --disable-pip-version-check "uv==0.13.3"
bash .devcontainer/setup.sh --genlayer-only
.venv/bin/python scripts/genlayer_rpc_qualification.py --rpc-url https://rpc-bradbury.genlayer.com
```

The setup command installs the checked-in lock without changing dependency pins. The final command prints a JSON evidence report and exits `0` only when the read-only identity checks and all three method-dispatch checks pass; otherwise it exits `2`. Preserve the full JSON output, including observed values/errors and timestamp. Do not add credentials, API keys, query strings, or path tokens to the URL. For a separately approved operator node, pass `--allow-operator-node` and its HTTPS host URL; the same identity checks still apply.

## What has and has not been verified

- **Already verified by existing CI:** GitHub Actions run [39631219400](https://github.com/timmyspurs12/Tenderlens/actions/runs/39631219400), on commit `22e96ac19a13442e1e7393cc9b86e42d5e3d6aa4`, successfully installed the pinned Python 3.12 GenLayer environment and passed the Direct Mode contract tests. The Direct Mode cases include exact submission payload handling, UTF-8 size limits, digest/metadata validation, fabricated evidence-reference rejection, and output traceability checks. This is test-harness/runtime evidence, not Bradbury-node evidence.
- **SDK initialization:** a credential-free offline construction test for the pinned SDK client has been added; it asserts no account is attached and the provider makes no request during initialization. It has not yet run in Python 3.12 CI, so SDK initialization remains unverified until that test passes.
- **Not verified here:** no Bradbury RPC request has been made. No chain ID, genesis, registry, `gen_*` response, SDK network operation, fee/balance, or deployment result has been observed. The probe has not been run because this workspace has no authorized RPC network runner.
- **Still separate:** Direct Mode contract tests do not prove the exact contract's GenVM compatibility on Bradbury. No deploy-type simulation or transaction is authorized by this qualification task. Deployment, wallet, funding, fee, signing, and broadcast gates remain closed.
