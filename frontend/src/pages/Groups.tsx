import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { api } from "../api/client";

interface GroupRow { id: string; name: string; member_count: number; }

export default function Groups() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [name, setName] = useState("");

  const list = useQuery({ queryKey: ["groups"], queryFn: () => api.get<{ items: GroupRow[] }>("/groups") });
  const create = useMutation({
    mutationFn: () => api.post("/groups", { name }),
    onSuccess: () => { setName(""); qc.invalidateQueries({ queryKey: ["groups"] }); },
  });

  return (
    <div>
      <h1>{t("groups.title")}</h1>
      <div className="card row" style={{ marginBottom: 16 }}>
        <input className="input" placeholder={t("groups.name")} value={name} onChange={(e) => setName(e.target.value)} />
        <button className="btn" onClick={() => create.mutate()} disabled={!name}>{t("groups.new")}</button>
      </div>
      <div className="card">
        <table>
          <thead><tr><th>{t("groups.name")}</th><th>Members</th></tr></thead>
          <tbody>
            {list.data?.items.map((g) => (
              <tr key={g.id}><td>{g.name}</td><td>{g.member_count}</td></tr>
            ))}
            {!list.isLoading && (list.data?.items.length ?? 0) === 0 && (
              <tr><td colSpan={2} className="muted">{t("groups.empty")}</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
