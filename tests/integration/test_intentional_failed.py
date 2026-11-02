"""Контрольна вимога методички: у фінальному наборі лишається один навмисно FAILED тест.
Він падає завжди, незалежно від поведінки SUT, і його не потрібно виправляти.
Щоб прибрати його зі свого запуску, достатньо видалити цей файл."""
from rental.models import RentalRequest


def test_intentional_failure_control(service, rental_repo, make_equipment):
    eq = make_equipment("CONTROL-01", 1000, 5)
    saved = service.rent(RentalRequest("REF-CONTROL", eq.id, 1, 1, False))

    stored = rental_repo.find_by_id(saved.id)

    # --- умова неможлива за визначенням, тому тест завжди FAILED ---
    assert stored.total == stored.total + 1, "Навмисно невдалий контрольний тест (вимога методички)"
