# Continuous integration

The fork suite uses Python 3.10, Brownie 1.21.0, and Anvil 1.5.1 at Ethereum block
**26,050,313**. The block comes from the September 24, 2026 audit baseline. Change
it deliberately and rerun the full suite when updating dependency state.

Anvil runs with step tracing so Brownie can decode revert reasons, and a zero
base fee to match the tests' zero gas price. The session fixture in
`tests/conftest.py` selects Brownie 1.21's Anvil adapter when attaching to an
existing node and corrects its mining call: `evm_mine([1])` is interpreted as
timestamp 1 by Anvil 1.5.1, so ordinary mining sends an empty argument list
while explicit timestamps use `evm_setNextBlockTimestamp`.

Configure these GitHub Actions repository secrets:

- `ETH_RPC_URL`: an archive-capable Ethereum RPC URL for the historical baseline.
- `ETHERSCAN_API_KEY`: an Etherscan API V2 key for verified contract metadata.
  The helper maps this to the `ETHERSCAN_TOKEN` name expected by Brownie.

Fork pull requests do not receive repository secrets, so their fork test step
will fail with an explicit missing-secret message. Review and run those changes
on a trusted branch; do not expose secrets with `pull_request_target`.

The CI helper starts Anvil and registers an explicit `ci-fork` network in the
runner's disposable Brownie configuration. It does not change the project's
default network, and does not seed or repair deployment metadata. Deployed
contracts must resolve through `Contract(address)` and verified explorer data.

Keep the objects returned by fresh deployments. If canonical metadata is
unexpected or lacks a required method, stop and report the address, resolved
contract name, and missing method. Do not select another ABI or repair the shared
cache. Run local fork tests with an isolated Brownie data directory.

Format checks run with `npm ci && npm run lint:check` and
`black --check tests scripts`. The npm lockfile covers only the used formatters;
the commitlint action supplies its own conventional-commit dependencies.
