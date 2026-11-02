"""Service -> Repository -> Database: реальний сервіс, реальні репозиторії, реальна PostgreSQL.
Підміняється (Mock) лише зовнішня RentalPolicy."""
from dataclasses import replace

import psycopg
import pytest

from rental.database import connect
from rental.models import RentalRequest, RentalStatus
from rental.repositories import PostgresEquipmentRepository, PostgresRentalRepository
from rental.service import RentalRejectedError, ResourceNotFoundError


def make_request(eq, reference="REF-1", quantity=1, days=1, premium=False):
    return RentalRequest(reference, eq.id, quantity, days, premium)


def assert_nothing_saved(count_rows):
    """Відмова не залишає слідів у БД."""
    assert count_rows("rentals") == 0
    assert count_rows("rental_items") == 0


# ===============================================================
# Оформлення: позитивні сценарії
# ===============================================================
def test_rent_success_serviceState(service, equipment_repo, rental_repo, policy, make_equipment, count_rows):
    eq = make_equipment("PUMP-01", 2000, 5)

    result = service.rent(make_request(eq, "REF-SUCCESS", quantity=2, days=3))

    # --- що повернув сервіс ---
    assert result.status == RentalStatus.ACTIVE
    assert result.total == 12000  # 2000 * 2 * 3, без знижки

    # --- що реально лежить у БД ---
    stored = rental_repo.find_by_id(result.id)
    assert stored == result
    assert stored.reference == "REF-SUCCESS"
    assert stored.days == 3 and stored.premium is False
    assert stored.total == 12000 and stored.late_fee == 0
    assert stored.item.equipment_id == eq.id
    assert stored.item.quantity == 2 and stored.item.daily_rate == 2000
    assert rental_repo.find_rentals(eq.id, RentalStatus.ACTIVE) == [stored]
    assert (count_rows("rentals"), count_rows("rental_items")) == (1, 1)

    # --- залишок не змінюється ---
    assert equipment_repo.find_by_id(eq.id) == eq

    # --- зовнішня перевірка викликана рівно один раз ---
    policy.is_allowed.assert_called_once_with(eq, 2, 3)


def test_rent_quantity_equals_stock_serviceState(service, equipment_repo, rental_repo, make_equipment):
    eq = make_equipment("EDGE-STOCK", 1000, 3)

    # --- межа: кількість дорівнює залишку - дозволено ---
    saved = service.rent(make_request(eq, "REF-EDGE", quantity=3, days=2))

    assert rental_repo.find_by_id(saved.id).total == 6000
    assert equipment_repo.find_by_id(eq.id).stock == 3


def test_rent_boundary_values_serviceState(service, rental_repo, make_equipment):
    # --- верхні межі: 20 одиниць, 30 днів, максимальна ціна, код із 40 символів ---
    big = make_equipment("BIG", 1000000, 20)
    top = service.rent(make_request(big, "R" * 40, quantity=20, days=30))
    assert rental_repo.find_by_id(top.id).total == 600000000
    assert rental_repo.find_by_id(top.id).reference == "R" * 40

    # --- нижні межі: 1 одиниця, 1 день, мінімальна ціна, код із 1 символу ---
    small = make_equipment("SMALL", 100, 1)
    bottom = service.rent(make_request(small, "R", quantity=1, days=1))
    assert rental_repo.find_by_id(bottom.id).total == 100


# ===============================================================
# Персональне правило: знижка 15% для premium від 5 днів
# ===============================================================
@pytest.mark.parametrize("premium, days, rate, quantity, expected_total", [
    pytest.param(True, 1, 1000, 1, 1000, id="premium_1_day_no_discount"),
    pytest.param(True, 4, 1000, 1, 4000, id="premium_4_days_no_discount"),
    pytest.param(True, 5, 1000, 1, 4250, id="premium_5_days_discount"),
    pytest.param(False, 5, 1000, 1, 5000, id="regular_5_days_no_discount"),
    pytest.param(False, 30, 1000, 2, 60000, id="regular_30_days_no_discount"),
    pytest.param(True, 5, 101, 1, 430, id="discount_rounded_down"),
    pytest.param(True, 7, 333, 3, 5945, id="discount_rounded_down_many_units"),
    pytest.param(True, 30, 1000000, 20, 510000000, id="premium_max_values"),
])
def test_premium_discount_serviceState(service, rental_repo, make_equipment,
                                       premium, days, rate, quantity, expected_total):
    eq = make_equipment("RULE-01", rate, 20)

    saved = service.rent(make_request(eq, "REF-RULE", quantity=quantity, days=days, premium=premium))

    # --- сума і преміальність перевіряються в БД ---
    stored = rental_repo.find_by_id(saved.id)
    assert stored.total == expected_total
    assert stored.premium is premium
    assert stored.late_fee == 0
    assert stored.item.daily_rate == rate  # знімок ціни без знижки


# ===============================================================
# Оформлення: відмови (в БД нічого не з'являється)
# ===============================================================
def test_rent_rejected_not_enough_stock_serviceState(service, rental_repo, policy, make_equipment, count_rows):
    eq = make_equipment("NO-STOCK", 1000, 1)

    with pytest.raises(RentalRejectedError, match="Недостатньо обладнання"):
        service.rent(make_request(eq, "REF-FAIL", quantity=5))

    assert rental_repo.find_rentals(eq.id, RentalStatus.ACTIVE) == []
    assert_nothing_saved(count_rows)
    policy.is_allowed.assert_not_called()  # до зовнішньої перевірки справа не дійшла


def test_rent_rejected_by_policy_serviceState(service, rental_repo, policy, make_equipment, count_rows):
    eq = make_equipment("POLICY-NO", 1000, 5)
    policy.is_allowed.return_value = False

    with pytest.raises(RentalRejectedError, match="Оренду не дозволено"):
        service.rent(make_request(eq, "REF-POL", quantity=2, days=3))

    assert rental_repo.find_rentals(eq.id, RentalStatus.ACTIVE) == []
    assert_nothing_saved(count_rows)
    policy.is_allowed.assert_called_once_with(eq, 2, 3)


def test_rent_policy_exception_propagates_serviceState(service, policy, make_equipment, count_rows):
    eq = make_equipment("POLICY-ERR", 1000, 5)
    policy.is_allowed.side_effect = ConnectionError("зовнішній сервіс недоступний")

    # --- виняток зовнішньої перевірки передається виклику без змін ---
    with pytest.raises(ConnectionError):
        service.rent(make_request(eq, "REF-ERR"))

    assert_nothing_saved(count_rows)
    policy.is_allowed.assert_called_once()


@pytest.mark.parametrize("answer", [1, 0, "True", None], ids=["int_1", "int_0", "str", "none"])
def test_rent_policy_non_bool_answer_serviceState(service, policy, make_equipment, count_rows, answer):
    eq = make_equipment("POLICY-BAD", 1000, 5)
    policy.is_allowed.return_value = answer

    # --- відповідь не bool -> ValueError ---
    with pytest.raises(ValueError):
        service.rent(make_request(eq, "REF-BAD"))

    assert_nothing_saved(count_rows)
    policy.is_allowed.assert_called_once()


def test_rent_equipment_not_found_serviceState(service, policy, count_rows):
    request = RentalRequest("REF-GHOST", 999999, 1, 1, False)

    with pytest.raises(ResourceNotFoundError):
        service.rent(request)

    assert_nothing_saved(count_rows)
    policy.is_allowed.assert_not_called()


@pytest.mark.parametrize("override", [
    pytest.param({"quantity": 0}, id="quantity_0"),
    pytest.param({"quantity": 21}, id="quantity_21"),
    pytest.param({"quantity": True}, id="quantity_bool"),
    pytest.param({"quantity": 1.0}, id="quantity_float"),
    pytest.param({"days": 0}, id="days_0"),
    pytest.param({"days": 31}, id="days_31"),
    pytest.param({"days": True}, id="days_bool"),
    pytest.param({"premium": "yes"}, id="premium_str"),
    pytest.param({"premium": 1}, id="premium_int"),
    pytest.param({"reference": ""}, id="reference_empty"),
    pytest.param({"reference": "   "}, id="reference_spaces"),
    pytest.param({"reference": "R" * 41}, id="reference_41_chars"),
    pytest.param({"equipment_id": 0}, id="equipment_id_0"),
    pytest.param({"equipment_id": -1}, id="equipment_id_negative"),
    pytest.param({"equipment_id": True}, id="equipment_id_bool"),
    pytest.param({"equipment_id": 2 ** 63}, id="equipment_id_too_big"),
])
def test_rent_invalid_request_serviceState(service, policy, make_equipment, count_rows, override):
    eq = make_equipment("VALID-EQ", 1000, 20)
    data = dict(reference="REF-BAD", equipment_id=eq.id, quantity=1, days=1, premium=False)
    data.update(override)

    with pytest.raises(ValueError):
        service.rent(RentalRequest(**data))

    assert_nothing_saved(count_rows)
    policy.is_allowed.assert_not_called()


def test_rent_not_a_request_serviceState(service, policy, count_rows):
    with pytest.raises(ValueError):
        service.rent(None)

    assert_nothing_saved(count_rows)
    policy.is_allowed.assert_not_called()


def test_rent_duplicate_reference_serviceState(service, rental_repo, make_equipment, count_rows):
    eq = make_equipment("DUP-01", 1000, 5)
    first = service.rent(make_request(eq, "REF-DUP", quantity=1, days=2))

    # --- код оренди унікальний: помилку БД сервіс передає виклику ---
    with pytest.raises(psycopg.errors.UniqueViolation):
        service.rent(make_request(eq, "REF-DUP", quantity=2, days=4))

    # --- перша оренда на місці, друга не з'явилась (ні оренда, ні позиція) ---
    assert rental_repo.find_rentals(eq.id, RentalStatus.ACTIVE) == [first]
    assert (count_rows("rentals"), count_rows("rental_items")) == (1, 1)


def test_rent_price_snapshot_serviceState(service, equipment_repo, rental_repo, make_equipment):
    eq = make_equipment("SNAP-01", 1000, 5)
    first = service.rent(make_request(eq, "REF-SNAP-1", quantity=2, days=3))

    # --- ціну обладнання змінили вже після оформлення ---
    equipment_repo.update(replace(eq, daily_rate=5000, stock=1))

    old = rental_repo.find_by_id(first.id)
    assert old.total == 6000
    assert old.item.daily_rate == 1000  # знімок ціни не змінився

    # --- нова оренда вже рахується за новою ціною ---
    second = service.rent(make_request(eq, "REF-SNAP-2", quantity=1, days=1))
    assert rental_repo.find_by_id(second.id).total == 5000
    assert rental_repo.find_by_id(second.id).item.daily_rate == 5000


# ===============================================================
# Повернення
# ===============================================================
@pytest.mark.parametrize("overdue_days", [0, 1, 36500])
def test_return_rental_serviceState(service, equipment_repo, rental_repo, policy, make_equipment, overdue_days):
    eq = make_equipment("RET-01", 1000, 5)
    rented = service.rent(make_request(eq, "REF-RETURN", quantity=2, days=5, premium=True))
    assert rented.total == 8500  # 2*1000*5 = 10000, знижка 1500
    policy.is_allowed.reset_mock()

    returned = service.return_rental(rented.id, overdue_days)

    # --- стан у БД: статус змінено, total той самий, доплата нульова (варіант без доплати) ---
    stored = rental_repo.find_by_id(rented.id)
    assert stored == returned
    assert stored.status == RentalStatus.RETURNED
    assert stored.total == 8500
    assert stored.late_fee == 0
    assert stored.total + stored.late_fee == 8500  # сукупна сума
    assert stored.item == rented.item
    assert equipment_repo.find_by_id(eq.id) == eq  # залишок не змінився
    policy.is_allowed.assert_not_called()  # при поверненні зовнішня перевірка не викликається


def test_return_twice_rejected_serviceState(service, rental_repo, make_equipment):
    eq = make_equipment("RET-TWICE", 1000, 5)
    rented = service.rent(make_request(eq, "REF-TWICE"))
    returned = service.return_rental(rented.id, 0)

    with pytest.raises(RentalRejectedError):
        service.return_rental(rented.id, 0)

    assert rental_repo.find_by_id(rented.id) == returned


def test_return_missing_rental_serviceState(service, count_rows):
    with pytest.raises(ResourceNotFoundError):
        service.return_rental(424242, 0)

    assert_nothing_saved(count_rows)


@pytest.mark.parametrize("rental_id, overdue_days", [
    pytest.param(None, -1, id="overdue_negative"),
    pytest.param(None, 36501, id="overdue_36501"),
    pytest.param(None, True, id="overdue_bool"),
    pytest.param(None, "5", id="overdue_str"),
    pytest.param(0, 0, id="id_0"),
    pytest.param(-1, 0, id="id_negative"),
    pytest.param(True, 0, id="id_bool"),
])
def test_return_invalid_input_serviceState(service, rental_repo, make_equipment, rental_id, overdue_days):
    eq = make_equipment("RET-BAD", 1000, 5)
    rented = service.rent(make_request(eq, "REF-RET-BAD"))

    with pytest.raises(ValueError):
        service.return_rental(rented.id if rental_id is None else rental_id, overdue_days)

    # --- оренда лишилась активною ---
    assert rental_repo.find_by_id(rented.id) == rented


# ===============================================================
# Фактичний стан БД після операцій (через нове з'єднання)
# ===============================================================
def test_rent_and_return_persisted_state_persistedState(service, make_equipment):
    eq = make_equipment("PERSIST-01", 1000, 5)

    saved = service.rent(make_request(eq, "REF-PERSIST", quantity=2, days=5, premium=True))

    # --- нове з'єднання і нові репозиторії: дані справді закомічені в БД ---
    with connect() as other:
        fresh_rentals = PostgresRentalRepository(other)
        fresh_equipment = PostgresEquipmentRepository(other)
        stored = fresh_rentals.find_by_id(saved.id)
        assert stored == saved
        assert stored.status == RentalStatus.ACTIVE
        assert stored.total == 8500 and stored.late_fee == 0
        assert fresh_equipment.find_by_id(eq.id) == eq

    returned = service.return_rental(saved.id, overdue_days=10)

    with connect() as other:
        fresh_rentals = PostgresRentalRepository(other)
        fresh_equipment = PostgresEquipmentRepository(other)
        stored = fresh_rentals.find_by_id(saved.id)
        assert stored == returned
        assert stored.status == RentalStatus.RETURNED
        assert stored.total == 8500  # початкова сума не змінилась
        assert stored.late_fee == 0
        assert fresh_rentals.find_rentals(eq.id, RentalStatus.ACTIVE) == []
        assert fresh_rentals.find_rentals(eq.id, RentalStatus.RETURNED) == [stored]
        assert fresh_equipment.find_by_id(eq.id) == eq  # залишок так і не змінився
