import brownie
import pytest
from brownie import Contract, ZERO_ADDRESS, web3
from eth_abi import decode


DEFAULT_FEE_RECIPIENT = "0x044F9C86a0Da637a235E83564215DC271Bc0deFc"
MAX_UINT = 2**256 - 1
MAX_REWARD_FEE = 1_000
WITHDRAW_TOPIC = web3.keccak(
    text="Withdraw(address,address,address,uint256,uint256)"
).hex()


def _seed_reward_shares(strategy, reward_token, user, gov):
    strategy.setBypasses(True, True, {"from": gov})
    strategy.setSwapThresholds(1, 1_000_000 * 10**18, False, {"from": gov})

    shares = min(reward_token.balanceOf(user), 1_000 * 10**18)
    assert shares > strategy.swapThresholds()["min"]
    reward_token.transfer(strategy, shares, {"from": user})
    return strategy.balanceOfReward()


def _assert_reward_redemption(tx, reward_token, strategy, expected_shares):
    withdrawals = [
        log
        for log in tx.logs
        if log["address"].lower() == reward_token.address.lower()
        and log["topics"][0].hex() == WITHDRAW_TOPIC
    ]
    assert len(withdrawals) == 1

    withdrawal = withdrawals[0]
    expected_account = strategy.address[2:].lower()
    assert withdrawal["topics"][1].hex()[-40:].lower() == expected_account
    assert withdrawal["topics"][2].hex()[-40:].lower() == expected_account
    assert withdrawal["topics"][3].hex()[-40:].lower() == expected_account

    assets, shares = decode(["uint256", "uint256"], withdrawal["data"])
    assert assets > 0
    assert shares == expected_shares


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


def test_self_rewards_does_not_grant_public_access(
    accounts,
    strategy,
    vault,
    token,
    user,
    gov,
    reward_distributor,
    fund_ycrv,
):
    attacker = accounts[9]
    previous_rewards = strategy.rewards()

    strategy.setRewards(strategy, {"from": gov})

    # BaseStrategy revokes the former recipient and approves only itself.
    assert vault.allowance(strategy, previous_rewards) == 0
    assert vault.allowance(strategy, strategy) == MAX_UINT
    assert vault.allowance(strategy, attacker) == 0

    # Stage parent-vault shares and prove the self-allowance cannot be used by
    # an arbitrary caller to pull them out of the strategy.
    assets = 100 * 10 ** token.decimals()
    fund_ycrv(user, assets)
    token.approve(vault, assets, {"from": user})
    shares_before = vault.balanceOf(user)
    vault.deposit(assets, {"from": user})
    staged_shares = vault.balanceOf(user) - shares_before
    vault.transfer(strategy, staged_shares, {"from": user})

    strategy_shares_before = vault.balanceOf(strategy)
    attacker_shares_before = vault.balanceOf(attacker)
    with brownie.reverts():
        vault.transferFrom(
            strategy,
            attacker,
            strategy_shares_before,
            {"from": attacker},
        )
    assert vault.balanceOf(strategy) == strategy_shares_before
    assert vault.balanceOf(attacker) == attacker_shares_before

    # The inherited rewards address is unrelated to the YBS distributor's
    # claimer and recipient mappings. Only the strategy can configure those
    # mappings for its own account, via vault-manager-gated strategy methods.
    strategy_account_info = reward_distributor.accountInfo(strategy)
    assert not reward_distributor.approvedClaimer(strategy, attacker)
    with brownie.reverts("!approvedClaimer"):
        reward_distributor.claimFor(strategy, {"from": attacker})
    with brownie.reverts("!approvedClaimer"):
        reward_distributor.claimWithRangeFor(
            strategy,
            0,
            reward_distributor.getWeek() - 1,
            {"from": attacker},
        )

    reward_distributor.configureRecipient(attacker, {"from": attacker})
    assert reward_distributor.accountInfo(strategy) == strategy_account_info
    assert reward_distributor.accountInfo(attacker)["recipient"] == attacker

    with brownie.reverts():
        strategy.approveRewardClaimer(attacker, True, {"from": attacker})
    assert not reward_distributor.approvedClaimer(strategy, attacker)

    # Harvest is also not public, so an attacker cannot force the strategy to
    # run its own claim/report sequence.
    with brownie.reverts():
        strategy.harvest({"from": attacker})
