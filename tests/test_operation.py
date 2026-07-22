import brownie
from brownie import Contract, web3
from eth_abi import decode
import pytest

YCRV_ZAP = "0x78ada385b15D89a9B845D2Cac0698663F0c69e3C"
YCRV_POOL = "0x99f5aCc8EC2Da2BC0771c32814EFF52b712de1E5"
YCRV_SWAP_TOPIC = web3.keccak(
    text="TokenExchange(address,int128,uint256,int128,uint256)"
).hex()


def _events_for(tx, name):
    if name not in tx.events:
        return []
    events = tx.events[name]
    return events if isinstance(events, (list, tuple)) else [events]


def _assert_reward_conversion(tx, require_swap=False):
    mint_events = [
        event
        for event in _events_for(tx, "Mint")
        if event["minter"].lower() == YCRV_ZAP.lower()
    ]
    ycrv_swap_logs = [
        log
        for log in tx.logs
        if log["address"].lower() == YCRV_POOL.lower()
        and log["topics"][0].hex() == YCRV_SWAP_TOPIC
        and log["topics"][1].hex()[-40:].lower() == YCRV_ZAP[2:].lower()
    ]
    has_ycrv_swap = bool(ycrv_swap_logs)

    if require_swap:
        assert not mint_events, "expected a yCRV swap, but the strategy minted yCRV"
        assert has_ycrv_swap, "expected the yCRV pool TokenExchange"
    else:
        assert (
            bool(mint_events) != has_ycrv_swap
        ), "expected exactly one yCRV conversion route (mint or swap)"

    if mint_events:
        assert len(mint_events) == 1
        assert mint_events[0]["value"] > 0
        print("🏦 Just minted", mint_events[0]["value"] / 1e18, "yCRV\n")
        return

    assert len(ycrv_swap_logs) == 1
    sold_id, tokens_sold, bought_id, tokens_bought = decode(
        ["int128", "uint256", "int128", "uint256"],
        ycrv_swap_logs[0]["data"],
    )
    assert sold_id == 0
    assert bought_id == 1
    assert tokens_sold > 0
    assert tokens_bought > 0
    print(
        "🤑 Just swapped",
        tokens_sold / 1e18,
        "CRV for",
        tokens_bought / 1e18,
        "yCRV\n",
    )


def test_operation(
    chain,
    accounts,
    token,
    gov,
    vault,
    reward_distributor,
    strategy,
    user,
    utils,
    amount,
    RELATIVE_APPROX,
    deposit_rewards,
    fund_ycrv,
):
    # do a harvest to get all of our loose vault funds into the strategy (assuming no more profitable harvests left)
    assert vault.strategies(strategy)["debtRatio"] == 10_000
    strategy.harvest({"from": gov})

    # deposit rewards and have user deposit
    deposit_rewards()
    user_balance_before = token.balanceOf(user)
    token.approve(vault.address, amount, {"from": user})
    vault.deposit(amount, {"from": user})
    # assert token.balanceOf(vault.address) == amount

    # Sleep to the next week to be able to claim rewards
    chain.sleep(60 * 60 * 24 * 7)
    chain.mine()

    deposit_rewards()
    # if it's our first week, then push the rewards and sleep again
    if utils.getGlobalActiveBoostMultiplier() == 0:
        reward_distributor.pushRewards(utils.getWeek() - 1, {"from": gov})
        chain.sleep(60 * 60 * 24 * 7)
        chain.mine()
        assert utils.getGlobalActiveBoostMultiplier() > 0

    # now our strategy should have some claimable rewards
    assert reward_distributor.getClaimable(strategy) > 0
    strategy.setMinReportDelay(4 * 7 * 24 * 60 * 60, {"from": gov})
    strategy.setCreditThreshold(2**256 - 1, {"from": gov})
    assert strategy.harvestTrigger(0)

    # reduce debt on our strategy
    vault.updateStrategyDebtRatio(strategy, 5_000, {"from": gov})
    tx = strategy.harvest()

    # check how much we should be selling on each swap
    print("Amount to sell on each swap max:", strategy.swapThresholds()["max"] / 1e18)
    remaining = Contract(strategy.rewardTokenUnderlying()).balanceOf(strategy) / 1e18
    print("Amount of rewards in strategy after one swap:", remaining)
    print("Remaining split 6 ways:", remaining / 6)

    # Depending on the live fork price, the strategy should explicitly take
    # exactly one of the supported yCRV conversion routes.
    _assert_reward_conversion(tx)
    assert (
        vault.totalAssets() * 0.51
        > strategy.estimatedTotalAssets()
        > vault.totalAssets() * 0.49
    )

    # put it back up to 100%
    vault.updateStrategyDebtRatio(strategy, 10_000, {"from": gov})
    chain.sleep(1)
    tx = strategy.harvest()
    _assert_reward_conversion(tx)

    # have a whale swap in a 500k yCRV
    whale = accounts.at("0x71E47a4429d35827e0312AA13162197C23287546", force=True)
    pool = Contract("0x99f5aCc8EC2Da2BC0771c32814EFF52b712de1E5")
    swap_amount = 500_000 * 10 ** token.decimals()
    fund_ycrv(whale)
    assert token.balanceOf(whale) >= swap_amount
    token.approve(pool, 2**256 - 1, {"from": whale})
    pool.exchange(1, 0, swap_amount, 0, {"from": whale})

    # now we should swap instead of minting
    chain.sleep(1)
    tx = strategy.harvest()
    _assert_reward_conversion(tx, require_swap=True)

    # vault will have profit from our harvest sitting in it
    assert (
        pytest.approx(
            strategy.estimatedTotalAssets() + tx.events["Harvested"]["profit"],
            rel=RELATIVE_APPROX,
        )
        == vault.totalAssets()
    )

    # withdrawal
    vault.withdraw({"from": user})
    assert token.balanceOf(user) > user_balance_before


def test_sweep(gov, vault, strategy, token, user, amount, weth, weth_amount):
    # Strategy want token doesn't work
    token.transfer(strategy, amount, {"from": user})
    assert token.address == strategy.want()
    assert token.balanceOf(strategy) > 0
    with brownie.reverts("!want"):
        strategy.sweep(token, {"from": gov})

    # Vault share token doesn't work
    with brownie.reverts("!shares"):
        strategy.sweep(vault.address, {"from": gov})

    # Neither the reward vault shares nor their underlying may be swept.
    reward_token = strategy.rewardToken()
    reward_token_underlying = strategy.rewardTokenUnderlying()
    assert reward_token != reward_token_underlying
    with brownie.reverts("!protected"):
        strategy.sweep(reward_token, {"from": gov})
    with brownie.reverts("!protected"):
        strategy.sweep(reward_token_underlying, {"from": gov})

    before_balance = weth.balanceOf(gov)
    weth.transfer(strategy, weth_amount, {"from": user})
    assert weth.address != strategy.want()
    assert weth.balanceOf(user) == 0
    with brownie.reverts():
        strategy.sweep(weth, {"from": user})
    assert weth.balanceOf(strategy) == weth_amount

    strategy.sweep(weth, {"from": gov})
    assert weth.balanceOf(gov) == weth_amount + before_balance
