// One catalog: its rules, the references it holds, and what a learner would be served.
//
// The whole point of a catalog is that it owns no content, so this screen never edits an
// exercise - it names one. Every row in the list is a reference, and the title, the state
// and the "can a learner open this" answer are read from the row it points at, which is why
// a hole shows up here as `missing` or `broken_block` instead of being quietly dropped.
//
// Two things the teacher needs to see together: the lifecycle status of this catalog, and
// whether a learner can actually reach it. A ready collection inside a draft folder is not
// reachable, and saying only "ready" would send a teacher to a class with nothing to open.
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { ApiError } from "../api/client";
import { catalogsApi, type Catalog, type CatalogItem, type CatalogDraft } from "../api/catalogs";
import { listeningApi } from "../api/listening";
import { type Step } from "../api/practice";
import { questionsApi } from "../api/questions";
import { readingApi } from "../api/reading";
import { vocabularyApi } from "../api/vocabulary";
import PracticeStep from "../components/PracticeStep";

const PICKER_SIZE = 20;

export default function CatalogEditor() {
  const { t } = useTranslation();
  const nav = useNavigate();
  const { id = "" } = useParams();
  const [params] = useSearchParams();
  const qc = useQueryClient();
  const isNew = !id;

  const meta = useQuery({ queryKey: ["catalogs-meta"], queryFn: catalogsApi.meta });
  const catalog = useQuery({
    queryKey: ["catalog-one", id],
    queryFn: () => catalogsApi.get(id),
    enabled: !isNew,
  });

  const [draft, setDraft] = useState<CatalogDraft>({
    name: "",
    description: null,
    parent_id: params.get("parent") || null,
    learning_language: null,
    level: null,
    shuffle_default: true,
    known_states_enabled: false,
    feedback_timing: "instant",
  });
  const [message, setMessage] = useState<string | null>(null);
  const [previewLanguage, setPreviewLanguage] = useState("");

  useEffect(() => {
    if (!catalog.data) return;
    const row = catalog.data;
    setDraft({
      name: row.name,
      description: row.description,
      parent_id: row.parent_id,
      learning_language: row.learning_language,
      level: row.level,
      shuffle_default: row.shuffle_default,
      known_states_enabled: row.known_states_enabled,
      feedback_timing: row.feedback_timing,
    });
  }, [catalog.data]);

  // Any catalog can be a folder, so the parent picker offers the whole bank rather than a
  // separate "folders only" query; the backend refuses a cycle and a fifth level, and says
  // which rule was hit.
  const possibleParents = useQuery({
    queryKey: ["catalogs", "parents"],
    queryFn: () => catalogsApi.list({ view: "bank", page_size: 200, sort: "name", order: "asc" }),
  });

  const create = useMutation({
    mutationFn: () => catalogsApi.create(draft),
    onSuccess: (row) => {
      setMessage(null);
      nav(`/catalogs/${row.id}`, { replace: true });
    },
    onError: (e: ApiError) => setMessage(e.message),
  });

  const save = useMutation({
    mutationFn: () => catalogsApi.update(id, draft),
    onSuccess: (row) => {
      setMessage(null);
      qc.setQueryData(["catalog-one", id], row);
      qc.invalidateQueries({ queryKey: ["catalogs"] });
    },
    onError: (e: ApiError) => setMessage(e.message),
  });

  const lifecycle = useMutation({
    mutationFn: async ({ action, next }: { action: "status" | "trash" | "restore"; next?: string }) => {
      if (action === "trash") return catalogsApi.trash(id);
      if (action === "restore") return catalogsApi.restore(id);
      return catalogsApi.setStatus(id, next || "ready");
    },
    onSuccess: (row) => {
      setMessage(null);
      if (row && "id" in row) qc.setQueryData(["catalog-one", id], row);
      qc.invalidateQueries({ queryKey: ["catalogs"] });
    },
    // A publish that was refused has a reason per reference, and the server's sentence is
    // the one that names it, so it is shown rather than replaced with a generic failure.
    onError: (e: ApiError) => setMessage(e.message),
  });

  const rows = catalog.data?.items || [];
  const dirty = useMemo(
    () => JSON.stringify(draft) !== JSON.stringify(snapshot(catalog.data)),
    [draft, catalog.data],
  );

  if (isNew) {
    return (
      <div className="stack">
        <h1 style={{ margin: 0 }}>{params.get("folder") === "1" ? t("catalogs.new_folder") : t("catalogs.new")}</h1>
        <p className="muted small">{t("catalogs.create_hint")}</p>
        {message ? <div className="alert error">{message}</div> : null}
        <Fields
          draft={draft}
          onChange={setDraft}
          meta={meta.data}
          parents={(possibleParents.data?.items || []).filter((row) => row.id !== id)}
        />
        <div className="row" style={{ gap: 8 }}>
          <button type="button" className="btn" disabled={!draft.name.trim()} onClick={() => create.mutate()}>
            {t("catalogs.create")}
          </button>
          <Link className="btn secondary" to="/catalogs">
            {t("common.cancel")}
          </Link>
        </div>
      </div>
    );
  }

  if (catalog.isError) {
    return (
      <div className="stack">
        <Link className="btn secondary" to="/catalogs">
          ‹ {t("catalogs.back")}
        </Link>
        <div className="alert error">
          {t("common.could_not_load")} {(catalog.error as ApiError).message}
        </div>
      </div>
    );
  }

  if (!catalog.data) return <div className="card muted">{t("common.loading")}</div>;
  const row = catalog.data;

  return (
    <div className="stack">
      <div className="row">
        <Link className="btn secondary" to="/catalogs">
          ‹ {t("catalogs.back")}
        </Link>
        <span className="spacer" />
        {row.deleted_at ? (
          <button className="btn" onClick={() => lifecycle.mutate({ action: "restore" })}>
            {t("catalogs.restore")}
          </button>
        ) : (
          <>
            {row.status !== "ready" ? (
              <button className="btn" onClick={() => lifecycle.mutate({ action: "status", next: "ready" })}>
                {t("catalogs.publish")}
              </button>
            ) : (
              <button className="btn secondary" onClick={() => lifecycle.mutate({ action: "status", next: "archived" })}>
                {t("catalogs.archive")}
              </button>
            )}
            <button className="btn secondary" onClick={() => lifecycle.mutate({ action: "trash" })}>
              {t("catalogs.trash")}
            </button>
          </>
        )}
      </div>

      <h1 style={{ margin: 0 }}>{row.name}</h1>
      <div className="row small muted" style={{ flexWrap: "wrap", gap: 6 }}>
        <span className="chip">{t(`status.${row.status}`)}</span>
        <span>
          {row.available_to_learner ? t("catalogs.learners_can_open") : t("catalogs.learners_cannot_open")}
        </span>
        {row.path.length ? (
          <span>
            {t("catalogs.inside")}: {row.path.map((crumb) => crumb.name).join(" › ")}
          </span>
        ) : (
          <span>{t("catalogs.at_top_level")}</span>
        )}
      </div>

      {message ? <div className="alert error">{message}</div> : null}
      {row.unavailable_count ? (
        <div className="alert">{t("catalogs.unavailable_n", { n: row.unavailable_count })}</div>
      ) : null}

      <div className="card stack">
        <h2 style={{ margin: 0 }}>{t("catalogs.rules")}</h2>
        <p className="small muted" style={{ margin: 0 }}>
          {t("catalogs.rules_hint")}
        </p>
        <Fields
          draft={draft}
          onChange={setDraft}
          meta={meta.data}
          parents={(possibleParents.data?.items || []).filter((item) => item.id !== row.id)}
        />
        <div className="row" style={{ gap: 8 }}>
          <button type="button" className="btn" disabled={!dirty || !draft.name.trim()} onClick={() => save.mutate()}>
            {t("catalogs.save_changes")}
          </button>
          {!dirty ? <span className="small muted">{t("catalogs.saved")}</span> : null}
        </div>
      </div>

      <div className="card stack">
        <h2 style={{ margin: 0 }}>{t("catalogs.items_title")}</h2>
        <p className="small muted" style={{ margin: 0 }}>
          {t("catalogs.items_hint", { n: meta.data?.max_items ?? 300 })}
        </p>
        {rows.length === 0 ? <div className="muted small">{t("catalogs.empty_items")}</div> : null}
        {rows.map((item, index) => (
          <ItemRow
            key={item.id}
            item={item}
            index={index}
            first={index === 0}
            last={index === rows.length - 1}
            catalogId={row.id}
          />
        ))}
      </div>

      <AddPanel
        catalogId={row.id}
        held={new Set(rows.map((item) => `${item.kind}:${item.ref_id}`))}
        languages={meta.data?.learning_languages || []}
      />

      <PreviewPanel
        catalogId={row.id}
        language={previewLanguage}
        onLanguage={setPreviewLanguage}
        languages={meta.data?.learning_languages || []}
        shuffleDefault={row.shuffle_default}
        knownStatesEnabled={row.known_states_enabled}
      />
    </div>
  );
}

function snapshot(row?: Catalog): CatalogDraft | null {
  if (!row) return null;
  return {
    name: row.name,
    description: row.description,
    parent_id: row.parent_id,
    learning_language: row.learning_language,
    level: row.level,
    shuffle_default: row.shuffle_default,
    known_states_enabled: row.known_states_enabled,
    feedback_timing: row.feedback_timing,
  };
}

function Fields({
  draft,
  onChange,
  meta,
  parents,
}: {
  draft: CatalogDraft;
  onChange: (next: CatalogDraft) => void;
  meta: any;
  parents: { id: string; name: string; level_path?: string }[];
}) {
  const { t } = useTranslation();
  const set = (patch: Partial<CatalogDraft>) => onChange({ ...draft, ...patch });
  const empty = (value: string) => (value ? value : null);

  return (
    <div className="stack">
      <label className="field">
        <span>{t("catalogs.name")}</span>
        <input className="input" value={draft.name} onChange={(e) => set({ name: e.target.value })} />
      </label>
      <label className="field">
        <span>{t("catalogs.description")}</span>
        <textarea
          className="input"
          rows={2}
          value={draft.description || ""}
          onChange={(e) => set({ description: empty(e.target.value) })}
        />
      </label>
      <div className="row" style={{ flexWrap: "wrap", gap: 12 }}>
        <label className="field" style={{ minWidth: 180 }}>
          <span>{t("catalogs.parent")}</span>
          <select
            className="input"
            value={draft.parent_id || ""}
            onChange={(e) => set({ parent_id: empty(e.target.value) })}
          >
            <option value="">{t("catalogs.no_parent")}</option>
            {parents.map((row) => (
              <option key={row.id} value={row.id}>
                {row.name}
              </option>
            ))}
          </select>
        </label>
        <label className="field" style={{ minWidth: 140 }}>
          <span>{t("catalogs.language")}</span>
          <select
            className="input"
            value={draft.learning_language || ""}
            onChange={(e) => set({ learning_language: empty(e.target.value) })}
          >
            <option value="">{t("catalogs.any_language")}</option>
            {(meta?.learning_languages || []).map((code: string) => (
              <option key={code} value={code}>
                {code}
              </option>
            ))}
          </select>
        </label>
        <label className="field" style={{ minWidth: 120 }}>
          <span>{t("catalogs.level")}</span>
          <select
            className="input"
            value={draft.level || ""}
            onChange={(e) => set({ level: empty(e.target.value) })}
          >
            <option value="">{t("catalogs.no_level")}</option>
            {(meta?.levels || []).map((level: string) => (
              <option key={level} value={level}>
                {level}
              </option>
            ))}
          </select>
        </label>
        <label className="field" style={{ minWidth: 200 }}>
          <span>{t("catalogs.feedback_timing")}</span>
          <select
            className="input"
            value={draft.feedback_timing || "instant"}
            onChange={(e) => set({ feedback_timing: e.target.value })}
          >
            {(meta?.feedback_timings || ["instant", "after_session"]).map((timing: string) => (
              <option key={timing} value={timing}>
                {t(`catalogs.timing_${timing}`)}
              </option>
            ))}
          </select>
        </label>
      </div>
      <p className="small muted" style={{ margin: 0 }}>
        {t(`catalogs.timing_hint_${draft.feedback_timing || "instant"}`)}
      </p>
      <div className="row" style={{ flexWrap: "wrap", gap: 16 }}>
        <label className="row small" style={{ gap: 6 }}>
          <input
            type="checkbox"
            checked={Boolean(draft.shuffle_default)}
            onChange={(e) => set({ shuffle_default: e.target.checked })}
          />
          {t("catalogs.shuffle_default")}
        </label>
        <label className="row small" style={{ gap: 6 }}>
          <input
            type="checkbox"
            checked={Boolean(draft.known_states_enabled)}
            onChange={(e) => set({ known_states_enabled: e.target.checked })}
          />
          {t("catalogs.known_states")}
        </label>
      </div>
      <p className="small muted" style={{ margin: 0 }}>
        {t("catalogs.shuffle_hint")} · {t("catalogs.known_states_hint")}
      </p>
    </div>
  );
}

function ItemRow({
  item,
  index,
  first,
  last,
  catalogId,
}: {
  item: CatalogItem;
  index: number;
  first: boolean;
  last: boolean;
  catalogId: string;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [message, setMessage] = useState<string | null>(null);

  const commitOrder = useMutation({
    mutationFn: (itemIds: string[]) => catalogsApi.reorderItems(catalogId, itemIds),
    // The numbers come back from the server, so the optimistic swap is replaced by the
    // order it actually stored rather than the one this click asked for.
    onSuccess: (data) => {
      setMessage(null);
      qc.setQueryData<Catalog>(["catalog-one", catalogId], (current) =>
        current ? { ...current, items: data.items } : current,
      );
    },
    onError: (e: ApiError) => {
      setMessage(e.message);
      qc.invalidateQueries({ queryKey: ["catalog-one", catalogId] });
    },
  });

  const move = (delta: number) => {
    const current = qc.getQueryData<Catalog>(["catalog-one", catalogId]);
    if (!current) return;
    const items = current.items.slice();
    const target = index + delta;
    if (target < 0 || target >= items.length) return;
    [items[index], items[target]] = [items[target], items[index]];
    qc.setQueryData<Catalog>(["catalog-one", catalogId], { ...current, items });
    commitOrder.mutate(items.map((each) => each.id));
  };

  const remove = useMutation({
    mutationFn: () => catalogsApi.removeItem(item.id),
    onSuccess: () => {
      setMessage(null);
      qc.invalidateQueries({ queryKey: ["catalog-one", catalogId] });
      qc.invalidateQueries({ queryKey: ["catalogs"] });
    },
    onError: (e: ApiError) => setMessage(e.message),
  });

  return (
    <div className="row" style={{ flexWrap: "wrap", gap: 8, borderBottom: "1px solid var(--border)", paddingBottom: 8 }}>
      <span className="small muted" style={{ minWidth: 22 }}>
        {index + 1}
      </span>
      <div style={{ flex: "1 1 16rem" }}>
        <div className="small">{item.title || t("catalogs.content_gone")}</div>
        <div className="row small muted" style={{ gap: 6, flexWrap: "wrap" }}>
          <span className="chip">{t(`catalogs.kind_${item.kind}`)}</span>
          <span className="chip">{t(`catalogs.state_${item.state}`)}</span>
          {!item.available_to_learner && item.state !== "missing" && item.state !== "broken_block" ? (
            <span className="muted">{t("catalogs.not_servable_yet")}</span>
          ) : null}
          {item.detail ? <span>{item.detail}</span> : null}
        </div>
      </div>
      {item.kind === "reading" || item.kind === "listening" ? (
        <BlockPicker catalogId={catalogId} item={item} />
      ) : null}
      <div className="row" style={{ gap: 2 }}>
        <button type="button" className="btn ghost" disabled={first} onClick={() => move(-1)} aria-label={t("catalogs.move_up")}>
          ↑
        </button>
        <button type="button" className="btn ghost" disabled={last} onClick={() => move(1)} aria-label={t("catalogs.move_down")}>
          ↓
        </button>
        <button type="button" className="btn ghost" onClick={() => remove.mutate()}>
          {t("catalogs.remove")}
        </button>
      </div>
      {message ? <span className="small muted">{message}</span> : null}
    </div>
  );
}

function BlockPicker({ catalogId, item }: { catalogId: string; item: CatalogItem }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const setId = item.config?.set_id ? String(item.config.set_id) : "";

  const sets = useQuery({
    queryKey: ["catalog-blocks", item.kind, item.ref_id],
    queryFn: () => (item.kind === "reading" ? readingApi.sets(item.ref_id) : listeningApi.sets(item.ref_id)),
    // Fetched as soon as a block is named, not only once the panel opens: the closed row
    // has to say which block it points at, and it cannot answer that from an id. Opened
    // on a reference that has no block yet, the list is what the teacher came for.
    enabled: Boolean(setId) || open,
  });

  const choose = useMutation({
    mutationFn: (next: string) => catalogsApi.updateItem(item.id, { set_id: next || null }),
    onSuccess: () => {
      setMessage(null);
      setOpen(false);
      qc.invalidateQueries({ queryKey: ["catalog-one", catalogId] });
    },
    // A block that cannot be bound has a reason (it belongs to another text, for instance)
    // and the panel stays open on it rather than closing as if nothing happened.
    onError: (e: ApiError) => setMessage(e.message),
  });

  const current = (sets.data?.items || []).find((set) => set.id === setId);

  // The block is named with the words the teacher gave it. A block they left unnamed is
  // still identifiable by its place in the text, and an id on screen helps nobody. Only a
  // list that loaded and does not contain it means the block is gone.
  const blockLabel = setId
    ? current
      ? current.title || t("catalogs.block_n", { n: current.position + 1 })
      : sets.isLoading
        ? t("common.loading")
        : t("catalogs.block_gone")
    : "";

  return (
    <div className="row small" style={{ gap: 6, flexWrap: "wrap" }}>
      {!open ? (
        <button type="button" className="btn ghost" onClick={() => setOpen(true)}>
          {blockLabel ? t("catalogs.block_is", { name: blockLabel }) : t("catalogs.whole_item")}
        </button>
      ) : (
        <select
          className="input"
          style={{ maxWidth: 220 }}
          value={setId}
          autoFocus
          onChange={(e) => choose.mutate(e.target.value)}
        >
          <option value="">{t("catalogs.whole_item")}</option>
          {(sets.data?.items || []).map((set) => (
            <option key={set.id} value={set.id}>
              {set.title || t("catalogs.block_n", { n: set.position + 1 })}
            </option>
          ))}
        </select>
      )}
      {sets.isError ? <span className="muted">{t("common.could_not_load")}</span> : null}
      {message ? <span className="muted">{message}</span> : null}
    </div>
  );
}

function AddPanel({
  catalogId,
  held,
  languages,
}: {
  catalogId: string;
  held: Set<string>;
  languages: string[];
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const meta = useQuery({ queryKey: ["catalogs-meta"], queryFn: catalogsApi.meta });
  const [kind, setKind] = useState("question");
  const [q, setQ] = useState("");
  const [level, setLevel] = useState("");
  const [language, setLanguage] = useState("");
  const [page, setPage] = useState(1);
  const [message, setMessage] = useState<string | null>(null);

  // Each bank names its language filter its own way (`learning_language` for the two
  // authoring banks, `language` for the two skills banks), and an unrecognised query
  // parameter is quietly ignored, so sending the wrong one would look like a filter that
  // does nothing rather than one that was never applied.
  const bankFilters = (name: string) =>
    kind === "question" || kind === "vocabulary"
      ? { q, level, learning_language: name, status: "ready", view: "bank", page, page_size: PICKER_SIZE }
      : { q, level, language: name, status: "ready", view: "bank", page, page_size: PICKER_SIZE };

  const filters = bankFilters(language);

  const bank = useQuery({
    queryKey: ["catalog-bank", kind, filters],
    queryFn: () => {
      if (kind === "question")
        return questionsApi.list(filters).then((data) =>
          mapRows(data, (row) => ({
            id: row.id,
            title: row.prompt || t("catalogs.no_prompt"),
            detail: `${t(`questions.type_${row.type}`)} · ${row.level || "—"}`,
          })),
        );
      if (kind === "vocabulary")
        return vocabularyApi.list({ ...filters, sort: "word", order: "asc" }).then((data) =>
          mapRows(data, (row) => ({
            id: row.id,
            title: row.word,
            detail: `${row.part_of_speech || "—"} · ${row.level || "—"}`,
          })),
        );
      if (kind === "reading")
        return readingApi.list({ ...filters, sort: "updated_at" }).then((data) =>
          mapRows(data, (row) => ({
            id: row.id,
            title: row.title,
            detail: `${row.level || "—"} · ${t("catalogs.words_n", { n: row.word_count ?? 0 })}`,
          })),
        );
      return listeningApi.list({ ...filters, sort: "updated_at" }).then((data) =>
        mapRows(data, (row) => ({
          id: row.id,
          title: row.title,
          detail: `${row.level || "—"} · ${t("listening.seconds", { n: Math.round(row.duration_seconds ?? 0) })}`,
        })),
      );
    },
  });

  const add = useMutation({
    mutationFn: (refId: string) => catalogsApi.addItems(catalogId, [{ kind, ref_id: refId }]),
    onSuccess: (result) => {
      setMessage(null);
      qc.setQueryData(["catalog-one", catalogId], result.catalog);
      qc.invalidateQueries({ queryKey: ["catalogs"] });
    },
    // `reference_exists` is the double-add, and the catalog is unchanged by it, so the
    // panel reports it instead of looking like the button did nothing.
    onError: (e: ApiError) => setMessage(e.message),
  });

  return (
    <div className="card stack">
      <h2 style={{ margin: 0 }}>{t("catalogs.add_from_bank")}</h2>
      <p className="small muted" style={{ margin: 0 }}>
        {t("catalogs.add_hint")}
      </p>
      <div className="tabs">
        {(meta.data?.item_kinds || []).map((entry) => (
          <button
            key={entry.kind}
            type="button"
            className={`tab ${kind === entry.kind ? "active" : ""}`}
            onClick={() => {
              setKind(entry.kind);
              setPage(1);
            }}
          >
            {t(`catalogs.kind_${entry.kind}`)}
          </button>
        ))}
      </div>
      <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        <input className="input" style={{ maxWidth: 220 }} placeholder={t("catalogs.bank_search")} value={q} onChange={(e) => { setQ(e.target.value); setPage(1); }} />
        <select className="input" style={{ maxWidth: 110 }} value={level} onChange={(e) => { setLevel(e.target.value); setPage(1); }}>
          <option value="">{t("catalogs.all_levels")}</option>
          {(meta.data?.levels || []).map((each) => (
            <option key={each} value={each}>
              {each}
            </option>
          ))}
        </select>
        <select className="input" style={{ maxWidth: 140 }} value={language} onChange={(e) => { setLanguage(e.target.value); setPage(1); }}>
          <option value="">{t("catalogs.all_languages")}</option>
          {languages.map((code) => (
            <option key={code} value={code}>
              {code}
            </option>
          ))}
        </select>
      </div>
      {message ? <div className="alert error">{message}</div> : null}
      {bank.isError ? <div className="alert error">{t("common.could_not_load")}</div> : null}
      <div className="stack">
        {(bank.data?.items || []).map((row) => {
          const already = held.has(`${kind}:${row.id}`);
          return (
            <div className="row" key={row.id} style={{ gap: 8, borderBottom: "1px solid var(--border)" }}>
              <div style={{ flex: 1 }}>
                <div className="small">{row.title}</div>
                <div className="small muted">{row.detail}</div>
              </div>
              <button type="button" className="btn secondary" disabled={already} onClick={() => add.mutate(row.id)} style={{ minHeight: 44 }}>
                {already ? t("catalogs.already_added") : t("catalogs.add")}
              </button>
            </div>
          );
        })}
        {!bank.isLoading && (bank.data?.items || []).length === 0 ? (
          <div className="muted small">{t("catalogs.bank_empty")}</div>
        ) : null}
      </div>
      <div className="row">
        <span className="small muted">{t("catalogs.bank_showing", { total: bank.data?.total ?? 0 })}</span>
        <span className="spacer" />
        <button className="btn secondary" disabled={page <= 1} onClick={() => setPage(page - 1)}>
          ‹
        </button>
        <button className="btn secondary" disabled={page >= Math.max(1, Math.ceil((bank.data?.total ?? 0) / PICKER_SIZE))} onClick={() => setPage(page + 1)}>
          ›
        </button>
      </div>
    </div>
  );
}

function mapRows<T, R>(data: { items: T[]; total: number; page: number; page_size: number }, fn: (row: T) => R) {
  return { ...data, items: data.items.map(fn) };
}

function PreviewPanel({
  catalogId,
  language,
  onLanguage,
  languages,
  shuffleDefault,
  knownStatesEnabled,
}: {
  catalogId: string;
  language: string;
  onLanguage: (value: string) => void;
  languages: string[];
  shuffleDefault: boolean;
  knownStatesEnabled: boolean;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [shuffle, setShuffle] = useState(shuffleDefault);

  const preview = useQuery({
    queryKey: ["catalog-preview", catalogId, language, shuffle],
    queryFn: () => catalogsApi.preview(catalogId, { language: language || undefined, shuffle }),
    enabled: open,
  });

  return (
    <div className="card stack">
      <div className="row">
        <h2 style={{ margin: 0 }}>{t("catalogs.preview")}</h2>
        <span className="spacer" />
        <button type="button" className={open ? "btn secondary" : "btn"} onClick={() => setOpen((value) => !value)} style={{ minHeight: 44 }}>
          {open ? t("catalogs.close_preview") : t("catalogs.open_preview")}
        </button>
      </div>
      <p className="small muted" style={{ margin: 0 }}>
        {t("catalogs.preview_hint")}
      </p>
      {open ? (
        <div className="stack">
          <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
            <select className="input" style={{ maxWidth: 160 }} value={language} onChange={(e) => onLanguage(e.target.value)}>
              <option value="">{t("catalogs.all_languages")}</option>
              {languages.map((code) => (
                <option key={code} value={code}>
                  {code}
                </option>
              ))}
            </select>
            <label className="row small" style={{ gap: 6 }}>
              <input type="checkbox" checked={shuffle} onChange={(e) => setShuffle(e.target.checked)} />
              {t("catalogs.preview_shuffle")}
            </label>
          </div>
          {preview.isError ? (
            <div className="alert error">{t("common.could_not_load")} {(preview.error as ApiError).message}</div>
          ) : null}
          {preview.data ? (
            <>
              <div className="row small muted" style={{ gap: 8, flexWrap: "wrap" }}>
                <span>{t("catalogs.preview_steps", { n: preview.data.steps.length })}</span>
                <span>{t("catalogs.preview_skipped", { n: preview.data.skipped_count })}</span>
                <span>{t(`catalogs.timing_${preview.data.feedback_timing}`)}</span>
              </div>
              {preview.data.steps.length === 0 ? (
                <div className="muted small">{t("catalogs.nothing_to_serve")}</div>
              ) : null}
              {preview.data.steps.map((step: Step) => (
                <PracticeStep
                  key={step.item_id}
                  step={step}
                  sessionId={null}
                  results={{}}
                  busyId={null}
                  onAnswer={() => {}}
                  knownEnabled={knownStatesEnabled}
                  known={{}}
                  onMark={() => {}}
                  favorites={{}}
                  onFavorite={() => {}}
                />
              ))}
            </>
          ) : (
            <div className="muted small">{t("common.loading")}</div>
          )}
        </div>
      ) : null}
    </div>
  );
}
