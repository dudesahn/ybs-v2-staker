from scripts import deploy


WEEK = 7 * 24 * 60 * 60
DEFAULT_SWAP_THRESHOLDS = (100 * 10**18, 10_000 * 10**18, True)


def test_deploy_and_migrate_with_live_swapper(
    chain,
    strategist,
    management,
    gov,
    user,
    vault,
    old_strategy,
    reward_token,
    reward_underlying,
    ybs,
    swapper_v5,
    crvusd_whale,
):
    swapper = swapper_v5
    strategy = deploy.main(publish_source=False, deployer=strategist)
    assert strategy.swapper() == swapper.address == old_strategy.swapper()
    assert strategy.keeper() == old_strategy.keeper()
    assert strategy.strategist() == old_strategy.strategist()
    assert strategy.minReportDelay() == old_strategy.minReportDelay()
    assert strategy.maxReportDelay() == old_strategy.maxReportDelay()
    assert strategy.rewards() == strategy.address
    assert strategy.rewardFee() == 0
    assert strategy.maxSlippage() == 300
    assert strategy.swapThresholds() == DEFAULT_SWAP_THRESHOLDS
    assert vault.strategies(strategy)["activation"] == 0
    assert not swapper.allowedSwapper(strategy)
    assert reward_underlying.allowance(strategy, swapper) == 2**256 - 1

    # Setup must transfer held rewards without claiming, redeeming or selling.
    reward_token.transfer(old_strategy, 500 * 10**18, {"from": user})
    reward_shares_before = reward_token.balanceOf(old_strategy)
    underlying_before = reward_underlying.balanceOf(old_strategy)
    ratio_before_setup = vault.debtRatio()
    params_before_setup = vault.strategies(old_strategy)
    result = deploy.setup(strategy, sender=gov)

    assert result.address == strategy.address
    assert vault.strategies(old_strategy)["totalDebt"] == 0
    assert vault.strategies(old_strategy)["debtRatio"] == 0
    params_after_setup = vault.strategies(strategy)
    assert params_after_setup["totalDebt"] == params_before_setup["totalDebt"] > 0
    assert params_after_setup["debtRatio"] == params_before_setup["debtRatio"]
    assert params_after_setup["performanceFee"] == params_before_setup["performanceFee"]
    assert vault.debtRatio() == ratio_before_setup
    assert vault.withdrawalQueue(0) == strategy.address
    assert strategy.swapThresholds() == DEFAULT_SWAP_THRESHOLDS
    assert reward_token.balanceOf(strategy) == reward_shares_before
    assert reward_underlying.balanceOf(strategy) == underlying_before
    assert reward_token.balanceOf(old_strategy) == 0
    assert reward_underlying.balanceOf(old_strategy) == 0
    assert strategy.creditThreshold() == old_strategy.creditThreshold()
    assert strategy.baseFeeOracle() == old_strategy.baseFeeOracle()
    assert ybs.approvedWeightedStaker(strategy)
    assert strategy.balanceOfStaked() > 0
    assert strategy.balanceOfWant() <= 2
    assert vault.performanceFee() == 0
    assert vault.rewards() == deploy.FEE_RECIPIENT
    assert strategy.feeRecipient() == deploy.FEE_RECIPIENT
    assert strategy.rewardFee() == deploy.REWARD_FEE

    # The Vault registration lets the migrated strategy OTC against the live
    # inventory without an allowlist entry.
    swapper.enableOtc(True, {"from": management})
    assert not swapper.allowedSwapper(strategy)
    strategy.setBypasses(True, True, {"from": gov})
    strategy.setSwapThresholds(1, 1_000_000 * 10**18, False, {"from": gov})
    reward_token.transfer(strategy, 500 * 10**18, {"from": user})
    reward_shares = strategy.balanceOfReward()
    treasury_shares_before = reward_token.balanceOf(swapper.treasury())
    fee_shares_before = reward_token.balanceOf(deploy.FEE_RECIPIENT)
    gain_before = vault.strategies(strategy)["totalGain"]
    supply_before = vault.totalSupply()
    swapper_shares_before = vault.balanceOf(swapper)

    chain.sleep(1)
    chain.mine()
    tx = strategy.harvest({"from": gov})

    assert "OTC" in tx.events
    assert vault.strategies(strategy)["totalGain"] > gain_before
    assert strategy.balanceOfReward() == 0
    assert reward_underlying.balanceOf(swapper) == 0
    assert reward_token.balanceOf(swapper.treasury()) > treasury_shares_before
    # The strategy takes the fee in reward-vault shares, and the Vault mints none.
    # Only the swapper's own redemptions, if any, change the supply.
    assert (
        reward_token.balanceOf(deploy.FEE_RECIPIENT) - fee_shares_before
        == reward_shares * deploy.REWARD_FEE // 10_000
    )
    swapper_shares_redeemed = swapper_shares_before - vault.balanceOf(swapper)
    assert vault.totalSupply() == supply_before - swapper_shares_redeemed
    assert vault.balanceOf(strategy) == 0


def test_weekly_rewards_trigger_and_adjust_after_manual_pre_migration_harvest(
    chain,
    strategist,
    gov,
    management,
    old_strategy,
    reward_token,
    reward_underlying,
    reward_distributor,
    swapper_v5,
    deposit_rewards,
):
    # Simulate the operator clearing the old rewards before starting migration.
    # Use live OTC inventory so this large sale does not move the market pools.
    swapper_v5.enableOtc(True, {"from": management})
    old_strategy.setSwapThresholds(0, 1_000_000 * 10**18, False, {"from": gov})
    old_strategy.harvest({"from": gov})
    assert reward_token.balanceOf(old_strategy) == 0
    assert reward_underlying.balanceOf(old_strategy) == 0

    strategy = deploy.main(publish_source=False, deployer=strategist)
    deploy.setup(strategy, sender=gov)
    assert strategy.swapThresholds() == DEFAULT_SWAP_THRESHOLDS
    assert not strategy.bypassClaim()
    assert strategy.maxSlippage() == 300
    assert strategy.balanceOfReward() == 0
    assert reward_underlying.balanceOf(strategy) == 0

    # Isolate the reward trigger from elapsed-time and available-credit triggers.
    strategy.setMinReportDelay(4 * WEEK, {"from": gov})
    strategy.setCreditThreshold(2**256 - 1, {"from": gov})
    assert not strategy.harvestTrigger(0)
    chain.sleep(WEEK)
    chain.mine()
    deposit_rewards()
    chain.sleep(WEEK)
    chain.mine()
    assert reward_distributor.getClaimable(strategy) > DEFAULT_SWAP_THRESHOLDS[0]
    assert strategy.harvestTrigger(0)

    tx = strategy.harvest({"from": gov})
    sold = tx.events["OTC"]["sellTokenAmount"]
    remaining = reward_underlying.balanceOf(strategy)
    thresholds = strategy.swapThresholds()
    assert thresholds["autoAdjustThresholds"]
    assert thresholds["min"] == DEFAULT_SWAP_THRESHOLDS[0]
    assert thresholds["max"] == sold == (sold + remaining) * 101 // 700
    assert remaining > 0
    assert strategy.balanceOfReward() == 0
    assert not strategy.harvestTrigger(0)
