import brownie
import pytest


def test_vault_shutdown_can_withdraw(
    chain,
    token,
    vault,
    strategy,
    user,
    gov,
    amount,
    RELATIVE_APPROX,
    fund_ycrv,
):
    vault_assets_before_deposit = vault.totalAssets()
    strategy_debt_before_deposit = vault.strategies(strategy)["totalDebt"]
    user_balance_before_deposit = token.balanceOf(user)
    user_shares_before_deposit = vault.balanceOf(user)

    token.approve(vault.address, amount, {"from": user})
    vault.deposit(amount, {"from": user})

    assert token.balanceOf(user) == user_balance_before_deposit - amount
    assert vault.balanceOf(user) > user_shares_before_deposit
    assert vault.totalAssets() == vault_assets_before_deposit + amount

    chain.sleep(1)
    chain.mine(1)
    strategy.harvest()

    params_after_harvest = vault.strategies(strategy)
    assert params_after_harvest["totalDebt"] >= strategy_debt_before_deposit + amount

    vault.setEmergencyShutdown(True, {"from": gov})

    assert vault.emergencyShutdown()
    assert vault.creditAvailable(strategy.address) == 0
    assert vault.debtOutstanding(strategy.address) == params_after_harvest["totalDebt"]

    # Shutdown must block new capital while leaving share redemptions open.
    fund_ycrv(user, 1)
    assert token.balanceOf(user) >= 1
    with brownie.reverts():
        vault.deposit(1, {"from": user})

    user_shares = vault.balanceOf(user) - user_shares_before_deposit
    user_balance_before_withdraw = token.balanceOf(user)
    vault_assets_before_withdraw = vault.totalAssets()
    strategy_debt_before_withdraw = vault.strategies(strategy)["totalDebt"]

    tx = vault.withdraw(user_shares, user, {"from": user})
    amount_withdrawn = token.balanceOf(user) - user_balance_before_withdraw

    assert amount_withdrawn == tx.return_value
    assert pytest.approx(amount_withdrawn, rel=RELATIVE_APPROX) == amount
    assert vault.balanceOf(user) == user_shares_before_deposit
    assert vault.strategies(strategy)["totalDebt"] <= strategy_debt_before_withdraw
    assert vault.totalAssets() <= vault_assets_before_withdraw - amount_withdrawn


def test_basic_shutdown(
    chain, token, vault, strategy, user, gov, amount, RELATIVE_APPROX
):
    vault_assets_before_deposit = vault.totalAssets()
    strategy_debt_before_deposit = vault.strategies(strategy)["totalDebt"]

    token.approve(vault.address, amount, {"from": user})
    vault.deposit(amount, {"from": user})
    assert vault.totalAssets() == vault_assets_before_deposit + amount

    chain.sleep(1)
    chain.mine(1)
    strategy.harvest()

    params_before_shutdown = vault.strategies(strategy)
    strategy_assets_before_shutdown = strategy.estimatedTotalAssets()
    vault_balance_before_shutdown = token.balanceOf(vault)
    vault_total_debt_before_shutdown = vault.totalDebt()
    vault_total_assets_before_shutdown = vault.totalAssets()
    vault_debt_ratio_before_shutdown = vault.debtRatio()

    assert params_before_shutdown["totalDebt"] >= strategy_debt_before_deposit + amount

    vault.setEmergencyShutdown(True, {"from": gov})

    assert vault.emergencyShutdown()
    assert vault.creditAvailable(strategy.address) == 0
    assert (
        vault.debtOutstanding(strategy.address) == params_before_shutdown["totalDebt"]
    )

    # A harvest during Vault shutdown must repay all debt and leave the full
    # position idle in the Vault, without silently changing strategy limits.
    chain.sleep(1)
    chain.mine(1)
    strategy.harvest({"from": gov})

    params_after_shutdown = vault.strategies(strategy)
    assert params_after_shutdown["debtRatio"] == params_before_shutdown["debtRatio"]
    assert params_after_shutdown["totalDebt"] == 0
    assert vault.debtRatio() == vault_debt_ratio_before_shutdown
    assert vault.totalDebt() == (
        vault_total_debt_before_shutdown - params_before_shutdown["totalDebt"]
    )
    assert vault.debtOutstanding(strategy.address) == 0
    assert strategy.balanceOfStaked() <= 1
    assert strategy.estimatedTotalAssets() <= 1
    assert (
        token.balanceOf(vault) - vault_balance_before_shutdown
        >= strategy_assets_before_shutdown - 2
    )
    accounted_idle = vault.totalAssets() - vault.totalDebt()
    assert token.balanceOf(vault) >= accounted_idle
    tolerated_loss = max(2, int(vault_total_assets_before_shutdown * RELATIVE_APPROX))
    assert vault.totalAssets() >= vault_total_assets_before_shutdown - tolerated_loss
