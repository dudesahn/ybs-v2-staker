import brownie
from brownie import Contract, accounts, ZERO_ADDRESS
import pytest

WEEK = 60 * 60 * 24 * 7


def test_swapper(
    swapper_v5,
    vault,
    deposit_rewards,
    chain,
    gov,
    old_strategy,
    management,
    user,
    crvusd_dummy_vault,
    fund_ycrv,
):
    swapper = swapper_v5
    strategy = old_strategy
    old_swapper = Contract(strategy.swapper())
    price = 1e18 / swapper.priceOracle()  # yCRV price as crvUSD
    print("\n👀 Price:", price, "\n")
    assert price > 0.10 and price < 1.0
    tx = strategy.harvest({"from": gov})
    strategy.upgradeSwapper(swapper, {"from": gov})
    assert swapper.management() == management
    ycrv = Contract(vault.token())
    chain.sleep(3 * WEEK)
    chain.mine()

    v = swapper.vault()
    swapper.setVault(crvusd_dummy_vault, {"from": gov})
    swapper.setVault(v, {"from": gov})

    amounts = [10 * 10**18, 1_000 * 10**18, 100_000 * 10**18, 0]
    swapper.enableOtc(True, {"from": management})

    # test our operator role
    with brownie.reverts("!operator"):
        swapper.enableOtc(False, {"from": user})
    swapper.setOperator(user, True, {"from": management})
    swapper.enableOtc(False, {"from": user})
    assert not swapper.otcEnabled()
    swapper.enableOtc(True, {"from": user})
    assert swapper.otcEnabled()

    status = swapper.otcEnabled()

    for i in range(len(amounts)):
        fund_ycrv(swapper, amounts[i])

        deposit_rewards()

        chain.sleep(WEEK)
        chain.mine()

        status = swapper.otcEnabled()

        tx = strategy.harvest({"from": gov})

        if status:
            assert "OTC" in tx.events
            event = tx.events["OTC"]
            print("Sell amount", event["sellTokenAmount"] / 1e18)
            print("Buy amount", event["buyTokenAmount"] / 1e18)
            print(
                "Effective price:", event["sellTokenAmount"] / event["buyTokenAmount"]
            )
            bal = Contract(swapper.tokenOut()).balanceOf(swapper) / 1e18
            print(f"Remaining OTC balance {bal}\n")
        else:
            assert "OTC" not in tx.events


def test_swapper_withdraw(
    swapper_v5,
    vault,
    deposit_rewards,
    chain,
    gov,
    old_strategy,
    management,
    crvusd_dummy_vault,
    token,
    user,
    fund_ycrv,
):
    swapper = swapper_v5
    strategy = old_strategy
    old_swapper = Contract(strategy.swapper())
    price = 1e18 / swapper.priceOracle()  # yCRV price as crvUSD
    print("\n👀 Price:", price, "\n")
    assert price > 0.10 and price < 1.0
    tx = strategy.harvest({"from": gov})
    strategy.upgradeSwapper(swapper, {"from": gov})
    assert swapper.management() == management
    chain.sleep(3 * WEEK)
    chain.mine()

    v = swapper.vault()
    swapper.setVault(crvusd_dummy_vault, {"from": gov})
    swapper.setVault(v, {"from": gov})

    amounts = [10 * 10**18, 1_000 * 10**18, 100_000 * 10**18, 0]
    swapper.enableOtc(True, {"from": management})

    shares_needed = sum(amounts)
    assets_needed = (shares_needed * vault.pricePerShare()) // 10**18
    assets_needed = (assets_needed * 101) // 100
    fund_ycrv(user, assets_needed)
    token.approve(vault, assets_needed, {"from": user})
    vault.deposit(assets_needed, {"from": user})
    assert vault.balanceOf(user) >= shares_needed

    status = swapper.otcEnabled()

    for i in range(len(amounts)):
        vault.transfer(swapper, amounts[i], {"from": user})

        deposit_rewards()

        chain.sleep(WEEK)
        chain.mine()

        status = swapper.otcEnabled()

        tx = strategy.harvest({"from": gov})

        # first 2 shouldn't do any OTC
        vault_balance = vault.balanceOf(swapper)

        if status:
            assert "OTC" in tx.events
            event = tx.events["OTC"]
            print("Sell amount", event["sellTokenAmount"] / 1e18)
            print("Buy amount", event["buyTokenAmount"] / 1e18)
            print(
                "Effective price:", event["sellTokenAmount"] / event["buyTokenAmount"]
            )
            bal = Contract(swapper.tokenOut()).balanceOf(swapper) / 1e18
            vault_bal = vault.balanceOf(swapper) / 1e18
            print(f"Remaining yCRV balance {bal}")
            print(f"Remaining st-yCRV balance {vault_bal}\n")
        else:
            assert "OTC" not in tx.events
            print("⏭️ Skipped OTC for:", amounts[i] / 1e18, "st-yCRV donation")


def test_swapper_settings(swapper_v5, management, user):
    swapper = swapper_v5
    swapper.setAllowedSwapper(user, True, {"from": management})
    assert swapper.allowedSwapper(user)
    swapper.setAllowedSwapper(user, False, {"from": management})
    assert not swapper.allowedSwapper(user)

    with brownie.reverts():
        swapper.setAllowedSwapper(ZERO_ADDRESS, True, {"from": user})
