import brownie
import pytest
from brownie import Contract, ZERO_ADDRESS, chain


DEPLOYED_STRATEGY = "0xe3974e44bc08f435da2c6db7d01e1758496da119"
MIGRATION_MAX_WEIGHT_SHARE = 998 * 10**15


@pytest.fixture
def gov(accounts):
    yield accounts.at("0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52", force=True)


@pytest.fixture
def user(accounts):
    yield accounts[0]


@pytest.fixture
def rewards(accounts):
    yield accounts[1]


@pytest.fixture
def guardian(accounts):
    yield accounts[2]


@pytest.fixture
def management(accounts):
    yield accounts[3]


@pytest.fixture
def strategist(accounts):
    yield accounts[4]


@pytest.fixture
def keeper(accounts):
    yield accounts[5]


@pytest.fixture
def token():
    token_address = "0xFCc5c47bE19d06BF83eB04298b026F81069ff65b"  # this should be the address of the ERC-20 used by the strategy/vault (yCRV)
    yield Contract(token_address)


@pytest.fixture
def fund_ycrv(accounts, token):
    holders = [
        accounts.at("0x1Effd55A8646F7Dc67C7578c20Ce575CefEB1120", force=True),
        accounts.at("0xC5240FD754F4C52A3Cb71AB8625FB8E810E6fBB7", force=True),
        accounts.at("0xEfb8B98A4BBd793317a863f1Ec9B92641aB1CBbb", force=True),
    ]

    def fund(recipient, amount=None):
        balances = [token.balanceOf(holder) for holder in holders]
        available = sum(balances)
        balance_before = token.balanceOf(recipient)

        if amount is None:
            for holder, balance in zip(holders, balances):
                if balance > 0:
                    token.transfer(recipient, balance, {"from": holder})
                    assert token.balanceOf(holder) == 0
            assert token.balanceOf(recipient) - balance_before == available
            return available

        if available < amount:
            raise ValueError(
                f"Unable to source {amount / 1e18:,.2f} yCRV; "
                f"holders only have {available / 1e18:,.2f} yCRV"
            )

        remaining = amount
        for holder, balance in zip(holders, balances):
            to_transfer = min(balance, remaining)
            if to_transfer > 0:
                token.transfer(recipient, to_transfer, {"from": holder})
                remaining -= to_transfer
            if remaining == 0:
                break

        assert remaining == 0
        assert token.balanceOf(recipient) - balance_before == amount
        return amount

    yield fund


@pytest.fixture
def amount(token, user, fund_ycrv):
    amount = 10_000 * 10 ** token.decimals()
    fund_ycrv(user, amount)
    yield amount


@pytest.fixture
def weth():
    token_address = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
    yield Contract(token_address)


@pytest.fixture
def weth_amount(user, weth):
    weth_amount = 10 ** weth.decimals()
    user.transfer(weth, weth_amount)
    yield weth_amount


@pytest.fixture
def reward_token():
    yield Contract("0xBF319dDC2Edc1Eb6FDf9910E39b37Be221C8805F")  # crvUSD v3 vault


@pytest.fixture
def vault(pm, gov, rewards, guardian, management, token):
    vault = Contract("0x27B5739e22ad9033bcBf192059122d163b60349D")
    # for i in range(0,20):
    #     s = vault.withdrawalQueue(i)
    #     if s == ZERO_ADDRESS:
    #         break
    #     vault.updateStrategyDebtRatio(s, 0, {'from': gov})
    #     s = Contract(s, owner=gov)
    #     s.harvest()
    yield vault


@pytest.fixture
def registry(gov, reward_token, token):
    registry = Contract("0x262be1d31d0754399d8d5dc63B99c22146E9f738")
    deployment = registry.deployments(token)
    if deployment["yearnBoostedStaker"] == ZERO_ADDRESS:
        registry.createNewDeployment(token, 4, 0, reward_token, {"from": gov})
    yield registry


@pytest.fixture
def ybs(registry, interface, token):
    deployment = registry.deployments(token)
    ybs = interface.IYearnBoostedStaker(deployment["yearnBoostedStaker"])
    yield ybs


@pytest.fixture
def reward_distributor(registry, interface, token):
    deployment = registry.deployments(token)
    reward_distributor = interface.IRewardDistributor(deployment["rewardDistributor"])
    yield reward_distributor


@pytest.fixture
def utils(registry, interface, token):
    deployment = registry.deployments(token)
    utils = interface.IYBSUtilities(deployment["utilities"])
    yield utils


@pytest.fixture
def swapper_v5(gov, token, SwapperV5, management):
    token_in = "0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E"  # crvUSD
    token_out = token
    token_out_pool1 = "0xD533a949740bb3306d119CC777fa900bA034cd52"
    pool1 = "0x4eBdF703948ddCEA3B11f675B4D1Fba9d2414A14"
    pool2 = "0x99f5acc8ec2da2bc0771c32814eff52b712de1e5"
    swapper = gov.deploy(
        SwapperV5, management, token_in, token_out, pool1, token_out_pool1, pool2
    )
    yield swapper


@pytest.fixture
def old_strategy(vault):
    assert vault.withdrawalQueue(0) == DEPLOYED_STRATEGY, (
        "the deployed strategy is no longer first in the withdrawal queue; "
        "update the fork baseline deliberately before changing this fixture"
    )
    old_strategy = Contract(DEPLOYED_STRATEGY)
    yield old_strategy


@pytest.fixture
def live_strategy_active_boost(old_strategy, utils):
    yield utils.getUserActiveBoostMultiplier(old_strategy)


@pytest.fixture
def strategy(
    strategist,
    keeper,
    vault,
    Strategy,
    gov,
    ybs,
    reward_distributor,
    old_strategy,
    token,
    utils,
    live_strategy_active_boost,
    swapper_v5,
):
    # deploy!
    strategy = strategist.deploy(Strategy, vault, ybs, reward_distributor, swapper_v5)
    strategy.setKeeper(keeper)

    # check and print starting boost of strategy
    print("Current active boost:", live_strategy_active_boost / 1e18)

    # realistically, we'll migrate first before we do anything else
    vault.migrateStrategy(old_strategy, strategy, {"from": gov})

    # make sure it's empty
    assert token.balanceOf(old_strategy) == 0 == old_strategy.estimatedTotalAssets()

    # make gov an approved staker
    with brownie.reverts("!approvedStaker"):
        strategy.manualStakeAsMaxWeighted(
            MIGRATION_MAX_WEIGHT_SHARE,
            {"from": gov},
        )
    ybs.setWeightedStaker(strategy, True, {"from": gov})

    # approve new strategy as a locker on proxy
    proxy = Contract("0x78eDcb307AC1d1F8F5Fd070B377A6e69C8dcFC34")
    proxy.approveLocker(strategy, True, {"from": gov})

    # Match the live strategy's near-max boost while retaining a small regular stake.
    strategy.manualStakeAsMaxWeighted(
        MIGRATION_MAX_WEIGHT_SHARE,
        {"from": gov},
    )
    chain.mine()
    chain.sleep(1)

    print(
        "Current new strategy active boost:",
        utils.getUserActiveBoostMultiplier(strategy) / 1e18,
    )
    print(
        "Current new strategy projected boost:",
        utils.getUserProjectedBoostMultiplier(strategy) / 1e18,
    )
    assert strategy.balanceOfWant() <= 1
    assert strategy.estimatedTotalAssets() > 0

    yield strategy


@pytest.fixture(scope="session")
def RELATIVE_APPROX():
    yield 1e-5


@pytest.fixture(scope="session")
def migration_max_weight_share():
    yield MIGRATION_MAX_WEIGHT_SHARE


# Function scoped isolation fixture to enable xdist.
# Snapshots the chain before each test and reverts after test completion.
@pytest.fixture(scope="function", autouse=True)
def shared_setup(fn_isolation):
    pass


@pytest.fixture
def crvusd_dummy_vault(reward_token, gov):
    factory = Contract("0x444045c5c13c246e117ed36437303cac8e250ab0")
    tx = factory.deploy_new_vault(
        reward_token.asset(),
        "dummy-crvusd",
        "dummy-crvusd",
        "0xb3bd6B2E61753C311EFbCF0111f75D29706D9a41",
        0,
        {"from": gov},
    )
    yield tx.return_value


@pytest.fixture
def crvusd_whale(accounts, user, reward_token):
    # In order to get some funds for the token you are about to use,
    # it impersonate an exchange address to use it's funds.
    amount = 100_000 * 10**18
    crvusd = Contract(reward_token.asset())
    reserve = accounts.at("0xA920De414eA4Ab66b97dA1bFE9e6EcA7d4219635", force=True)
    assert crvusd.balanceOf(reserve) >= amount
    crvusd.transfer(user, amount, {"from": reserve})
    crvusd.approve(reward_token, 2**256 - 1, {"from": user})
    shares_before = reward_token.balanceOf(user)
    reward_token.deposit(amount, user, {"from": user})
    assert reward_token.balanceOf(user) > shares_before
    yield amount


@pytest.fixture(scope="function")
def deposit_rewards(user, reward_token, reward_distributor, crvusd_whale):

    def deposit_rewards(user=user, reward_distributor=reward_distributor):
        reward_token.approve(reward_distributor, 2**256 - 1, {"from": user})

        amt = 5_000 * 10**18
        week = reward_distributor.getWeek()
        rewards_before = reward_distributor.weeklyRewardAmount(week)
        reward_distributor.depositReward(amt, {"from": user})
        assert reward_distributor.weeklyRewardAmount(week) == rewards_before + amt

    yield deposit_rewards
