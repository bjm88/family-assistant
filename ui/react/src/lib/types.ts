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
  mobile_phone_number: string | null;
  home_phone_number: string | null;
  work_phone_number: string | null;
  profile_photo_path: string | null;
  notes: string | null;
  created_at: string;
  updated_at: string;
}

export interface Assistant {
  assistant_id: number;
  family_id: number;
  assistant_name: string;
  gender: "male" | "female" | null;
  visual_description: string | null;
  personality_description: string | null;
  profile_image_path: string | null;
  avatar_generation_note: string | null;
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

export interface Pet {
  pet_id: number;
  family_id: number;
  pet_name: string;
  animal_type: string;
  breed: string | null;
  color: string | null;
  date_of_birth: string | null;
  notes: string | null;
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
