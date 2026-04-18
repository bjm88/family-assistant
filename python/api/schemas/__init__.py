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
from .goal import GoalCreate, GoalUpdate, GoalRead, GoalPriority
from .medical_condition import (
    MedicalConditionCreate,
    MedicalConditionUpdate,
    MedicalConditionRead,
)
from .medication import (
    MedicationCreate,
    MedicationUpdate,
    MedicationRead,
)
from .physician import (
    PhysicianCreate,
    PhysicianUpdate,
    PhysicianRead,
)
from .pet import PetCreate, PetUpdate, PetRead
from .pet_photo import PetPhotoRead, PetPhotoUpdate
from .address import AddressCreate, AddressUpdate, AddressRead
from .residence import (
    ResidenceCreate,
    ResidenceUpdate,
    ResidenceRead,
)
from .residence_photo import ResidencePhotoRead, ResidencePhotoUpdate
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
from .live_session import (
    EndSessionRequest,
    EnsureActiveSessionRequest,
    LiveSessionDetail,
    LiveSessionEndReason,
    LiveSessionMessageRead,
    LiveSessionMessageRole,
    LiveSessionParticipantRead,
    LiveSessionRead,
)

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
    "GoalCreate",
    "GoalUpdate",
    "GoalRead",
    "GoalPriority",
    "MedicalConditionCreate",
    "MedicalConditionUpdate",
    "MedicalConditionRead",
    "MedicationCreate",
    "MedicationUpdate",
    "MedicationRead",
    "PhysicianCreate",
    "PhysicianUpdate",
    "PhysicianRead",
    "PetCreate",
    "PetUpdate",
    "PetRead",
    "PetPhotoRead",
    "PetPhotoUpdate",
    "AddressCreate",
    "AddressUpdate",
    "AddressRead",
    "ResidenceCreate",
    "ResidenceUpdate",
    "ResidenceRead",
    "ResidencePhotoRead",
    "ResidencePhotoUpdate",
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
    "LiveSessionRead",
    "LiveSessionDetail",
    "LiveSessionParticipantRead",
    "LiveSessionMessageRead",
    "LiveSessionMessageRole",
    "LiveSessionEndReason",
    "EnsureActiveSessionRequest",
    "EndSessionRequest",
]
