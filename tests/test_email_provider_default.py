def test_default_provider_is_safe_while_postmark_is_pending():
    """The default must not be a provider that cannot reach public inboxes.

    A Postmark account in "pending approval" rejects any recipient whose
    domain differs from the From address. Defaulting to it in that state
    would fail every signup from a gmail.com or outlook.com user while
    looking perfectly healthy in config. Flip this default only after the
    account is approved.
    """
    import inspect

    from app.core.config import Settings

    src = inspect.getsource(Settings)
    line = next(
        ln for ln in src.splitlines() if ln.strip().startswith("email_provider:")
    )
    assert '"resend"' in line, (
        "email_provider default changed — confirm the Postmark account is out "
        "of pending approval and can send to arbitrary recipient domains "
        "before making it the default"
    )
