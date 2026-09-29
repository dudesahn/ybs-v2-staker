"""Deploy a replacement swapper, then stage and migrate the yCRV YBS strategy.

Call ``deploy_swapper(management)`` with the intended management address, then
``main(swapper)`` to stage a strategy using that explicit replacement.
The new swapper starts with OTC disabled and no inventory or operator grants.
For brownie-safe, call ``setup(strategy, swapper, safe.account)``
and build the multisend from the receipts in the multisig repository.
Addresses resolve through canonical metadata; fresh deployment objects can be
passed directly, including during fork rehearsals before source verification.
"""

from brownie import Strategy, SwapperV5, accounts, chain

from scripts.contract_helpers import deployed_contract


VAULT = "0x27B5739e22ad9033bcBf192059122d163b60349D"
YBS = "0xE9A115b77A1057C918F997c32663FdcE24FB873f"
REWARD_DISTRIBUTOR = "0xB226c52EB411326CdB54824a88aBaFDAAfF16D3d"
STRATEGY_PROXY = "0x78eDcb307AC1d1F8F5Fd070B377A6e69C8dcFC34"
OLD_STRATEGY = "0xe3974e44bc08f435da2c6db7d01e1758496da119"
LEGACY_SWAPPER_V5 = "0x1B7e6fB817112b036EAa4AE85479fF1C2E9330A2"
FEE_RECIPIENT = "0x044F9C86a0Da637a235E83564215DC271Bc0deFc"
GOVERNANCE = "0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52"

DEPLOYER_ACCOUNT = "wavey3"
CREDIT_THRESHOLD = 25_000 * 10**18
MIGRATION_MAX_WEIGHT_SHARE = 998 * 10**15


def _account(sender):
    return accounts.at(sender, force=True) if isinstance(sender, str) else sender


def _assert_staged(strategy, vault, old_strategy, swapper):
    assert strategy.vault() == vault.address
    assert strategy.ybs() == YBS
    assert strategy.rewardDistributor() == REWARD_DISTRIBUTOR
    assert strategy.swapper() == swapper.address, "unexpected staged swapper"
    assert strategy.feeRecipient() == FEE_RECIPIENT
    assert strategy.rewards() == strategy.address
    assert strategy.keeper() == old_strategy.keeper()
    assert strategy.strategist() == old_strategy.strategist()
    assert vault.strategies(strategy)["activation"] == 0
    assert strategy.estimatedTotalAssets() == 0
    assert vault.balanceOf(strategy) == 0
    assert not strategy.feeModeActive()


def deploy_swapper(management, deployer=None, publish_source=True):
    """Deploy current SwapperV5 code with the established pool/token configuration."""
    assert chain.id == 1
    deployer = (
        accounts.load(DEPLOYER_ACCOUNT) if deployer is None else _account(deployer)
    )
    legacy = deployed_contract(
        LEGACY_SWAPPER_V5,
        "tokenIn",
        "tokenOut",
        "pool1",
        "tokenOutPool1",
        "pool2",
    )
    swapper = deployer.deploy(
        SwapperV5,
        management,
        legacy.tokenIn(),
        legacy.tokenOut(),
        legacy.pool1(),
        legacy.tokenOutPool1(),
        legacy.pool2(),
        publish_source=publish_source,
    )
    assert swapper.owner() == GOVERNANCE
    assert swapper.management() == management
    assert not swapper.otcEnabled()
    print(f"Replacement swapper: {swapper.address}")
    return swapper


def main(swapper_address, publish_source=True, deployer=None):
    """Deploy and perform every setup transaction available to the strategist."""
    assert chain.id == 1
    deployer = (
        accounts.load(DEPLOYER_ACCOUNT) if deployer is None else _account(deployer)
    )
    vault = deployed_contract(VAULT, "strategies", "balanceOf")
    old_strategy = deployed_contract(
        OLD_STRATEGY,
        "keeper",
        "strategist",
        "minReportDelay",
        "maxReportDelay",
    )
    ybs = deployed_contract(YBS, "MAX_STAKE_GROWTH_WEEKS")
    reward_distributor = deployed_contract(REWARD_DISTRIBUTOR, "staker", "rewardToken")
    swapper = deployed_contract(swapper_address, "tokenIn", "tokenOut")

    assert reward_distributor.staker() == ybs.address
    strategy = deployer.deploy(
        Strategy,
        vault,
        ybs,
        reward_distributor,
        swapper,
        publish_source=publish_source,
    )
    strategy.setKeeper(old_strategy.keeper(), {"from": deployer})
    strategy.setMinReportDelay(old_strategy.minReportDelay(), {"from": deployer})
    strategy.setMaxReportDelay(old_strategy.maxReportDelay(), {"from": deployer})
    strategy.setRewards(strategy, {"from": deployer})
    strategy.setStrategist(old_strategy.strategist(), {"from": deployer})

    _assert_staged(strategy, vault, old_strategy, swapper)
    print(f"Staged strategy: {strategy.address}")
    return strategy


def setup(strategy_address, swapper_address, sender=GOVERNANCE):
    """Execute the ordered governance migration calls using brownie receipts."""
    assert chain.id == 1
    sender = _account(sender)
    vault = deployed_contract(
        VAULT,
        "governance",
        "managementFee",
        "performanceFee",
        "rewards",
        "strategies",
        "balanceOf",
        "migrateStrategy",
        "setRewards",
    )
    strategy = deployed_contract(
        strategy_address,
        "vault",
        "ybs",
        "rewardDistributor",
        "swapper",
        "feeRecipient",
        "rewards",
        "keeper",
        "strategist",
        "estimatedTotalAssets",
        "feeModeActive",
        "setCreditThreshold",
        "setBaseFeeOracle",
        "manualStakeAsMaxWeighted",
        "balanceOfStaked",
        "balanceOfWant",
    )
    old_strategy = deployed_contract(
        OLD_STRATEGY, "keeper", "strategist", "baseFeeOracle", "harvest"
    )
    ybs = deployed_contract(YBS, "owner", "setWeightedStaker", "approvedWeightedStaker")
    proxy = deployed_contract(STRATEGY_PROXY, "governance", "approveLocker", "lockers")
    swapper = deployed_contract(swapper_address, "tokenIn", "tokenOut")

    assert sender.address == GOVERNANCE
    assert vault.governance() == ybs.owner() == proxy.governance() == sender.address
    assert vault.managementFee() == 0
    assert vault.performanceFee() == 1_000
    assert vault.rewards() == FEE_RECIPIENT
    assert vault.strategies(old_strategy)["performanceFee"] == 0
    assert vault.strategies(old_strategy)["totalDebt"] > 0
    assert vault.balanceOf(old_strategy) == 0
    _assert_staged(strategy, vault, old_strategy, swapper)

    strategy.setCreditThreshold(CREDIT_THRESHOLD, {"from": sender})
    strategy.setBaseFeeOracle(old_strategy.baseFeeOracle(), {"from": sender})
    ybs.setWeightedStaker(strategy, True, {"from": sender})
    proxy.approveLocker(strategy, True, {"from": sender})
    # Settle the old position using its existing swapper before transferring it.
    # The replacement strategy uses the swapper explicitly selected above.
    old_strategy.harvest({"from": sender})
    vault.migrateStrategy(old_strategy, strategy, {"from": sender})
    strategy.manualStakeAsMaxWeighted(MIGRATION_MAX_WEIGHT_SHARE, {"from": sender})
    vault.setRewards(strategy, {"from": sender})

    assert vault.strategies(old_strategy)["totalDebt"] == 0
    assert vault.strategies(strategy)["totalDebt"] > 0
    assert ybs.approvedWeightedStaker(strategy)
    assert proxy.lockers(strategy)
    assert strategy.balanceOfStaked() > 0
    assert strategy.balanceOfWant() <= 1
    assert strategy.feeModeActive()
    return strategy
