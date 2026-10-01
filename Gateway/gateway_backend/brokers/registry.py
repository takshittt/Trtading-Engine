"""Broker registry — maps broker names to adapter instances."""

from brokers.base import BrokerBase
from brokers.shoonya import ShoonyaBroker


BROKER_REGISTRY: dict[str, BrokerBase] = {
    "shoonya": ShoonyaBroker(),
}


def get_broker(broker_name: str) -> BrokerBase:
    """
    Look up a broker adapter by name.

    Args:
        broker_name: Broker name (case-insensitive), e.g. "shoonya"

    Returns:
        BrokerBase instance

    Raises:
        KeyError: If broker_name is not registered
    """
    try:
        return BROKER_REGISTRY[broker_name.lower()]
    except KeyError:
        available = list(BROKER_REGISTRY.keys())
        raise KeyError(
            f"No broker adapter registered for '{broker_name}'. "
            f"Available: {available}"
        )
