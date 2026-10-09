import brownie
import pytest


WEEK = 7 * 24 * 60 * 60


def test_migration_preserves_near_max_boost(
    chain,
    strategy,
    utils,
    ybs,
    gov,
    live_strategy_active_boost,
    migration_max_weight_share,
):
    max_boost = 10**18 * (utils.MAX_STAKE_GROWTH_WEEKS() + 1) // 2
    expected_projected_boost = migration_max_weight_share * max_boost // 10**18
    projected_boost = utils.getUserProjectedBoostMultiplier(strategy)
    staked_before = strategy.balanceOfStaked()
    stake_week = utils.getWeek()
    max_weighted_stake = ybs.accountWeeklyMaxStake(strategy, stake_week)

    assert live_strategy_active_boost > 0
    assert ybs.approvedWeightedStaker(strategy)
    assert utils.getUserActiveBoostMultiplier(strategy) == 0
    # Check the configured target directly, independent of the live position's boost.
    assert abs(projected_boost - expected_projected_boost) <= 10
    assert (
        abs(max_weighted_stake * 10**18 // staked_before - migration_max_weight_share)
        <= 1
    )

    loose_before = strategy.balanceOfWant()

    # Exactly 100% is intentionally rejected, and an existing YBS position
    # cannot be reset.
    with brownie.reverts("!percentage"):
        strategy.manualStakeAsMaxWeighted(10**18, {"from": gov})
    with brownie.reverts("!empty"):
        strategy.manualStakeAsMaxWeighted(
            migration_max_weight_share,
            {"from": gov},
        )

    assert strategy.balanceOfStaked() == staked_before
    assert strategy.balanceOfWant() == loose_before

    # The projected boost for the migration week becomes active next week.
    chain.sleep(WEEK)
    chain.mine()
    assert abs(utils.getUserActiveBoostMultiplier(strategy) - projected_boost) <= 10
    assert strategy.balanceOfStaked() == staked_before


def test_migration_stake_requires_vault_manager(
    Strategy,
    strategist,
    vault,
    ybs,
    reward_distributor,
    swapper_v5,
    gov,
    user,
    fund_ycrv,
    migration_max_weight_share,
):
    replacement = strategist.deploy(
        Strategy, vault, ybs, reward_distributor, swapper_v5
    )
    ybs.setWeightedStaker(replacement, True, {"from": gov})
    amount = 1_000 * 10**18
    fund_ycrv(replacement, amount)
    assert ybs.approvedWeightedStaker(replacement)
    assert replacement.balanceOfStaked() == 0
    assert replacement.balanceOfWant() == amount

    # The position is empty, approved and funded: authorization must be the
    # only reason this call fails.
    with brownie.reverts():
        replacement.manualStakeAsMaxWeighted(migration_max_weight_share, {"from": user})
    assert replacement.balanceOfStaked() == 0
    assert replacement.balanceOfWant() == amount

    replacement.manualStakeAsMaxWeighted(migration_max_weight_share, {"from": gov})
    assert replacement.balanceOfStaked() == amount
    assert replacement.balanceOfWant() == 0


def test_migration(
    chain,
    token,
    vault,
    strategy,
    amount,
    Strategy,
    strategist,
    gov,
    user,
    RELATIVE_APPROX,
    reward_distributor,
    ybs,
    swapper_v5,
    reward_token,
    reward_underlying,
    crvusd_whale,
    fund_ycrv,
):
    # The fixture has already migrated the live strategy, so measure the user's
    # deposit on top of the strategy's existing mainnet debt and assets.
    vault_assets_before_deposit = vault.totalAssets()
    strategy_debt_before_deposit = vault.strategies(strategy)["totalDebt"]
    user_balance_before_deposit = token.balanceOf(user)
    user_shares_before_deposit = vault.balanceOf(user)

    token.approve(vault.address, amount, {"from": user})
    vault.deposit(amount, {"from": user})
    operation_shares = vault.balanceOf(user) - user_shares_before_deposit

    assert token.balanceOf(user) == user_balance_before_deposit - amount
    assert operation_shares > 0
    assert vault.totalAssets() == vault_assets_before_deposit + amount

    chain.sleep(1)
    chain.mine(1)
    strategy.harvest()

    strategy_params_before_migration = vault.strategies(strategy)
    strategy_assets_before_migration = strategy.estimatedTotalAssets()
    strategy_debt_before_migration = strategy_params_before_migration["totalDebt"]

    assert strategy_debt_before_migration >= strategy_debt_before_deposit + amount
    assert (
        pytest.approx(strategy_assets_before_migration, rel=RELATIVE_APPROX)
        == strategy_debt_before_migration
    )

    # Stage both forms of unclaimed rewards so prepareMigration has to move
    # more than just the staked want position.
    reward_shares_to_stage = 10 * 10**18
    assert reward_token.balanceOf(user) >= 2 * reward_shares_to_stage
    reward_token.transfer(strategy, reward_shares_to_stage, {"from": user})

    assert reward_underlying.address == strategy.rewardTokenUnderlying()
    underlying_before = reward_underlying.balanceOf(user)
    reward_token.redeem(
        reward_shares_to_stage,
        user,
        user,
        {"from": user},
    )
    underlying_to_stage = reward_underlying.balanceOf(user) - underlying_before
    assert underlying_to_stage > 0
    reward_underlying.transfer(strategy, underlying_to_stage, {"from": user})

    old_want = strategy.balanceOfWant()
    old_staked = strategy.balanceOfStaked()
    old_reward = strategy.balanceOfReward()
    old_reward_underlying = reward_underlying.balanceOf(strategy)
    assert old_reward >= reward_shares_to_stage
    assert old_reward_underlying >= underlying_to_stage

    # Stray parent-vault shares stay with the old strategy. Stage a separate
    # tranche to preserve the user's original withdrawal shares.
    parent_share_assets = 100 * 10 ** token.decimals()
    assert vault.balanceOf(strategy) == 0
    fund_ycrv(user, parent_share_assets)
    token.approve(vault, parent_share_assets, {"from": user})
    user_vault_shares_before = vault.balanceOf(user)
    vault.deposit(parent_share_assets, {"from": user})
    parent_shares_to_stage = vault.balanceOf(user) - user_vault_shares_before
    assert parent_shares_to_stage > 0
    vault.transfer(strategy, parent_shares_to_stage, {"from": user})
    old_vault_shares = vault.balanceOf(strategy)
    assert old_vault_shares == parent_shares_to_stage

    # The replacement must use the current SwapperV5 integration and be empty
    # before the Vault entrusts it with the old strategy's accounting record.
    new_strategy = strategist.deploy(
        Strategy, vault, ybs, reward_distributor, swapper_v5
    )
    assert new_strategy.swapper() == swapper_v5.address
    assert new_strategy.estimatedTotalAssets() == 0

    vault_assets_before_migration = vault.totalAssets()
    vault_debt_before_migration = vault.totalDebt()
    vault_debt_ratio_before_migration = vault.debtRatio()
    vault_supply_before_migration = vault.totalSupply()

    vault.migrateStrategy(strategy, new_strategy, {"from": gov})

    old_params = vault.strategies(strategy)
    new_params = vault.strategies(new_strategy)

    # Migration moves the debt record and revokes the old strategy.
    assert old_params["debtRatio"] == 0
    assert old_params["totalDebt"] == 0
    assert new_params["debtRatio"] == strategy_params_before_migration["debtRatio"]
    assert new_params["totalDebt"] == strategy_debt_before_migration
    assert (
        new_params["performanceFee"]
        == strategy_params_before_migration["performanceFee"]
    )
    assert new_params["totalGain"] == 0
    assert new_params["totalLoss"] == 0
    # Migration leaves the Vault's aggregate accounting and supply unchanged.
    assert vault.totalDebt() == vault_debt_before_migration
    assert vault.debtRatio() == vault_debt_ratio_before_migration
    assert vault.totalSupply() == vault_supply_before_migration
    assert vault.totalAssets() == vault_assets_before_migration

    # Want and both reward balances move to the replacement. Want that was
    # staked is unstaked directly to the replacement.
    assert strategy.balanceOfWant() == 0
    assert strategy.balanceOfStaked() == 0
    assert strategy.balanceOfReward() == 0
    assert reward_underlying.balanceOf(strategy) == 0
    assert vault.balanceOf(strategy) == old_vault_shares
    assert vault.balanceOf(new_strategy) == 0
    assert new_strategy.balanceOfStaked() == 0
    assert abs(new_strategy.balanceOfWant() - (old_want + old_staked)) <= 1
    assert new_strategy.balanceOfReward() == old_reward
    assert reward_underlying.balanceOf(new_strategy) == old_reward_underlying
    assert (
        pytest.approx(new_strategy.estimatedTotalAssets(), rel=RELATIVE_APPROX)
        == strategy_assets_before_migration
    )

    withdrawal_queue = [vault.withdrawalQueue(i) for i in range(20)]
    assert new_strategy.address in withdrawal_queue
    assert strategy.address not in withdrawal_queue

    # The migrated position remains liquid through the replacement strategy.
    user_balance_before_withdraw = token.balanceOf(user)
    vault.withdraw(operation_shares, user, {"from": user})
    recovered = token.balanceOf(user) - user_balance_before_withdraw
    tolerated_loss = max(2, int(amount * RELATIVE_APPROX))
    assert recovered >= amount - tolerated_loss

    # Governance can recover them from the old strategy through the inherited
    # rewards allowance.
    strategy.setRewards(gov, {"from": gov})
    gov_shares_before = vault.balanceOf(gov)
    vault.transferFrom(strategy, gov, old_vault_shares, {"from": gov})
    assert vault.balanceOf(strategy) == 0
    assert vault.balanceOf(gov) - gov_shares_before == old_vault_shares
