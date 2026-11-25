import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileText, Image as ImageIcon, Trash2, Upload } from "lucide-react";
import { api, resolveApiPath } from "@/lib/api";
import type { RecordAttachment } from "@/lib/types";
import { useConfirm } from "@/components/ConfirmDialog";
import {
  RotateImageButton,
  type RotatableImageKind,
} from "@/components/RotateImageButton";
import { useToast } from "@/components/Toast";

export interface AttachmentKindOption {
  value: string;
  label: string;
}

interface RecordAttachmentsProps {
  // Where this record's attachments live, e.g. "/api/vehicles/12/attachments".
  basePath: string;
  heading: string;
  hint: string;
  // What a file can be filed as; the first is preselected.
  kinds: AttachmentKindOption[];
  // Which kind of image this is to the rotate endpoint.
  rotateKind: RotatableImageKind;
  // Called once, when this section goes away (the dialog closes), if
  // files were added or removed — so the page behind can refresh its
  // file counts without disturbing a form that is still being edited.
  onChanged?: () => void;
}

function formatSize(bytes: number | null): string {
  if (bytes == null) return "";
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * Photos and PDFs attached to one record (an insurance policy or a
 * vehicle): add, open, rotate (photos) and remove.
 */
export function RecordAttachments({
  basePath,
  heading,
  hint,
  kinds,
  rotateKind,
  onChanged,
}: RecordAttachmentsProps) {
  const qc = useQueryClient();
  const toast = useToast();
  const confirm = useConfirm();
  const fileRef = useRef<HTMLInputElement>(null);
  const [kind, setKind] = useState(kinds[0].value);
  const [label, setLabel] = useState("");
  const queryKey = ["record-attachments", basePath];

  const { data: attachments } = useQuery<RecordAttachment[]>({
    queryKey,
    queryFn: () => api.get<RecordAttachment[]>(basePath),
  });

  // Tell the page behind about changes when the dialog closes.
  const changed = useRef(false);
  const closed = useRef(false);
  const onChangedRef = useRef(onChanged);
  onChangedRef.current = onChanged;
  useEffect(() => {
    closed.current = false;
    return () => {
      closed.current = true;
      if (changed.current) onChangedRef.current?.();
    };
  }, []);

  const refresh = () => {
    // An upload or removal that finishes after the dialog was closed
    // still has to reach the page behind.
    if (closed.current) onChangedRef.current?.();
    else changed.current = true;
    return qc.invalidateQueries({ queryKey });
  };

  const upload = useMutation({
    mutationFn: async (files: File[]) => {
      // One at a time, so a failure says which file it was and the
      // ones before it are kept.
      for (const file of files) {
        const form = new FormData();
        form.append("file", file);
        form.append("attachment_kind", kind);
        if (label.trim()) form.append("label", label.trim());
        try {
          await api.upload<RecordAttachment>(basePath, form);
        } catch (err) {
          throw new Error(`${file.name}: ${(err as Error).message}`);
        }
      }
      return files.length;
    },
    onSuccess: (count) => {
      setLabel("");
      toast.success(count === 1 ? "File added." : `${count} files added.`);
    },
    onError: (err: Error) => toast.error(`Could not add file. ${err.message}`),
    onSettled: () => {
      if (fileRef.current) fileRef.current.value = "";
      return refresh();
    },
  });

  const remove = useMutation({
    mutationFn: (id: number) => api.del(`${basePath}/${id}`),
    onSuccess: () => {
      toast.success("File removed.");
      return refresh();
    },
    onError: (err: Error) => toast.error(`Could not remove file: ${err.message}`),
  });

  // Re-file under a different kind (say, a PDF picked together with
  // the card photos and filed as a card).
  const refile = useMutation({
    mutationFn: (v: { id: number; kind: string }) =>
      api.patch<RecordAttachment>(`${basePath}/${v.id}`, {
        attachment_kind: v.kind,
      }),
    onError: (err: Error) => toast.error(`Could not update file: ${err.message}`),
    onSettled: () => qc.invalidateQueries({ queryKey }),
  });

  const kindLabel = (value: string) =>
    kinds.find((k) => k.value === value)?.label ?? value.replace(/_/g, " ");

  return (
    <div className="mt-6 border-t border-border pt-4">
      <div className="text-sm font-medium">{heading}</div>
      <div className="text-xs text-muted-foreground mt-1 mb-3">{hint}</div>

      <div className="flex flex-wrap items-center gap-2 mb-4">
        <select
          aria-label="Kind of file"
          className="input w-auto"
          value={kind}
          onChange={(e) => setKind(e.target.value)}
        >
          {kinds.map((k) => (
            <option key={k.value} value={k.value}>
              {k.label}
            </option>
          ))}
        </select>
        <input
          aria-label="Label (optional)"
          className="input w-auto flex-1 min-w-[10rem]"
          placeholder='Label (optional), e.g. "Front"'
          maxLength={200}
          value={label}
          onChange={(e) => setLabel(e.target.value)}
        />
        <input
          ref={fileRef}
          type="file"
          accept="image/*,application/pdf"
          multiple
          className="hidden"
          onChange={(e) => {
            const files = Array.from(e.target.files ?? []);
            if (files.length > 0) upload.mutate(files);
          }}
        />
        <button
          type="button"
          className="btn-secondary"
          onClick={() => fileRef.current?.click()}
          disabled={upload.isPending}
        >
          <Upload className="h-4 w-4" />
          {upload.isPending ? "Uploading…" : "Add photo or PDF"}
        </button>
      </div>

      {!attachments || attachments.length === 0 ? (
        <div className="text-xs text-muted-foreground flex items-center gap-2">
          <ImageIcon className="h-4 w-4" />
          No files yet.
        </div>
      ) : (
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-4">
          {attachments.map((a) => {
            // The address stays the same when a photo is rotated, so tag
            // it with the last-changed time to get the current picture.
            const url = `${resolveApiPath(
              `${basePath}/${a.attachment_id}/file`
            )}?v=${encodeURIComponent(a.updated_at)}`;
            const name = a.label ?? a.original_file_name;
            return (
              <div
                key={a.attachment_id}
                className="border border-border rounded-lg overflow-hidden flex flex-col bg-white"
              >
                <AttachmentPreview attachment={a} url={url} />
                <div className="p-3 flex-1 flex flex-col gap-1">
                  <select
                    aria-label={`Kind of file for ${name}`}
                    className="font-medium text-sm bg-transparent -ml-1 w-full cursor-pointer"
                    value={a.attachment_kind}
                    disabled={refile.isPending}
                    onChange={(e) =>
                      refile.mutate({ id: a.attachment_id, kind: e.target.value })
                    }
                  >
                    {!kinds.some((k) => k.value === a.attachment_kind) && (
                      <option value={a.attachment_kind}>
                        {kindLabel(a.attachment_kind)}
                      </option>
                    )}
                    {kinds.map((k) => (
                      <option key={k.value} value={k.value}>
                        {k.label}
                      </option>
                    ))}
                  </select>
                  <div
                    className="text-xs text-muted-foreground truncate"
                    title={a.original_file_name}
                  >
                    {name}
                    {a.file_size_bytes != null && ` · ${formatSize(a.file_size_bytes)}`}
                  </div>
                  <div className="mt-auto pt-2 flex items-center justify-between gap-2">
                    {a.is_image ? (
                      <RotateImageButton
                        kind={rotateKind}
                        recordId={a.attachment_id}
                        src={url}
                        label={`${kindLabel(a.attachment_kind).toLowerCase()} "${name}"`}
                        onRotated={() => qc.invalidateQueries({ queryKey })}
                      />
                    ) : (
                      <span />
                    )}
                    <button
                      type="button"
                      className="text-destructive hover:text-destructive/80 text-xs inline-flex items-center gap-1"
                      disabled={remove.isPending}
                      onClick={async () => {
                        if (
                          await confirm({
                            title: `Remove "${name}"?`,
                            message: "The file will be permanently deleted.",
                            destructive: true,
                            confirmLabel: "Remove file",
                          })
                        ) {
                          remove.mutate(a.attachment_id);
                        }
                      }}
                    >
                      <Trash2 className="h-3.5 w-3.5" /> Remove
                    </button>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

/** The picture itself, or a tile for a PDF; either opens the file in a new tab. */
function AttachmentPreview({
  attachment,
  url,
}: {
  attachment: RecordAttachment;
  url: string;
}) {
  // Some browsers can't draw every photo format (HEIC outside Safari).
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [url]);
  const showImage = attachment.is_image && !failed;

  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer"
      title={`Open ${attachment.original_file_name}`}
      className="aspect-[4/3] bg-muted flex items-center justify-center overflow-hidden"
    >
      {showImage ? (
        <img
          src={url}
          alt={attachment.label ?? attachment.original_file_name}
          className="h-full w-full object-contain"
          onError={() => setFailed(true)}
        />
      ) : (
        <div className="text-xs text-muted-foreground flex flex-col items-center gap-1">
          {attachment.is_image ? (
            <ImageIcon className="h-8 w-8" />
          ) : (
            <FileText className="h-8 w-8" />
          )}
          <span>{attachment.is_image ? "Open photo" : "Open PDF"}</span>
        </div>
      )}
    </a>
  );
}
