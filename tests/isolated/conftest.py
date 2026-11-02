"""Лаба 5: ізольоване середовище. PostgreSQL і WireMock стартують у контейнерах самі,
параметри підключення беруться з працюючих контейнерів (жодних фіксованих портів чи паролів)."""
import time
from pathlib import Path

import psycopg
import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.postgres import PostgresContainer

from rental.http_rental_policy import HttpRentalPolicy
from rental.models import Equipment
from rental.repositories import PostgresEquipmentRepository, PostgresRentalRepository
from rental.service import RentalService
from .wiremock_client import WireMock

POSTGRES_IMAGE = "postgres:17.6-alpine"
WIREMOCK_IMAGE = "wiremock/wiremock:3.9.1"
MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "V1__create_tables.sql"
TABLES = ("equipment", "rentals", "rental_items")

DOCKER_HINT = (
    "Не вдалося запустити контейнер. Потрібен Docker-сумісний runtime (Docker Desktop, Colima або Podman), "
    "який уже запущений. Перевір командою `docker ps`."
)


def _start_or_exit(container):
    """Старт контейнера; якщо Docker недоступний, зупиняємось з зрозумілим поясненням."""
    try:
        container.start()
    except Exception as error:  # noqa: BLE001 - показуємо першопричину як є
        pytest.exit(f"{DOCKER_HINT}\nДеталі: {error!r}", returncode=2)


# ===============================================================
# PostgreSQL у контейнері (один на весь запуск)
# ===============================================================
@pytest.fixture(scope="session")
def postgres_options():
    container = PostgresContainer(POSTGRES_IMAGE)
    _start_or_exit(container)
    try:
        # --- параметри підключення беремо з працюючого контейнера ---
        options = dict(
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(5432)),  # порт видає Docker, він щоразу інший
            user=container.username,
            password=container.password,
            dbname=container.dbname,
            autocommit=True,
        )
        print(f"\n[testcontainers] PostgreSQL: {options['host']}:{options['port']} db={options['dbname']}")

        # --- чекаємо, поки сервер реально приймає з'єднання ---
        deadline = time.monotonic() + 60
        while True:
            try:
                with psycopg.connect(**options):
                    break
            except psycopg.OperationalError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)

        # --- міграція з SUT застосовується автоматично, один раз перед усіма тестами ---
        with psycopg.connect(**options) as db:
            db.execute(MIGRATION.read_text(encoding="utf-8"))

        yield options
    finally:
        container.stop()


# ===============================================================
# WireMock у контейнері (один на весь запуск)
# ===============================================================
@pytest.fixture(scope="session")
def wiremock_url():
    container = DockerContainer(WIREMOCK_IMAGE).with_exposed_ports(8080)
    _start_or_exit(container)
    try:
        # --- адреса теж із працюючого контейнера (динамічний порт) ---
        url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(8080)}"
        print(f"\n[testcontainers] WireMock: {url}")

        deadline = time.monotonic() + 60
        client = WireMock(url)
        while not client.is_ready():
            if time.monotonic() > deadline:
                raise RuntimeError("WireMock не став готовим за 60 секунд")
            time.sleep(0.5)

        yield url
    finally:
        container.stop()


# ===============================================================
# Ізоляція: чистий стан PostgreSQL і WireMock перед кожним тестом
# ===============================================================
def _clean_database(connection):
    connection.execute("TRUNCATE rental_items, rentals, equipment RESTART IDENTITY CASCADE")


@pytest.fixture
def db(postgres_options):
    with psycopg.connect(**postgres_options) as connection:
        _clean_database(connection)
        yield connection
        _clean_database(connection)


@pytest.fixture
def wiremock(wiremock_url):
    client = WireMock(wiremock_url)
    client.reset()  # прибрати старі відповіді і журнал запитів
    yield client
    client.reset()


# ===============================================================
# SUT: реальні репозиторії + реальна HTTP-політика + реальний сервіс
# ===============================================================
@pytest.fixture
def equipment_repo(db):
    return PostgresEquipmentRepository(db)


@pytest.fixture
def rental_repo(db):
    return PostgresRentalRepository(db)


@pytest.fixture
def policy(wiremock):
    return HttpRentalPolicy(wiremock.base_url, timeout=2)


@pytest.fixture
def service(equipment_repo, rental_repo, policy):
    return RentalService(equipment_repo, rental_repo, policy)


# ===============================================================
# Допоміжні функції для початкових даних
# ===============================================================
@pytest.fixture
def make_equipment(equipment_repo):
    counter = {"n": 0}

    def make(code=None, daily_rate=1000, stock=5):
        counter["n"] += 1
        return equipment_repo.create(Equipment(0, code or f"EQ-{counter['n']}", daily_rate, stock))

    return make


@pytest.fixture
def count_rows(db):
    def count(table):
        assert table in TABLES
        return db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    return count
