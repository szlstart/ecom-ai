import ipaddress

from app.core.client_ip import resolve_client_ip

TRUSTED = (
    ipaddress.ip_network("127.0.0.1/32"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("::1/128"),
)


def test_untrusted_peer_cannot_spoof_forwarded_for() -> None:
    client, source = resolve_client_ip("198.51.100.7", "203.0.113.9", TRUSTED)
    assert client == "198.51.100.7"
    assert source == "peer"


def test_trusted_multi_hop_chain_selects_first_untrusted_client() -> None:
    client, source = resolve_client_ip(
        "172.18.0.4", "203.0.113.9, 172.18.0.3", TRUSTED
    )
    assert client == "203.0.113.9"
    assert source == "trusted_proxy_chain"


def test_malformed_forwarded_chain_fails_closed_to_peer() -> None:
    client, source = resolve_client_ip("127.0.0.1", "unknown, 203.0.113.9", TRUSTED)
    assert client == "127.0.0.1"
    assert source == "peer_invalid_forwarded_chain"


def test_ipv6_client_is_normalized() -> None:
    client, source = resolve_client_ip("::1", "2001:db8::1234", TRUSTED)
    assert client == "2001:db8::1234"
    assert source == "trusted_proxy_chain"
