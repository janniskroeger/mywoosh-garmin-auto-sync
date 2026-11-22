from mywhoosh_garmin_sync.crypto import decrypt_payload, encrypt_payload

KEY = "D9436E508087E863"


def test_roundtrip():
    payload = {"type": "", "page": 1, "sortDate": "DESC"}
    assert decrypt_payload(encrypt_payload(payload, KEY), KEY) == payload


def test_openssl_salted_format():
    ct = encrypt_payload({"fileId": "abc"}, KEY)
    assert ct.startswith("U2FsdGVkX1")  # base64 of "Salted__"


def test_random_salt():
    assert encrypt_payload({"a": 1}, KEY) != encrypt_payload({"a": 1}, KEY)


def test_decrypts_known_cryptojs_ciphertext():
    # generated on 2026-10-30, cross-checked with openssl enc -d
    ct = "U2FsdGVkX1+7JfTMlxQ6pSQCosKA/3AEWSnzP7gIPfP1rsX9rtIBv7pX7xDm5EMXY1yMmOaKJ5ZxJdG12RSAfQ=="
    assert decrypt_payload(ct, KEY) == {"type": "", "page": 1, "sortDate": "DESC"}
