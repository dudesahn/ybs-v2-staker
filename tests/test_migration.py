import brownie
import pytest
from brownie import ZERO_ADDRESS


WEEK = 7 * 24 * 60 * 60


def test_migration_preserves_near_max_boost(
    chain,
    strategy,
    utils,
    ybs,
    gov,
    user,
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
    assert abs(projected_boost - expected_projected_boost) <= 10
    assert abs(projected_boost - live_strategy_active_boost) <= 5 * 10**15
    assert (
        abs(max_weighted_stake * 10**18 // staked_before - migration_max_weight_share)
        <= 1
    )

    loose_before = strategy.balanceOfWant()

    # Only Vault managers may configure the migrated position, exactly 100%
    # is intentionally rejected, and an existing YBS position cannot be reset.
    with brownie.reverts():
        strategy.manualStakeAsMaxWeighted(
            migration_max_weight_share,
            {"from": user},
        )
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
    interface,
    reward_token,
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

    assert token.balanceOf(user) == user_balance_before_deposit - amount
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

    reward_underlying = interface.IERC20(strategy.rewardTokenUnderlying())
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

    # Parent-vault shares can be held between fee reports. Stage a separate
    # tranche so migration must redeem that balance without reducing the user's
    # original deposit shares used by the withdrawal assertion below.
    parent_share_assets = 100 * 10 ** token.decimals()
    fund_ycrv(user, parent_share_assets)
    token.approve(vault, parent_share_assets, {"from": user})
    user_vault_shares_before = vault.balanceOf(user)
    vault.deposit(parent_share_assets, {"from": user})
    parent_shares_to_stage = vault.balanceOf(user) - user_vault_shares_before
    assert parent_shares_to_stage > 0
    vault.transfer(strategy, parent_shares_to_stage, {"from": user})
    old_vault_shares = vault.balanceOf(strategy)
    assert old_vault_shares >= parent_shares_to_stage

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
    expected_parent_assets = (
        old_vault_shares
        * vault_assets_before_migration
        // vault_supply_before_migration
    )

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
    # Redeeming self-held shares removes their gross claim from total assets.
    # If idle is insufficient, the Vault may also reduce other strategy debt
    # while sourcing liquidity, but it must never increase aggregate debt.
    assert vault.totalDebt() <= vault_debt_before_migration
    assert vault.debtRatio() == vault_debt_ratio_before_migration
    assert vault.totalSupply() == vault_supply_before_migration - old_vault_shares
    assert vault.totalAssets() == vault_assets_before_migration - expected_parent_assets

    # Every persistent balance is transferred and the old strategy is fully
    # drained. Want that was staked is unstaked directly to the replacement.
    assert strategy.balanceOfWant() == 0
    assert strategy.balanceOfStaked() == 0
    assert strategy.balanceOfReward() == 0
    assert reward_underlying.balanceOf(strategy) == 0
    assert vault.balanceOf(strategy) == 0
    assert vault.balanceOf(new_strategy) == 0
    redeemed_parent_assets = (
        new_strategy.balanceOfWant() - old_want - old_staked
    )
    assert redeemed_parent_assets > 0
    assert redeemed_parent_assets <= expected_parent_assets
    assert (
        expected_parent_assets - redeemed_parent_assets
        <= expected_parent_assets // 10_000
    )
    assert (
        abs(
            new_strategy.balanceOfWant()
            - (old_want + old_staked + redeemed_parent_assets)
        )
        <= 1
    )
    assert new_strategy.balanceOfReward() == old_reward
    assert reward_underlying.balanceOf(new_strategy) == old_reward_underlying
    assert (
        pytest.approx(new_strategy.estimatedTotalAssets(), rel=RELATIVE_APPROX)
        == strategy_assets_before_migration + redeemed_parent_assets
    )

    withdrawal_queue = [vault.withdrawalQueue(i) for i in range(20)]
    assert new_strategy.address in withdrawal_queue
    assert strategy.address not in withdrawal_queue

    # The migrated position remains liquid through the replacement strategy.
    user_shares = vault.balanceOf(user) - user_shares_before_deposit
    user_balance_before_withdraw = token.balanceOf(user)
    vault.withdraw(user_shares, user, {"from": user})
    recovered = token.balanceOf(user) - user_balance_before_withdraw
    tolerated_loss = max(2, int(amount * RELATIVE_APPROX))
    assert recovered >= amount - tolerated_loss


def test_migration_reverts_if_parent_shares_cannot_be_fully_redeemed(
    token,
    vault,
    strategy,
    amount,
    Strategy,
    strategist,
    gov,
    user,
    ybs,
    reward_distributor,
    swapper_v5,
):
    # This test deliberately leaves the Vault unable to redeem the shares held
    # by the strategy during migration. Because the migrating strategy's debt
    # is set to zero before prepareMigration and it is the only queued strategy,
    # its own position cannot be used to satisfy the nested withdrawal.
    assert not strategy.feeModeActive()
    withdrawal_queue = [vault.withdrawalQueue(i) for i in range(20)]
    active_queue = [address for address in withdrawal_queue if address != ZERO_ADDRESS]
    assert active_queue == [strategy.address]

    token.approve(vault, amount, {"from": user})
    user_shares_before = vault.balanceOf(user)
    vault.deposit(amount, {"from": user})
    staged_shares = vault.balanceOf(user) - user_shares_before
    assert staged_shares > 0
    vault.transfer(strategy, staged_shares, {"from": user})

    # Invest the deposit so it is debt of the strategy rather than idle funds
    # that the nested Vault withdrawal can use.
    strategy.harvest({"from": gov})
    strategy_parent_shares = vault.balanceOf(strategy)
    assert strategy_parent_shares >= staged_shares
    parent_share_value = (
        strategy_parent_shares * vault.totalAssets() // vault.totalSupply()
    )
    accounted_idle = vault.totalAssets() - vault.totalDebt()
    assert accounted_idle < parent_share_value

    new_strategy = strategist.deploy(
        Strategy, vault, ybs, reward_distributor, swapper_v5
    )
    params_before = vault.strategies(strategy)
    assets_before = vault.totalAssets()
    debt_before = vault.totalDebt()
    supply_before = vault.totalSupply()
    staked_before = strategy.balanceOfStaked()

    with brownie.reverts("!vault shares"):
        vault.migrateStrategy(strategy, new_strategy, {"from": gov})

    # The strict redemption check makes the entire migration atomic: neither
    # strategy balances nor Vault accounting/queue state may be partially moved.
    params_after = vault.strategies(strategy)
    assert params_after["debtRatio"] == params_before["debtRatio"]
    assert params_after["totalDebt"] == params_before["totalDebt"]
    assert vault.strategies(new_strategy)["activation"] == 0
    assert vault.balanceOf(strategy) == strategy_parent_shares
    assert vault.balanceOf(new_strategy) == 0
    assert strategy.balanceOfStaked() == staked_before
    assert new_strategy.balanceOfStaked() == 0
    assert vault.totalAssets() == assets_before
    assert vault.totalDebt() == debt_before
    assert vault.totalSupply() == supply_before
    assert vault.withdrawalQueue(0) == strategy.address
