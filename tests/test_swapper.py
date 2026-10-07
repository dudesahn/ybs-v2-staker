import brownie
import pytest
from brownie import Contract


WEEK = 60 * 60 * 24 * 7
PRECISION = 10**18
MAX_UINT = 2**256 - 1


def _prepare_swapper(swapper, strategy, chain, gov, management):
    old_swapper = strategy.swapper()
    token_in = Contract(swapper.tokenIn())

    strategy.harvest({"from": gov})
    strategy.upgradeSwapper(swapper, {"from": gov})

    assert strategy.swapper() == swapper
    assert token_in.allowance(strategy, old_swapper) == 0
    assert token_in.allowance(strategy, swapper) == MAX_UINT
    assert swapper.management() == management
    assert swapper.priceOracle() > 0

    chain.sleep(3 * WEEK)
    chain.mine()


def _harvest_week(strategy, deposit_rewards, chain, gov):
    deposit_rewards()
    chain.sleep(WEEK)
    chain.mine()
    return strategy.harvest({"from": gov})


def _assert_otc_event(tx):
    assert "OTC" in tx.events
    event = tx.events["OTC"]

    assert event["price"] > 0
    assert event["sellTokenAmount"] > 0
    assert event["buyTokenAmount"] > 0
    quoted_buy_amount = event["sellTokenAmount"] * event["price"] // PRECISION
    # When the available buy-token balance caps an OTC trade, SwapperV5 derives
    # sellTokenAmount with a division and rounds down. Reconstructing the quote
    # here performs a second round down, so the emitted buy amount can exceed it
    # by at most ceil(price / PRECISION) token-wei.
    rounding_tolerance = (event["price"] + PRECISION - 1) // PRECISION
    assert quoted_buy_amount <= event["buyTokenAmount"]
    assert event["buyTokenAmount"] - quoted_buy_amount <= rounding_tolerance
    return event


def _assert_otc_enabled_event(tx, enabled):
    assert tx.events["OTCEnabled"]["enabled"] is enabled


@pytest.fixture
def loose_crvusd(reward_underlying, user, funded_crvusd):
    assert reward_underlying.balanceOf(user) >= 1_000 * PRECISION
    return reward_underlying


@pytest.mark.parametrize("with_inventory", [False, True])
def test_current_strategy_uses_market_after_otc_inventory_runs_out(
    strategy,
    swapper_v5,
    vault,
    token,
    loose_crvusd,
    user,
    gov,
    management,
    fund_ycrv,
    chain,
    with_inventory,
):
    swapper = swapper_v5
    treasury_vault = Contract(swapper.vault())
    strategy.setBypasses(True, True, {"from": gov})
    strategy.setSwapThresholds(0, 1_000_000 * PRECISION, False, {"from": gov})
    strategy.setWeekEndHarvestTrigger(0, {"from": gov})
    strategy.harvest({"from": gov})
    assert strategy.balanceOfReward() == 0
    assert loose_crvusd.balanceOf(strategy) < PRECISION
    assert vault.balanceOf(swapper) == 0
    assert token.balanceOf(swapper) == 0

    swapper.enableOtc(True, {"from": management})
    if with_inventory:
        # Enough for only a quarter of the next sale, with the rest going to market.
        fund_ycrv(swapper, 25 * swapper.priceOracle())

    for sale in range(2):
        loose_crvusd.transfer(strategy, 100 * PRECISION, {"from": user})
        inventory_before = token.balanceOf(swapper)
        treasury_before = treasury_vault.balanceOf(swapper.treasury())
        params_before = vault.strategies(strategy)

        # The vault rejects two fee assessments at the same timestamp.
        chain.sleep(1)
        chain.mine()
        tx = strategy.harvest({"from": gov})

        if with_inventory and sale == 0:
            event = _assert_otc_event(tx)
            assert event["buyTokenAmount"] == inventory_before
            assert treasury_vault.balanceOf(swapper.treasury()) > treasury_before
        else:
            assert "OTC" not in tx.events
            assert treasury_vault.balanceOf(swapper.treasury()) == treasury_before

        assert token.balanceOf(swapper) == 0
        assert loose_crvusd.balanceOf(swapper) == 0
        assert loose_crvusd.balanceOf(strategy) == 0
        assert vault.strategies(strategy)["totalGain"] > params_before["totalGain"]
        assert vault.strategies(strategy)["totalLoss"] == params_before["totalLoss"]


@pytest.mark.parametrize("dust", ["ycrv", "shares"])
def test_otc_dust_inventory_cannot_block_strategy_sales(
    strategy,
    swapper_v5,
    vault,
    token,
    loose_crvusd,
    user,
    gov,
    management,
    fund_ycrv,
    chain,
    dust,
):
    swapper = swapper_v5
    treasury_vault = Contract(swapper.vault())
    strategy.setBypasses(True, True, {"from": gov})
    strategy.setSwapThresholds(0, 1_000_000 * PRECISION, False, {"from": gov})
    strategy.setWeekEndHarvestTrigger(0, {"from": gov})
    if dust == "shares":
        # Mint parent-vault shares for the dust donation below.
        assets = 100 * PRECISION
        fund_ycrv(user, assets)
        token.approve(vault, assets, {"from": user})
        vault.deposit(assets, {"from": user})
    strategy.harvest({"from": gov})
    assert vault.balanceOf(swapper) == 0
    assert token.balanceOf(swapper) == 0

    swapper.enableOtc(True, {"from": management})
    if dust == "ycrv":
        # A capped fill against this inventory would sell 1 wei of crvUSD, and
        # the treasury vault mints no shares for 1 wei.
        donated = swapper.priceOracle() // PRECISION + 1
        fund_ycrv(swapper, donated)
        assert donated * PRECISION // swapper.priceOracle() == 1
    else:
        # Redeeming 2 share-wei would ask the strategy for an odd few wei.
        donated = 2
        vault.transfer(swapper, donated, {"from": user})

    loose_crvusd.transfer(strategy, 100 * PRECISION, {"from": user})
    treasury_before = treasury_vault.balanceOf(swapper.treasury())
    gain_before = vault.strategies(strategy)["totalGain"]

    chain.sleep(1)
    chain.mine()
    tx = strategy.harvest({"from": gov})

    # The swapper ignores the dust, and the whole sale uses the market route.
    assert "OTC" not in tx.events
    assert treasury_vault.balanceOf(swapper.treasury()) == treasury_before
    assert loose_crvusd.balanceOf(strategy) == 0
    assert vault.strategies(strategy)["totalGain"] > gain_before
    if dust == "ycrv":
        assert token.balanceOf(swapper) == donated
    else:
        assert vault.balanceOf(swapper) == donated


@pytest.mark.parametrize("otc_enabled", [False, True])
@pytest.mark.parametrize("amount", [0, 1, PRECISION - 1])
def test_swapper_refunds_input_below_market_minimum(
    swapper_v5,
    loose_crvusd,
    token,
    vault,
    user,
    management,
    fund_ycrv,
    otc_enabled,
    amount,
):
    swapper = swapper_v5
    treasury_vault = Contract(swapper.vault())
    swapper.setAllowedSwapper(user, True, {"from": management})
    swapper.enableOtc(otc_enabled, {"from": management})
    loose_crvusd.approve(swapper, MAX_UINT, {"from": user})
    # Parent-vault share inventory is larger than a tiny quote and smaller than
    # a near-1e18 quote, so both OTC branches would redeem it without the
    # sub-minimum input check.
    share_assets = 10 * PRECISION
    fund_ycrv(user, share_assets)
    token.approve(vault, share_assets, {"from": user})
    user_shares_before = vault.balanceOf(user)
    vault.deposit(share_assets, {"from": user})
    vault.transfer(swapper, vault.balanceOf(user) - user_shares_before, {"from": user})
    swapper_shares = vault.balanceOf(swapper)
    assert swapper_shares > 0
    # A refund belongs only to the current caller; pre-existing funds stay put.
    stranded = 3 * PRECISION
    loose_crvusd.transfer(swapper, stranded, {"from": user})
    input_before = loose_crvusd.balanceOf(user)
    output_before = token.balanceOf(user)
    treasury_before = treasury_vault.balanceOf(swapper.treasury())

    tx = swapper.swap(amount, {"from": user})

    assert tx.return_value == 0
    assert "OTC" not in tx.events
    assert loose_crvusd.balanceOf(user) == input_before
    assert loose_crvusd.balanceOf(swapper) == stranded
    assert token.balanceOf(user) == output_before
    assert treasury_vault.balanceOf(swapper.treasury()) == treasury_before
    assert vault.balanceOf(swapper) == swapper_shares


def test_swapper_refunds_dust_after_partial_otc_fill(
    swapper_v5,
    loose_crvusd,
    token,
    user,
    management,
    fund_ycrv,
):
    swapper = swapper_v5
    treasury_vault = Contract(swapper.vault())
    swapper.setAllowedSwapper(user, True, {"from": management})
    swapper.enableOtc(True, {"from": management})
    loose_crvusd.approve(swapper, MAX_UINT, {"from": user})
    amount = 10 * PRECISION
    inventory = (amount - PRECISION // 2) * swapper.priceOracle() // PRECISION
    fund_ycrv(swapper, inventory)
    input_before = loose_crvusd.balanceOf(user)
    output_before = token.balanceOf(user)
    treasury_before = treasury_vault.balanceOf(swapper.treasury())

    tx = swapper.swap(amount, {"from": user})

    event = _assert_otc_event(tx)
    remainder = amount - event["sellTokenAmount"]
    assert 0 < remainder < PRECISION
    assert tx.return_value == event["buyTokenAmount"] == inventory
    assert input_before - loose_crvusd.balanceOf(user) == event["sellTokenAmount"]
    assert token.balanceOf(user) - output_before == inventory
    assert loose_crvusd.balanceOf(swapper) == 0
    assert token.balanceOf(swapper) == 0
    assert treasury_vault.balanceOf(swapper.treasury()) > treasury_before


def test_otc_permission_is_required_and_revocable(
    swapper_v5,
    loose_crvusd,
    token,
    user,
    management,
    fund_ycrv,
):
    swapper = swapper_v5
    treasury_vault = Contract(swapper.vault())
    amount = 100 * PRECISION
    # Keep enough inventory after the permitted sale for another real trade.
    inventory = 3 * amount * swapper.priceOracle() // PRECISION
    fund_ycrv(swapper, inventory)
    swapper.enableOtc(True, {"from": management})
    loose_crvusd.approve(swapper, MAX_UINT, {"from": user})

    def balances():
        return {
            "caller_input": loose_crvusd.balanceOf(user),
            "caller_output": token.balanceOf(user),
            "swapper_input": loose_crvusd.balanceOf(swapper),
            "swapper_output": token.balanceOf(swapper),
            "treasury_shares": treasury_vault.balanceOf(swapper.treasury()),
        }

    assert not swapper.allowedSwapper(user)
    before = balances()
    with brownie.reverts("!AllowedSwapper"):
        swapper.swap(amount, {"from": user})
    assert balances() == before

    swapper.setAllowedSwapper(user, True, {"from": management})
    before = balances()
    tx = swapper.swap(amount, {"from": user})
    event = _assert_otc_event(tx)
    after = balances()

    assert event["sellTokenAmount"] == amount
    assert tx.return_value == event["buyTokenAmount"]
    assert before["caller_input"] - after["caller_input"] == amount
    assert after["caller_output"] - before["caller_output"] == tx.return_value
    assert after["swapper_input"] == before["swapper_input"]
    assert before["swapper_output"] - after["swapper_output"] == tx.return_value
    assert after["treasury_shares"] > before["treasury_shares"]

    swapper.setAllowedSwapper(user, False, {"from": management})
    before = balances()
    with brownie.reverts("!AllowedSwapper"):
        swapper.swap(amount, {"from": user})
    assert balances() == before


def test_swapper(
    swapper_v5,
    deposit_rewards,
    chain,
    gov,
    old_strategy,
    management,
    fund_ycrv,
):
    swapper = swapper_v5
    strategy = old_strategy
    token_out = Contract(swapper.tokenOut())
    treasury_vault = Contract(swapper.vault())

    _prepare_swapper(swapper, strategy, chain, gov, management)

    assert not swapper.otcEnabled()
    token_out_before = token_out.balanceOf(swapper)
    treasury_shares_before = treasury_vault.balanceOf(swapper.treasury())
    tx = _harvest_week(strategy, deposit_rewards, chain, gov)
    assert "OTC" not in tx.events
    assert token_out.balanceOf(swapper) == token_out_before
    assert treasury_vault.balanceOf(swapper.treasury()) == treasury_shares_before

    tx = swapper.enableOtc(True, {"from": management})
    _assert_otc_enabled_event(tx, True)
    assert swapper.otcEnabled()

    fund_ycrv(swapper, 10 * 10 ** token_out.decimals())
    token_out_before = token_out.balanceOf(swapper)
    treasury_shares_before = treasury_vault.balanceOf(swapper.treasury())

    tx = _harvest_week(strategy, deposit_rewards, chain, gov)
    event = _assert_otc_event(tx)

    assert event["buyTokenAmount"] <= token_out_before
    assert token_out.balanceOf(swapper) == token_out_before - event["buyTokenAmount"]
    assert treasury_vault.balanceOf(swapper.treasury()) > treasury_shares_before
    assert swapper.otcEnabled()

    tx = swapper.enableOtc(False, {"from": management})
    _assert_otc_enabled_event(tx, False)
    assert not swapper.otcEnabled()

    token_out_before = token_out.balanceOf(swapper)
    treasury_shares_before = treasury_vault.balanceOf(swapper.treasury())
    tx = _harvest_week(strategy, deposit_rewards, chain, gov)
    assert "OTC" not in tx.events
    assert token_out.balanceOf(swapper) == token_out_before
    assert treasury_vault.balanceOf(swapper.treasury()) == treasury_shares_before


def test_swapper_withdraw(
    swapper_v5,
    vault,
    deposit_rewards,
    chain,
    gov,
    old_strategy,
    management,
    token,
    user,
    fund_ycrv,
):
    swapper = swapper_v5
    strategy = old_strategy
    treasury_vault = Contract(swapper.vault())

    _prepare_swapper(swapper, strategy, chain, gov, management)

    shares_to_supply = 10 * PRECISION
    assets_needed = (
        shares_to_supply * vault.pricePerShare() + PRECISION - 1
    ) // PRECISION
    assets_to_deposit = assets_needed * 101 // 100 + 1

    fund_ycrv(user, assets_to_deposit)
    token.approve(vault, assets_to_deposit, {"from": user})
    vault.deposit(assets_to_deposit, {"from": user})
    assert vault.balanceOf(user) >= shares_to_supply
    vault.transfer(swapper, shares_to_supply, {"from": user})

    tx = swapper.enableOtc(True, {"from": management})
    _assert_otc_enabled_event(tx, True)
    assert swapper.otcEnabled()

    shares_before = vault.balanceOf(swapper)
    token_out_before = token.balanceOf(swapper)
    treasury_shares_before = treasury_vault.balanceOf(swapper.treasury())

    tx = _harvest_week(strategy, deposit_rewards, chain, gov)
    event = _assert_otc_event(tx)

    shares_after = vault.balanceOf(swapper)
    token_out_after = token.balanceOf(swapper)
    assert shares_after < shares_before
    assert token_out_after + event["buyTokenAmount"] > token_out_before
    assert treasury_vault.balanceOf(swapper.treasury()) > treasury_shares_before

    tx = swapper.enableOtc(False, {"from": management})
    _assert_otc_enabled_event(tx, False)
    assert not swapper.otcEnabled()

    shares_before = vault.balanceOf(swapper)
    token_out_before = token.balanceOf(swapper)
    treasury_shares_before = treasury_vault.balanceOf(swapper.treasury())
    tx = _harvest_week(strategy, deposit_rewards, chain, gov)
    assert "OTC" not in tx.events
    assert vault.balanceOf(swapper) == shares_before
    assert token.balanceOf(swapper) == token_out_before
    assert treasury_vault.balanceOf(swapper.treasury()) == treasury_shares_before


def test_swapper_settings(
    swapper_v5,
    gov,
    management,
    user,
    crvusd_dummy_vault,
):
    swapper = swapper_v5

    with brownie.reverts("!ownerOrManagement"):
        swapper.setAllowedSwapper(user, True, {"from": user})

    tx = swapper.setAllowedSwapper(user, True, {"from": management})
    assert tx.events["SetAllowedSwapper"]["caller"] == user
    assert tx.events["SetAllowedSwapper"]["isAllowed"] is True
    assert swapper.allowedSwapper(user)

    tx = swapper.setAllowedSwapper(user, False, {"from": gov})
    assert tx.events["SetAllowedSwapper"]["caller"] == user
    assert tx.events["SetAllowedSwapper"]["isAllowed"] is False
    assert not swapper.allowedSwapper(user)

    original_vault = swapper.vault()
    token_in = Contract(swapper.tokenIn())
    assert token_in.allowance(swapper, original_vault) == MAX_UINT

    with brownie.reverts("!owner"):
        swapper.setVault(crvusd_dummy_vault, {"from": management})

    tx = swapper.setVault(crvusd_dummy_vault, {"from": gov})
    assert tx.events["SetVault"]["vault"] == crvusd_dummy_vault
    assert swapper.vault() == crvusd_dummy_vault
    assert token_in.allowance(swapper, original_vault) == 0
    assert token_in.allowance(swapper, crvusd_dummy_vault) == MAX_UINT

    tx = swapper.setVault(original_vault, {"from": gov})
    assert tx.events["SetVault"]["vault"] == original_vault
    assert swapper.vault() == original_vault
    assert token_in.allowance(swapper, crvusd_dummy_vault) == 0
    assert token_in.allowance(swapper, original_vault) == MAX_UINT


def test_swapper_operator_and_management_access(
    swapper_v5,
    gov,
    management,
    user,
):
    swapper = swapper_v5

    with brownie.reverts("!ownerOrManagement"):
        swapper.setOperator(user, True, {"from": user})
    with brownie.reverts("!operator"):
        swapper.enableOtc(True, {"from": user})

    tx = swapper.setOperator(user, True, {"from": management})
    assert tx.events["SetOperator"]["caller"] == user
    assert tx.events["SetOperator"]["isAllowed"] is True
    assert swapper.operator(user)

    tx = swapper.enableOtc(True, {"from": user})
    _assert_otc_enabled_event(tx, True)
    assert swapper.otcEnabled()

    tx = swapper.setOperator(user, False, {"from": gov})
    assert tx.events["SetOperator"]["caller"] == user
    assert tx.events["SetOperator"]["isAllowed"] is False
    assert not swapper.operator(user)
    with brownie.reverts("!operator"):
        swapper.enableOtc(False, {"from": user})

    tx = swapper.enableOtc(False, {"from": management})
    _assert_otc_enabled_event(tx, False)
    assert not swapper.otcEnabled()

    with brownie.reverts("!owner"):
        swapper.setManagement(user, {"from": management})

    tx = swapper.setManagement(user, {"from": gov})
    assert tx.events["SetManagement"]["management"] == user
    assert swapper.management() == user
    with brownie.reverts("!ownerOrManagement"):
        swapper.setAllowedSwapper(user, True, {"from": management})
    with brownie.reverts("!operator"):
        swapper.enableOtc(True, {"from": management})

    tx = swapper.enableOtc(True, {"from": user})
    _assert_otc_enabled_event(tx, True)
    assert swapper.otcEnabled()

    tx = swapper.setManagement(management, {"from": gov})
    assert tx.events["SetManagement"]["management"] == management
    assert swapper.management() == management
    with brownie.reverts("!operator"):
        swapper.enableOtc(False, {"from": user})

    tx = swapper.enableOtc(False, {"from": management})
    _assert_otc_enabled_event(tx, False)
    assert not swapper.otcEnabled()
