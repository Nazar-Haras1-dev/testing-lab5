"""Repository <-> Database: репозиторії без підміни, реальна PostgreSQL."""
from dataclasses import replace

import psycopg
import pytest

from rental.models import Equipment, RentalItem, RentalOrder, RentalStatus
from rental.service import RentalRejectedError


def build_order(equipment_id, reference="R-1", days=3, quantity=1, rate=1000, total=3000, premium=False):
    """Оренда для прямого збереження (ідентифікатори 0 - їх видасть БД)."""
    item = RentalItem(0, equipment_id, quantity, rate)
    return RentalOrder(0, reference, days, premium, total, 0, RentalStatus.ACTIVE, item)


# ===============================================================
# Equipment
# ===============================================================
def test_equipment_create_and_find_repositoryState(equipment_repo):
    # --- збереження і повторне читання ---
    saved = equipment_repo.create(Equipment(777, "DRILL-01", 1000, 5))

    assert saved.id != 777 and saved.id >= 1  # id видає БД, вхідний ігнорується
    fetched = equipment_repo.find_by_id(saved.id)
    assert fetched == Equipment(saved.id, "DRILL-01", 1000, 5)
    assert fetched == saved

    # --- відсутній запис ---
    assert equipment_repo.find_by_id(saved.id + 1000) is None


def test_equipment_update_repositoryState(equipment_repo):
    saved = equipment_repo.create(Equipment(0, "SAW-01", 500, 2))

    # --- зміна даних ---
    changed = replace(saved, code="SAW-02", daily_rate=2500, stock=0)
    equipment_repo.update(changed)
    assert equipment_repo.find_by_id(saved.id) == changed

    # --- оновлення відсутнього обладнання -> ValueError, нових записів немає ---
    with pytest.raises(ValueError):
        equipment_repo.update(Equipment(424242, "GHOST", 500, 1))
    assert equipment_repo.find_by_id(424242) is None


def test_equipment_delete_repositoryState(equipment_repo):
    first = equipment_repo.create(Equipment(0, "DEL-01", 150, 1))
    second = equipment_repo.create(Equipment(0, "DEL-02", 150, 1))

    # --- видалення ---
    equipment_repo.delete(first.id)
    assert equipment_repo.find_by_id(first.id) is None

    # --- видалення відсутнього нічого не змінює ---
    equipment_repo.delete(first.id)
    assert equipment_repo.find_by_id(second.id) == second


def test_equipment_duplicate_code_rejected_repositoryState(equipment_repo, count_rows):
    equipment_repo.create(Equipment(0, "UNIQ-01", 1000, 1))

    # --- унікальність коду забезпечує БД ---
    with pytest.raises(psycopg.errors.UniqueViolation):
        equipment_repo.create(Equipment(0, "UNIQ-01", 2000, 9))
    assert count_rows("equipment") == 1


# ===============================================================
# Rental
# ===============================================================
def test_rental_create_and_find_repositoryState(equipment_repo, rental_repo, count_rows):
    eq = equipment_repo.create(Equipment(0, "PUMP-01", 1000, 5))
    order = replace(build_order(eq.id, "REF-REPO-1", days=4, quantity=2, rate=1000, total=8000, premium=True),
                    id=99, item=RentalItem(88, eq.id, 2, 1000))

    # --- збереження оренди разом з позицією ---
    saved = rental_repo.create(order)
    assert saved.id != 99 and saved.id >= 1  # вхідні id ігноруються
    assert saved.item.id != 88 and saved.item.id >= 1

    # --- повторне читання: маппінг рядків БД у доменні об'єкти ---
    fetched = rental_repo.find_by_id(saved.id)
    assert fetched == saved
    assert fetched.reference == "REF-REPO-1"
    assert fetched.days == 4 and fetched.premium is True
    assert fetched.total == 8000 and fetched.late_fee == 0
    assert fetched.status == RentalStatus.ACTIVE
    assert fetched.item.equipment_id == eq.id
    assert fetched.item.quantity == 2 and fetched.item.daily_rate == 1000
    assert (count_rows("rentals"), count_rows("rental_items")) == (1, 1)

    # --- відсутня оренда ---
    assert rental_repo.find_by_id(saved.id + 1000) is None


def test_rental_find_rentals_filters_repositoryState(equipment_repo, rental_repo):
    first = equipment_repo.create(Equipment(0, "FIL-A", 1000, 5))
    second = equipment_repo.create(Equipment(0, "FIL-B", 1000, 5))

    a1 = rental_repo.create(build_order(first.id, "A-1"))
    a2 = rental_repo.create(build_order(first.id, "A-2"))
    a3 = rental_repo.create(build_order(first.id, "A-3"))
    b1 = rental_repo.create(build_order(second.id, "B-1"))
    rental_repo.mark_returned(a3.id, 0)

    # --- обидва фільтри одночасно, порядок за зростанням id ---
    assert rental_repo.find_rentals(first.id, RentalStatus.ACTIVE) == [a1, a2]
    assert rental_repo.find_rentals(first.id, RentalStatus.RETURNED) == [replace(a3, status=RentalStatus.RETURNED)]
    assert rental_repo.find_rentals(second.id, RentalStatus.ACTIVE) == [b1]
    assert rental_repo.find_rentals(second.id, RentalStatus.RETURNED) == []

    # --- некоректні фільтри ---
    with pytest.raises(ValueError):
        rental_repo.find_rentals(0, RentalStatus.ACTIVE)
    with pytest.raises(ValueError):
        rental_repo.find_rentals(first.id, "Active")


def test_rental_mark_returned_repositoryState(equipment_repo, rental_repo):
    eq = equipment_repo.create(Equipment(0, "RET-REPO", 1000, 5))
    saved = rental_repo.create(build_order(eq.id, "REF-RET", total=3000))

    # --- зміна статусу і доплати ---
    rental_repo.mark_returned(saved.id, 150)
    fetched = rental_repo.find_by_id(saved.id)
    assert fetched.status == RentalStatus.RETURNED
    assert fetched.late_fee == 150
    assert fetched.total == 3000  # початкова сума не змінюється

    # --- повторне повернення і відсутня оренда -> RentalRejectedError, стан не змінюється ---
    with pytest.raises(RentalRejectedError):
        rental_repo.mark_returned(saved.id, 999)
    with pytest.raises(RentalRejectedError):
        rental_repo.mark_returned(saved.id + 1000, 0)
    assert rental_repo.find_by_id(saved.id).late_fee == 150


def test_rental_late_fee_above_total_rejected_repositoryState(equipment_repo, rental_repo):
    eq = equipment_repo.create(Equipment(0, "FEE-CHECK", 1000, 5))
    saved = rental_repo.create(build_order(eq.id, "REF-FEE", total=3000))

    # --- CHECK у БД: late_fee не може бути більшим за total ---
    with pytest.raises(psycopg.errors.CheckViolation):
        rental_repo.mark_returned(saved.id, 3001)
    assert rental_repo.find_by_id(saved.id).status == RentalStatus.ACTIVE


def test_rental_delete_and_restrict_repositoryState(equipment_repo, rental_repo, count_rows):
    eq = equipment_repo.create(Equipment(0, "CASC-01", 1000, 5))
    saved = rental_repo.create(build_order(eq.id, "REF-CASC"))

    # --- обладнання з позицією видалити не можна (ON DELETE RESTRICT) ---
    with pytest.raises(psycopg.errors.IntegrityError):
        equipment_repo.delete(eq.id)
    assert equipment_repo.find_by_id(eq.id) == eq

    # --- видалення оренди каскадно прибирає позицію ---
    rental_repo.delete(saved.id)
    assert rental_repo.find_by_id(saved.id) is None
    assert count_rows("rental_items") == 0

    # --- видалення відсутньої оренди нічого не змінює, тепер обладнання видаляється ---
    rental_repo.delete(saved.id)
    equipment_repo.delete(eq.id)
    assert equipment_repo.find_by_id(eq.id) is None


def test_rental_create_is_atomic_repositoryState(rental_repo, count_rows):
    # --- обладнання 999999 не існує: INSERT позиції падає, оренда теж не повинна лишитися ---
    with pytest.raises(psycopg.errors.IntegrityError):
        rental_repo.create(build_order(999999, "REF-ATOMIC"))

    assert count_rows("rentals") == 0
    assert count_rows("rental_items") == 0
