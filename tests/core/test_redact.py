from mcperf.core.redact import redact_headers, redact_ws_url


def test_redact_ws_url():
    url = "wss://x.test/stg1?Authorization=eyJsecret&tenant=1"
    assert redact_ws_url(url) == "wss://x.test/stg1?Authorization=<redacted>&tenant=1"


def test_redact_headers_only_touches_secrets():
    out = redact_headers({"Authorization": "abcdefgh", "x-api-key": "12345678", "Accept": "json"})
    assert out == {"Authorization": "abcd...<redacted>", "x-api-key": "1234...<redacted>", "Accept": "json"}
