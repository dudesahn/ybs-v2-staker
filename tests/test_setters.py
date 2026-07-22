import brownie
from brownie import Contract, ZERO_ADDRESS


WEEK = 7 * 24 * 60 * 60
MAX_UINT = 2**256 - 1
MAX_UINT112 = 2**112 - 1


def test_custom_setters_enforce_manager_access_and_bounds(
    accounts,
    strategy,
    vault,
    reward_distributor,
    gov,
    strategist,
    MockBaseFeeOracle,
):
    management = accounts.at(vault.management(), force=True)
    attacker = accounts[9]
    claimer = accounts[8]

    original_thresholds = strategy.swapThresholds()
    with brownie.reverts():
        strategy.setSwapThresholds(10, 1_000, False, {"from": attacker})
    assert strategy.swapThresholds() == original_thresholds

    # uint112.max itself is rejected, and the minimum must remain strictly
    # below the maximum.
    with brownie.reverts():
        strategy.setSwapThresholds(1, MAX_UINT112, False, {"from": gov})
    with brownie.reverts():
        strategy.setSwapThresholds(100, 100, False, {"from": gov})
    with brownie.reverts():
        strategy.setSwapThresholds(101, 100, False, {"from": gov})
    assert strategy.swapThresholds() == original_thresholds

    strategy.setSwapThresholds(10, 1_000, False, {"from": management})
    thresholds = strategy.swapThresholds()
    assert thresholds["min"] == 10
    assert thresholds["max"] == 1_000
    assert not thresholds["autoAdjustThresholds"]

    assert not strategy.bypassClaim()
    assert not strategy.bypassMaxStake()
    with brownie.reverts():
        strategy.setBypasses(True, True, {"from": attacker})
    assert not strategy.bypassClaim()
    assert not strategy.bypassMaxStake()
    strategy.setBypasses(True, True, {"from": management})
    assert strategy.bypassClaim()
    assert strategy.bypassMaxStake()
    strategy.setBypasses(False, False, {"from": gov})
    assert not strategy.bypassClaim()
    assert not strategy.bypassMaxStake()

    assert strategy.weekEndLockWindow() == 2 * 24 * 60 * 60
    with brownie.reverts():
        strategy.setWeekEndLockWindow(0, {"from": attacker})
    with brownie.reverts("Too High"):
        strategy.setWeekEndLockWindow(WEEK, {"from": gov})
    assert strategy.weekEndLockWindow() == 2 * 24 * 60 * 60
    strategy.setWeekEndLockWindow(WEEK - 1, {"from": management})
    assert strategy.weekEndLockWindow() == WEEK - 1
    strategy.setWeekEndLockWindow(0, {"from": gov})
    assert strategy.weekEndLockWindow() == 0

    assert not reward_distributor.approvedClaimer(strategy, claimer)
    with brownie.reverts():
        strategy.approveRewardClaimer(claimer, True, {"from": attacker})
    assert not reward_distributor.approvedClaimer(strategy, claimer)
    strategy.approveRewardClaimer(claimer, True, {"from": management})
    assert reward_distributor.approvedClaimer(strategy, claimer)
    strategy.approveRewardClaimer(claimer, False, {"from": gov})
    assert not reward_distributor.approvedClaimer(strategy, claimer)

    oracle = strategist.deploy(MockBaseFeeOracle, False)
    with brownie.reverts():
        strategy.setBaseFeeOracle(oracle, {"from": attacker})
    assert strategy.baseFeeOracle() == ZERO_ADDRESS
    tx = strategy.setBaseFeeOracle(oracle, {"from": management})
    assert tx.events["UpdatedBaseFeeOracle"]["baseFeeOracle"] == oracle
    assert strategy.baseFeeOracle() == oracle


def test_upgrade_swapper_requires_governance_and_compatible_tokens(
    accounts,
    strategy,
    vault,
    token,
    weth,
    gov,
    strategist,
    MockSwapper,
):
    management = accounts.at(vault.management(), force=True)
    reward_underlying = Contract(strategy.rewardTokenUnderlying())
    old_swapper = strategy.swapper()

    valid_swapper = strategist.deploy(MockSwapper, reward_underlying, token)
    wrong_output = strategist.deploy(MockSwapper, reward_underlying, weth)
    wrong_input = strategist.deploy(MockSwapper, weth, token)

    assert reward_underlying.allowance(strategy, old_swapper) == MAX_UINT

    with brownie.reverts():
        strategy.upgradeSwapper(valid_swapper, {"from": management})
    with brownie.reverts("Invalid Swapper"):
        strategy.upgradeSwapper(wrong_output, {"from": gov})
    with brownie.reverts():
        strategy.upgradeSwapper(wrong_input, {"from": gov})

    assert strategy.swapper() == old_swapper
    assert reward_underlying.allowance(strategy, old_swapper) == MAX_UINT
    assert reward_underlying.allowance(strategy, valid_swapper) == 0
    assert reward_underlying.allowance(strategy, wrong_output) == 0
    assert reward_underlying.allowance(strategy, wrong_input) == 0

    strategy.upgradeSwapper(valid_swapper, {"from": gov})

    assert strategy.swapper() == valid_swapper
    assert reward_underlying.allowance(strategy, old_swapper) == 0
    assert reward_underlying.allowance(strategy, valid_swapper) == MAX_UINT


def test_rewards_configuration_requires_rewarder(
    accounts,
    strategy,
    vault,
    gov,
    strategist,
):
    management = accounts.at(vault.management(), force=True)
    attacker = accounts[9]
    new_rewards = accounts[7]
    old_rewards = strategy.rewards()

    assert vault.allowance(strategy, old_rewards) == MAX_UINT
    assert vault.allowance(strategy, new_rewards) == 0

    with brownie.reverts():
        strategy.setRewards(new_rewards, {"from": attacker})
    with brownie.reverts():
        strategy.setRewards(new_rewards, {"from": management})
    with brownie.reverts():
        strategy.setRewards(ZERO_ADDRESS, {"from": gov})

    assert strategy.rewards() == old_rewards
    assert vault.allowance(strategy, old_rewards) == MAX_UINT
    assert vault.allowance(strategy, new_rewards) == 0

    tx = strategy.setRewards(new_rewards, {"from": strategist})

    assert tx.events["UpdatedRewards"]["rewards"] == new_rewards
    assert strategy.rewards() == new_rewards
    assert vault.allowance(strategy, old_rewards) == 0
    assert vault.allowance(strategy, new_rewards) == MAX_UINT
