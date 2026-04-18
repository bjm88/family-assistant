import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { Camera, Car, Pencil, Plus, Trash2, X } from "lucide-react";
import { api } from "@/lib/api";
import type { Person, Residence, Vehicle } from "@/lib/types";
import { PageHeader } from "@/components/PageHeader";
import { EmptyState } from "@/components/EmptyState";
import { Modal } from "@/components/Modal";
import { Field } from "@/components/Field";
import { useToast } from "@/components/Toast";
import { EncryptedField } from "@/components/EncryptedField";
import { cleanPayload } from "@/lib/form";
import { VEHICLE_TYPES, VEHICLE_TYPE_LABELS } from "@/lib/enums";

const BODY_STYLES = [
  "sedan",
  "suv",
  "minivan",
  "pickup",
  "coupe",
  "hatchback",
  "wagon",
  "convertible",
  "motorcycle",
  "other",
];
const FUEL_TYPES = ["gasoline", "diesel", "hybrid", "plug_in_hybrid", "electric"];

const NUMERIC_FIELDS = [
  "year",
  "current_mileage",
  "primary_driver_person_id",
  "residence_id",
  "purchase_price_usd",
] as const;

// Every field the form registers. Listed exhaustively so `reset()` can
// blank the form completely — react-hook-form preserves fields that
// aren't present in the reset payload, which previously leaked the last
// edited vehicle's values into the "Add vehicle" modal.
type VehicleForm = {
  vehicle_type: string;
  nickname: string;
  year: string;
  make: string;
  model: string;
  trim: string;
  color: string;
  body_style: string;
  fuel_type: string;
  primary_driver_person_id: string;
  residence_id: string;
  vehicle_identification_number: string;
  license_plate_number: string;
  license_plate_state_or_region: string;
  registration_expiration_date: string;
  purchase_date: string;
  purchase_price_usd: string;
  current_mileage: string;
  notes: string;
};

function emptyForm(): VehicleForm {
  return {
    vehicle_type: "car",
    nickname: "",
    year: "",
    make: "",
    model: "",
    trim: "",
    color: "",
    body_style: "",
    fuel_type: "",
    primary_driver_person_id: "",
    residence_id: "",
    vehicle_identification_number: "",
    license_plate_number: "",
    license_plate_state_or_region: "",
    registration_expiration_date: "",
    purchase_date: "",
    purchase_price_usd: "",
    current_mileage: "",
    notes: "",
  };
}

function vehicleToForm(v: Vehicle): VehicleForm {
  return {
    vehicle_type: v.vehicle_type ?? "car",
    nickname: v.nickname ?? "",
    year: v.year != null ? String(v.year) : "",
    make: v.make,
    model: v.model,
    trim: v.trim ?? "",
    color: v.color ?? "",
    body_style: v.body_style ?? "",
    fuel_type: v.fuel_type ?? "",
    primary_driver_person_id:
      v.primary_driver_person_id != null
        ? String(v.primary_driver_person_id)
        : "",
    residence_id: v.residence_id != null ? String(v.residence_id) : "",
    // VIN/plate are encrypted server-side; never round-trip the cleartext.
    vehicle_identification_number: "",
    license_plate_number: "",
    license_plate_state_or_region: v.license_plate_state_or_region ?? "",
    registration_expiration_date: v.registration_expiration_date ?? "",
    purchase_date: v.purchase_date ?? "",
    purchase_price_usd: v.purchase_price_usd ?? "",
    current_mileage: v.current_mileage != null ? String(v.current_mileage) : "",
    notes: v.notes ?? "",
  };
}

export default function VehiclesPage() {
  const { familyId } = useParams();
  const qc = useQueryClient();
  const toast = useToast();
  // null = closed, "new" = create mode, number = edit mode for that vehicle id.
  const [editingId, setEditingId] = useState<number | "new" | null>(null);

  const { data: vehicles } = useQuery<Vehicle[]>({
    queryKey: ["vehicles", familyId],
    queryFn: () => api.get<Vehicle[]>(`/api/vehicles?family_id=${familyId}`),
  });
  const { data: people } = useQuery<Person[]>({
    queryKey: ["people", familyId],
    queryFn: () => api.get<Person[]>(`/api/people?family_id=${familyId}`),
  });
  const { data: residences } = useQuery<Residence[]>({
    queryKey: ["residences", Number(familyId)],
    queryFn: () => api.get<Residence[]>(`/api/residences?family_id=${familyId}`),
  });

  const del = useMutation({
    mutationFn: (id: number) => api.del(`/api/vehicles/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["vehicles", familyId] });
      toast.success("Vehicle removed.");
    },
    onError: (err: Error) => toast.error(`Could not remove vehicle: ${err.message}`),
  });

  const peopleById = new Map((people ?? []).map((p) => [p.person_id, p]));
  const residencesById = new Map(
    (residences ?? []).map((r) => [r.residence_id, r])
  );
  const editingVehicle =
    typeof editingId === "number"
      ? vehicles?.find((v) => v.vehicle_id === editingId) ?? null
      : null;

  return (
    <div>
      <PageHeader
        title="Vehicles"
        description="Cars, trucks, motorcycles and boats. VIN and plate are encrypted."
        actions={
          <button className="btn-primary" onClick={() => setEditingId("new")}>
            <Plus className="h-4 w-4" /> Add vehicle
          </button>
        }
      />

      {!vehicles || vehicles.length === 0 ? (
        <EmptyState
          icon={Car}
          title="No vehicles yet"
          description="Track what the family drives — plates, VINs, and who drives each one."
          action={
            <button className="btn-primary" onClick={() => setEditingId("new")}>
              <Plus className="h-4 w-4" /> Add a vehicle
            </button>
          }
        />
      ) : (
        <div className="card">
          <div className="card-body">
            <table className="w-full text-sm">
              <thead className="text-xs text-muted-foreground">
                <tr className="border-b border-border">
                  <th className="text-left py-2">Vehicle</th>
                  <th className="text-left">Type</th>
                  <th className="text-left">Plate</th>
                  <th className="text-left">VIN</th>
                  <th className="text-left">Driver</th>
                  <th className="text-left">Parked at</th>
                  <th className="text-left">Reg. expires</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {vehicles.map((v) => {
                  const typeLabel =
                    VEHICLE_TYPE_LABELS[
                      v.vehicle_type as keyof typeof VEHICLE_TYPE_LABELS
                    ] ?? v.vehicle_type.replace(/_/g, " ");
                  return (
                    <tr
                      key={v.vehicle_id}
                      className="border-b border-border table-row-hover cursor-pointer"
                      onClick={() => setEditingId(v.vehicle_id)}
                    >
                      <td className="py-2">
                        <div className="flex items-center gap-3">
                          <VehicleThumb vehicle={v} />
                          <div>
                            <div className="font-medium">
                              {v.year ? `${v.year} ` : ""}
                              {v.make} {v.model}
                            </div>
                            <div className="text-xs text-muted-foreground">
                              {v.nickname ?? v.color ?? ""}
                            </div>
                          </div>
                        </div>
                      </td>
                      <td className="capitalize">{typeLabel}</td>
                      <td>
                        {v.license_plate_number_last_four
                          ? `•••${v.license_plate_number_last_four}${
                              v.license_plate_state_or_region
                                ? ` (${v.license_plate_state_or_region})`
                                : ""
                            }`
                          : "—"}
                      </td>
                      <td>
                        {v.vehicle_identification_number_last_four
                          ? `•••${v.vehicle_identification_number_last_four}`
                          : "—"}
                      </td>
                      <td>
                        {v.primary_driver_person_id
                          ? (() => {
                              const p = peopleById.get(v.primary_driver_person_id!);
                              return p ? `${p.first_name} ${p.last_name}` : "—";
                            })()
                          : "—"}
                      </td>
                      <td>
                        {v.residence_id
                          ? residencesById.get(v.residence_id)?.label ?? "—"
                          : "—"}
                      </td>
                      <td>{v.registration_expiration_date ?? "—"}</td>
                      <td className="text-right whitespace-nowrap">
                        <button
                          className="text-muted-foreground hover:text-foreground mr-3"
                          onClick={(e) => {
                            e.stopPropagation();
                            setEditingId(v.vehicle_id);
                          }}
                          aria-label="Edit vehicle"
                        >
                          <Pencil className="h-4 w-4" />
                        </button>
                        <button
                          className="text-destructive hover:text-destructive/80"
                          onClick={(e) => {
                            e.stopPropagation();
                            if (confirm("Delete this vehicle?"))
                              del.mutate(v.vehicle_id);
                          }}
                          aria-label="Delete vehicle"
                        >
                          <Trash2 className="h-4 w-4" />
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <VehicleModal
        open={editingId !== null}
        mode={editingId === "new" ? "create" : "edit"}
        vehicle={editingVehicle}
        familyId={Number(familyId)}
        people={people ?? []}
        residences={residences ?? []}
        onClose={() => setEditingId(null)}
      />
    </div>
  );
}

function VehicleThumb({ vehicle }: { vehicle: Vehicle }) {
  if (vehicle.profile_image_path) {
    return (
      <img
        src={`/api/media/${vehicle.profile_image_path}`}
        alt={`${vehicle.make} ${vehicle.model}`}
        className="h-10 w-14 object-cover rounded border border-border bg-muted"
      />
    );
  }
  return (
    <div className="h-10 w-14 rounded border border-border bg-muted flex items-center justify-center text-muted-foreground">
      <Car className="h-5 w-5" />
    </div>
  );
}

function VehicleModal({
  open,
  mode,
  vehicle,
  familyId,
  people,
  residences,
  onClose,
}: {
  open: boolean;
  mode: "create" | "edit";
  vehicle: Vehicle | null;
  familyId: number;
  people: Person[];
  residences: Residence[];
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const { register, handleSubmit, reset } = useForm<VehicleForm>({
    defaultValues: emptyForm(),
  });

  useEffect(() => {
    if (!open) return;
    if (mode === "edit" && vehicle) reset(vehicleToForm(vehicle));
    else reset(emptyForm());
  }, [open, mode, vehicle, reset]);

  const invalidate = () =>
    qc.invalidateQueries({ queryKey: ["vehicles", String(familyId)] });

  const create = useMutation({
    mutationFn: (v: VehicleForm) => {
      const cleaned = cleanPayload(v, [...NUMERIC_FIELDS]);
      return api.post<Vehicle>("/api/vehicles", {
        ...cleaned,
        family_id: familyId,
      });
    },
    onSuccess: (v) => {
      invalidate();
      toast.success(
        `Added ${v.year ? `${v.year} ` : ""}${v.make} ${v.model}.`
      );
      onClose();
    },
    onError: (err: Error) => toast.error(`Could not add vehicle: ${err.message}`),
  });

  const update = useMutation({
    mutationFn: (v: VehicleForm) => {
      if (!vehicle) throw new Error("No vehicle selected");
      const cleaned = cleanPayload(v, [...NUMERIC_FIELDS]);
      return api.patch<Vehicle>(`/api/vehicles/${vehicle.vehicle_id}`, cleaned);
    },
    onSuccess: (v) => {
      invalidate();
      toast.success(`Saved ${v.make} ${v.model}.`);
      onClose();
    },
    onError: (err: Error) => toast.error(`Save failed: ${err.message}`),
  });

  const onSubmit = (v: VehicleForm) => {
    if (mode === "create") create.mutate(v);
    else update.mutate(v);
  };

  const pending = create.isPending || update.isPending;

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={mode === "create" ? "Add vehicle" : "Edit vehicle"}
      wide
      footer={
        <>
          <button className="btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn-primary"
            disabled={pending}
            onClick={handleSubmit(onSubmit)}
          >
            {pending
              ? "Saving…"
              : mode === "create"
                ? "Add vehicle"
                : "Save changes"}
          </button>
        </>
      }
    >
      <form className="grid grid-cols-3 gap-4" onSubmit={(e) => e.preventDefault()}>
        <Field label="Type" htmlFor="vehicle_type">
          <select
            id="vehicle_type"
            className="input"
            {...register("vehicle_type", { required: true })}
          >
            {VEHICLE_TYPES.map((t) => (
              <option key={t} value={t}>
                {VEHICLE_TYPE_LABELS[t]}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Year" htmlFor="year">
          <input id="year" className="input" type="number" {...register("year")} />
        </Field>
        <Field label="Nickname" htmlFor="nickname">
          <input id="nickname" className="input" {...register("nickname")} />
        </Field>
        <Field label="Make" htmlFor="make">
          <input id="make" className="input" {...register("make", { required: true })} />
        </Field>
        <Field label="Model" htmlFor="model">
          <input id="model" className="input" {...register("model", { required: true })} />
        </Field>
        <Field label="Trim" htmlFor="trim">
          <input id="trim" className="input" {...register("trim")} />
        </Field>
        <Field label="Color" htmlFor="color">
          <input id="color" className="input" {...register("color")} />
        </Field>
        <Field label="Body style" htmlFor="body_style">
          <select id="body_style" className="input" {...register("body_style")}>
            <option value="">—</option>
            {BODY_STYLES.map((b) => (
              <option key={b} value={b}>
                {b.replace(/_/g, " ")}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Fuel type" htmlFor="fuel_type">
          <select id="fuel_type" className="input" {...register("fuel_type")}>
            <option value="">—</option>
            {FUEL_TYPES.map((b) => (
              <option key={b} value={b}>
                {b.replace(/_/g, " ")}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Primary driver" htmlFor="primary_driver_person_id">
          <select
            id="primary_driver_person_id"
            className="input"
            {...register("primary_driver_person_id")}
          >
            <option value="">—</option>
            {people.map((p) => (
              <option key={p.person_id} value={p.person_id}>
                {p.first_name} {p.last_name}
              </option>
            ))}
          </select>
        </Field>
        <Field
          label="Parked at"
          htmlFor="residence_id"
          hint="Optional home base for this vehicle (e.g. the boat at the lake cabin)."
        >
          <select id="residence_id" className="input" {...register("residence_id")}>
            <option value="">—</option>
            {residences.map((r) => (
              <option key={r.residence_id} value={r.residence_id}>
                {r.label}
                {r.is_primary_residence ? " (primary)" : ""}
              </option>
            ))}
          </select>
        </Field>
        <EncryptedField
          label="VIN"
          htmlFor="vehicle_identification_number"
          lastFour={vehicle?.vehicle_identification_number_last_four ?? null}
          mode={mode}
        >
          <input
            id="vehicle_identification_number"
            className="input"
            autoComplete="off"
            placeholder={
              mode === "edit" && vehicle?.vehicle_identification_number_last_four
                ? "Type a new VIN to replace, or leave blank"
                : ""
            }
            {...register("vehicle_identification_number")}
          />
        </EncryptedField>
        <EncryptedField
          label="License plate"
          htmlFor="license_plate_number"
          lastFour={vehicle?.license_plate_number_last_four ?? null}
          mode={mode}
        >
          <input
            id="license_plate_number"
            className="input"
            autoComplete="off"
            placeholder={
              mode === "edit" && vehicle?.license_plate_number_last_four
                ? "Type a new plate to replace, or leave blank"
                : ""
            }
            {...register("license_plate_number")}
          />
        </EncryptedField>
        <Field label="Plate state / region" htmlFor="license_plate_state_or_region">
          <input
            id="license_plate_state_or_region"
            className="input"
            {...register("license_plate_state_or_region")}
          />
        </Field>
        <Field label="Registration expires" htmlFor="registration_expiration_date">
          <input
            id="registration_expiration_date"
            type="date"
            className="input"
            {...register("registration_expiration_date")}
          />
        </Field>
        <Field label="Current mileage" htmlFor="current_mileage">
          <input
            id="current_mileage"
            type="number"
            className="input"
            {...register("current_mileage")}
          />
        </Field>
        <Field label="Purchase date" htmlFor="purchase_date">
          <input
            id="purchase_date"
            type="date"
            className="input"
            {...register("purchase_date")}
          />
        </Field>
        <Field label="Purchase price (USD)" htmlFor="purchase_price_usd">
          <input
            id="purchase_price_usd"
            type="number"
            step="0.01"
            className="input"
            {...register("purchase_price_usd")}
          />
        </Field>
        <div className="col-span-3">
          <Field label="Notes" htmlFor="notes">
            <textarea id="notes" rows={2} className="input" {...register("notes")} />
          </Field>
        </div>
      </form>

      {mode === "edit" && vehicle && (
        <VehiclePhotoSection vehicle={vehicle} />
      )}
    </Modal>
  );
}

function VehiclePhotoSection({ vehicle }: { vehicle: Vehicle }) {
  const qc = useQueryClient();
  const toast = useToast();
  const fileRef = useRef<HTMLInputElement>(null);

  const invalidate = () =>
    qc.invalidateQueries({
      queryKey: ["vehicles", String(vehicle.family_id)],
    });

  const upload = useMutation({
    mutationFn: async (file: File) => {
      const form = new FormData();
      form.append("file", file);
      return api.upload<Vehicle>(
        `/api/vehicles/${vehicle.vehicle_id}/profile-photo`,
        form
      );
    },
    onSuccess: () => {
      invalidate();
      toast.success("Vehicle photo updated.");
      if (fileRef.current) fileRef.current.value = "";
    },
    onError: (err: Error) => toast.error(`Photo upload failed: ${err.message}`),
  });

  const remove = useMutation({
    mutationFn: () => api.del(`/api/vehicles/${vehicle.vehicle_id}/profile-photo`),
    onSuccess: () => {
      invalidate();
      toast.success("Vehicle photo removed.");
    },
    onError: (err: Error) => toast.error(`Could not remove photo: ${err.message}`),
  });

  return (
    <div className="mt-6 border-t border-border pt-4">
      <div className="text-sm font-medium mb-3">Profile photo</div>
      <div className="flex items-center gap-4">
        <div className="h-24 w-32 rounded-md border border-border bg-muted overflow-hidden flex items-center justify-center">
          {vehicle.profile_image_path ? (
            <img
              src={`/api/media/${vehicle.profile_image_path}`}
              alt={`${vehicle.make} ${vehicle.model}`}
              className="h-full w-full object-cover"
            />
          ) : (
            <Car className="h-8 w-8 text-muted-foreground" />
          )}
        </div>
        <div className="flex flex-col gap-2">
          <input
            ref={fileRef}
            type="file"
            accept="image/*"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) upload.mutate(f);
            }}
          />
          <button
            type="button"
            className="btn-secondary"
            onClick={() => fileRef.current?.click()}
            disabled={upload.isPending}
          >
            <Camera className="h-4 w-4" />
            {upload.isPending
              ? "Uploading…"
              : vehicle.profile_image_path
                ? "Replace photo"
                : "Upload photo"}
          </button>
          {vehicle.profile_image_path && (
            <button
              type="button"
              className="text-destructive hover:text-destructive/80 text-xs inline-flex items-center gap-1 self-start"
              onClick={() => {
                if (confirm("Remove vehicle photo?")) remove.mutate();
              }}
              disabled={remove.isPending}
            >
              <X className="h-3.5 w-3.5" /> Remove
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
