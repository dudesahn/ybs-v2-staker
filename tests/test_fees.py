import brownie
import pytest
from brownie import Contract, ZERO_ADDRESS


DEFAULT_FEE_RECIPIENT = "0x044F9C86a0Da637a235E83564215DC271Bc0deFc"
MAX_REWARD_FEE = 1_000


def _seed_reward_shares(strategy, reward_token, user, gov):
    strategy.setBypasses(True, True, {"from": gov})
    strategy.setSwapThresholds(1, 1_000_000 * 10**18, False, {"from": gov})
    # This sells the whole migrated carryover at once, past the default slippage floor.
    strategy.setMaxSlippage(10_000, {"from": gov})

    shares = min(reward_token.balanceOf(user), 1_000 * 10**18)
    assert shares > strategy.swapThresholds()["min"]
    reward_token.transfer(strategy, shares, {"from": user})
    return strategy.balanceOfReward()


def _assert_reward_redemption(tx, reward_token, strategy, expected_shares):
    redemptions = [
        event
        for event in tx.events["Withdraw"]
        if event.address.lower() == reward_token.address.lower()
    ]
    assert len(redemptions) == 1

    redemption = redemptions[0]
    assert redemption["sender"] == strategy.address
    assert redemption["receiver"] == strategy.address
    assert redemption["owner"] == strategy.address
    assert redemption["assets"] > 0
    assert redemption["shares"] == expected_shares


@pytest.mark.parametrize("reward_fee", [0, 1, MAX_REWARD_FEE])
def test_reward_fee_is_taken_in_reward_vault_shares(
    accounts,
    strategy,
    reward_token,
    user,
    gov,
    crvusd_whale,
    reward_fee,
):
    fee_recipient = accounts[6]
    strategy.setFee(fee_recipient, reward_fee, {"from": gov})

    reward_balance = _seed_reward_shares(strategy, reward_token, user, gov)
    recipient_before = reward_token.balanceOf(fee_recipient)
    underlying = Contract(reward_token.asset())
    recipient_underlying_before = underlying.balanceOf(fee_recipient)

    tx = strategy.harvest({"from": gov})

    expected_fee = reward_balance * reward_fee // 10_000
    assert reward_token.balanceOf(fee_recipient) - recipient_before == expected_fee
    assert underlying.balanceOf(fee_recipient) == recipient_underlying_before
    assert strategy.balanceOfReward() == 0
    _assert_reward_redemption(
        tx,
        reward_token,
        strategy,
        reward_balance - expected_fee,
    )


def test_harvest_mints_no_vault_shares_with_zero_vault_fees(
    accounts,
    chain,
    strategy,
    vault,
    reward_token,
    user,
    gov,
    crvusd_whale,
):
    fee_recipient = accounts[6]
    vault.setPerformanceFee(0, {"from": gov})
    vault.setManagementFee(0, {"from": gov})
    vault.updateStrategyPerformanceFee(strategy, 0, {"from": gov})
    strategy.setFee(fee_recipient, MAX_REWARD_FEE, {"from": gov})

    reward_balance = _seed_reward_shares(strategy, reward_token, user, gov)
    recipient_before = reward_token.balanceOf(fee_recipient)
    vault_rewards = vault.rewards()
    vault_rewards_shares_before = vault.balanceOf(vault_rewards)
    supply_before = vault.totalSupply()

    chain.sleep(1)
    chain.mine()
    tx = strategy.harvest({"from": gov})

    # The report has a gain, but every Vault fee is zero, so it mints no shares.
    assert tx.events["Harvested"]["profit"] > 0
    assert vault.totalSupply() == supply_before
    assert vault.balanceOf(vault_rewards) == vault_rewards_shares_before
    assert vault.balanceOf(strategy) == 0
    assert (
        reward_token.balanceOf(fee_recipient) - recipient_before
        == reward_balance * MAX_REWARD_FEE // 10_000
    )


def test_set_fee_access_and_bounds(strategy, vault, accounts, gov, strategist):
    new_recipient = accounts[6]
    vault_management = accounts.at(vault.management(), force=True)

    assert strategy.feeRecipient() == DEFAULT_FEE_RECIPIENT
    assert strategy.rewardFee() == 0

    # Only governance sets the fee, as with the Vault's own fee settings.
    for caller in (accounts[0], strategist, vault_management):
        with brownie.reverts():
            strategy.setFee(new_recipient, MAX_REWARD_FEE, {"from": caller})
    with brownie.reverts():
        strategy.setFee(ZERO_ADDRESS, MAX_REWARD_FEE, {"from": gov})
    with brownie.reverts():
        strategy.setFee(new_recipient, MAX_REWARD_FEE + 1, {"from": gov})
    assert strategy.feeRecipient() == DEFAULT_FEE_RECIPIENT
    assert strategy.rewardFee() == 0

    strategy.setFee(new_recipient, MAX_REWARD_FEE, {"from": gov})
    assert strategy.feeRecipient() == new_recipient
    assert strategy.rewardFee() == MAX_REWARD_FEE

    strategy.setFee(new_recipient, 0, {"from": gov})
    assert strategy.rewardFee() == 0
