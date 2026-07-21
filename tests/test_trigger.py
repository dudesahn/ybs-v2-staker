def test_operation(
    chain,
    token,
    gov,
    vault,
    reward_distributor,
    strategy,
    user,
    fund_ycrv,
):
    WEEK = 60 * 60 * 24 * 7
    threshold = strategy.thresholdTimeUntilWeekEnd()
    week_end = (chain.time() // WEEK + 1) * WEEK
    time_until_week_end = week_end - chain.time()
    if time_until_week_end <= threshold:
        chain.sleep(time_until_week_end + 1)
        chain.mine()

    credit_threshold = 25_000 * 10 ** token.decimals()
    params = vault.strategies(strategy)
    migration_profit = max(
        strategy.estimatedTotalAssets() - params["totalDebt"],
        0,
    )
    assert migration_profit < credit_threshold

    strategy.setCreditThreshold(credit_threshold, {"from": gov})
    assert not strategy.harvestTrigger(0)

    amount = strategy.creditThreshold() + 1
    below_threshold_amount = 10 ** token.decimals()
    fund_ycrv(user, amount + below_threshold_amount)
    assert token.balanceOf(user) >= amount + below_threshold_amount
    token.approve(vault.address, 2**256 - 1, {"from": user})

    vault.deposit(amount, {"from": user})
    assert strategy.harvestTrigger(0)
    strategy.harvest({"from": gov})
    params = vault.strategies(strategy)
    trigger_state = {
        "force": strategy.forceHarvestTriggerOnce(),
        "report_age": chain.time() - params["lastReport"],
        "min_report_delay": strategy.minReportDelay(),
        "claimable": reward_distributor.getClaimable(strategy),
        "credit_available": vault.creditAvailable({"from": strategy}),
        "credit_threshold": strategy.creditThreshold(),
        "time_until_week_end": (chain.time() // WEEK + 1) * WEEK - chain.time(),
        "week_end_threshold": threshold,
    }
    print("Trigger state after harvest:", trigger_state)
    assert not strategy.harvestTrigger(0), trigger_state
    vault.deposit(below_threshold_amount, {"from": user})
    assert not strategy.harvestTrigger(0)
