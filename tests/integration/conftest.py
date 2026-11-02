"""Спільні фікстури для інтеграційних тестів: Service -> Repository -> PostgreSQL."""
from unittest.mock import MagicMock

import psycopg
import pytest

from rental.database import connect
from rental.models import Equipment
from rental.ports import RentalPolicy
from rental.repositories import PostgresEquipmentRepository, PostgresRentalRepository
from rental.service import RentalService

TABLES = ("equipment", "rentals", "rental_items")


# ---------------------------------------------------------------
# Перевірка середовища (виконується один раз на весь запуск)
# ---------------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def database_ready():
    """Є підключення, це тестова БД і міграцію вже застосовано."""
    try:
        with connect() as db:
            name = db.info.dbname
            missing = [
                table for table in TABLES
                if db.execute("SELECT to_regclass(%s)", (f"public.{table}",)).fetchone()[0] is None
            ]
    except psycopg.OperationalError as error:
        pytest.exit(
            "Немає підключення до PostgreSQL. Перевір DB_HOST, DB_PORT, DB_NAME, DB_USER, "
            f"DB_PASSWORD і що сервер запущений.\nДеталі: {error}",
            returncode=2,
        )

    # захист: тести роблять TRUNCATE, тому працюємо лише з БД, де в назві є "test"
    if "test" not in name.lower():
        pytest.exit(f"БД '{name}' не схожа на тестову (в назві немає 'test'). Зупиняюсь.", returncode=2)
    if missing:
        pytest.exit(f"У БД '{name}' немає таблиць {missing}. Застосуй migrations/V1__create_tables.sql.", returncode=2)


# ---------------------------------------------------------------
# Ізоляція: чистий стан БД до і після кожного тесту
# ---------------------------------------------------------------
def _clean(connection):
    connection.execute("TRUNCATE rental_items, rentals, equipment RESTART IDENTITY CASCADE")


@pytest.fixture
def db():
    """Реальне з'єднання (autocommit=True), яке віддає rental.database.connect."""
    with connect() as connection:
        _clean(connection)
        yield connection
        _clean(connection)


# ---------------------------------------------------------------
# Межа інтеграції: реальні репозиторії + реальний сервіс
# ---------------------------------------------------------------
@pytest.fixture
def equipment_repo(db):
    return PostgresEquipmentRepository(db)


@pytest.fixture
def rental_repo(db):
    return PostgresRentalRepository(db)


@pytest.fixture
def policy():
    """Зовнішня залежність за межею інтеграції: Mock із заданою відповіддю (Stub) і перевіркою викликів."""
    mock = MagicMock(spec=RentalPolicy)
    mock.is_allowed.return_value = True
    return mock


@pytest.fixture
def service(equipment_repo, rental_repo, policy):
    return RentalService(equipment_repo, rental_repo, policy)


# ---------------------------------------------------------------
# Допоміжні функції для підготовки даних
# ---------------------------------------------------------------
@pytest.fixture
def make_equipment(equipment_repo):
    """Фабрика: створює обладнання в БД. Без коду дає унікальний EQ-n."""
    counter = {"n": 0}

    def make(code=None, daily_rate=1000, stock=5):
        counter["n"] += 1
        return equipment_repo.create(Equipment(0, code or f"EQ-{counter['n']}", daily_rate, stock))

    return make


@pytest.fixture
def count_rows(db):
    """Рахує рядки в таблиці прямим SQL (щоб не покладатися лише на репозиторій)."""
    def count(table):
        assert table in TABLES
        return db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    return count
