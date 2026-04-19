"""SQLAlchemy ORM models for the Family Assistant domain.

Naming convention
-----------------
Table and column names use **verbose natural-language snake_case**. This makes
the generated Postgres catalog (``information_schema.columns``, ``pg_description``)
self-explanatory so a local LLM can build dynamic SQL against it without
additional prompt engineering.

Every encrypted column is named ``*_encrypted`` and is paired, where useful,
with a plain helper column (``*_last_four``, ``*_masked``) so structured
queries never need to touch ciphertext.
"""

from .family import Family
from .assistant import Assistant
from .google_oauth_credential import GoogleOAuthCredential
from .agent_task import AgentTask, AgentStep
from .person import Person
from .person_photo import PersonPhoto
from .face_embedding import FaceEmbedding
from .person_relationship import PersonRelationship, RELATIONSHIP_TYPES
from .goal import Goal, GOAL_PRIORITIES
from .medical_condition import MedicalCondition
from .medication import Medication
from .physician import Physician
from .pet import Pet, COMMON_PET_ANIMAL_TYPES
from .pet_photo import PetPhoto
from .address import Address
from .residence import Residence
from .residence_photo import ResidencePhoto
from .identity_document import IdentityDocument
from .sensitive_identifier import SensitiveIdentifier
from .vehicle import Vehicle, COMMON_VEHICLE_TYPES
from .insurance_policy import (
    InsurancePolicy,
    InsurancePolicyPerson,
    InsurancePolicyVehicle,
)
from .financial_account import FinancialAccount
from .document import Document
from .live_session import (
    LiveSession,
    LIVE_SESSION_END_REASONS,
    LIVE_SESSION_SOURCES,
)
from .live_session_participant import LiveSessionParticipant
from .live_session_message import (
    LiveSessionMessage,
    LIVE_SESSION_MESSAGE_ROLES,
)
from .email_inbox_message import EmailInboxMessage, EMAIL_INBOX_STATUSES
from .task import (
    Task,
    TaskAttachment,
    TaskComment,
    TaskFollower,
    TASK_ATTACHMENT_KINDS,
    TASK_COMMENT_AUTHOR_KINDS,
    TASK_PRIORITIES,
    TASK_STATUSES,
)

__all__ = [
    "Family",
    "Assistant",
    "GoogleOAuthCredential",
    "AgentTask",
    "AgentStep",
    "Person",
    "PersonPhoto",
    "FaceEmbedding",
    "PersonRelationship",
    "RELATIONSHIP_TYPES",
    "Goal",
    "GOAL_PRIORITIES",
    "MedicalCondition",
    "Medication",
    "Physician",
    "Pet",
    "COMMON_PET_ANIMAL_TYPES",
    "PetPhoto",
    "Address",
    "Residence",
    "ResidencePhoto",
    "IdentityDocument",
    "SensitiveIdentifier",
    "Vehicle",
    "COMMON_VEHICLE_TYPES",
    "InsurancePolicy",
    "InsurancePolicyPerson",
    "InsurancePolicyVehicle",
    "FinancialAccount",
    "Document",
    "LiveSession",
    "LIVE_SESSION_END_REASONS",
    "LIVE_SESSION_SOURCES",
    "LiveSessionParticipant",
    "LiveSessionMessage",
    "LIVE_SESSION_MESSAGE_ROLES",
    "EmailInboxMessage",
    "EMAIL_INBOX_STATUSES",
    "Task",
    "TaskAttachment",
    "TaskComment",
    "TaskFollower",
    "TASK_ATTACHMENT_KINDS",
    "TASK_COMMENT_AUTHOR_KINDS",
    "TASK_PRIORITIES",
    "TASK_STATUSES",
]
