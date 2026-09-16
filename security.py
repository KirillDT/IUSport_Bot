import logging
from cryptography.fernet import Fernet, InvalidToken
from config import config

logger = logging.getLogger("security")


def _fernet():
    key = config.COOKIE_ENCRYPTION_KEY.strip()
    if not key:
        raise RuntimeError("COOKIE_ENCRYPTION_KEY не задан в .env")
    return Fernet(key.encode())


def encrypt_secret(value: str) -> str:
    if not value:
        return ""
    return "enc$" + _fernet().encrypt(value.encode()).decode()


def decrypt_secret(value: str) -> str:
    if not value:
        return ""
    if not value.startswith("enc$"):
        # Backward compatibility: старые значения будут прочитаны один раз,
        # а при следующем сохранении зашифрованы.
        return value
    try:
        return _fernet().decrypt(value[4:].encode()).decode()
    except InvalidToken:
        logger.error("Не удалось расшифровать cookie: неверный ключ шифрования")
        return ""


def save_credentials(user, sessionid: str, csrftoken: str):
    user.sessionid = encrypt_secret(sessionid.strip())
    user.csrftoken = encrypt_secret(csrftoken.strip())


def load_credentials(user):
    return decrypt_secret(user.sessionid), decrypt_secret(user.csrftoken)
