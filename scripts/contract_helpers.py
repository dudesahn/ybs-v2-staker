"""Resolve deployed contracts through Brownie's canonical metadata."""

from brownie import Contract
from brownie.network.contract import ProjectContract


def deployed_contract(address, *required_methods):
    # Keep the object returned by a fresh project deployment; its address has
    # no explorer metadata on a fork. Existing addresses still resolve normally.
    contract = address if isinstance(address, ProjectContract) else Contract(address)
    missing = [method for method in required_methods if not hasattr(contract, method)]
    if missing:
        raise RuntimeError(
            f"Contract {contract.address} resolved as {contract._name}; "
            f"missing required method(s): {', '.join(missing)}. "
            "Check the canonical metadata before continuing; no ABI fallback was used."
        )
    return contract
