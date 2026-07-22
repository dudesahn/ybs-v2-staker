"""Deploy and migrate the yCRV YBS strategy.

For brownie-safe, call ``setup(STRATEGY, safe.account)`` and build the
multisend from the resulting receipts in the multisig repository.
"""

from brownie import Contract, Strategy, SwapperV5, accounts, chain, interface


VAULT = "0x27B5739e22ad9033bcBf192059122d163b60349D"
YBS = "0xE9A115b77A1057C918F997c32663FdcE24FB873f"
REWARD_DISTRIBUTOR = "0xB226c52EB411326CdB54824a88aBaFDAAfF16D3d"
STRATEGY_PROXY = "0x78eDcb307AC1d1F8F5Fd070B377A6e69C8dcFC34"
OLD_STRATEGY = "0xe3974e44bc08f435da2c6db7d01e1758496da119"
SWAPPER_V5 = "0x1B7e6fB817112b036EAa4AE85479fF1C2E9330A2"
FEE_RECIPIENT = "0x044F9C86a0Da637a235E83564215DC271Bc0deFc"
GOVERNANCE = "0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52"

DEPLOYER_ACCOUNT = "wavey3"
CREDIT_THRESHOLD = 25_000 * 10**18
MIGRATION_MAX_WEIGHT_SHARE = 998 * 10**15


def _account(sender):
    return accounts.at(sender, force=True) if isinstance(sender, str) else sender


def _assert_staged(strategy, vault, old_strategy):
    assert strategy.vault() == vault.address
    assert strategy.ybs() == YBS
    assert strategy.rewardDistributor() == REWARD_DISTRIBUTOR
    assert strategy.swapper() == SWAPPER_V5
    assert strategy.feeRecipient() == FEE_RECIPIENT
    assert strategy.rewards() == strategy.address
    assert strategy.keeper() == old_strategy.keeper()
    assert strategy.strategist() == old_strategy.strategist()
    assert vault.strategies(strategy)["activation"] == 0
    assert strategy.estimatedTotalAssets() == 0
    assert vault.balanceOf(strategy) == 0
    assert not strategy.feeModeActive()


def main(publish_source=True):
    """Deploy and perform every setup transaction available to the strategist."""
    assert chain.id == 1
    deployer = accounts.load(DEPLOYER_ACCOUNT)
    vault = Contract(VAULT)
    old_strategy = Strategy.at(OLD_STRATEGY)
    ybs = interface.IYearnBoostedStaker(YBS)
    reward_distributor = interface.IRewardDistributor(REWARD_DISTRIBUTOR)
    swapper = SwapperV5.at(SWAPPER_V5)

    assert old_strategy.swapper() == swapper.address
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

    _assert_staged(strategy, vault, old_strategy)
    print(f"Staged strategy: {strategy.address}")
    return strategy


def setup(strategy_address, sender=GOVERNANCE):
    """Execute the ordered governance migration calls using brownie receipts."""
    assert chain.id == 1
    sender = _account(sender)
    vault = Contract(VAULT)
    strategy = Strategy.at(strategy_address)
    old_strategy = Strategy.at(OLD_STRATEGY)
    ybs = interface.IYearnBoostedStaker(YBS)
    proxy = Contract(STRATEGY_PROXY)

    assert sender.address == GOVERNANCE
    assert vault.governance() == ybs.owner() == proxy.governance() == sender.address
    assert vault.managementFee() == 0
    assert vault.performanceFee() == 1_000
    assert vault.rewards() == FEE_RECIPIENT
    assert vault.strategies(old_strategy)["performanceFee"] == 0
    assert vault.strategies(old_strategy)["totalDebt"] > 0
    assert vault.balanceOf(old_strategy) == 0
    _assert_staged(strategy, vault, old_strategy)

    strategy.setCreditThreshold(CREDIT_THRESHOLD, {"from": sender})
    strategy.setBaseFeeOracle(old_strategy.baseFeeOracle(), {"from": sender})
    ybs.setWeightedStaker(strategy, True, {"from": sender})
    proxy.approveLocker(strategy, True, {"from": sender})
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
