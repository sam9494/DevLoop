import pytest

from devloop.core.crypto import decrypt, encrypt, mask


def test_round_trip() -> None:
    assert decrypt(encrypt("ATATT-super-secret")) == "ATATT-super-secret"


def test_ciphertext_does_not_contain_the_plaintext() -> None:
    assert "super-secret" not in encrypt("ATATT-super-secret")


def test_same_plaintext_encrypts_differently_each_time() -> None:
    assert encrypt("同一段") != encrypt("同一段")


def test_mask_keeps_only_the_tail() -> None:
    masked = mask("ATATT3xFfGF0abcd1234")
    assert masked.endswith("1234")
    assert "ATATT" not in masked


def test_mask_of_a_short_secret_reveals_nothing() -> None:
    assert mask("abc") == "•" * 8


def test_tampered_ciphertext_is_rejected() -> None:
    bad = encrypt("x")[:-4] + "AAAA"
    with pytest.raises(ValueError, match="解不開"):
        decrypt(bad)
