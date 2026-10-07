from brownie import Contract, web3


WEEK = 7 * 24 * 60 * 60
MAX_UINT256 = 2**256 - 1
MAX_LOCK_TIME = 4 * 365 * 24 * 60 * 60
CRV = "0xD533a949740bb3306d119CC777fa900bA034cd52"
VE_CRV = "0x5f3b5DfEb7B28CDbD7FAba78963EE202a494e2A2"
YEARN_VOTER = "0xF147b8125d2ef93FB6965Db97D6746952a133934"
CRV_HOLDER = "0xF977814e90dA44bFA03b6295A0616a897441aceC"


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
    chain,
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

    chain.sleep(1)
    chain.mine()
    strategy.harvest({"from": gov})
    assert vault.creditAvailable({"from": strategy}) <= credit_threshold
    assert not strategy.harvestTrigger(0)


def test_force_trigger_is_consumed_by_harvest(chain, gov, vault, strategy):
    _reset_trigger_state(strategy, vault, gov)

    strategy.setForceHarvestTriggerOnce(True, {"from": gov})
    assert strategy.forceHarvestTriggerOnce()
    assert strategy.harvestTrigger(0)

    chain.sleep(1)
    chain.mine()
    strategy.harvest({"from": gov})
    assert not strategy.forceHarvestTriggerOnce()
    assert not strategy.harvestTrigger(0)


def test_unacceptable_base_fee_blocks_forced_trigger(
    gov,
    strategist,
    vault,
    strategy,
    MockBaseFeeOracle,
):
    _reset_trigger_state(strategy, vault, gov)
    oracle = strategist.deploy(MockBaseFeeOracle, False)
    strategy.setBaseFeeOracle(oracle, {"from": gov})
    strategy.setForceHarvestTriggerOnce(True, {"from": gov})

    assert not strategy.isBaseFeeAcceptable()
    assert not strategy.harvestTrigger(0)
    assert not strategy.harvestTrigger(MAX_UINT256)

    oracle.setAcceptable(True, {"from": strategist})

    assert strategy.isBaseFeeAcceptable()
    assert strategy.harvestTrigger(0)
    assert strategy.harvestTrigger(MAX_UINT256)


def test_min_report_delay_uses_strict_boundary(chain, gov, vault, strategy):
    _reset_trigger_state(strategy, vault, gov)

    min_report_delay = 60 * 60
    strategy.setMinReportDelay(min_report_delay, {"from": gov})
    last_report = vault.strategies(strategy)["lastReport"]

    _mine_at(chain, last_report + min_report_delay)
    assert _latest_timestamp(chain) - last_report == min_report_delay
    assert not strategy.harvestTrigger(0)

    _mine_at(chain, last_report + min_report_delay + 1)
    assert _latest_timestamp(chain) - last_report == min_report_delay + 1
    assert strategy.harvestTrigger(0)


def test_claim_bypass_preserves_other_harvest_triggers(
    chain,
    gov,
    vault,
    strategy,
    reward_distributor,
    deposit_rewards,
    token,
    user,
    fund_ycrv,
):
    _reset_trigger_state(strategy, vault, gov)
    deposit_rewards()
    chain.sleep(WEEK)
    chain.mine()
    assert reward_distributor.getClaimable(strategy) > 0
    assert strategy.harvestTrigger(0)

    strategy.setBypasses(True, False, {"from": gov})
    assert not strategy.harvestTrigger(0)
    strategy.harvest({"from": gov})
    assert reward_distributor.getClaimable(strategy) > 0
    assert not strategy.harvestTrigger(0)

    strategy.setForceHarvestTriggerOnce(True, {"from": gov})
    assert strategy.harvestTrigger(0)
    strategy.setForceHarvestTriggerOnce(False, {"from": gov})

    deposit = 100 * 10 ** token.decimals()
    starting_credit = vault.creditAvailable({"from": strategy})
    strategy.setCreditThreshold(starting_credit, {"from": gov})
    _fund_and_approve(token, vault, user, fund_ycrv, deposit)
    vault.deposit(deposit, {"from": user})
    assert strategy.harvestTrigger(0)
    strategy.setCreditThreshold(MAX_UINT256, {"from": gov})
    assert not strategy.harvestTrigger(0)

    strategy.setMinReportDelay(60 * 60, {"from": gov})
    _mine_at(chain, vault.strategies(strategy)["lastReport"] + 60 * 60 + 1)
    assert strategy.harvestTrigger(0)
    strategy.setMinReportDelay(4 * WEEK, {"from": gov})
    assert not strategy.harvestTrigger(0)

    strategy.setBypasses(False, False, {"from": gov})
    assert strategy.harvestTrigger(0)


def test_week_end_lock_window_does_not_override_trigger_checks(
    chain,
    token,
    gov,
    vault,
    strategy,
    strategist,
    user,
    fund_ycrv,
    MockBaseFeeOracle,
):
    starting_credit = _reset_trigger_state(strategy, vault, gov)
    now = _latest_timestamp(chain)
    week_end = (now // WEEK + 1) * WEEK
    lock_window = strategy.thresholdTimeUntilWeekEnd()
    assert lock_window == 2 * 24 * 60 * 60

    # Start a fresh Curve week when the fork begins inside the lock window, so
    # the last report is unambiguously outside the next lock window.
    if week_end - now <= lock_window:
        _mine_at(chain, week_end + 1)
        strategy.harvest({"from": gov})
        starting_credit = vault.creditAvailable({"from": strategy})
        week_end += WEEK

    oracle = strategist.deploy(MockBaseFeeOracle, False)
    strategy.setBaseFeeOracle(oracle, {"from": gov})
    assert not strategy.isBaseFeeAcceptable()

    deposit = 100 * 10 ** token.decimals()
    credit_threshold = starting_credit + deposit

    strategy.setCreditThreshold(credit_threshold, {"from": gov})
    _fund_and_approve(token, vault, user, fund_ycrv, deposit)
    vault.deposit(deposit, {"from": user})

    available_credit = vault.creditAvailable({"from": strategy})
    assert 0 < available_credit == credit_threshold
    assert week_end - vault.strategies(strategy)["lastReport"] > lock_window

    window_start = week_end - lock_window
    _mine_at(chain, window_start - 1)
    assert week_end - _latest_timestamp(chain) > lock_window
    assert not strategy.harvestTrigger(0)

    _mine_at(chain, window_start)
    assert week_end - _latest_timestamp(chain) <= lock_window
    # Near-week-end status only gates proxy maintenance in prepareReturn; it no
    # longer bypasses base-fee or normal trigger checks.
    assert not strategy.harvestTrigger(0)
    oracle.setAcceptable(True, {"from": strategist})
    assert strategy.isBaseFeeAcceptable()
    assert not strategy.harvestTrigger(0)
    strategy.setForceHarvestTriggerOnce(True, {"from": gov})
    assert strategy.harvestTrigger(0)


def test_harvest_locks_voter_crv_only_in_week_end_window(
    accounts,
    chain,
    gov,
    strategy,
):
    crv = Contract(CRV)
    ve_crv = Contract(VE_CRV)
    lock_window = strategy.thresholdTimeUntilWeekEnd()
    # Keep the harvests to proxy maintenance: no claims, sales or stakes.
    strategy.setBypasses(True, True, {"from": gov})

    week_end = (_latest_timestamp(chain) // WEEK + 1) * WEEK
    if week_end - _latest_timestamp(chain) <= lock_window + 60:
        # Start a fresh Curve week, so the first harvest is outside the window.
        _mine_at(chain, week_end + 1)
        week_end += WEEK

    crv_holder = accounts.at(CRV_HOLDER, force=True)
    crv.transfer(YEARN_VOTER, 1_000 * 10**18, {"from": crv_holder})
    voter_crv = crv.balanceOf(YEARN_VOTER)
    locked_before = ve_crv.locked(YEARN_VOTER)

    tx = strategy.harvest({"from": gov})
    assert week_end - tx.timestamp > lock_window
    assert crv.balanceOf(YEARN_VOTER) == voter_crv
    assert ve_crv.locked(YEARN_VOTER) == locked_before

    _mine_at(chain, week_end - lock_window // 2)
    tx = strategy.harvest({"from": gov})
    assert week_end - tx.timestamp <= lock_window
    # The proxy locks all of the voter's CRV and extends the lock to the maximum.
    assert crv.balanceOf(YEARN_VOTER) == 0
    assert ve_crv.locked(YEARN_VOTER)["amount"] == locked_before["amount"] + voter_crv
    max_lock_end = (tx.timestamp + MAX_LOCK_TIME) // WEEK * WEEK
    assert ve_crv.locked__end(YEARN_VOTER) == max_lock_end
