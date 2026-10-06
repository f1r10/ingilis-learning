import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { api } from "../api/client";

interface StudentRow {
  id: string;
  name: string;
  surname: string;
  username: string;
  status: string;
  ui_language?: string | null;
  active_key_prefix?: string | null;
}

export default function Students() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const [form, setForm] = useState({ name: "", surname: "", username: "" });
  const [revealed, setRevealed] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const list = useQuery({
    queryKey: ["students", q],
    queryFn: () => api.get<{ items: StudentRow[] }>(`/students?q=${encodeURIComponent(q)}`),
  });

  const create = useMutation({
    mutationFn: (payload: typeof form) => api.post<{ access_key: string }>("/students", payload),
    onSuccess: (data) => {
      setRevealed(data.access_key);
      setForm({ name: "", surname: "", username: "" });
      qc.invalidateQueries({ queryKey: ["students"] });
    },
    onError: (e: any) => setError(e?.message),
  });

  const suggest = useMutation({
    mutationFn: () => api.get<{ username: string }>(`/students/suggest-username?name=${encodeURIComponent(form.name)}&surname=${encodeURIComponent(form.surname)}`),
    onSuccess: (d) => setForm((f) => ({ ...f, username: d.username })),
  });

  async function rotate(id: string) {
    try {
      const d = await api.post<{ access_key: string }>(`/students/${id}/access-key/rotate`);
      setError(null);
      setRevealed(d.access_key);
      qc.invalidateQueries({ queryKey: ["students"] });
    } catch (e: any) {
      // 409 rotation_in_progress / 404 unknown student must not die silently.
      setError(e?.message ?? "Could not rotate the key");
    }
  }

  return (
    <div>
      <h1>{t("students.title")}</h1>

      <div className="card stack" style={{ marginBottom: 16 }}>
        <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
          <input className="input" style={{ maxWidth: 180 }} placeholder={t("students.name")} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <input className="input" style={{ maxWidth: 180 }} placeholder={t("students.surname")} value={form.surname} onChange={(e) => setForm({ ...form, surname: e.target.value })} />
          <input className="input" style={{ maxWidth: 180 }} placeholder={t("students.username")} value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} />
          <button type="button" className="btn secondary" onClick={() => suggest.mutate()} disabled={!form.name || !form.surname}>Suggest</button>
        </div>
        {error && <div className="alert error">{error}</div>}
        <button className="btn" onClick={() => create.mutate(form)} disabled={!form.name || !form.surname || !form.username}>
          {t("students.new")}
        </button>
        {revealed && (
          <div className="card" style={{ background: "var(--accent-weak)" }}>
            <div className="small muted">{t("students.key_reveal")}</div>
            <div className="mono">{revealed}</div>
            <button className="btn ghost" onClick={() => navigator.clipboard?.writeText(revealed)}>Copy</button>
          </div>
        )}
      </div>

      <input className="input" style={{ maxWidth: 300, marginBottom: 12 }} placeholder="Search…" value={q} onChange={(e) => setQ(e.target.value)} />

      <div className="card">
        <table>
          <thead>
            <tr>
              <th>{t("students.name")}</th>
              <th>{t("students.username")}</th>
              <th>Status</th>
              <th>Key</th>
              <th>{t("common.actions")}</th>
            </tr>
          </thead>
          <tbody>
            {list.data?.items.map((s) => (
              <tr key={s.id}>
                <td>{s.name} {s.surname}</td>
                <td className="mono">{s.username}</td>
                <td>{s.status}</td>
                <td className="mono small">{s.active_key_prefix || "—"}</td>
                <td><button className="btn ghost" onClick={() => rotate(s.id)}>Rotate key</button></td>
              </tr>
            ))}
            {!list.isLoading && (list.data?.items.length ?? 0) === 0 && (
              <tr><td colSpan={5} className="muted">{t("students.empty")}</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
