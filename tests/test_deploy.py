from scripts import deploy


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
    assert vault.strategies(strategy)["activation"] == 0
    assert not swapper.allowedSwapper(strategy)
    assert reward_underlying.allowance(strategy, swapper) == 2**256 - 1

    ratio_before_setup = vault.debtRatio()
    params_before_setup = vault.strategies(old_strategy)
    result = deploy.setup(strategy, sender=gov)

    assert result.address == strategy.address
    assert vault.strategies(old_strategy)["totalDebt"] == 0
    assert vault.strategies(old_strategy)["debtRatio"] == 0
    params_after_setup = vault.strategies(strategy)
    assert params_after_setup["totalDebt"] > 0
    assert params_after_setup["debtRatio"] == params_before_setup["debtRatio"]
    assert params_after_setup["performanceFee"] == params_before_setup["performanceFee"]
    assert vault.debtRatio() == ratio_before_setup
    assert vault.withdrawalQueue(0) == strategy.address
    assert strategy.swapThresholds() == old_strategy.swapThresholds()
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
