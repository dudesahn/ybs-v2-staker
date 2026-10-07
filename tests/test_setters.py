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

    assert strategy.thresholdTimeUntilWeekEnd() == 2 * 24 * 60 * 60
    with brownie.reverts():
        strategy.setWeekEndHarvestTrigger(0, {"from": attacker})
    with brownie.reverts("Too High"):
        strategy.setWeekEndHarvestTrigger(WEEK, {"from": gov})
    assert strategy.thresholdTimeUntilWeekEnd() == 2 * 24 * 60 * 60
    strategy.setWeekEndHarvestTrigger(WEEK - 1, {"from": management})
    assert strategy.thresholdTimeUntilWeekEnd() == WEEK - 1
    strategy.setWeekEndHarvestTrigger(0, {"from": gov})
    assert strategy.thresholdTimeUntilWeekEnd() == 0

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


def test_rewards_allowance_and_reward_claims_are_not_public(
    accounts,
    strategy,
    vault,
    token,
    user,
    gov,
    reward_distributor,
    fund_ycrv,
):
    attacker = accounts[9]
    previous_rewards = strategy.rewards()

    strategy.setRewards(strategy, {"from": gov})

    # BaseStrategy revokes the former recipient and approves only itself.
    assert vault.allowance(strategy, previous_rewards) == 0
    assert vault.allowance(strategy, strategy) == MAX_UINT
    assert vault.allowance(strategy, attacker) == 0

    # Stage parent-vault shares and prove the self-allowance cannot be used by
    # an arbitrary caller to pull them out of the strategy.
    assets = 100 * 10 ** token.decimals()
    fund_ycrv(user, assets)
    token.approve(vault, assets, {"from": user})
    shares_before = vault.balanceOf(user)
    vault.deposit(assets, {"from": user})
    staged_shares = vault.balanceOf(user) - shares_before
    vault.transfer(strategy, staged_shares, {"from": user})

    strategy_shares_before = vault.balanceOf(strategy)
    attacker_shares_before = vault.balanceOf(attacker)
    with brownie.reverts():
        vault.transferFrom(
            strategy,
            attacker,
            strategy_shares_before,
            {"from": attacker},
        )
    assert vault.balanceOf(strategy) == strategy_shares_before
    assert vault.balanceOf(attacker) == attacker_shares_before

    # The inherited rewards address is unrelated to the YBS distributor's
    # claimer and recipient mappings. Only the strategy can configure those
    # mappings for its own account, via vault-manager-gated strategy methods.
    strategy_account_info = reward_distributor.accountInfo(strategy)
    assert not reward_distributor.approvedClaimer(strategy, attacker)
    with brownie.reverts("!approvedClaimer"):
        reward_distributor.claimFor(strategy, {"from": attacker})
    with brownie.reverts("!approvedClaimer"):
        reward_distributor.claimWithRangeFor(
            strategy,
            0,
            reward_distributor.getWeek() - 1,
            {"from": attacker},
        )

    reward_distributor.configureRecipient(attacker, {"from": attacker})
    assert reward_distributor.accountInfo(strategy) == strategy_account_info
    assert reward_distributor.accountInfo(attacker)["recipient"] == attacker

    with brownie.reverts():
        strategy.approveRewardClaimer(attacker, True, {"from": attacker})
    assert not reward_distributor.approvedClaimer(strategy, attacker)

    # Harvest is also not public, so an attacker cannot force the strategy to
    # run its own claim/report sequence.
    with brownie.reverts():
        strategy.harvest({"from": attacker})
