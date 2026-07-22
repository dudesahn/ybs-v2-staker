from brownie import web3


WEEK = 7 * 24 * 60 * 60
MAX_UINT256 = 2**256 - 1


def _latest_timestamp(chain):
    return chain[-1].timestamp


def _mine_at(chain, timestamp):
    """Mine at an exact timestamp without Brownie's broken Anvil timestamp path."""
    assert timestamp > _latest_timestamp(chain)
    response = web3.provider.make_request("evm_setNextBlockTimestamp", [timestamp])
    assert "error" not in response, response
    chain.mine()
    assert _latest_timestamp(chain) == timestamp


def _reset_trigger_state(strategy, vault, gov):
    """Clear every trigger and report once to establish a quiet baseline."""
    strategy.setWeekEndHarvestTrigger(0, {"from": gov})
    strategy.setMinReportDelay(4 * WEEK, {"from": gov})
    strategy.setCreditThreshold(MAX_UINT256, {"from": gov})
    strategy.setForceHarvestTriggerOnce(False, {"from": gov})
    strategy.harvest({"from": gov})

    assert not strategy.forceHarvestTriggerOnce()
    assert not strategy.harvestTrigger(0)
    return vault.creditAvailable({"from": strategy})


def _fund_and_approve(token, vault, user, fund_ycrv, amount):
    fund_ycrv(user, amount)
    token.approve(vault.address, MAX_UINT256, {"from": user})


def test_credit_threshold_is_strict_and_resets_after_harvest(
    token,
    gov,
    vault,
    strategy,
    user,
    fund_ycrv,
):
    starting_credit = _reset_trigger_state(strategy, vault, gov)
    deposit_at_threshold = 1_000 * 10 ** token.decimals()
    deposit_above_threshold = 10 ** token.decimals()
    credit_threshold = starting_credit + deposit_at_threshold

    strategy.setCreditThreshold(credit_threshold, {"from": gov})
    _fund_and_approve(
        token,
        vault,
        user,
        fund_ycrv,
        deposit_at_threshold + deposit_above_threshold,
    )

    vault.deposit(deposit_at_threshold, {"from": user})
    assert vault.creditAvailable({"from": strategy}) == credit_threshold
    assert not strategy.harvestTrigger(0)

    vault.deposit(deposit_above_threshold, {"from": user})
    assert vault.creditAvailable({"from": strategy}) > credit_threshold
    assert strategy.harvestTrigger(0)

    strategy.harvest({"from": gov})
    assert vault.creditAvailable({"from": strategy}) <= credit_threshold
    assert not strategy.harvestTrigger(0)


def test_force_trigger_is_consumed_by_harvest(gov, vault, strategy):
    _reset_trigger_state(strategy, vault, gov)

    strategy.setForceHarvestTriggerOnce(True, {"from": gov})
    assert strategy.forceHarvestTriggerOnce()
    assert strategy.harvestTrigger(0)

    strategy.harvest({"from": gov})
    assert not strategy.forceHarvestTriggerOnce()
    assert not strategy.harvestTrigger(0)


def test_min_report_delay_uses_strict_boundary(chain, gov, vault, strategy):
    _reset_trigger_state(strategy, vault, gov)
    strategy.setWeekEndHarvestTrigger(0, {"from": gov})

    min_report_delay = 60 * 60
    now = _latest_timestamp(chain)
    week_end = (now // WEEK + 1) * WEEK
    if week_end - now <= min_report_delay + 60:
        _mine_at(chain, week_end + 1)
        strategy.harvest({"from": gov})

    strategy.setMinReportDelay(min_report_delay, {"from": gov})
    last_report = vault.strategies(strategy)["lastReport"]

    _mine_at(chain, last_report + min_report_delay)
    assert _latest_timestamp(chain) - last_report == min_report_delay
    assert not strategy.harvestTrigger(0)

    _mine_at(chain, last_report + min_report_delay + 1)
    assert _latest_timestamp(chain) - last_report == min_report_delay + 1
    assert strategy.harvestTrigger(0)


def test_custom_week_end_window_triggers_on_any_positive_credit(
    chain,
    token,
    gov,
    vault,
    strategy,
    user,
    fund_ycrv,
):
    starting_credit = _reset_trigger_state(strategy, vault, gov)
    now = _latest_timestamp(chain)
    week_end = (now // WEEK + 1) * WEEK

    # Keep the boundary in the current reward week so no claimable rewards can
    # appear while this test advances time. If necessary, start a fresh week.
    if week_end - now <= 10 * 60:
        _mine_at(chain, week_end + 1)
        strategy.harvest({"from": gov})
        starting_credit = vault.creditAvailable({"from": strategy})
        week_end += WEEK

    boundary_lead_time = 5 * 60
    custom_window = week_end - _latest_timestamp(chain) - boundary_lead_time
    deposit = 100 * 10 ** token.decimals()
    credit_threshold = starting_credit + deposit

    strategy.setWeekEndHarvestTrigger(custom_window, {"from": gov})
    strategy.setCreditThreshold(credit_threshold, {"from": gov})
    _fund_and_approve(token, vault, user, fund_ycrv, deposit)
    vault.deposit(deposit, {"from": user})

    available_credit = vault.creditAvailable({"from": strategy})
    assert 0 < available_credit == credit_threshold
    assert week_end - vault.strategies(strategy)["lastReport"] > custom_window

    window_start = week_end - custom_window
    _mine_at(chain, window_start - 1)
    assert week_end - _latest_timestamp(chain) == custom_window + 1
    assert not strategy.harvestTrigger(0)

    _mine_at(chain, window_start)
    assert week_end - _latest_timestamp(chain) == custom_window
    assert strategy.harvestTrigger(0)
