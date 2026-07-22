import brownie
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
