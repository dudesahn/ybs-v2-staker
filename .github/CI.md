# Continuous integration

The fork suite uses Python 3.10, Brownie 1.21.0, and Anvil 1.5.1 at Ethereum block
**26,050,313**. Change the block deliberately and rerun the full suite.

Anvil runs with step tracing, so Brownie can decode revert reasons, and a zero base
fee to match the tests' zero gas price. A session fixture in `tests/conftest.py`
adapts Brownie 1.21 to Anvil 1.5.1, which reads `evm_mine([1])` as timestamp 1.

`.github/scripts/run-fork-tests.sh` starts Anvil and registers a `ci-fork` network
in the runner's Brownie configuration, so it only runs on GitHub Actions. The test
job needs two repository secrets:

- `ETH_RPC_URL`: an archive-capable Ethereum RPC URL.
- `ETHERSCAN_API_KEY`: an Etherscan API V2 key, passed to Brownie as `ETHERSCAN_TOKEN`.

Pull requests from forks don't receive secrets, so their fork tests fail. Run those
changes on a trusted branch; don't expose secrets with `pull_request_target`.

Format checks run `npm ci && npm run lint:check` and `black --check tests scripts`.
