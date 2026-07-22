import brownie
import pytest
from brownie import Contract, ZERO_ADDRESS, web3
from eth_abi import decode


DEFAULT_FEE_RECIPIENT = "0x044F9C86a0Da637a235E83564215DC271Bc0deFc"
MAX_UINT = 2**256 - 1
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


@pytest.mark.parametrize(
    "strategy_is_vault_rewards,strategy_rewards_is_self",
    [(False, False), (True, False), (False, True), (True, True)],
)
def test_reward_fee_requires_both_rewards_addresses(
    accounts,
    strategy,
    vault,
    reward_token,
    user,
    gov,
    crvusd_whale,
    strategy_is_vault_rewards,
    strategy_rewards_is_self,
):
    fee_recipient = accounts[6]
    other_vault_rewards = accounts[7]
    other_strategy_rewards = accounts[8]
    performance_fee = 1_234

    strategy.setFeeRecipient(fee_recipient, {"from": gov})
    previous_strategy_rewards = strategy.rewards()
    strategy.setRewards(
        strategy if strategy_rewards_is_self else other_strategy_rewards,
        {"from": gov},
    )
    if strategy_rewards_is_self:
        assert vault.allowance(strategy, previous_strategy_rewards) == 0
        assert vault.allowance(strategy, strategy) == MAX_UINT
    vault.setPerformanceFee(performance_fee, {"from": gov})
    vault.setRewards(
        strategy if strategy_is_vault_rewards else other_vault_rewards,
        {"from": gov},
    )
    fee_mode_active = strategy_is_vault_rewards and strategy_rewards_is_self
    assert strategy.feeModeActive() is fee_mode_active

    reward_balance = _seed_reward_shares(strategy, reward_token, user, gov)
    recipient_before = reward_token.balanceOf(fee_recipient)
    underlying = Contract(reward_token.asset())
    recipient_underlying_before = underlying.balanceOf(fee_recipient)

    tx = strategy.harvest({"from": gov})

    expected_fee = reward_balance * performance_fee // 10_000 if fee_mode_active else 0
    assert reward_token.balanceOf(fee_recipient) - recipient_before == expected_fee
    assert underlying.balanceOf(fee_recipient) == recipient_underlying_before
    assert strategy.balanceOfReward() == 0
    _assert_reward_redemption(
        tx,
        reward_token,
        strategy,
        reward_balance - expected_fee,
    )


def test_vault_fee_shares_recycle_while_fee_remains_active(
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

    strategy.setFeeRecipient(fee_recipient, {"from": gov})
    strategy.setRewards(strategy, {"from": gov})
    vault.setManagementFee(0, {"from": gov})
    vault.updateStrategyPerformanceFee(strategy, 0, {"from": gov})
    vault.setPerformanceFee(1_000, {"from": gov})
    vault.setRewards(strategy, {"from": gov})
    assert strategy.feeModeActive()
    _seed_reward_shares(strategy, reward_token, user, gov)

    strategy.harvest({"from": gov})

    parent_shares = vault.balanceOf(strategy)
    assert parent_shares > 0
    recipient_reward_shares = reward_token.balanceOf(fee_recipient)
    assert recipient_reward_shares > 0

    # Keep the fee configuration active. Each report redeems the prior report's
    # parent-vault shares, then the Vault mints a smaller performance-fee claim
    # against that recycled profit. This is the intended geometric tail.
    for _ in range(2):
        shares_before = parent_shares
        supply_before = vault.totalSupply()
        chain.sleep(1)
        chain.mine()

        strategy.harvest({"from": gov})

        parent_shares = vault.balanceOf(strategy)
        assert 0 < parent_shares < shares_before
        assert reward_token.balanceOf(fee_recipient) == recipient_reward_shares
        assert strategy.balanceOfReward() == 0
        assert supply_before - vault.totalSupply() == shares_before - parent_shares


def test_set_fee_recipient_access_and_event(strategy, vault, accounts, gov):
    new_recipient = accounts[6]
    management_recipient = accounts[7]
    management = accounts.at(vault.management(), force=True)

    assert strategy.feeRecipient() == DEFAULT_FEE_RECIPIENT

    with brownie.reverts():
        strategy.setFeeRecipient(new_recipient, {"from": accounts[0]})
    with brownie.reverts("!recipient"):
        strategy.setFeeRecipient(ZERO_ADDRESS, {"from": gov})

    tx = strategy.setFeeRecipient(new_recipient, {"from": gov})
    assert (
        tx.events["FeeRecipientUpdated"]["previousRecipient"] == DEFAULT_FEE_RECIPIENT
    )
    assert tx.events["FeeRecipientUpdated"]["newRecipient"] == new_recipient
    assert strategy.feeRecipient() == new_recipient

    tx = strategy.setFeeRecipient(management_recipient, {"from": management})
    assert tx.events["FeeRecipientUpdated"]["previousRecipient"] == new_recipient
    assert tx.events["FeeRecipientUpdated"]["newRecipient"] == management_recipient
    assert strategy.feeRecipient() == management_recipient


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
    # run its own claim/refund/report sequence.
    with brownie.reverts():
        strategy.harvest({"from": attacker})
