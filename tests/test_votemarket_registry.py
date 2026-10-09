import pytest

from shared.constants import ContractRegistry


@pytest.mark.parametrize("chain_id", [1, 10, 137, 8453, 42161])
def test_votemarket_reward_token_factory_is_available(chain_id):
    assert ContractRegistry.get_address("TOKEN_FACTORY_VM_V2", chain_id) == (
        "0x96006425Da428E45c282008b00004a00002B345e"
    )
