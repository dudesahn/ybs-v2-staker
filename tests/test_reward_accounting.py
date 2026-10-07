import pytest


@pytest.mark.parametrize("repayment", ["revoke", "shutdown"])
def test_debt_is_refreshed_after_otc_withdrawal_during_repayment(
    strategy,
    swapper_v5,
    vault,
    token,
    reward_underlying,
    user,
    gov,
    management,
    funded_crvusd,
    chain,
    repayment,
):
    # Isolate the swap from claiming, max-weight staking and proxy maintenance.
    strategy.setBypasses(True, True, {"from": gov})
    strategy.setSwapThresholds(0, 2**112 - 2, False, {"from": gov})
    # This sells the whole migrated carryover at once, past the default slippage floor.
    strategy.setMaxSlippage(10_000, {"from": gov})
    strategy.setWeekEndHarvestTrigger(0, {"from": gov})

    # Move the swapper's loose yCRV out, so the OTC sale must redeem its live
    # parent-vault shares.
    swapper_v5.sweep(token, {"from": management})
    inventory_shares = vault.balanceOf(swapper_v5)
    assert inventory_shares > 0
    # Vault 0.4.3 keeps each report's gain as idle. The first harvest reports the
    # migrated position's gain; the second lends that idle back with no new gain.
    for _ in range(2):
        chain.sleep(1)
        chain.mine()
        strategy.harvest({"from": gov})
    swapper_v5.enableOtc(True, {"from": management})

    if repayment == "revoke":
        vault.revokeStrategy(strategy, {"from": gov})
    else:
        vault.setEmergencyShutdown(True, {"from": gov})
    debt_before = vault.strategies(strategy)["totalDebt"]
    assert vault.debtOutstanding(strategy) == debt_before

    # The redemption is worth more than the Vault's idle yCRV, so the Vault must
    # pull it from the strategy.
    sale = 100 * 10**18
    reward_underlying.transfer(strategy, sale, {"from": user})
    quoted_shares = sale * swapper_v5.priceOracle() // 10**18
    quoted_value = quoted_shares * vault.pricePerShare() // 10 ** vault.decimals()
    assert token.balanceOf(vault) < quoted_value

    chain.sleep(1)
    chain.mine()
    tx = strategy.harvest({"from": gov})

    # The swapper's redemption repaid part of the debt through Strategy.withdraw
    # during the swap. The report must repay only the remainder; with a stale
    # debtOutstanding, liquidatePosition would ask YBS for more than is staked.
    trade = tx.events["OTC"]
    assert vault.balanceOf(swapper_v5) == inventory_shares - trade["buyTokenAmount"]
    report = tx.events["Harvested"]
    assert report["profit"] > 0
    assert report["loss"] <= 1
    assert 0 < report["debtPayment"] < debt_before
    assert vault.strategies(strategy)["totalDebt"] == 0
    assert strategy.estimatedTotalAssets() <= 1


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
