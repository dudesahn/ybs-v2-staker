"""Resolve deployed contracts through Brownie's canonical metadata."""

from brownie import Contract


def deployed_contract(address, *required_methods):
    contract = Contract(address)
    missing = [method for method in required_methods if not hasattr(contract, method)]
    if missing:
        raise RuntimeError(
            f"Contract {contract.address} resolved as {contract._name}; "
            f"missing required method(s): {', '.join(missing)}. "
            "Check the canonical metadata before continuing; no ABI fallback was used."
        )
    return contract
