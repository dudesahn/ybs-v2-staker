import brownie
import pytest

from scripts import deploy
from scripts.contract_helpers import deployed_contract


def test_deploy_and_migrate_with_explicit_replacement_swapper(
    chain,
    strategist,
    management,
    gov,
    user,
    vault,
    old_strategy,
    token,
    reward_token,
    reward_underlying,
    ybs,
    crvusd_whale,
):
    legacy = deployed_contract(
        deploy.LEGACY_SWAPPER_V5,
        "pool1",
        "pool2",
        "tokenOutPool1",
        "pool1InTokenIdx",
        "pool1OutTokenIdx",
    )
    swapper = deploy.deploy_swapper(
        management, deployer=strategist, publish_source=False
    )

    assert swapper.address != legacy.address
    assert swapper.tokenIn() == reward_underlying.address
    assert swapper.tokenOut() == token.address
    assert swapper.pool1() == legacy.pool1()
    assert swapper.pool2() == legacy.pool2()
    assert swapper.tokenOutPool1() == legacy.tokenOutPool1()
    assert swapper.pool1InTokenIdx() == legacy.pool1InTokenIdx()
    assert swapper.pool1OutTokenIdx() == legacy.pool1OutTokenIdx()
    assert swapper.approvedVault() == vault.address
    assert swapper.vault() == reward_token.address
    assert swapper.owner() == gov.address
    assert swapper.management() == management.address
    assert not swapper.otcEnabled()
    assert token.balanceOf(swapper) == 0
    assert vault.balanceOf(swapper) == 0
    assert not swapper.operator(strategist)
    assert not swapper.operator(user)

    # Deploying the swapper grants no ongoing administrative rights to its deployer.
    with brownie.reverts("!operator"):
        swapper.enableOtc(True, {"from": strategist})
    with brownie.reverts("!ownerOrManagement"):
        swapper.setOperator(strategist, True, {"from": strategist})
    with brownie.reverts("!owner"):
        swapper.setManagement(strategist, {"from": management})

    strategy = deploy.main(swapper, publish_source=False, deployer=strategist)
    assert strategy.swapper() == swapper.address
    assert strategy.keeper() == old_strategy.keeper()
    assert strategy.strategist() == old_strategy.strategist()
    assert strategy.minReportDelay() == old_strategy.minReportDelay()
    assert strategy.maxReportDelay() == old_strategy.maxReportDelay()
    assert strategy.rewards() == strategy.address
    assert not strategy.feeModeActive()
    assert vault.strategies(strategy)["activation"] == 0
    assert not swapper.allowedSwapper(strategy)
    assert reward_underlying.allowance(strategy, swapper) == 2**256 - 1

    # A mismatched replacement selection must fail before any migration setup.
    debt_before_setup = vault.totalDebt()
    with pytest.raises(AssertionError, match="unexpected staged swapper"):
        deploy.setup(strategy, legacy.address, sender=gov)
    assert vault.strategies(strategy)["activation"] == 0
    assert vault.totalDebt() == debt_before_setup
    assert not ybs.approvedWeightedStaker(strategy)

    ratio_before_setup = vault.debtRatio()
    params_before_setup = vault.strategies(old_strategy)
    old_swapper_before_setup = old_strategy.swapper()
    result = deploy.setup(strategy, swapper, sender=gov)

    assert result.address == strategy.address
    assert old_strategy.swapper() == old_swapper_before_setup
    assert vault.strategies(old_strategy)["totalDebt"] == 0
    assert vault.strategies(old_strategy)["debtRatio"] == 0
    params_after_setup = vault.strategies(strategy)
    assert params_after_setup["totalDebt"] > 0
    assert params_after_setup["debtRatio"] == params_before_setup["debtRatio"]
    assert params_after_setup["performanceFee"] == params_before_setup["performanceFee"]
    assert vault.debtRatio() == ratio_before_setup
    assert vault.withdrawalQueue(0) == strategy.address
    assert strategy.creditThreshold() == deploy.CREDIT_THRESHOLD
    assert strategy.baseFeeOracle() == old_strategy.baseFeeOracle()
    assert ybs.approvedWeightedStaker(strategy)
    assert strategy.balanceOfStaked() > 0
    assert strategy.balanceOfWant() <= 1
    assert vault.rewards() == strategy.address
    assert strategy.feeModeActive()

    # The migrated strategy is automatically allowed to OTC by its Vault
    # registration. Empty new reserves must fall back to the market route.
    swapper.enableOtc(True, {"from": management})
    assert not swapper.allowedSwapper(strategy)
    strategy.setBypasses(True, True, {"from": gov})
    strategy.setSwapThresholds(1, 1_000_000 * 10**18, False, {"from": gov})
    reward_token.transfer(strategy, 500 * 10**18, {"from": user})
    treasury_shares_before = reward_token.balanceOf(swapper.treasury())
    gain_before = vault.strategies(strategy)["totalGain"]

    chain.sleep(1)
    chain.mine()
    strategy.harvest({"from": gov})

    assert vault.strategies(strategy)["totalGain"] > gain_before
    assert strategy.balanceOfReward() == 0
    assert reward_underlying.balanceOf(swapper) == 0
    assert reward_token.balanceOf(swapper.treasury()) == treasury_shares_before
    assert vault.balanceOf(strategy) > 0
