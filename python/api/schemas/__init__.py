"""Pydantic schemas used for request validation and API responses.

By convention we never return encrypted ciphertext to the client; sensitive
columns expose only their ``*_last_four`` counterpart, plus a boolean
``*_is_stored`` so the UI can render a "change" vs "add" button.
"""

from .family import FamilyCreate, FamilyUpdate, FamilyRead, FamilySummary
from .assistant import AssistantCreate, AssistantUpdate, AssistantRead
from .person import PersonCreate, PersonUpdate, PersonRead, PersonSummary
from .person_photo import PersonPhotoRead, PersonPhotoUpdate
from .person_relationship import (
    PersonRelationshipCreate,
    PersonRelationshipRead,
)
from .address import AddressCreate, AddressUpdate, AddressRead
from .identity_document import (
    IdentityDocumentCreate,
    IdentityDocumentUpdate,
    IdentityDocumentRead,
)
from .sensitive_identifier import (
    SensitiveIdentifierCreate,
    SensitiveIdentifierUpdate,
    SensitiveIdentifierRead,
)
from .vehicle import VehicleCreate, VehicleUpdate, VehicleRead
from .insurance_policy import (
    InsurancePolicyCreate,
    InsurancePolicyUpdate,
    InsurancePolicyRead,
)
from .financial_account import (
    FinancialAccountCreate,
    FinancialAccountUpdate,
    FinancialAccountRead,
)
from .document import DocumentRead, DocumentUpdate

__all__ = [
    "FamilyCreate",
    "FamilyUpdate",
    "FamilyRead",
    "FamilySummary",
    "AssistantCreate",
    "AssistantUpdate",
    "AssistantRead",
    "PersonCreate",
    "PersonUpdate",
    "PersonRead",
    "PersonSummary",
    "PersonPhotoRead",
    "PersonPhotoUpdate",
    "PersonRelationshipCreate",
    "PersonRelationshipRead",
    "AddressCreate",
    "AddressUpdate",
    "AddressRead",
    "IdentityDocumentCreate",
    "IdentityDocumentUpdate",
    "IdentityDocumentRead",
    "SensitiveIdentifierCreate",
    "SensitiveIdentifierUpdate",
    "SensitiveIdentifierRead",
    "VehicleCreate",
    "VehicleUpdate",
    "VehicleRead",
    "InsurancePolicyCreate",
    "InsurancePolicyUpdate",
    "InsurancePolicyRead",
    "FinancialAccountCreate",
    "FinancialAccountUpdate",
    "FinancialAccountRead",
    "DocumentRead",
    "DocumentUpdate",
]
