import logging

from app.infrastructure.logging import SecretRedactingFormatter


def test_secrets_redacted_in_message_extra_and_traceback():
    secret = "configured-private-secret"
    token = "123456789:" + ("x" * 35)
    formatter = SecretRedactingFormatter(secrets=[secret])
    try:
        raise ValueError("unsafe error " + secret)
    except ValueError:
        import sys

        record = logging.LogRecord(
            "test", logging.ERROR, __file__, 1, "GET https://api.telegram.org/bot%s/getMe", (token,), sys.exc_info()
        )
    record.upstream = "sk-" + ("y" * 64)
    record.partner_key = "nrn_" + ("z" * 43)
    result = formatter.format(record)
    assert secret not in result and token not in result and record.upstream not in result
    assert record.partner_key not in result
    assert "[REDACTED]" in result
