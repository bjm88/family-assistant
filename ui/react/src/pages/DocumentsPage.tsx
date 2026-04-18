import { useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { Download, FileText, Trash2, Upload } from "lucide-react";
import { api } from "@/lib/api";
import type { DocumentRecord, Person } from "@/lib/types";
import { PageHeader } from "@/components/PageHeader";
import { EmptyState } from "@/components/EmptyState";
import { Modal } from "@/components/Modal";
import { Field } from "@/components/Field";
import { useToast } from "@/components/Toast";

const CATEGORIES = [
  "tax",
  "medical",
  "legal",
  "education",
  "financial",
  "identity_scan",
  "insurance",
  "receipt",
  "warranty",
  "other",
];

export default function DocumentsPage() {
  const { familyId } = useParams();
  const qc = useQueryClient();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const { data } = useQuery<DocumentRecord[]>({
    queryKey: ["documents", familyId],
    queryFn: () => api.get<DocumentRecord[]>(`/api/documents?family_id=${familyId}`),
  });
  const { data: people } = useQuery<Person[]>({
    queryKey: ["people", familyId],
    queryFn: () => api.get<Person[]>(`/api/people?family_id=${familyId}`),
  });

  const peopleById = new Map((people ?? []).map((p) => [p.person_id, p]));

  const upload = useMutation({
    mutationFn: async (v: any) => {
      const file: File | undefined = fileRef.current?.files?.[0];
      if (!file) throw new Error("Please choose a file.");
      const form = new FormData();
      form.append("file", file);
      form.append("family_id", String(familyId));
      form.append("title", v.title);
      if (v.document_category) form.append("document_category", v.document_category);
      if (v.person_id) form.append("person_id", String(v.person_id));
      if (v.notes) form.append("notes", v.notes);
      return api.upload<DocumentRecord>("/api/documents", form);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["documents", familyId] });
      setOpen(false);
      reset();
      if (fileRef.current) fileRef.current.value = "";
      toast.success("Document uploaded.");
    },
    onError: (err: Error) => toast.error(`Upload failed: ${err.message}`),
  });
  const del = useMutation({
    mutationFn: (id: number) => api.del(`/api/documents/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["documents", familyId] });
      toast.success("Document removed.");
    },
    onError: (err: Error) => toast.error(`Could not remove document: ${err.message}`),
  });

  const { register, handleSubmit, reset } = useForm<any>();

  return (
    <div>
      <PageHeader
        title="Documents"
        description="Tax returns, medical records, wills, receipts — anything worth keeping."
        actions={
          <button className="btn-primary" onClick={() => setOpen(true)}>
            <Upload className="h-4 w-4" /> Upload
          </button>
        }
      />

      {!data || data.length === 0 ? (
        <EmptyState
          icon={FileText}
          title="No documents yet"
          description="Upload PDFs and scans — categorized by family or by person."
        />
      ) : (
        <div className="card">
          <div className="card-body">
            <table className="w-full text-sm">
              <thead className="text-xs text-muted-foreground">
                <tr className="border-b border-border">
                  <th className="text-left py-2">Title</th>
                  <th className="text-left">Category</th>
                  <th className="text-left">Person</th>
                  <th className="text-left">File</th>
                  <th className="text-left">Uploaded</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {data.map((d) => (
                  <tr key={d.document_id} className="border-b border-border table-row-hover">
                    <td className="py-2 font-medium">{d.title}</td>
                    <td>{d.document_category ?? "—"}</td>
                    <td>
                      {d.person_id
                        ? (() => {
                            const p = peopleById.get(d.person_id!);
                            return p ? `${p.first_name} ${p.last_name}` : "—";
                          })()
                        : "family"}
                    </td>
                    <td className="text-muted-foreground">{d.original_file_name}</td>
                    <td>{new Date(d.created_at).toLocaleDateString()}</td>
                    <td className="text-right">
                      <a
                        className="text-primary hover:text-primary/80 mr-3 inline-flex"
                        href={`/api/documents/${d.document_id}/download`}
                      >
                        <Download className="h-4 w-4" />
                      </a>
                      <button
                        className="text-destructive hover:text-destructive/80"
                        onClick={() => {
                          if (confirm("Delete this document?")) del.mutate(d.document_id);
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
        title="Upload document"
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
              disabled={upload.isPending}
              onClick={handleSubmit((v) => upload.mutate(v))}
            >
              {upload.isPending ? "Uploading…" : "Upload"}
            </button>
          </>
        }
      >
        <form className="space-y-4" onSubmit={(e) => e.preventDefault()}>
          <Field label="Title" htmlFor="title">
            <input
              id="title"
              className="input"
              {...register("title", { required: true })}
            />
          </Field>
          <Field label="File" htmlFor="file">
            <input id="file" ref={fileRef} type="file" className="input" />
          </Field>
          <div className="grid grid-cols-2 gap-4">
            <Field label="Category" htmlFor="document_category">
              <select
                id="document_category"
                className="input"
                {...register("document_category")}
              >
                <option value="">—</option>
                {CATEGORIES.map((c) => (
                  <option key={c} value={c}>
                    {c.replace(/_/g, " ")}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Person (optional)" htmlFor="person_id">
              <select id="person_id" className="input" {...register("person_id")}>
                <option value="">— family-wide</option>
                {(people ?? []).map((p) => (
                  <option key={p.person_id} value={p.person_id}>
                    {p.first_name} {p.last_name}
                  </option>
                ))}
              </select>
            </Field>
          </div>
          <Field label="Notes" htmlFor="notes">
            <textarea id="notes" className="input" rows={2} {...register("notes")} />
          </Field>
        </form>
      </Modal>
    </div>
  );
}
