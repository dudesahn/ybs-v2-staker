import pytest


@pytest.mark.parametrize("repayment", ["revoke", "shutdown"])
@pytest.mark.parametrize("idle_covers_refund", [False, True])
def test_fee_share_refund_during_full_repayment(
    strategy,
    vault,
    token,
    user,
    gov,
    accounts,
    fund_ycrv,
    chain,
    repayment,
    idle_covers_refund,
):
    # Isolate parent-share recycling from claiming, swaps and proxy maintenance.
    strategy.setBypasses(True, True, {"from": gov})
    strategy.setSwapThresholds(2**112 - 3, 2**112 - 2, False, {"from": gov})
    strategy.setWeekEndLockWindow(0, {"from": gov})
    vault.setManagementFee(0, {"from": gov})
    vault.updateStrategyPerformanceFee(strategy, 0, {"from": gov})
    vault.setRewards(accounts[6], {"from": gov})
    strategy.setRewards(strategy, {"from": gov})

    # Stage the same fungible yvyCRV shares that a fee report leaves behind.
    # Keep recycling inactive until their underlying has been invested.
    assets = 1_000 * 10 ** token.decimals()
    fund_ycrv(user, assets)
    token.approve(vault, 2**256 - 1, {"from": user})
    shares_before = vault.balanceOf(user)
    vault.deposit(assets, {"from": user})
    shares = vault.balanceOf(user) - shares_before
    assert shares > 0
    vault.transfer(strategy, shares, {"from": user})
    strategy.harvest({"from": gov})
    assert vault.balanceOf(strategy) >= shares
    assert vault.withdrawalQueue(0) == strategy.address

    if idle_covers_refund:
        fund_ycrv(user, 2 * assets)
        vault.deposit(2 * assets, {"from": user})

    refund_value = (
        vault.balanceOf(strategy) * vault.pricePerShare() // 10 ** vault.decimals()
    )
    idle = token.balanceOf(vault)
    assert (idle >= refund_value) is idle_covers_refund
    vault.setRewards(strategy, {"from": gov})
    assert strategy.feeModeActive()
    assert not strategy.emergencyExit()

    if repayment == "revoke":
        vault.revokeStrategy(strategy, {"from": gov})
    else:
        vault.setEmergencyShutdown(True, {"from": gov})
    debt_before = vault.strategies(strategy)["totalDebt"]
    assert vault.debtOutstanding(strategy) == debt_before

    chain.sleep(1)
    chain.mine()
    tx = strategy.harvest({"from": gov})

    report = tx.events["Harvested"]
    assert report["profit"] > 0
    assert report["loss"] == 0
    assert vault.strategies(strategy)["totalDebt"] == 0
    assert strategy.estimatedTotalAssets() <= 1
    if idle_covers_refund:
        assert report["debtPayment"] == debt_before
    else:
        # The share redemption already repaid some debt via Strategy.withdraw.
        # The report must repay only the remainder, alongside the recycled profit.
        assert 0 < report["debtPayment"] < debt_before


@pytest.mark.parametrize("carry", [0, 5_000 * 10**18])
def test_auto_sale_limit_includes_unsold_underlying(
    strategy,
    reward_token,
    reward_underlying,
    user,
    gov,
    crvusd_whale,
    carry,
):
    strategy.setBypasses(True, True, {"from": gov})
    strategy.setSwapThresholds(100 * 10**18, 10_000 * 10**18, True, {"from": gov})
    if carry:
        reward_token.withdraw(carry, user, user, {"from": user})
        reward_underlying.transfer(strategy, carry, {"from": user})
    reward_token.transfer(strategy, 1_000 * 10**18, {"from": user})
    shares = reward_token.balanceOf(strategy)
    underlying_before = reward_underlying.balanceOf(strategy)

    tx = strategy.harvest({"from": gov})

    # The ERC4626 redemption records actual proceeds, including any rounding.
    redemptions = [
        event
        for event in tx.events["Withdraw"]
        if event.address.lower() == reward_token.address.lower()
    ]
    assert len(redemptions) == 1
    redemption = redemptions[0]
    assert redemption["shares"] == shares
    available = underlying_before + redemption["assets"]
    expected_max = available * 101 // 700
    assert strategy.swapThresholds()["max"] == expected_max
    assert reward_underlying.balanceOf(strategy) == available - expected_max
    assert strategy.balanceOfReward() == 0
