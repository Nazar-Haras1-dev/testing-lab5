from unittest.mock import MagicMock
import pytest

from rental.ports import EquipmentRepository, RentalRepository, RentalPolicy
from rental.service import RentalService


@pytest.fixture
def mock_equipment_repo():
    """Мок-об'єкт для репозиторію обладнання."""
    return MagicMock(spec=EquipmentRepository)


@pytest.fixture
def mock_rental_repo():
    """Мок-об'єкт для репозиторію замовлень оренди."""
    return MagicMock(spec=RentalRepository)


@pytest.fixture
def mock_policy():
    """Мок-об'єкт для зовнішньої HTTP-політики оренди."""
    policy = MagicMock(spec=RentalPolicy)
    policy.is_allowed.return_value = True
    return policy


@pytest.fixture
def rental_service(mock_equipment_repo, mock_rental_repo, mock_policy):
    """
    Фікстура створення тестованого сервісу RentalService
    із повністю ізольованими (замокованими) залежностями.
    """
    return RentalService(
        equipment=mock_equipment_repo,
        rentals=mock_rental_repo,
        policy=mock_policy,
    )