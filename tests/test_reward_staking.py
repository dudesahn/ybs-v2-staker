import pytest


@pytest.mark.parametrize("bypass_max_stake", [False, True])
def test_harvest_stakes_swap_proceeds_at_maximum_weight(
    strategy,
    swapper_v5,
    ybs,
    reward_underlying,
    funded_crvusd,
    user,
    gov,
    management,
    chain,
    bypass_max_stake,
):
    strategy.setBypasses(True, bypass_max_stake, {"from": gov})
    strategy.setSwapThresholds(0, 1_000_000 * 10**18, False, {"from": gov})
    strategy.setWeekEndHarvestTrigger(0, {"from": gov})
    strategy.harvest({"from": gov})
    assert ybs.approvedWeightedStaker(strategy)

    # The live inventory fills the whole sale, giving observable reward proceeds.
    sale = 100 * 10**18
    swapper_v5.enableOtc(True, {"from": management})
    reward_underlying.transfer(strategy, sale, {"from": user})
    chain.sleep(1)
    chain.mine()

    tx = strategy.harvest({"from": gov})

    trade = tx.events["OTC"]
    assert trade["sellTokenAmount"] == sale
    assert trade["buyTokenAmount"] > 1
    assert reward_underlying.balanceOf(strategy) == 0
    stakes = [
        event
        for event in (tx.events["Staked"] if "Staked" in tx.events else [])
        if event.address.lower() == ybs.address.lower()
        and event["account"] == strategy.address
    ]
    boosted_stakes = [
        event for event in stakes if event["weightAdded"] > event["amount"] // 2
    ]

    if bypass_max_stake:
        assert boosted_stakes == []
        for event in stakes:
            assert event["weightAdded"] == event["amount"] // 2
    else:
        assert len(boosted_stakes) == 1
        stake = boosted_stakes[0]
        # YBS rounds stakes down to an even amount. Check the stake event itself:
        # the harvest subsequently unstakes funds to report profit to the vault.
        expected_amount = trade["buyTokenAmount"] // 2 * 2
        assert stake["amount"] == expected_amount
        assert stake["weightAdded"] == (
            expected_amount // 2 * (ybs.MAX_STAKE_GROWTH_WEEKS() + 1)
        )
