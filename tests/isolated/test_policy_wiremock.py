"""Лаба 5: Service -> Repository -> PostgreSQL (Testcontainers) + реальний HTTP до WireMock (Testcontainers)."""
import json
import socket

import psycopg
import pytest

from rental.http_rental_policy import HttpRentalPolicy, RentalPolicyError
from rental.models import RentalRequest, RentalStatus
from rental.repositories import PostgresRentalRepository
from rental.service import RentalRejectedError, RentalService, ResourceNotFoundError


def make_request(eq, reference="REF-1", quantity=1, days=1, premium=False):
    return RentalRequest(reference, eq.id, quantity, days, premium)


def assert_nothing_saved(count_rows):
    """У PostgreSQL немає ні оренди, ні позиції."""
    assert count_rows("rentals") == 0
    assert count_rows("rental_items") == 0


def assert_one_policy_call(calls, eq, quantity, days):
    """Рівно один POST /rental-policy з очікуваним JSON у тілі."""
    assert len(calls) == 1
    call = calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "/rental-policy"
    assert call["headers"]["content-type"].startswith("application/json")
    assert json.loads(call["body"]) == {"equipmentId": eq.id, "quantity": quantity, "days": days}


# ===============================================================
# Позитивний сценарій: WireMock дозволяє
# ===============================================================
def test_rent_allowed_by_wiremock_creates_rental(wiremock, service, equipment_repo, make_equipment,
                                                 count_rows, postgres_options):
    wiremock.allow()  # відповідь: 200 {"allowed": true}
    eq = make_equipment("EXCAVATOR-01", 1000, 5)

    result = service.rent(make_request(eq, "REF-HTTP-OK", quantity=2, days=7, premium=True))

    # --- HTTP: один запит з правильним методом, шляхом і тілом ---
    assert_one_policy_call(wiremock.requests(expected=1), eq, quantity=2, days=7)

    # --- PostgreSQL: читаємо через НОВЕ з'єднання, щоб побачити реально збережений стан ---
    with psycopg.connect(**postgres_options) as other:
        stored = PostgresRentalRepository(other).find_by_id(result.id)
    assert stored == result
    assert stored.reference == "REF-HTTP-OK"
    assert stored.status == RentalStatus.ACTIVE
    assert stored.premium is True and stored.days == 7
    assert stored.total == 11900  # 1000*2*7 = 14000, знижка 15% = 2100
    assert stored.late_fee == 0
    assert stored.item.equipment_id == eq.id
    assert stored.item.quantity == 2 and stored.item.daily_rate == 1000
    assert (count_rows("rentals"), count_rows("rental_items")) == (1, 1)
    assert equipment_repo.find_by_id(eq.id) == eq  # залишок не змінився


# ===============================================================
# Персональне правило при реальному HTTP
# ===============================================================
@pytest.mark.parametrize("premium, days, expected_total", [
    pytest.param(True, 4, 4000, id="premium_4_days_no_discount"),
    pytest.param(True, 5, 4250, id="premium_5_days_discount"),
    pytest.param(False, 5, 5000, id="regular_5_days_no_discount"),
])
def test_premium_discount_with_http_policy(wiremock, service, rental_repo, make_equipment,
                                           premium, days, expected_total):
    wiremock.allow()
    eq = make_equipment("RULE-01", 1000, 5)

    saved = service.rent(make_request(eq, "REF-RULE", quantity=1, days=days, premium=premium))

    assert rental_repo.find_by_id(saved.id).total == expected_total
    assert rental_repo.find_by_id(saved.id).premium is premium
    assert_one_policy_call(wiremock.requests(expected=1), eq, quantity=1, days=days)


# ===============================================================
# Зовнішня бізнес-відмова: HTTP 200 {"allowed": false}
# ===============================================================
def test_rent_denied_by_wiremock_saves_nothing(wiremock, service, make_equipment, count_rows):
    wiremock.deny()
    eq = make_equipment("DENY-01", 1000, 5)

    with pytest.raises(RentalRejectedError, match="Оренду не дозволено"):
        service.rent(make_request(eq, "REF-DENY", quantity=3, days=2))

    # --- запит до зовнішнього сервісу був, параметри правильні ---
    assert_one_policy_call(wiremock.requests(expected=1), eq, quantity=3, days=2)
    # --- у PostgreSQL нічого немає ---
    assert_nothing_saved(count_rows)


# ===============================================================
# Недоступність зовнішнього сервісу: HTTP-помилки
# ===============================================================
@pytest.mark.parametrize("status", [400, 404, 500, 503, 201, 204, 302])
def test_rent_http_error_status_saves_nothing(wiremock, service, make_equipment, count_rows, status):
    headers = {"Location": "/rental-policy-other"} if status == 302 else None
    wiremock.stub_policy(status=status, body=None if status == 204 else {"error": "x"}, headers=headers)
    eq = make_equipment("ERR-01", 1000, 5)

    with pytest.raises(RentalPolicyError):
        service.rent(make_request(eq, "REF-ERR", quantity=1, days=1))

    # --- рівно один запит (без перенаправлень і повторів), оренда не створена ---
    assert_one_policy_call(wiremock.requests(expected=1), eq, quantity=1, days=1)
    assert_nothing_saved(count_rows)


@pytest.mark.parametrize("body", [
    pytest.param("", id="empty_body"),
    pytest.param("not json", id="not_json"),
    pytest.param("{}", id="no_allowed_field"),
    pytest.param('{"allowed": "true"}', id="allowed_string"),
    pytest.param('{"allowed": 1}', id="allowed_int"),
    pytest.param('{"allowed": null}', id="allowed_null"),
    pytest.param("[]", id="json_list"),
])
def test_rent_invalid_policy_body_saves_nothing(wiremock, service, make_equipment, count_rows, body):
    wiremock.stub_policy(status=200, body=body)
    eq = make_equipment("BODY-01", 1000, 5)

    with pytest.raises(RentalPolicyError):
        service.rent(make_request(eq, "REF-BODY"))

    assert_one_policy_call(wiremock.requests(expected=1), eq, quantity=1, days=1)
    assert_nothing_saved(count_rows)


def test_rent_policy_timeout_saves_nothing(wiremock, equipment_repo, rental_repo, make_equipment, count_rows):
    # --- WireMock відповідає із затримкою 1.5 с, а політика чекає лише 0.5 с ---
    wiremock.stub_policy(body={"allowed": True}, delay_ms=1500)
    slow = RentalService(equipment_repo, rental_repo, HttpRentalPolicy(wiremock.base_url, timeout=0.5))
    eq = make_equipment("SLOW-01", 1000, 5)

    with pytest.raises(RentalPolicyError):
        slow.rent(make_request(eq, "REF-SLOW"))

    assert_nothing_saved(count_rows)
    # чекаємо, поки WireMock допише запит у журнал (щоб він не потрапив у наступний тест)
    assert len(wiremock.requests(expected=1, wait=10)) == 1


def test_rent_policy_unreachable_saves_nothing(equipment_repo, rental_repo, make_equipment, count_rows):
    # --- порт, на якому ніхто не слухає (береться вільний із системи) ---
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    offline = RentalService(equipment_repo, rental_repo, HttpRentalPolicy(f"http://127.0.0.1:{closed_port}", timeout=1))
    eq = make_equipment("OFF-01", 1000, 5)

    with pytest.raises(RentalPolicyError):
        offline.rent(make_request(eq, "REF-OFF"))

    assert_nothing_saved(count_rows)


# ===============================================================
# Локальна відмова: HTTP-запитів немає взагалі
# ===============================================================
@pytest.mark.parametrize("case", ["not_enough_stock", "invalid_days", "invalid_reference", "equipment_missing"])
def test_local_rejection_makes_no_http_request(wiremock, service, make_equipment, count_rows, case):
    wiremock.allow()  # WireMock готовий дозволити, тож відсутність запиту - заслуга локальної перевірки
    eq = make_equipment("LOCAL-01", 1000, 5)

    if case == "not_enough_stock":
        with pytest.raises(RentalRejectedError, match="Недостатньо обладнання"):
            service.rent(make_request(eq, "REF-LOCAL", quantity=6))
    elif case == "invalid_days":
        with pytest.raises(ValueError):
            service.rent(make_request(eq, "REF-LOCAL", days=31))
    elif case == "invalid_reference":
        with pytest.raises(ValueError):
            service.rent(make_request(eq, "   "))
    else:
        with pytest.raises(ResourceNotFoundError):
            service.rent(RentalRequest("REF-LOCAL", 999999, 1, 1, False))

    assert wiremock.requests() == []  # local rejection -> HTTP request = 0
    assert_nothing_saved(count_rows)


# ===============================================================
# Повернення не звертається до зовнішнього сервісу
# ===============================================================
def test_return_does_not_call_policy(wiremock, service, rental_repo, make_equipment):
    wiremock.allow()
    eq = make_equipment("RET-01", 1000, 5)
    rented = service.rent(make_request(eq, "REF-RET", quantity=1, days=5, premium=True))
    assert len(wiremock.requests(expected=1)) == 1

    # --- чистимо журнал і ламаємо політику: якби повернення її викликало, воно б упало ---
    wiremock.reset()
    wiremock.stub_policy(status=503)

    returned = service.return_rental(rented.id, overdue_days=10)

    assert wiremock.requests() == []
    stored = rental_repo.find_by_id(rented.id)
    assert stored == returned
    assert stored.status == RentalStatus.RETURNED
    assert stored.total == 4250 and stored.late_fee == 0


# ===============================================================
# Порядок: спершу зовнішня перевірка, потім запис у БД
# ===============================================================
def test_duplicate_reference_after_policy_call(wiremock, service, rental_repo, make_equipment, count_rows):
    wiremock.allow()
    eq = make_equipment("DUP-01", 1000, 5)
    first = service.rent(make_request(eq, "REF-DUP"))

    with pytest.raises(psycopg.errors.UniqueViolation):
        service.rent(make_request(eq, "REF-DUP", quantity=2))

    assert len(wiremock.requests(expected=2)) == 2  # політику питали обидва рази
    assert rental_repo.find_rentals(eq.id, RentalStatus.ACTIVE) == [first]
    assert (count_rows("rentals"), count_rows("rental_items")) == (1, 1)


# ===============================================================
# Ізоляція тестів
# ===============================================================
def test_every_test_starts_clean(wiremock, count_rows):
    assert count_rows("equipment") == 0
    assert_nothing_saved(count_rows)
    assert wiremock.mappings() == []
    assert wiremock.requests() == []


@pytest.mark.parametrize("run", [1, 2])
def test_same_data_in_two_tests_does_not_clash(wiremock, service, make_equipment, run):
    # --- обидва запуски використовують той самий код обладнання і код оренди ---
    wiremock.allow()
    eq = make_equipment("SHARED-CODE", 1000, 5)

    saved = service.rent(make_request(eq, "SHARED-REF"))

    assert saved.id == 1  # лічильник id скинутий, тест бачить порожню БД
    assert len(wiremock.requests(expected=1)) == 1  # журнал WireMock теж порожній на старті
