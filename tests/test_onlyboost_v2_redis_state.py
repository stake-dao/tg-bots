"""
Tests for the Redis checkpoint + alert dedup in the OnlyBoost V2 bot.

Checkpoints: one hash keyed by chain_id holding the next from_block.
Dedup: one set of "txhash:logIndex" ids for already-alerted events.
"""

from unittest.mock import MagicMock, patch

import pytest

from bots.onlyboost_v2 import main as ob


class FakeRedis:
    """Minimal in-memory stand-in for the upstash client."""

    def __init__(self):
        self.hashes = {}
        self.sets = {}
        self.expirations = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    def sismember(self, key, member):
        return member in self.sets.get(key, set())

    def sadd(self, key, member):
        self.sets.setdefault(key, set()).add(member)

    def expire(self, key, ttl):
        self.expirations[key] = ttl


class BrokenRedis(FakeRedis):
    def __getattribute__(self, name):
        if name in ("hgetall", "hset", "sismember", "sadd", "expire"):
            raise ConnectionError("redis down")
        return super().__getattribute__(name)


@pytest.fixture
def fake_redis():
    client = FakeRedis()
    with patch.object(ob, "get_redis_client", return_value=client):
        yield client


@pytest.fixture
def broken_redis():
    client = BrokenRedis()
    with patch.object(ob, "get_redis_client", return_value=client):
        yield client


class TestCheckpoints:
    def test_save_then_load_roundtrip(self, fake_redis):
        ob.save_checkpoint_to_redis(1, 25594225)
        ob.save_checkpoint_to_redis(42161, 999)

        records = ob.load_checkpoints_from_redis()

        assert {"chain_id": 1, "last_block": 25594225} in records
        assert {"chain_id": 42161, "last_block": 999} in records
        assert len(records) == 2

    def test_load_decodes_string_values(self, fake_redis):
        # decode_responses=True returns str keys/values
        fake_redis.hashes[ob.REDIS_CHECKPOINT_KEY] = {"1": "123"}

        assert ob.load_checkpoints_from_redis() == [
            {"chain_id": 1, "last_block": 123}
        ]

    def test_load_empty(self, fake_redis):
        assert ob.load_checkpoints_from_redis() == []

    def test_load_failure_raises(self, broken_redis):
        # Fail loud: a broken checkpoint read must abort the run, not
        # silently bootstrap forward and skip events.
        with pytest.raises(ConnectionError):
            ob.load_checkpoints_from_redis()

    def test_save_failure_does_not_raise(self, broken_redis):
        ob.save_checkpoint_to_redis(1, 123)

    def test_save_failure_raises_when_fail_loud(self, broken_redis):
        # Bootstrap writes must fail loud: a silently lost bootstrap
        # checkpoint skips every block until the next bootstrap.
        with pytest.raises(ConnectionError):
            ob.save_checkpoint_to_redis(1, 123, fail_loud=True)


class TestEventId:
    def test_hexbytes_tx_hash(self):
        event = {"transactionHash": bytes.fromhex("ab" * 32), "logIndex": 7}
        assert ob.get_event_id(event) == "0x" + "ab" * 32 + ":7"

    def test_str_tx_hash_with_prefix(self):
        event = {"transactionHash": "0xAB" + "cd" * 31, "logIndex": 0}
        assert ob.get_event_id(event) == ("0xAB" + "cd" * 31).lower() + ":0"


class TestSeenEvents:
    def test_mark_then_seen(self, fake_redis):
        event_id = "0x" + "ab" * 32 + ":7"
        assert ob.is_event_seen(event_id) is False

        ob.mark_event_seen(event_id)

        assert ob.is_event_seen(event_id) is True
        assert fake_redis.expirations[ob.REDIS_SEEN_KEY] == ob.REDIS_SEEN_TTL

    def test_redis_failure_defaults_to_not_seen(self, broken_redis):
        assert ob.is_event_seen("whatever") is False

    def test_mark_failure_does_not_raise(self, broken_redis):
        ob.mark_event_seen("whatever")


class TestProcessVaultEventDedup:
    def test_seen_event_short_circuits(self, fake_redis):
        """A seen event returns before any web3/vault access."""
        event = {"transactionHash": bytes.fromhex("ab" * 32), "logIndex": 7}
        ob.mark_event_seen(ob.get_event_id(event))

        bot = MagicMock(spec=ob.OnlyBoostV2Bot)
        with patch.object(ob, "PROD", True):
            # web3=None: any access past the dedup check would raise
            result = ob.OnlyBoostV2Bot.process_vault_event(bot, {}, event, None)

        assert result is True  # legit skip must not block the checkpoint

    def test_dedup_not_consulted_outside_prod(self, fake_redis):
        """Dev runs go to the test telegram channel; they must never read or
        write the shared prod dedup set."""
        event = {"transactionHash": bytes.fromhex("ab" * 32), "logIndex": 7}
        bot = MagicMock(spec=ob.OnlyBoostV2Bot)

        with patch.object(ob, "PROD", False), patch.object(
            ob, "is_event_seen"
        ) as mock_seen:
            ob.OnlyBoostV2Bot.process_vault_event(bot, {}, event, None)

        mock_seen.assert_not_called()


class TestDeliver:
    def _bot(self):
        return MagicMock(spec=ob.OnlyBoostV2Bot)

    def test_successful_send_marks_seen(self, fake_redis):
        with patch.object(ob, "PROD", True), patch.object(
            ob, "DRY_RUN", False
        ), patch.object(ob, "send_telegram_message", return_value=True):
            assert ob.OnlyBoostV2Bot._deliver(self._bot(), "msg", "id-1") is True

        assert ob.is_event_seen("id-1") is True

    def test_failed_send_returns_false_and_does_not_mark(self, fake_redis):
        with patch.object(ob, "PROD", True), patch.object(
            ob, "DRY_RUN", False
        ), patch.object(ob, "send_telegram_message", return_value=False):
            assert ob.OnlyBoostV2Bot._deliver(self._bot(), "msg", "id-1") is False

        assert ob.is_event_seen("id-1") is False

    def test_dry_run_does_not_send_or_mark(self, fake_redis):
        with patch.object(ob, "PROD", True), patch.object(
            ob, "DRY_RUN", True
        ), patch.object(ob, "send_telegram_message") as mock_send:
            assert ob.OnlyBoostV2Bot._deliver(self._bot(), "msg", "id-1") is True

        mock_send.assert_not_called()
        assert ob.is_event_seen("id-1") is False

    def test_non_prod_sends_but_does_not_mark(self, fake_redis):
        with patch.object(ob, "PROD", False), patch.object(
            ob, "DRY_RUN", False
        ), patch.object(
            ob, "send_telegram_message", return_value=True
        ) as mock_send:
            assert ob.OnlyBoostV2Bot._deliver(self._bot(), "msg", "id-1") is True

        mock_send.assert_called_once()
        assert ob.is_event_seen("id-1") is False
