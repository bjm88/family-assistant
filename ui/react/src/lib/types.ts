export interface Family {
  family_id: number;
  family_name: string;
  head_of_household_notes: string | null;
  created_at: string;
  updated_at: string;
}

export interface FamilySummary {
  family_id: number;
  family_name: string;
  people_count: number;
  vehicles_count: number;
  insurance_policies_count: number;
  financial_accounts_count: number;
  documents_count: number;
}

export interface Person {
  person_id: number;
  family_id: number;
  first_name: string;
  middle_name: string | null;
  last_name: string;
  preferred_name: string | null;
  date_of_birth: string | null;
  gender: string | null;
  primary_family_relationship: string | null;
  email_address: string | null;
  work_email: string | null;
  mobile_phone_number: string | null;
  home_phone_number: string | null;
  work_phone_number: string | null;
  profile_photo_path: string | null;
  interests_and_activities: string | null;
  notes: string | null;
  created_at: string;
  updated_at: string;
}

/**
 * Face geometry detected on the assistant's generated avatar by
 * InsightFace. All coordinates are expressed as percentages of the
 * image (0..1), so the UI can overlay a responsive <img> without
 * knowing pixel dimensions. `null` when no face was detected or the
 * detector hasn't been initialised yet.
 */
export interface AvatarLandmarks {
  bbox: { x: number; y: number; w: number; h: number };
  mouth: { cx: number; cy: number; w: number; h: number };
  eyes: { lx: number; ly: number; rx: number; ry: number };
}

// ============================================================================
// Live AI-assistant sessions
// ============================================================================

export type LiveSessionMessageRole = "user" | "assistant" | "system";
export type LiveSessionEndReason = "timeout" | "manual" | "superseded";
export type LiveSessionSource = "live" | "email";

export interface LiveSessionParticipant {
  live_session_participant_id: number;
  live_session_id: number;
  person_id: number;
  person_name: string | null;
  joined_at: string;
  greeted_already: boolean;
}

export interface LiveSessionMessage {
  live_session_message_id: number;
  live_session_id: number;
  role: LiveSessionMessageRole;
  person_id: number | null;
  person_name: string | null;
  content: string;
  meta: Record<string, unknown> | null;
  created_at: string;
}

export interface LiveSession {
  live_session_id: number;
  family_id: number;
  started_at: string;
  ended_at: string | null;
  last_activity_at: string;
  start_context: string | null;
  end_reason: LiveSessionEndReason | null;
  source: LiveSessionSource;
  external_thread_id: string | null;
  is_active: boolean;
  participant_count: number;
  message_count: number;
  participants_preview: string[];
  last_message_preview: string | null;
}

export interface LiveSessionDetail extends LiveSession {
  participants: LiveSessionParticipant[];
  messages: LiveSessionMessage[];
}

export interface Assistant {
  assistant_id: number;
  family_id: number;
  assistant_name: string;
  gender: "male" | "female" | null;
  visual_description: string | null;
  personality_description: string | null;
  email_address: string | null;
  profile_image_path: string | null;
  avatar_generation_note: string | null;
  avatar_landmarks: AvatarLandmarks | null;
  created_at: string;
  updated_at: string;
}

export interface PersonPhoto {
  person_photo_id: number;
  person_id: number;
  title: string;
  description: string | null;
  use_for_face_recognition: boolean;
  stored_file_path: string;
  original_file_name: string;
  mime_type: string | null;
  file_size_bytes: number | null;
  created_at: string;
  updated_at: string;
}

export type RelationshipType = "parent_of" | "spouse_of";

export interface PersonRelationship {
  person_relationship_id: number;
  from_person_id: number;
  to_person_id: number;
  relationship_type: RelationshipType;
  notes: string | null;
}

export interface Address {
  address_id: number;
  family_id: number;
  person_id: number | null;
  label: string;
  street_line_1: string;
  street_line_2: string | null;
  city: string;
  state_or_region: string | null;
  postal_code: string | null;
  country: string;
  is_primary_residence: boolean;
  notes: string | null;
}

export interface IdentityDocument {
  identity_document_id: number;
  person_id: number;
  document_type: string;
  document_number_last_four: string | null;
  issuing_authority: string | null;
  country_of_issue: string;
  state_or_region_of_issue: string | null;
  issue_date: string | null;
  expiration_date: string | null;
  notes: string | null;
  front_image_path: string | null;
  back_image_path: string | null;
}

export type IdentityDocumentImageSide = "front" | "back";

export interface SensitiveIdentifier {
  sensitive_identifier_id: number;
  person_id: number;
  identifier_type: string;
  identifier_last_four: string | null;
  notes: string | null;
}

export interface Vehicle {
  vehicle_id: number;
  family_id: number;
  primary_driver_person_id: number | null;
  residence_id: number | null;
  vehicle_type: string;
  nickname: string | null;
  year: number | null;
  make: string;
  model: string;
  trim: string | null;
  color: string | null;
  body_style: string | null;
  fuel_type: string | null;
  vehicle_identification_number_last_four: string | null;
  license_plate_number_last_four: string | null;
  license_plate_state_or_region: string | null;
  registration_expiration_date: string | null;
  purchase_date: string | null;
  purchase_price_usd: string | null;
  current_mileage: number | null;
  profile_image_path: string | null;
  notes: string | null;
}

export interface InsurancePolicy {
  insurance_policy_id: number;
  family_id: number;
  policy_type: string;
  carrier_name: string;
  plan_name: string | null;
  policy_number_last_four: string | null;
  premium_amount_usd: string | null;
  premium_billing_frequency: string | null;
  deductible_amount_usd: string | null;
  coverage_limit_amount_usd: string | null;
  effective_date: string | null;
  expiration_date: string | null;
  agent_name: string | null;
  agent_phone_number: string | null;
  agent_email_address: string | null;
  notes: string | null;
  covered_person_ids: number[];
  covered_vehicle_ids: number[];
}

export interface FinancialAccount {
  financial_account_id: number;
  family_id: number;
  primary_holder_person_id: number | null;
  account_type: string;
  institution_name: string;
  account_nickname: string | null;
  account_number_last_four: string | null;
  current_balance_usd: string | null;
  credit_limit_usd: string | null;
  online_login_url: string | null;
  notes: string | null;
}

export type GoalPriority = "urgent" | "semi_urgent" | "normal" | "low";

export interface Goal {
  goal_id: number;
  person_id: number;
  goal_name: string;
  description: string | null;
  start_date: string | null;
  priority: GoalPriority;
}

export interface MedicalCondition {
  medical_condition_id: number;
  person_id: number;
  condition_name: string;
  icd10_code: string | null;
  start_date: string | null;
  end_date: string | null;
  description: string | null;
}

export interface Medication {
  medication_id: number;
  person_id: number;
  ndc_number: string | null;
  generic_name: string | null;
  brand_name: string | null;
  dosage: string | null;
  start_date: string | null;
  end_date: string | null;
  notes: string | null;
}

export interface Physician {
  physician_id: number;
  person_id: number;
  physician_name: string;
  specialty: string | null;
  address: string | null;
  phone_number: string | null;
  email_address: string | null;
  description: string | null;
}

export interface Pet {
  pet_id: number;
  family_id: number;
  pet_name: string;
  animal_type: string;
  breed: string | null;
  color: string | null;
  date_of_birth: string | null;
  notes: string | null;
  cover_photo_path: string | null;
}

export interface Residence {
  residence_id: number;
  family_id: number;
  label: string;
  street_line_1: string;
  street_line_2: string | null;
  city: string;
  state_or_region: string | null;
  postal_code: string | null;
  country: string;
  is_primary_residence: boolean;
  notes: string | null;
  cover_photo_path: string | null;
}

export interface ResidencePhoto {
  residence_photo_id: number;
  residence_id: number;
  title: string;
  description: string | null;
  stored_file_path: string;
  original_file_name: string;
  mime_type: string | null;
  file_size_bytes: number | null;
  created_at: string;
  updated_at: string;
}

export interface PetPhoto {
  pet_photo_id: number;
  pet_id: number;
  title: string;
  description: string | null;
  stored_file_path: string;
  original_file_name: string;
  mime_type: string | null;
  file_size_bytes: number | null;
  created_at: string;
  updated_at: string;
}

// ============================================================================
// Family task board (kanban)
// ============================================================================

export type TaskStatus = "new" | "in_progress" | "finalizing" | "done";
export type TaskPriority =
  | "urgent"
  | "high"
  | "normal"
  | "low"
  | "future_idea";
export type TaskCommentAuthorKind = "person" | "assistant";
export type TaskAttachmentKind = "photo" | "pdf" | "document" | "other";

export interface Task {
  task_id: number;
  family_id: number;
  created_by_person_id: number | null;
  assigned_to_person_id: number | null;
  title: string;
  description: string | null;
  status: TaskStatus;
  priority: TaskPriority;
  start_date: string | null;
  desired_end_date: string | null;
  end_date: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
  follower_count: number;
  comment_count: number;
  attachment_count: number;
}

export interface TaskComment {
  task_comment_id: number;
  task_id: number;
  author_person_id: number | null;
  author_kind: TaskCommentAuthorKind;
  body: string;
  created_at: string;
}

export interface TaskFollower {
  task_follower_id: number;
  task_id: number;
  person_id: number;
  added_at: string;
}

export interface TaskAttachment {
  task_attachment_id: number;
  task_id: number;
  uploaded_by_person_id: number | null;
  attachment_kind: TaskAttachmentKind;
  original_file_name: string;
  mime_type: string | null;
  file_size_bytes: number | null;
  caption: string | null;
  created_at: string;
}

export interface TaskDetail extends Task {
  followers: TaskFollower[];
  comments: TaskComment[];
  attachments: TaskAttachment[];
}

export interface DocumentRecord {
  document_id: number;
  family_id: number;
  person_id: number | null;
  title: string;
  document_category: string | null;
  original_file_name: string;
  mime_type: string | null;
  file_size_bytes: number | null;
  notes: string | null;
  created_at: string;
  updated_at: string;
}
