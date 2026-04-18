import { useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { Car, Plus, Trash2 } from "lucide-react";
import { api } from "@/lib/api";
import type { Person, Vehicle } from "@/lib/types";
import { PageHeader } from "@/components/PageHeader";
import { EmptyState } from "@/components/EmptyState";
import { Modal } from "@/components/Modal";
import { Field } from "@/components/Field";
import { useToast } from "@/components/Toast";
import { cleanPayload } from "@/lib/form";

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

type VehicleForm = Record<string, string | undefined>;

export default function VehiclesPage() {
  const { familyId } = useParams();
  const qc = useQueryClient();
  const toast = useToast();
  const [open, setOpen] = useState(false);

  const { data: vehicles } = useQuery<Vehicle[]>({
    queryKey: ["vehicles", familyId],
    queryFn: () => api.get<Vehicle[]>(`/api/vehicles?family_id=${familyId}`),
  });
  const { data: people } = useQuery<Person[]>({
    queryKey: ["people", familyId],
    queryFn: () => api.get<Person[]>(`/api/people?family_id=${familyId}`),
  });

  const create = useMutation({
    mutationFn: (v: VehicleForm) => {
      const cleaned = cleanPayload(v, [
        "year",
        "current_mileage",
        "primary_driver_person_id",
        "purchase_price_usd",
      ]);
      return api.post<Vehicle>("/api/vehicles", {
        ...cleaned,
        family_id: Number(familyId),
      });
    },
    onSuccess: (v) => {
      qc.invalidateQueries({ queryKey: ["vehicles", familyId] });
      setOpen(false);
      reset();
      toast.success(
        `Added ${v.year ? `${v.year} ` : ""}${v.make} ${v.model}.`
      );
    },
    onError: (err: Error) => toast.error(`Could not add vehicle: ${err.message}`),
  });
  const del = useMutation({
    mutationFn: (id: number) => api.del(`/api/vehicles/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["vehicles", familyId] });
      toast.success("Vehicle removed.");
    },
    onError: (err: Error) => toast.error(`Could not remove vehicle: ${err.message}`),
  });

  const { register, handleSubmit, reset } = useForm<VehicleForm>();

  const peopleById = new Map((people ?? []).map((p) => [p.person_id, p]));

  return (
    <div>
      <PageHeader
        title="Vehicles"
        description="Cars, trucks, motorcycles and boats. VIN and plate are encrypted."
        actions={
          <button className="btn-primary" onClick={() => setOpen(true)}>
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
            <button className="btn-primary" onClick={() => setOpen(true)}>
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
                  <th className="text-left">Plate</th>
                  <th className="text-left">VIN</th>
                  <th className="text-left">Driver</th>
                  <th className="text-left">Reg. expires</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {vehicles.map((v) => (
                  <tr key={v.vehicle_id} className="border-b border-border table-row-hover">
                    <td className="py-2">
                      <div className="font-medium">
                        {v.year ? `${v.year} ` : ""}
                        {v.make} {v.model}
                      </div>
                      <div className="text-xs text-muted-foreground">
                        {v.nickname ?? v.color ?? ""}
                      </div>
                    </td>
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
                    <td>{v.registration_expiration_date ?? "—"}</td>
                    <td className="text-right">
                      <button
                        className="text-destructive hover:text-destructive/80"
                        onClick={() => {
                          if (confirm("Delete this vehicle?")) del.mutate(v.vehicle_id);
                        }}
                      >
                        <Trash2 className="h-4 w-4" />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <Modal
        open={open}
        onClose={() => {
          setOpen(false);
          reset();
        }}
        title="Add vehicle"
        wide
        footer={
          <>
            <button
              className="btn-secondary"
              onClick={() => {
                setOpen(false);
                reset();
              }}
            >
              Cancel
            </button>
            <button
              className="btn-primary"
              disabled={create.isPending}
              onClick={handleSubmit((v) => create.mutate(v))}
            >
              {create.isPending ? "Adding…" : "Add vehicle"}
            </button>
          </>
        }
      >
        <form className="grid grid-cols-3 gap-4" onSubmit={(e) => e.preventDefault()}>
          <Field label="Year" htmlFor="year">
            <input id="year" className="input" type="number" {...register("year")} />
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
          <Field label="Nickname" htmlFor="nickname">
            <input id="nickname" className="input" {...register("nickname")} />
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
              {(people ?? []).map((p) => (
                <option key={p.person_id} value={p.person_id}>
                  {p.first_name} {p.last_name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="VIN" htmlFor="vehicle_identification_number" hint="Encrypted at rest">
            <input
              id="vehicle_identification_number"
              className="input"
              {...register("vehicle_identification_number")}
            />
          </Field>
          <Field label="License plate" htmlFor="license_plate_number" hint="Encrypted at rest">
            <input
              id="license_plate_number"
              className="input"
              {...register("license_plate_number")}
            />
          </Field>
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
          <div className="col-span-3">
            <Field label="Notes" htmlFor="notes">
              <textarea id="notes" rows={2} className="input" {...register("notes")} />
            </Field>
          </div>
        </form>
      </Modal>
    </div>
  );
}
