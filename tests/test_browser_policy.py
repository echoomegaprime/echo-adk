"""The browser's host policy is a boundary, not a model preference."""

import socket

import pytest

from echo_adk.browser.config import BrowserConfig
from echo_adk.browser.errors import BrowserError
from echo_adk.browser.policy import NetworkPolicy, safe_url


def test_browser_is_disabled_and_deny_all_without_host_configuration():
    config = BrowserConfig.from_env({})
    assert not config.enabled
    assert config.allowed_origins == ()
    assert config.private_origins == ()
    assert config.chromium_sandbox


@pytest.mark.parametrize(
    "env",
    [
        {"ECHO_ADK_BROWSER_ENABLED": "yes"},
        {"ECHO_ADK_BROWSER_ALLOWED_ORIGINS": '["https://*.example.com"]'},
        {"ECHO_ADK_BROWSER_ALLOWED_ORIGINS": '["https://example.com/path"]'},
        {"ECHO_ADK_BROWSER_ALLOWED_ORIGINS": '["https://user:secret@example.com"]'},
        {"ECHO_ADK_BROWSER_ALLOWED_ORIGINS": '"https://example.com"'},
        {"ECHO_ADK_BROWSER_PRIVATE_ORIGINS": '["http://localhost"]'},
        {"ECHO_ADK_BROWSER_EXECUTABLE": "relative/chrome"},
    ],
)
def test_malformed_host_policy_fails_closed(env):
    with pytest.raises(BrowserError) as caught:
        BrowserConfig.from_env(env)
    assert caught.value.status == 400


def test_exact_origin_canonicalization_and_private_exception():
    config = BrowserConfig(
        enabled=True,
        allowed_origins=("HTTPS://Example.COM:443", "http://127.0.0.1:8123"),
        private_origins=("http://127.0.0.1:8123",),
    )
    assert config.allowed_origins == ("https://example.com", "http://127.0.0.1:8123")
    policy = NetworkPolicy(config)
    assert policy.validate("https://example.com/a?q=hi").origin == "https://example.com"
    assert policy.addresses(policy.validate("http://127.0.0.1:8123"))[0][1] == "127.0.0.1"
    for url in ("https://sub.example.com", "http://example.com", "https://example.com:8443", "file:///etc/passwd"):
        with pytest.raises(BrowserError) as caught:
            policy.validate(url)
        assert caught.value.status == 403


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "10.0.0.1", "::1", "fc00::1", "100.64.0.1"])
def test_dns_private_and_metadata_addresses_are_blocked(monkeypatch, address):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(family, socket.SOCK_STREAM, 6, "", (address, 443))])
    policy = NetworkPolicy(BrowserConfig(allowed_origins=("https://example.com",)))
    with pytest.raises(BrowserError) as caught:
        policy.addresses(policy.validate("https://example.com"))
    assert caught.value.code == "network_denied"


def test_mixed_dns_answers_fail_closed_and_return_only_pinned_addresses(monkeypatch):
    answers = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
    ]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: answers)
    policy = NetworkPolicy(BrowserConfig(allowed_origins=("https://example.com",)))
    with pytest.raises(BrowserError):
        policy.addresses(policy.validate("https://example.com"))
    answers.pop()
    assert policy.addresses(policy.validate("https://example.com")) == [(socket.AF_INET, "93.184.216.34")]


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/?access_token=secret",
        "https://example.com/?code=oauth-code",
        "https://a:b@example.com/",
        "https://example.com/\r\nX-Evil: yes",
        "https://example.com\\@evil.example/",
    ],
)
def test_navigation_never_accepts_credential_urls(url):
    policy = NetworkPolicy(BrowserConfig(allowed_origins=("https://example.com",)))
    with pytest.raises(BrowserError):
        policy.validate(url, navigation=True)


def test_returned_urls_omit_userinfo_query_and_fragment():
    assert safe_url("https://example.com/path?access_token=secret#code=secret") == "https://example.com/path"
    assert safe_url("https://user:secret@example.com/path") == ""
    assert safe_url("file:///private/key") == ""
