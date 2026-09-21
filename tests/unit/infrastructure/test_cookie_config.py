from auth.entrypoint.ioc.adapters import AuthProvider


def test_cookie_stays_secure_by_default_and_allows_explicit_local_http(monkeypatch):
    monkeypatch.delenv("SESSION_COOKIE_SECURE", raising=False)
    value = AuthProvider().provide_cookie_params()
    assert value.secure is True
    assert value.samesite == "strict"
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    value = AuthProvider().provide_cookie_params()
    assert value.secure is False
    assert value.samesite == "strict"
