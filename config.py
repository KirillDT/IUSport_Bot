from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"


class Settings(BaseSettings):
    BOT_TOKEN: str
    # Единственный владелец бота. Это НЕ обычный администратор.
    OWNER_TELEGRAM_ID: int
    SPORT_API_URL: str = "https://sport.innopolis.university"

    # Сколько раз повторять check-in после открытия записи.
    BOOKING_REQUEST_COUNT: int = 8
    BOOKING_RETRY_INTERVAL_MS: int = 150
    # Если сервер ещё не разрешает запись, повторно проверяем расписание в течение окна.
    BOOKING_WAIT_SECONDS: int = 20
    BOOKING_POLL_INTERVAL_MS: int = 250
    TIMEZONE: str = "Europe/Moscow"
    COOKIE_ENCRYPTION_KEY: str = ""

    model_config = SettingsConfigDict(
        env_file=str(ENV_PATH),
        env_file_encoding="utf-8",
        extra="ignore",
    )


config = Settings()
