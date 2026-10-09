import brownie
import pytest
from brownie import Contract


WEEK = 60 * 60 * 24 * 7
PRECISION = 10**18
MAX_UINT = 2**256 - 1
CRV_HOLDER = "0xF977814e90dA44bFA03b6295A0616a897441aceC"


def _settle_strategy(swapper, strategy, chain, gov):
    # Report the migrated position, then let its stake earn weekly reward weight.
    assert strategy.swapper() == swapper
    assert swapper.priceOracle() > 0

    strategy.harvest({"from": gov})
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


def test_harvest_needs_otc_inventory_or_otc_disabled(
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
):
    swapper = swapper_v5
    treasury_vault = Contract(swapper.vault())
    strategy.setBypasses(True, True, {"from": gov})
    strategy.setSwapThresholds(0, 1_000_000 * PRECISION, False, {"from": gov})
    # This sells the whole migrated carryover at once, past the default slippage floor.
    strategy.setMaxSlippage(10_000, {"from": gov})
    strategy.setWeekEndHarvestTrigger(0, {"from": gov})
    strategy.harvest({"from": gov})
    assert strategy.balanceOfReward() == 0
    assert loose_crvusd.balanceOf(strategy) == 0

    # Replace the live inventory with enough for only a quarter of the next sale.
    swapper.sweep(token, {"from": management})
    swapper.sweep(vault, {"from": management})
    inventory = 25 * swapper.priceOracle()
    fund_ycrv(swapper, inventory)
    swapper.enableOtc(True, {"from": management})

    loose_crvusd.transfer(strategy, 100 * PRECISION, {"from": user})
    treasury_before = treasury_vault.balanceOf(swapper.treasury())
    chain.sleep(1)
    chain.mine()
    tx = strategy.harvest({"from": gov})

    # A partial fill sells the whole inventory, and the rest goes to market.
    event = _assert_otc_event(tx)
    assert event["buyTokenAmount"] == inventory
    assert treasury_vault.balanceOf(swapper.treasury()) > treasury_before
    assert token.balanceOf(swapper) == 0
    assert vault.balanceOf(swapper) == 0
    assert loose_crvusd.balanceOf(strategy) == 0

    # With OTC still on and no inventory, the swapper deposits nothing into
    # the treasury vault, which reverts the harvest.
    loose_crvusd.transfer(strategy, 100 * PRECISION, {"from": user})
    chain.sleep(1)
    chain.mine()
    with brownie.reverts():
        strategy.harvest({"from": gov})

    # Operating rule: disable OTC when inventory is empty or too small to mint
    # treasury-vault shares (covered separately below).
    swapper.enableOtc(False, {"from": management})
    gain_before = vault.strategies(strategy)["totalGain"]
    tx = strategy.harvest({"from": gov})
    assert "OTC" not in tx.events
    assert loose_crvusd.balanceOf(strategy) == 0
    assert vault.strategies(strategy)["totalGain"] > gain_before


@pytest.mark.parametrize("sell_wei", [0, 1])
def test_tiny_otc_inventory_requires_otc_disabled(
    sell_wei,
    strategy,
    swapper_v5,
    vault,
    token,
    reward_underlying,
    gov,
    management,
    fund_ycrv,
    chain,
):
    swapper = swapper_v5
    treasury_vault = Contract(swapper.vault())
    strategy.setBypasses(True, True, {"from": gov})
    strategy.setWeekEndHarvestTrigger(0, {"from": gov})
    sale = 1_000 * PRECISION
    strategy.setSwapThresholds(100 * PRECISION, sale, False, {"from": gov})
    assert strategy.maxSlippage() == 300
    balance_before = reward_underlying.balanceOf(strategy)
    assert balance_before > sale

    swapper.sweep(token, {"from": management})
    swapper.sweep(vault, {"from": management})
    # Nonzero yCRV inventory can quote either zero crvUSD or one wei, which
    # rounds to zero treasury-vault shares at this fork's share price.
    price = swapper.priceOracle()
    inventory = 1 if sell_wei == 0 else price // PRECISION + 1
    assert inventory * PRECISION // price == sell_wei
    fund_ycrv(swapper, inventory)
    assert token.balanceOf(swapper) == inventory
    assert vault.balanceOf(swapper) == 0
    swapper.enableOtc(True, {"from": management})
    treasury_before = treasury_vault.balanceOf(swapper.treasury())

    chain.sleep(1)
    chain.mine()
    with brownie.reverts("cannot mint zero"):
        strategy.harvest({"from": gov})
    assert reward_underlying.balanceOf(strategy) == balance_before
    assert token.balanceOf(swapper) == inventory
    assert treasury_vault.balanceOf(swapper.treasury()) == treasury_before

    # Disable OTC even when its inventory is nonzero but unusably small.
    # The same sale must succeed with the default slippage floor still active.
    swapper.enableOtc(False, {"from": management})
    gain_before = vault.strategies(strategy)["totalGain"]
    tx = strategy.harvest({"from": gov})
    assert "OTC" not in tx.events
    assert reward_underlying.balanceOf(strategy) == balance_before - sale
    assert vault.strategies(strategy)["totalGain"] > gain_before
    assert token.balanceOf(swapper) == inventory


def test_market_sale_must_clear_the_oracle_floor(
    accounts,
    strategy,
    swapper_v5,
    vault,
    reward_underlying,
    gov,
    chain,
):
    crv = Contract(swapper_v5.tokenOutPool1())
    ycrv_pool = Contract(swapper_v5.pool2())
    vault_management = accounts.at(vault.management(), force=True)
    strategy.setBypasses(True, True, {"from": gov})
    strategy.setWeekEndHarvestTrigger(0, {"from": gov})
    assert strategy.maxSlippage() == 300
    assert not swapper_v5.otcEnabled()

    # Sell the migrated crvUSD in typical 1,000 crvUSD slices.
    sale = 1_000 * PRECISION
    strategy.setSwapThresholds(1, sale, False, {"from": gov})
    balance = reward_underlying.balanceOf(strategy)
    assert balance > 3 * sale
    strategy.harvest({"from": gov})
    assert balance - reward_underlying.balanceOf(strategy) == sale

    # Buying yCRV just before the harvest, as a sandwich would, moves the pool
    # while the EMA oracle lags, so the sale lands well below the oracle price.
    crv_holder = accounts.at(CRV_HOLDER, force=True)
    crv.approve(ycrv_pool, MAX_UINT, {"from": crv_holder})
    ycrv_pool.exchange(0, 1, 100_000 * PRECISION, 0, {"from": crv_holder})
    balance = reward_underlying.balanceOf(strategy)
    chain.sleep(1)
    chain.mine()
    with brownie.reverts("!slippage"):
        strategy.harvest({"from": gov})
    assert reward_underlying.balanceOf(strategy) == balance

    # Vault management can widen the tolerance to let the sale through.
    strategy.setMaxSlippage(10_000, {"from": vault_management})
    strategy.harvest({"from": gov})
    assert balance - reward_underlying.balanceOf(strategy) == sale


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


def test_weekly_harvests_follow_otc_toggle(
    swapper_v5,
    deposit_rewards,
    chain,
    gov,
    strategy,
    management,
):
    swapper = swapper_v5
    token_out = Contract(swapper.tokenOut())
    treasury_vault = Contract(swapper.vault())

    _settle_strategy(swapper, strategy, chain, gov)

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

    # The live yCRV inventory covers the sale.
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


def test_weekly_harvest_redeems_parent_vault_share_inventory(
    swapper_v5,
    vault,
    deposit_rewards,
    chain,
    gov,
    strategy,
    management,
    token,
):
    swapper = swapper_v5
    treasury_vault = Contract(swapper.vault())

    _settle_strategy(swapper, strategy, chain, gov)

    # Move the loose yCRV out, so the sale must redeem live parent-vault shares.
    swapper.sweep(token, {"from": management})
    assert token.balanceOf(swapper) == 0
    assert vault.balanceOf(swapper) > 0

    tx = swapper.enableOtc(True, {"from": management})
    _assert_otc_enabled_event(tx, True)
    assert swapper.otcEnabled()

    shares_before = vault.balanceOf(swapper)
    token_out_before = token.balanceOf(swapper)
    treasury_shares_before = treasury_vault.balanceOf(swapper.treasury())

    tx = _harvest_week(strategy, deposit_rewards, chain, gov)
    event = _assert_otc_event(tx)

    # The quote is redeemed as shares, which are worth more than 1 yCRV each,
    # so the swapper keeps the surplus yCRV as inventory.
    assert vault.balanceOf(swapper) == shares_before - event["buyTokenAmount"]
    assert token.balanceOf(swapper) > token_out_before
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
