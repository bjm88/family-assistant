// Shared enum-ish option lists used by multiple forms.

export const GENDERS = ["male", "female"] as const;

export const GOAL_PRIORITIES = [
  "urgent",
  "semi_urgent",
  "normal",
  "low",
] as const;

export const GOAL_PRIORITY_LABELS: Record<(typeof GOAL_PRIORITIES)[number], string> = {
  urgent: "Urgent",
  semi_urgent: "Semi-urgent",
  normal: "Normal",
  low: "Low",
};

// Common pet species shown in the pet-add dropdown. "other" opens a free
// text field so unusual pets aren't blocked.
export const PET_ANIMAL_TYPES = [
  "dog",
  "cat",
  "bird",
  "rabbit",
  "guinea_pig",
  "hamster",
  "mouse",
  "rat",
  "ferret",
  "turtle",
  "tortoise",
  "lizard",
  "snake",
  "fish",
  "frog",
  "chicken",
  "duck",
  "goose",
  "goat",
  "sheep",
  "ram",
  "pig",
  "cow",
  "horse",
  "donkey",
  "other",
] as const;

// Vehicle high-level categories. Mirrors COMMON_VEHICLE_TYPES in
// ``python/api/models/vehicle.py``. The dashboard's "Cars" gallery
// filters on the literal value "car".
export const VEHICLE_TYPES = [
  "car",
  "truck",
  "motorcycle",
  "boat",
  "atv",
  "rv",
  "airplane",
  "bicycle",
  "golf_cart",
  "tractor",
  "trailer",
  "other",
] as const;

export const VEHICLE_TYPE_LABELS: Record<(typeof VEHICLE_TYPES)[number], string> = {
  car: "Car",
  truck: "Truck",
  motorcycle: "Motorcycle",
  boat: "Boat",
  atv: "ATV",
  rv: "RV",
  airplane: "Airplane",
  bicycle: "Bicycle",
  golf_cart: "Golf cart",
  tractor: "Tractor",
  trailer: "Trailer",
  other: "Other",
};

export const PRIMARY_RELATIONSHIPS = [
  "self",
  "spouse",
  "partner",
  "parent",
  "child",
  "sibling",
  "grandparent",
  "grandchild",
  "aunt",
  "uncle",
  "cousin",
  "niece",
  "nephew",
  "guardian",
  "other",
] as const;
