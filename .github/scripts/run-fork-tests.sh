#!/usr/bin/env bash
set -euo pipefail

# This setup writes Brownie's network settings only on disposable CI runners.
if [[ "${GITHUB_ACTIONS:-}" != "true" ]]; then
  echo "This helper is for disposable GitHub Actions runners only." >&2
  exit 1
fi

: "${ETH_RPC_URL:?Set the ETH_RPC_URL Actions secret to an archive-capable Ethereum RPC.}"
: "${ETHERSCAN_API_KEY:?Set the ETHERSCAN_API_KEY Actions secret.}"
export ETHERSCAN_TOKEN="$ETHERSCAN_API_KEY"

# Historical state must stay on this baseline, rather than silently using latest.
fork_block=26050313
anvil --quiet --fork-url "$ETH_RPC_URL" --fork-block-number "$fork_block" \
  --chain-id 1 --port 8545 --steps-tracing --block-base-fee-per-gas 0 \
  >/dev/null 2>&1 &
fork_pid=$!
trap 'kill "$fork_pid" 2>/dev/null || true' EXIT

python - "$fork_block" <<'PY'
import json
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

expected_block = int(sys.argv[1])
request = Request(
    "http://127.0.0.1:8545",
    data=json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}
    ).encode(),
    headers={"Content-Type": "application/json"},
)
for _ in range(60):
    try:
        with urlopen(request, timeout=1) as response:
            result = json.load(response)
        actual_block = int(result["result"], 16)
        if actual_block != expected_block:
            raise SystemExit(f"Unexpected fork block: {actual_block}")
        break
    except (URLError, TimeoutError):
        time.sleep(1)
else:
    raise SystemExit("Anvil failed to start; check the archive RPC secret and availability.")
PY

# Attach to the already running fork so Brownie never logs the upstream URL.
# The named mainnet fork also retains chain ID / explorer metadata for Contract(address).
brownie networks modify mainnet host=http://127.0.0.1:8545
brownie networks add development ci-fork cmd=anvil host=http://127.0.0.1 \
  port=8545 fork=mainnet
brownie test --network ci-fork "$@"
