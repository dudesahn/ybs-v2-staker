import brownie


def _deposit_and_harvest(chain, token, vault, strategy, amount, user):
    vault_assets_before = vault.totalAssets()
    strategy_debt_before = vault.strategies(strategy)["totalDebt"]

    token.approve(vault.address, amount, {"from": user})
    vault.deposit(amount, {"from": user})
    assert vault.totalAssets() == vault_assets_before + amount

    chain.sleep(1)
    chain.mine(1)
    strategy.harvest()

    strategy_debt_after = vault.strategies(strategy)["totalDebt"]
    assert strategy_debt_after >= strategy_debt_before + amount


def _assert_unwound(
    token,
    vault,
    strategy,
    strategy_assets_before,
    strategy_debt_before,
    vault_balance_before,
    vault_total_debt_before,
    vault_total_assets_before,
    RELATIVE_APPROX,
):
    params = vault.strategies(strategy)

    assert params["debtRatio"] == 0
    assert params["totalDebt"] == 0
    assert vault.totalDebt() == vault_total_debt_before - strategy_debt_before
    assert strategy.balanceOfStaked() <= 1
    assert strategy.estimatedTotalAssets() <= 1
    assert token.balanceOf(vault) - vault_balance_before >= strategy_assets_before - 2
    accounted_idle = vault.totalAssets() - vault.totalDebt()
    assert token.balanceOf(vault) >= accounted_idle
    tolerated_loss = max(2, int(vault_total_assets_before * RELATIVE_APPROX))
    assert vault.totalAssets() >= vault_total_assets_before - tolerated_loss


def test_revoke_strategy_from_vault(
    chain, token, vault, strategy, amount, user, gov, RELATIVE_APPROX
):
    _deposit_and_harvest(chain, token, vault, strategy, amount, user)

    params_before = vault.strategies(strategy)
    strategy_assets_before = strategy.estimatedTotalAssets()
    vault_balance_before = token.balanceOf(vault)
    vault_total_debt_before = vault.totalDebt()
    vault_total_assets_before = vault.totalAssets()
    vault_debt_ratio_before = vault.debtRatio()

    # Governance revocation immediately removes future credit, but the debt is
    # outstanding until the strategy reports and returns its position.
    vault.revokeStrategy(strategy.address, {"from": gov})
    revoked_params = vault.strategies(strategy)

    assert revoked_params["debtRatio"] == 0
    assert revoked_params["totalDebt"] == params_before["totalDebt"]
    assert vault.debtRatio() == vault_debt_ratio_before - params_before["debtRatio"]
    assert vault.debtOutstanding(strategy.address) == params_before["totalDebt"]
    assert vault.creditAvailable(strategy.address) == 0

    chain.sleep(1)
    chain.mine(1)
    strategy.harvest()

    _assert_unwound(
        token,
        vault,
        strategy,
        strategy_assets_before,
        params_before["totalDebt"],
        vault_balance_before,
        vault_total_debt_before,
        vault_total_assets_before,
        RELATIVE_APPROX,
    )


def test_revoke_strategy_from_strategy(
    chain,
    token,
    vault,
    strategy,
    amount,
    strategist,
    user,
    RELATIVE_APPROX,
):
    _deposit_and_harvest(chain, token, vault, strategy, amount, user)

    params_before = vault.strategies(strategy)
    strategy_assets_before = strategy.estimatedTotalAssets()
    vault_balance_before = token.balanceOf(vault)
    vault_total_debt_before = vault.totalDebt()
    vault_total_assets_before = vault.totalAssets()
    vault_debt_ratio_before = vault.debtRatio()

    # setEmergencyExit makes the strategy revoke itself through the Vault.
    with brownie.reverts():
        strategy.setEmergencyExit({"from": user})
    assert not strategy.emergencyExit()

    strategy.setEmergencyExit({"from": strategist})

    assert strategy.emergencyExit()
    assert vault.strategies(strategy)["debtRatio"] == 0
    assert vault.debtRatio() == vault_debt_ratio_before - params_before["debtRatio"]
    assert vault.debtOutstanding(strategy.address) == params_before["totalDebt"]

    chain.sleep(1)
    chain.mine(1)
    strategy.harvest({"from": strategist})

    _assert_unwound(
        token,
        vault,
        strategy,
        strategy_assets_before,
        params_before["totalDebt"],
        vault_balance_before,
        vault_total_debt_before,
        vault_total_assets_before,
        RELATIVE_APPROX,
    )
