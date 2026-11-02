import pytest
from unittest.mock import Mock, patch
from rental.service import integer, validate

from rental.service import RentalService, ResourceNotFoundError, RentalRejectedError
from rental.models import RentalRequest, RentalOrder, RentalItem, RentalStatus
from rental.ports import EquipmentRepository, RentalRepository, RentalPolicy

try:
    from rental.models import Equipment
except ImportError:
    pass
# 1. КОРЕКТНЕ ВИКОНАННЯ ОПЕРАЦІЇ (HAPPY PATH / POSITIVE SCENARIO)

def test_rent_success_creates_active_rental_order(rental_service, mock_equipment_repo, mock_rental_repo, mock_policy):
    """
    Перевірка успішного оформлення оренди з розрахунком вартості та збереженням у репозиторій.
    """
    # Arrange (Підготовка даних)
    equipment_id = 10
    equipment = Equipment(id=equipment_id, code="DRILL-01", daily_rate=1000, stock=5)
    mock_equipment_repo.find_by_id.return_value = equipment
    mock_policy.is_allowed.return_value = True

    # Імітуємо повернення створеного ордера з репозиторію
    created_order = RentalOrder(
        id=1,
        reference="REF-100",
        days=3,
        premium=False,
        total=6000, 
        late_fee=0,
        status=RentalStatus.ACTIVE,
        item=RentalItem(id=1, equipment_id=equipment_id, quantity=2, daily_rate=1000)
    )
    mock_rental_repo.create.return_value = created_order

    request = RentalRequest(
        reference="REF-100",
        equipment_id=equipment_id,
        quantity=2,
        days=3,
        premium=False
    )

    # Act (Виконання дії)
    result = rental_service.rent(request)

    # Assert (Перевірка результатів)
    assert result.id == 1
    assert result.status == RentalStatus.ACTIVE
    assert result.total == 6000
    mock_equipment_repo.find_by_id.assert_called_once_with(equipment_id)
    mock_policy.is_allowed.assert_called_once_with(equipment, 2, 3)
    mock_rental_repo.create.assert_called_once()


# 2. НЕГАТИВНИЙ БІЗНЕС-СЦЕНАРІЙ (NEGATIVE BUSINESS SCENARIOS)

def test_rent_rejected_when_quantity_exceeds_stock(rental_service, mock_equipment_repo):
    """
    Бізнес-відмова: Запитана кількість обладнання перевищує наявний залишок на складі.
    """
    # Arrange
    equipment_id = 5
    equipment = Equipment(id=equipment_id, code="SAW-01", daily_rate=500, stock=2)
    mock_equipment_repo.find_by_id.return_value = equipment

    request = RentalRequest(
        reference="REF-OVERSTOCK",
        equipment_id=equipment_id,
        quantity=5,  
        days=1,
        premium=False
    )

    # Act & Assert
    with pytest.raises(RentalRejectedError, match="Недостатньо обладнання"):
        rental_service.rent(request)


def test_rent_rejected_when_external_policy_denies(rental_service, mock_equipment_repo, mock_policy):
    """
    Бізнес-відмова: Зовнішня HTTP-політика забороняє оренду за персональними правилами.
    """
    # Arrange
    equipment = Equipment(id=1, code="GEN-01", daily_rate=2000, stock=10)
    mock_equipment_repo.find_by_id.return_value = equipment
    mock_policy.is_allowed.return_value = False  # Зовнішній сервіс відхиляє запит

    request = RentalRequest(reference="REF-POLICY", equipment_id=1, quantity=1, days=2, premium=False)

    # Act & Assert
    with pytest.raises(RentalRejectedError, match="Оренду не дозволено"):
        rental_service.rent(request)


# 3. НЕКОРЕКТНІ ВХІДНІ ДАНІ (INVALID INPUT DATA / VALIDATION ERRORS)

def test_rent_raises_value_error_for_invalid_days_range(rental_service):
    """
    Валідація: Кількість днів перевищує максимально допустиму межу в 30 днів.
    """
    # Arrange
    request = RentalRequest(
        reference="REF-INVALID-DAYS",
        equipment_id=1,
        quantity=1,
        days=31,  # Допустимо лише 1..30
        premium=False
    )

    # Act & Assert
    with pytest.raises(ValueError, match="Ціле число поза допустимими межами"):
        rental_service.rent(request)


def test_rent_raises_value_error_for_empty_reference(rental_service):
    """
    Валідація: Порожній або невалідний код оренди (reference).
    """
    # Arrange
    request = RentalRequest(
        reference="   ",  # Лише пробіли
        equipment_id=1,
        quantity=1,
        days=2,
        premium=False
    )

    # Act & Assert
    with pytest.raises(ValueError, match="Код оренди має містити 1–40 символів"):
        rental_service.rent(request)


# 4. ГРАНИЧНИЙ АБО НЕТИПОВИЙ ВИПАДОК (BOUNDARY & EDGE CASES)

@pytest.mark.parametrize("days, expected_total", [
    (4, 4000),  
    (5, 4250),  
])
def test_calculate_total_premium_discount_boundary(rental_service, days, expected_total):
    """
    Граничні значення: Преміум-знижка 15% застосовується РІВНО починаючи з 5 днів оренди.
    """
    # Arrange & Act
    total = rental_service.calculate_total(
        daily_rate=1000,
        quantity=1,
        days=days,
        premium=True
    )

    # Assert
    assert total == expected_total



# 5. ВИНЯТКОВА СИТУАЦІЯ (EXCEPTIONS / SYSTEM FAILURES)

def test_rent_raises_resource_not_found_when_equipment_missing(rental_service, mock_equipment_repo):
    """
    Виняток: Спроба замовити обладнання, якого взагалі немає в базі даних.
    """
    # Arrange
    mock_equipment_repo.find_by_id.return_value = None  # Обладнання відсутнє
    request = RentalRequest(reference="REF-404", equipment_id=999, quantity=1, days=1, premium=False)

    # Act & Assert
    with pytest.raises(ResourceNotFoundError, match="Обладнання не знайдено"):
        rental_service.rent(request)


def test_return_rental_already_returned_raises_rental_rejected_error(rental_service, mock_rental_repo):
    """
    Виняток / Некоректний стан: Спроба повторно повернути оренду, яка вже має статус Returned.
    """
    # Arrange
    rental_id = 42
    already_returned_order = RentalOrder(
        id=rental_id,
        reference="REF-CLOSED",
        days=2,
        premium=False,
        total=2000,
        late_fee=0,
        status=RentalStatus.RETURNED,  # Оренда вже була повернута
        item=RentalItem(id=1, equipment_id=10, quantity=1, daily_rate=1000)
    )
    mock_rental_repo.find_by_id.return_value = already_returned_order

    # Act & Assert
    with pytest.raises(RentalRejectedError, match="Оренду вже повернено"):
        rental_service.return_rental(rental_id=rental_id, overdue_days=0)

# ПАРАМЕТРИЗОВАНІ ТЕСТИ (6+ НАБОРІВ ДАНИХ)

@pytest.mark.parametrize(
    "daily_rate, quantity, days, premium, expected_total, test_description",
    [
        # 1. Валідне значення (Стандартний розрахунок без знижки)
        (1000, 2, 3, False, 6000, "Базовий валідний розрахунок"),
        
        # 2. Нижня межа допустимих параметрів (100 коп, 1 шт, 1 день)
        (100, 1, 1, False, 100, "Нижня межа допустимих параметрів"),
        
        # 3. Граничне значення до межки знижки (4 дні, преміум -> без знижки)
        (1000, 1, 4, True, 4000, "Преміум 4 дні (знижка 15% ще не діє)"),
        
        # 4. Граничне значення на межі знижки (5 днів, преміум -> 15% знижки)
        (1000, 1, 5, True, 4250, "Преміум 5 днів (початок дії знижки 15%)"),
        
        # 5. Верхня межа параметрів (1 млн коп, 20 шт, 30 днів зі знижкою)
        (1000000, 20, 30, True, 510000000, "Верхня межа допустимих параметрів"),
        
        # 6. Нетипове значення (Дробова знижка з цілочисельним округленням)
        (105, 1, 5, True, 447, "Некратна сума з округленням копійок"),
    ],
)
def test_calculate_total_parameterized(
    rental_service, daily_rate, quantity, days, premium, expected_total, test_description
):
    """Параметризований тест розрахунку вартості оренди."""
    actual_total = rental_service.calculate_total(
        daily_rate=daily_rate, quantity=quantity, days=days, premium=premium
    )
    assert actual_total == expected_total, f"Помилка у сценарії: {test_description}"


@pytest.mark.parametrize(
    "daily_rate, quantity, days, premium, expected_error_msg, test_description",
    [
        # 1. Невалідна ціна (< 100 коп)
        (99, 1, 1, False, "Ціле число поза допустимими межами", "Ціна нижче 100 копійок"),
        
        # 2. Невалідна ціна (> 1 000 000 коп)
        (1000001, 1, 1, False, "Ціле число поза допустимими межами", "Ціна вище 1 000 000 копійок"),
        
        # 3. Невалідна кількість (= 0 шт)
        (1000, 0, 1, False, "Ціле число поза допустимими межами", "Кількість дорівнює 0"),
        
        # 4. Невалідна кількість (> 20 шт)
        (1000, 21, 1, False, "Ціле число поза допустимими межами", "Кількість перевищує 20"),
        
        # 5. Невалідні дні (= 0 днів)
        (1000, 1, 0, False, "Ціле число поза допустимими межами", "Тривалість 0 днів"),
        
        # 6. Невалідні дні (> 30 днів)
        (1000, 1, 31, False, "Ціле число поза допустимими межами", "Тривалість перевищує 30 днів"),
        
        # 7. Нетиповий невалідний тип (рядок замість bool)
        (1000, 1, 1, "True", "Преміальність має бути логічною ознакою", "Невалідний тип для premium"),
    ],
)
def test_calculate_total_validation_parameterized(
    rental_service, daily_rate, quantity, days, premium, expected_error_msg, test_description
):
    """Параметризований тест валідації вхідних даних."""
    with pytest.raises(ValueError, match=expected_error_msg):
        rental_service.calculate_total(
            daily_rate=daily_rate, quantity=quantity, days=days, premium=premium
        )

# ІЗОЛЯЦІЯ ЗАЛЕЖНОСТЕЙ ЗА ДОПОМОГОЮ TEST DOUBLES (STUB, MOCK, EXCEPTIONS)

def test_stub_dependency_returns_predefined_result(rental_service, mock_equipment_repo, mock_rental_repo):
    """
    1. Stub (Предефінований результат):
    Репозиторій обладнання повертає заздалегідь підготовлений об'єкт-заглушку (Stub Equipment).
    """
    # Arrange (Налаштування Stub)
    stub_equipment = Equipment(id=100, code="STUB-DRILL", daily_rate=1200, stock=3)
    mock_equipment_repo.find_by_id.return_value = stub_equipment

    # Фіксуємо повернення збереженого замовлення
    mock_rental_repo.create.side_effect = lambda order: order

    request = RentalRequest(reference="REF-STUB-01", equipment_id=100, quantity=1, days=2, premium=False)

    # Act
    result = rental_service.rent(request)

    # Assert
    assert result.total == 2400
    mock_equipment_repo.find_by_id.assert_called_once_with(100)


def test_double_returns_alternative_denial_result(rental_service, mock_equipment_repo, mock_policy):
    """
    2. Альтернативний результат:
    Зовнішня HTTP-політика повертає альтернативне значення (False), що змінює гілку бізнес-логіки.
    """
    # Arrange
    mock_equipment_repo.find_by_id.return_value = Equipment(id=1, code="GEN-01", daily_rate=1000, stock=5)
    mock_policy.is_allowed.return_value = False  # Альтернативна відповідь (відмова)

    request = RentalRequest(reference="REF-DENY", equipment_id=1, quantity=1, days=1, premium=False)

    # Act & Assert
    with pytest.raises(RentalRejectedError, match="Оренду не дозволено"):
        rental_service.rent(request)

    # Перевірка виклику залежності (Mock Verification)
    mock_policy.is_allowed.assert_called_once()


def test_double_raises_exception_on_dependency_failure(rental_service, mock_equipment_repo):
    """
    3. Виняток залежності:
    Репозиторій генерує критичний виняток (наприклад, збій підключення до БД).
    """
    # Arrange: Імітуємо аварійну ситуацію в залежності через side_effect
    mock_equipment_repo.find_by_id.side_effect = RuntimeError("Database connection failure")

    request = RentalRequest(reference="REF-DB-FAIL", equipment_id=1, quantity=1, days=1, premium=False)

    # Act & Assert
    with pytest.raises(RuntimeError, match="Database connection failure"):
        rental_service.rent(request)

from dataclasses import replace


# STATE-BASED TESTING (ПЕРЕВІРКА СТАНУ ТА РЕЗУЛЬТАТУ)


def test_state_based_rent_creates_correct_order_and_total_state(
    rental_service, mock_equipment_repo, mock_rental_repo
):
    """
    State-Based Test 1: Перевірка стану сформованого об'єкта RentalOrder після оренди.
    Об'єкти перевірки: статус Active, підсумкова сума total зі знижкою, атрибути RentalItem.
    """
    # Arrange (Підготовка даних)
    equipment = Equipment(id=50, code="COMPRESSOR-01", daily_rate=2000, stock=10)
    mock_equipment_repo.find_by_id.return_value = equipment

    # Імітуємо присвоєння ID у базі даних під час збереження
    mock_rental_repo.create.side_effect = lambda order: replace(order, id=777)

    request = RentalRequest(
        reference="STATE-RENT-01",
        equipment_id=50,
        quantity=2,
        days=5,
        premium=True  # 2000 * 2 * 5 = 20000 - 15% (3000) = 17000
    )

    # Act (Виконання бізнес-операції)
    saved_order = rental_service.rent(request)

    # Assert (Перевірка стану об'єкта)
    assert saved_order.id == 777
    assert saved_order.status == RentalStatus.ACTIVE
    assert saved_order.total == 17000
    assert saved_order.late_fee == 0
    assert saved_order.item.equipment_id == 50
    assert saved_order.item.quantity == 2
    assert saved_order.item.daily_rate == 2000


def test_state_based_return_updates_status_to_returned(
    rental_service, mock_rental_repo
):
    """
    State-Based Test 2: Перевірка зміни стану замовлення після повернення обладнання.
    Об'єкти перевірки: зміна статусу з Active на Returned та встановлення late_fee.
    """
    # Arrange (Початковий стан: активне замовлення)
    active_order = RentalOrder(
        id=123,
        reference="STATE-RETURN-01",
        days=2,
        premium=False,
        total=4000,
        late_fee=0,
        status=RentalStatus.ACTIVE,
        item=RentalItem(id=1, equipment_id=50, quantity=2, daily_rate=1000)
    )
    mock_rental_repo.find_by_id.return_value = active_order

    # Act (Виконання повернення)
    returned_order = rental_service.return_rental(rental_id=123, overdue_days=0)

    # Assert (Перевірка оновленого стану)
    assert returned_order.id == 123
    assert returned_order.status == RentalStatus.RETURNED
    assert returned_order.late_fee == 0
    mock_rental_repo.mark_returned.assert_called_once_with(123, 0)

# INTERACTION-BASED TESTING (ПЕРЕВІРКА ВЗАЄМОДІЇ ІЗ ЗАЛЕЖНОСТЯМИ)

def test_interaction_verify_call_arguments_and_count_on_success(
    rental_service, mock_equipment_repo, mock_rental_repo, mock_policy
):
    """
    Interaction-Based Test 1: Перевірка факту, кількості викликів та ТОЧНИХ аргументів,
    які сервіс передає у залежності під час успішного створення оренди.
    """
    # Arrange (Підготовка даних)
    equipment = Equipment(id=15, code="SAW-99", daily_rate=1500, stock=4)
    mock_equipment_repo.find_by_id.return_value = equipment
    mock_policy.is_allowed.return_value = True

    request = RentalRequest(
        reference="INT-ARG-01",
        equipment_id=15,
        quantity=2,
        days=3,
        premium=False
    )

    # Act (Виконання)
    rental_service.rent(request)

    # Assert 1: Перевірка кількості викликів (Call count)
    assert mock_equipment_repo.find_by_id.call_count == 1
    assert mock_policy.is_allowed.call_count == 1
    assert mock_rental_repo.create.call_count == 1

    # Assert 2: Перевірка точних аргументів виклику зовнішньої політики
    mock_policy.is_allowed.assert_called_once_with(equipment, 2, 3)

    # Assert 3: Перевірка значень аргументів, які сервіс сформував і передав у mock_rental_repo.create()
    created_order_arg = mock_rental_repo.create.call_args[0][0]
    assert created_order_arg.reference == "INT-ARG-01"
    assert created_order_arg.days == 3
    assert created_order_arg.total == 9000  # 1500 * 2 * 3
    assert created_order_arg.status == RentalStatus.ACTIVE
    assert created_order_arg.item.equipment_id == 15
    assert created_order_arg.item.quantity == 2
    assert created_order_arg.item.daily_rate == 1500


def test_interaction_verify_absence_of_calls_on_early_rejection(
    rental_service, mock_equipment_repo, mock_rental_repo, mock_policy
):
    """
    Interaction-Based Test 2: Перевірка ВІДСУТНОСТІ викликів (Absence of calls) подальших залежностей,
    якщо бізнес-операція переривається через брак товару на складі.
    """
    # Arrange
    equipment = Equipment(id=20, code="DRILL-55", daily_rate=1000, stock=1)
    mock_equipment_repo.find_by_id.return_value = equipment

    request = RentalRequest(
        reference="INT-NO-CALL",
        equipment_id=20,
        quantity=5,  # 5 > stock (1)
        days=1,
        premium=False
    )

    # Act & Assert
    with pytest.raises(RentalRejectedError):
        rental_service.rent(request)

    # Перевірка: пошук у базі відбувся 1 раз, але політика і створення замовлення НЕ викликалися взагалі
    mock_equipment_repo.find_by_id.assert_called_once_with(20)
    mock_policy.is_allowed.assert_not_called()
    mock_rental_repo.create.assert_not_called()

# ТЕСТУВАННЯ ВИНЯТКОВИХ СЦЕНАРІЇВ (EXCEPTION SCENARIOS & FAIL-FAST)

def test_exception_scenario_dependency_failure_prevents_db_save(
    rental_service, mock_equipment_repo, mock_rental_repo, mock_policy
):
    """
    Винятковий сценарій 1: Зовнішня залежність (політика) генерує мережевий виняток.
    Перевіряється:
    1. Реакція сервісу: прокидання винятку ConnectionError дальше.
    2. Відсутність побічних операцій: замовлення НЕ зберігається у БД (mock_rental_repo.create НЕ викликається).
    """
    # Arrange (Підготовка даних)
    equipment = Equipment(id=1, code="PUMP-01", daily_rate=500, stock=3)
    mock_equipment_repo.find_by_id.return_value = equipment
    
    # Імітуємо збій мережі / таймаут у зовнішній залежності
    mock_policy.is_allowed.side_effect = ConnectionError("Помилка з'єднання з сервісом політик")

    request = RentalRequest(reference="REF-CONN-FAIL", equipment_id=1, quantity=1, days=2, premium=False)

    # Act & Assert (Перевірка винятку)
    with pytest.raises(ConnectionError, match="Помилка з'єднання з сервісом політик"):
        rental_service.rent(request)

    # Перевірка відсутності операцій після помилки:
    mock_equipment_repo.find_by_id.assert_called_once_with(1)
    mock_policy.is_allowed.assert_called_once()
    mock_rental_repo.create.assert_not_called()  # Гарантія: у БД нічого не збережено!


def test_exception_scenario_invalid_input_stops_execution_immediately(
    rental_service, mock_equipment_repo, mock_rental_repo, mock_policy
):
    """
    Винятковий сценарій 2: Передано некоректні вхідні дані (порожній reference).
    Перевіряється:
    1. Реакція сервісу: перехоплення та генерування ValueError.
    2. Поведінка Fail-Fast: ЖОДНА зовнішня залежність (БД, API) НЕ викликається взагалі.
    """
    # Arrange: Запит із невалідним (порожнім) кодом оренди
    invalid_request = RentalRequest(reference="  ", equipment_id=1, quantity=1, days=2, premium=False)

    # Act & Assert
    with pytest.raises(ValueError, match="Код оренди має містити 1–40 символів"):
        rental_service.rent(invalid_request)

    # Перевірка: Раннє переривання без зачіпання залежностей
    mock_equipment_repo.find_by_id.assert_not_called()
    mock_policy.is_allowed.assert_not_called()
    mock_rental_repo.create.assert_not_called()

def test_init_missing_dependencies():
    """Покриває гілку: if equipment is None or rentals is None or policy is None"""
    with pytest.raises(ValueError, match="Усі залежності обов'язкові"):
        RentalService(equipment=None, rentals=Mock(), policy=Mock())


def test_rent_invalid_request_type():
    """Покриває гілку: if not isinstance(request, RentalRequest)"""
    service = RentalService(equipment=Mock(), rentals=Mock(), policy=Mock())
    
    with pytest.raises(ValueError, match="Некоректна заявка"):
        service.rent(request={"equipment_id": 1, "days": 5})


def test_rent_personal_conditions_not_met():
    """Покриває гілку: if not self.is_rental_allowed(...)"""
    service = RentalService(equipment=Mock(), rentals=Mock(), policy=Mock())
    request = RentalRequest(reference="REF123", equipment_id=1, quantity=1, days=5, premium=False)
    
    # Налаштовуємо мок обладнання, щоб пройти попередні перевірки
    mock_eq = Mock()
    mock_eq.stock = 10
    mock_eq.daily_rate = 150
    service.equipment.find_by_id.return_value = mock_eq
    
    # Примусово змушуємо is_rental_allowed повернути False
    with patch.object(service, 'is_rental_allowed', return_value=False):
        with pytest.raises(RentalRejectedError, match="Персональні умови оренди не виконані"):
            service.rent(request)


def test_rent_invalid_policy_response_type():
    """Покриває гілку: if type(allowed) is not bool"""
    service = RentalService(equipment=Mock(), rentals=Mock(), policy=Mock())
    request = RentalRequest(reference="REF123", equipment_id=1, quantity=1, days=5, premium=False)
    
    mock_eq = Mock()
    mock_eq.stock = 10
    mock_eq.daily_rate = 150
    service.equipment.find_by_id.return_value = mock_eq
    
    # Імітуємо збій зовнішнього сервісу (повертає рядок замість bool)
    service.policy.is_allowed.return_value = "Yes"
    
    with pytest.raises(ValueError, match="Некоректна відповідь зовнішньої перевірки"):
        service.rent(request)


def test_return_rental_not_found():
    """Покриває гілку: if rental is None"""
    service = RentalService(equipment=Mock(), rentals=Mock(), policy=Mock())
    
    # Імітуємо, що база даних не знайшла замовлення
    service.rentals.find_by_id.return_value = None
    
    with pytest.raises(ResourceNotFoundError, match="Оренду не знайдено"):
        service.return_rental(rental_id=9999, overdue_days=0)


def test_calculate_total_boundary_exact_days(rental_service):
    """
    Покриває вижившего мутанта №35: 
    Перевіряє точну границю days = 5 для преміального клієнта (умова days >= 5).
    """
    # 1000 * 1 * 5 = 5000, знижка 15% (5000 - 750) = 4250
    total = rental_service.calculate_total(daily_rate=1000, quantity=1, days=5, premium=True)
    assert total == 4250


def test_validate_quantity_upper_bound_limit():
    """
    Покриває вижившего мутанта №9:
    Перевіряє верхню межу валідації кількості (quantity <= 20). 
    Спроба орендувати 21 одиницю має викликати ValueError.
    """
    from rental.service import validate
    with pytest.raises(ValueError):
        validate(daily_rate=1000, quantity=21, days=5, premium=False)


def test_integer_boundary_out_of_range():
    """
    Покриває вижившего мутанта №2:
    Перевіряє виняток для функції integer при виході за межі (значення менше мінімуму або більше максимуму).
    """
    with pytest.raises(ValueError):
        integer(value=0, minimum=1, maximum=10)
    
    with pytest.raises(ValueError):
        integer(value=11, minimum=1, maximum=10)
def test_integer_invalid_type_raises_value_error():
    
    with pytest.raises(ValueError):
        integer(value="10", minimum=1, maximum=30)
        
    with pytest.raises(ValueError):
        validate(daily_rate=1000, quantity=2, days=5, premium="yes")