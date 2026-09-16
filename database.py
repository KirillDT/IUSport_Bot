from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, UniqueConstraint, func, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = "sqlite:///users_sessions.sqlite"

Base = declarative_base()
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class UserSession(Base):
    __tablename__ = "user_sessions"
    telegram_id = Column(Integer, primary_key=True, index=True)
    username = Column(String, nullable=True)
    sessionid = Column(String, nullable=True)
    csrftoken = Column(String, nullable=True)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    last_seen_at = Column(DateTime, nullable=True)


class SpotTracker(Base):
    __tablename__ = "spot_trackers"
    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(Integer, index=True)
    training_id = Column(Integer, index=True)
    training_name = Column(String, nullable=True)
    training_time = Column(String, nullable=True)
    auto_book = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=func.now())
    __table_args__ = (UniqueConstraint("telegram_id", "training_id", name="uq_spot_user_training"),)


class AllowedUser(Base):
    __tablename__ = "allowed_users"
    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(Integer, unique=True, index=True, nullable=True)
    username = Column(String, unique=True, index=True, nullable=True)
    role = Column(String, nullable=False, default="user")  # user | admin
    added_at = Column(DateTime, default=func.now())
    last_seen_at = Column(DateTime, nullable=True)
    granted_by = Column(Integer, nullable=True)


class BookingPlan(Base):
    __tablename__ = "booking_plans"
    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(Integer, index=True, nullable=False)
    training_id = Column(Integer, index=True, nullable=False)
    training_name = Column(String, nullable=False, default="Тренировка")
    training_start = Column(DateTime, nullable=False)
    booking_open = Column(DateTime, nullable=False)
    status = Column(String, nullable=False, default="waiting")
    last_error = Column(String, nullable=True)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    created_at = Column(DateTime, default=func.now())
    __table_args__ = (UniqueConstraint("telegram_id", "training_id", name="uq_booking_user_training"),)


class NotificationSettings(Base):
    __tablename__ = "notification_settings"
    telegram_id = Column(Integer, primary_key=True)
    training_reminders = Column(Boolean, nullable=False, default=True)
    free_spot_notifications = Column(Boolean, nullable=False, default=True)
    auto_book_free_spot = Column(Boolean, nullable=False, default=False)
    timezone = Column(String, nullable=False, default="Europe/Moscow")


class ReminderLog(Base):
    __tablename__ = "reminder_logs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(Integer, nullable=False, index=True)
    training_id = Column(Integer, nullable=False, index=True)
    training_start = Column(DateTime, nullable=False)
    sent_at = Column(DateTime, default=func.now())
    __table_args__ = (UniqueConstraint("telegram_id", "training_id", "training_start", name="uq_reminder"),)


def _add_missing_columns(table, columns):
    inspector = inspect(engine)
    existing = {c["name"] for c in inspector.get_columns(table)} if table in inspector.get_table_names() else set()
    for name, ddl in columns.items():
        if name not in existing:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


Base.metadata.create_all(bind=engine)

# Backward-compatible migration for the previous version.
_add_missing_columns("user_sessions", {
    "last_seen_at": "DATETIME",
})
_add_missing_columns("spot_trackers", {
    "auto_book": "BOOLEAN NOT NULL DEFAULT 0",
    "created_at": "DATETIME",
})
_add_missing_columns("allowed_users", {
    "last_seen_at": "DATETIME",
    "granted_by": "INTEGER",
})

# PHPSESSID is intentionally no longer used. On modern SQLite we remove it;
# on older SQLite it may remain physically, but the application never reads it.
inspector = inspect(engine)
if "user_sessions" in inspector.get_table_names():
    columns = {c["name"] for c in inspector.get_columns("user_sessions")}
    if "phpsessid" in columns:
        try:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE user_sessions DROP COLUMN phpsessid"))
        except Exception:
            pass


def get_db():
    return SessionLocal()
