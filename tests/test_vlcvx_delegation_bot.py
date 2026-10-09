"""Stake DAO vlCVX delegation activity bot (ENG-2101).

The Convex Delegation contract emits a single event, DelegateSet(user,
delegate), with no trace of the previous delegate. Everything the bot says
therefore comes from a fold over the window's events on top of the state read
just before it — that fold and its message rendering are what these tests
cover.
"""

from bots.vlcvx_delegation import main as bot
from shared.utils.globals import pad_address

SD = bot.STAKE_DAO_DELEGATE
VOTIUM = "0xde1E6A7ED0ad3F61D531a8a78E83CCDdbd6E0c49"
OTHER = "0x947b7742C403f20e5FaCcDAc5E092C943E7D0277"
ZERO = bot.ZERO_ADDRESS

USER = "0x00000000000000000000000000000000000000A1"
USER_2 = "0x00000000000000000000000000000000000000B2"


def _log(user, delegate, block, log_index=0, tx="0xdead"):
    return {
        "topics": [
            bot.TOPIC_DELEGATE_SET,
            pad_address(user),
            pad_address(delegate),
        ],
        "blockNumber": hex(block),
        "logIndex": hex(log_index),
        "transactionHash": tx,
    }


# --------------------------------------------------------------------------
# _classify_transition — only the changes that involve Stake DAO
# --------------------------------------------------------------------------

def test_transitions_relative_to_stake_dao():
    assert bot._classify_transition(None, SD) == bot.NEW_DELEGATION
    assert bot._classify_transition(OTHER, SD) == bot.NEW_DELEGATION
    assert bot._classify_transition(ZERO, SD) == bot.NEW_DELEGATION
    assert bot._classify_transition(SD, VOTIUM) == bot.FORWARDED
    assert bot._classify_transition(SD, ZERO) == bot.REMOVED


def test_changes_not_involving_stake_dao_are_ignored():
    # Someone else's delegation churn must not reach our activity channel
    assert bot._classify_transition(VOTIUM, OTHER) is None
    assert bot._classify_transition(None, VOTIUM) is None
    assert bot._classify_transition(VOTIUM, ZERO) is None
    # Re-setting the same delegate (Convex UI re-submit) is not an event
    assert bot._classify_transition(SD, SD) is None


# --------------------------------------------------------------------------
# _stake_dao_transitions — fold over the window
# --------------------------------------------------------------------------

def test_window_state_comes_from_the_lookback_once_per_user():
    calls = []

    def previous(user):
        calls.append(user)
        return SD

    logs = [_log(USER, VOTIUM, 100), _log(USER, SD, 101), _log(USER, ZERO, 102)]
    transitions = bot._stake_dao_transitions(logs, previous)

    # The pre-window delegate is read once, then the fold carries the state
    assert calls == [bot.Web3.to_checksum_address(USER)]
    assert [kind for _log_, _user, kind, _delegate in transitions] == [
        bot.FORWARDED,  # SD -> Votium
        bot.NEW_DELEGATION,  # Votium -> SD
        bot.REMOVED,  # SD -> cleared
    ]


def test_events_are_replayed_in_chain_order():
    # Etherscan ordering is not guaranteed once several logs share a block:
    # folded out of order, the SD -> zero removal below would read as a
    # delegation to nobody and be dropped
    logs = [
        _log(USER, ZERO, 100, log_index=2),
        _log(USER, SD, 100, log_index=1),
        _log(USER, VOTIUM, 99, log_index=7),
    ]
    kinds = [k for _l, _u, k, _d in bot._stake_dao_transitions(logs, lambda _u: None)]
    assert kinds == [bot.NEW_DELEGATION, bot.REMOVED]


def test_users_are_folded_independently():
    logs = [_log(USER, SD, 100), _log(USER_2, VOTIUM, 100, log_index=1)]
    previous = {bot.Web3.to_checksum_address(USER_2): SD}
    transitions = bot._stake_dao_transitions(logs, lambda user: previous.get(user))

    assert [(user, kind) for _l, user, kind, _d in transitions] == [
        (bot.Web3.to_checksum_address(USER), bot.NEW_DELEGATION),
        (bot.Web3.to_checksum_address(USER_2), bot.FORWARDED),
    ]


def test_zero_index_log_is_not_dropped():
    # Etherscan returns "0x" (not "0x0") for the first log of a block
    log = _log(USER, SD, 100)
    log["logIndex"] = "0x"
    assert bot._log_order(log) == (100, 0)


# --------------------------------------------------------------------------
# _fetch_delegate_set_logs — Etherscan's silent 1000-entry cap
# --------------------------------------------------------------------------

def _capped_fetcher(logs_by_block):
    """Etherscan stand-in: returns at most 1000 entries for a range, in order."""
    def fetch(_address, from_block, to_block, _topics, _chain_id):
        window = [
            log
            for block, block_logs in sorted(logs_by_block.items())
            if from_block <= block <= to_block
            for log in block_logs
        ]
        return window[: bot.ETHERSCAN_LOG_PAGE_SIZE]

    return fetch


def test_a_capped_range_is_split_until_every_log_comes_back(monkeypatch):
    # 1400 logs over two blocks: a single call returns the first 1000 and
    # drops the rest without any error
    logs_by_block = {
        100: [_log(USER, SD, 100, i) for i in range(1000)],
        101: [_log(USER_2, SD, 101, i) for i in range(400)],
    }
    fetch = _capped_fetcher(logs_by_block)
    monkeypatch.setattr(bot, "get_logs_by_address_and_topics", fetch)

    assert len(fetch(None, 100, 101, None, 1)) == 1000  # what one call gives
    assert len(bot._fetch_delegate_set_logs(100, 101, {})) == 1400


def test_an_unsplittable_block_returns_what_it_can(monkeypatch):
    # A single block over the cap cannot be split further: return the page
    # rather than recursing forever
    logs_by_block = {100: [_log(USER, SD, 100, i) for i in range(1200)]}
    monkeypatch.setattr(
        bot, "get_logs_by_address_and_topics", _capped_fetcher(logs_by_block)
    )
    assert len(bot._fetch_delegate_set_logs(100, 100, {})) == 1000


# --------------------------------------------------------------------------
# Message rendering
# --------------------------------------------------------------------------

USER_LINE = "User: <a href='https://etherscan.io/address/0xA1'>0xA1…</a>\n"


def test_new_delegation_message():
    message = bot._build_message(
        bot.NEW_DELEGATION, "0xabc", " : 1.20M vlCVX (~$8.40M)", USER_LINE
    )
    assert message == (
        "🟢 <a href='https://etherscan.io/tx/0xabc'>New vlCVX delegation to "
        "Stake DAO</a> : 1.20M vlCVX (~$8.40M)\n" + USER_LINE
    )


def test_forward_message_names_the_new_delegate():
    delegate_line = (
        f"New delegate: <a href='https://etherscan.io/address/{VOTIUM}'>"
        "votium.eth</a>\n"
    )
    message = bot._build_message(
        bot.FORWARDED, "0xabc", " : 1.20M vlCVX", USER_LINE, delegate_line
    )
    assert message.startswith("🔁 ")
    assert message.endswith(delegate_line)


def test_removal_message_has_no_delegate_line():
    message = bot._build_message(bot.REMOVED, "0xabc", " : 900.00k vlCVX", USER_LINE)
    assert message.startswith("🔴 ")
    assert message.endswith(USER_LINE)


def test_total_line_is_appended_last():
    message = bot._build_message(
        bot.NEW_DELEGATION,
        "0xabc",
        " : 1.20M vlCVX",
        USER_LINE,
        total_line=bot._format_total(9_980_205 * 10**18),
    )
    assert message.endswith("Total delegated to Stake DAO : 9.98M vlCVX")
    assert bot._format_total(0) == ""


def test_amount_is_omitted_when_the_lock_is_empty():
    # An expired lock delegates nothing; "0.00 vlCVX" would read as a bug
    assert bot._format_amount(0, 3.5) == ""
    assert bot._format_amount(1_200_000 * 10**18, None) == " : 1.20M vlCVX"
    assert bot._format_amount(1_200_000 * 10**18, 3.5) == " : 1.20M vlCVX (~$4.20M)"


# --------------------------------------------------------------------------
# _weight_epoch — the epoch a delegation change applies to
# --------------------------------------------------------------------------

class _FakeVlCVX:
    def __init__(self, epoch_count, current_epoch):
        self._epoch_count = epoch_count
        self._current_epoch = current_epoch
        self.functions = self

    def epochCount(self):
        return _FakeCall(self._epoch_count)

    def findEpochId(self, _timestamp):
        return _FakeCall(self._current_epoch)


class _FakeCall:
    def __init__(self, value):
        self._value = value

    def call(self):
        return self._value


def test_weight_is_read_at_the_next_epoch():
    # A lock created in the current epoch only votes from the next one, so
    # reading "now" reports 0 vlCVX for whoever locks and delegates at once
    assert bot._weight_epoch(_FakeVlCVX(epoch_count=233, current_epoch=231), 0) == 232


def test_weight_epoch_never_runs_past_the_checkpointed_epochs():
    # balanceAtEpochOf reverts on an epoch vlCVX has not checkpointed yet
    assert bot._weight_epoch(_FakeVlCVX(epoch_count=232, current_epoch=231), 0) == 231
