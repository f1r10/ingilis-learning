// One reading text: its prose, the blocks filed under it, and the questions waiting to be
// filed.
//
// Two rules shape this screen. The server counts the words, so the editor shows the number
// it got back instead of one computed here. And filing is not editing: moving a question
// into a block never rewrites the question, so the assignment answer reports what left the
// block, what moved in from a sibling, and nothing else.
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { ApiError } from "../api/client";
import LearnerPreview from "../components/LearnerPreview";
import { readingApi, type AssignmentResult, type QuestionSet, type QuestionStub, type ReadingDraft } from "../api/reading";

export default function ReadingEditor() {
  const { id } = useParams();
  const isNew = !id || id === "new";
  return isNew ? <NewReading /> : <ExistingReading id={id as string} />;
}

/** What this form owns. `status` only exists while the row is new - afterwards the status
 * card posts it through its own endpoint, so the body form never carries a stale pick. */
type BodyDraft = ReadingDraft & { status?: string };

function NewReading() {
  const { t } = useTranslation();
  const nav = useNavigate();
  const meta = useQuery({ queryKey: ["reading-meta"], queryFn: readingApi.meta });
  const [draft, setDraft] = useState<BodyDraft>({
    title: "",
    body: "",
    language: null,
    level: null,
    layout: null,
    status: "draft",
  });
  const [error, setError] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: () => readingApi.create({ ...draft, status: draft.status ?? "draft" }),
    onSuccess: (reading) => nav(`/reading/${reading.id}`, { replace: true }),
    onError: (e: ApiError) => setError(e.message),
  });

  return (
    <div className="stack">
      <div className="row">
        <h1 style={{ margin: 0 }}>{t("reading.new")}</h1>
        <span className="spacer" />
        <Link className="btn ghost" to="/reading">
          {t("reading.back_to_library")}
        </Link>
      </div>
      <p className="muted small">{t("reading.new_hint")}</p>
      {error ? <div className="alert error">{error}</div> : null}
      <BodyCard
        draft={draft}
        meta={meta.data}
        wordCount={null}
        busy={create.isPending}
        onChange={setDraft}
        onSave={() => create.mutate()}
        saveLabel={t("reading.create")}
      />
    </div>
  );
}

function ExistingReading({ id }: { id: string }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const meta = useQuery({ queryKey: ["reading-meta"], queryFn: readingApi.meta });
  const reading = useQuery({ queryKey: ["reading", id], queryFn: () => readingApi.get(id) });
  const [draft, setDraft] = useState<BodyDraft | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  useEffect(() => {
    if (reading.data && !draft) {
      setDraft({
        title: reading.data.title,
        body: reading.data.body,
        language: reading.data.language,
        level: reading.data.level,
        layout: reading.data.layout,
      });
    }
  }, [reading.data, draft]);

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["reading", id] });
    qc.invalidateQueries({ queryKey: ["reading"] });
    qc.invalidateQueries({ queryKey: ["questions"] });
  };

  const save = useMutation({
    mutationFn: () => readingApi.update(id, draft as ReadingDraft),
    onSuccess: () => {
      setError(null);
      setNote(t("reading.saved"));
      refresh();
    },
    onError: (e: ApiError) => {
      setError(e.message);
      setNote(null);
    },
  });

  const status = useMutation({
    mutationFn: (next: string) => readingApi.setStatus(id, next),
    onSuccess: () => {
      setError(null);
      setNote(t("reading.saved"));
      refresh();
    },
    onError: (e: ApiError) => setError(e.message),
  });

  const trash = useMutation({
    mutationFn: async () => readingApi.trash(id),
    onSuccess: refresh,
    onError: (e: ApiError) => setError(e.message),
  });

  const restore = useMutation({
    mutationFn: async () => readingApi.restore(id),
    onSuccess: () => {
      setError(null);
      refresh();
    },
    onError: (e: ApiError) => setError(e.message),
  });

  const row = reading.data;
  const dirty = useMemo(() => {
    if (!row || !draft) return false;
    return (
      draft.title !== row.title ||
      draft.body !== row.body ||
      draft.language !== row.language ||
      draft.level !== row.level ||
      draft.layout !== row.layout
    );
  }, [row, draft]);

  if (reading.isError) {
    return <div className="alert error">{t("common.could_not_load")} {(reading.error as ApiError).message}</div>;
  }
  if (!row || !draft) return <div className="card muted">{t("common.loading")}</div>;

  return (
    <div className="stack">
      <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        <h1 style={{ margin: 0 }}>{row.title}</h1>
        <span className="chip">{t(`status.${row.status}`)}</span>
        {row.deleted_at ? <span className="chip">{t("reading.in_trash")}</span> : null}
        <span className="spacer" />
        <Link className="btn ghost" to="/reading">
          {t("reading.back_to_library")}
        </Link>
        {row.deleted_at ? (
          <button className="btn secondary" onClick={() => restore.mutate()}>
            {t("reading.restore")}
          </button>
        ) : (
          <button className="btn ghost" onClick={() => trash.mutate()}>
            {t("reading.trash")}
          </button>
        )}
      </div>

      {error ? <div className="alert error">{error}</div> : null}
      {note ? <div className="alert">{note}</div> : null}

      <BodyCard
        draft={draft}
        meta={meta.data}
        wordCount={row.word_count}
        busy={save.isPending}
        onChange={setDraft}
        onSave={() => save.mutate()}
        saveLabel={t("reading.save")}
        dirty={dirty}
      />

      <div className="card row" style={{ flexWrap: "wrap", gap: 8 }}>
        <strong className="small">{t("reading.status")}</strong>
        {(meta.data?.statuses || []).map((next) => (
          <button
            key={next}
            className={row.status === next ? "btn" : "btn secondary"}
            disabled={row.status === next || Boolean(row.deleted_at)}
            onClick={() => status.mutate(next)}
          >
            {t(`status.${next}`)}
          </button>
        ))}
        <span className="small muted">{t("reading.publish_hint")}</span>
      </div>

      <BlockList readingId={id} sets={row.sets} unfiled={row.unfiled} maxSets={meta.data?.max_sets ?? 50} />
      <PreviewCard id={id} />
    </div>
  );
}

function BodyCard({
  draft,
  meta,
  wordCount,
  busy,
  dirty,
  onChange,
  onSave,
  saveLabel,
}: {
  draft: BodyDraft;
  meta?: { learning_languages: string[]; levels: string[]; layouts: string[]; max_body_characters: number };
  wordCount: number | null;
  busy: boolean;
  dirty?: boolean;
  onChange: (next: BodyDraft) => void;
  onSave: () => void;
  saveLabel: string;
}) {
  const { t } = useTranslation();
  const limit = meta?.max_body_characters ?? 20_000;
  return (
    <div className="card stack">
      <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        <input
          className="input"
          style={{ flex: 2, minWidth: 220 }}
          value={draft.title}
          maxLength={400}
          placeholder={t("reading.title_placeholder")}
          onChange={(e) => onChange({ ...draft, title: e.target.value })}
          aria-label={t("reading.text_title")}
        />
        <select
          className="input"
          style={{ maxWidth: 130 }}
          value={draft.language || ""}
          onChange={(e) => onChange({ ...draft, language: e.target.value || null })}
          aria-label={t("reading.learning_language")}
        >
          <option value="">{t("reading.all_languages")}</option>
          {(meta?.learning_languages || []).map((code) => (
            <option key={code} value={code}>
              {code}
            </option>
          ))}
        </select>
        <select
          className="input"
          style={{ maxWidth: 120 }}
          value={draft.level || ""}
          onChange={(e) => onChange({ ...draft, level: e.target.value || null })}
          aria-label={t("reading.level")}
        >
          <option value="">{t("reading.no_level")}</option>
          {(meta?.levels || []).map((level) => (
            <option key={level} value={level}>
              {level}
            </option>
          ))}
        </select>
        <select
          className="input"
          style={{ maxWidth: 170 }}
          value={draft.layout || ""}
          onChange={(e) => onChange({ ...draft, layout: e.target.value || null })}
          aria-label={t("reading.layout")}
        >
          <option value="">{t("reading.default_layout")}</option>
          {(meta?.layouts || []).map((layout) => (
            <option key={layout} value={layout}>
              {t(`reading.layout_${layout}`)}
            </option>
          ))}
        </select>
      </div>

      <textarea
        className="input"
        rows={12}
        value={draft.body}
        maxLength={limit}
        placeholder={t("reading.body_placeholder")}
        onChange={(e) => onChange({ ...draft, body: e.target.value })}
        aria-label={t("reading.body")}
      />
      <div className="row">
        <span className="small muted">
          {t("reading.characters", { n: draft.body.length, max: limit })} ·{" "}
          {wordCount === null ? t("reading.words_after_saving") : t("reading.words_server", { n: wordCount })}
        </span>
        <span className="spacer" />
        <button className="btn" disabled={busy || !draft.title.trim() || !draft.body.trim()} onClick={onSave}>
          {saveLabel}
        </button>
        {dirty ? <span className="small muted">{t("reading.unsaved")}</span> : null}
      </div>
      <div className="small muted">{t("reading.layout_hint")}</div>
    </div>
  );
}

function BlockList({
  readingId,
  sets,
  unfiled,
  maxSets,
}: {
  readingId: string;
  sets: QuestionSet[];
  unfiled: QuestionStub[];
  maxSets: number;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [title, setTitle] = useState("");
  const [error, setError] = useState<string | null>(null);

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["reading", readingId] });
    qc.invalidateQueries({ queryKey: ["questions"] });
  };

  const create = useMutation({
    mutationFn: () => readingApi.createSet(readingId, { title, instructions: null, config: {} }),
    onSuccess: () => {
      setTitle("");
      setError(null);
      refresh();
    },
    onError: (e: ApiError) => setError(e.message),
  });

  const reorder = useMutation({
    mutationFn: (ids: string[]) => readingApi.reorder(readingId, ids),
    onSuccess: refresh,
    onError: (e: ApiError) => setError(e.message),
  });

  const move = (index: number, delta: number) => {
    const ids = sets.map((set) => set.id);
    const target = index + delta;
    if (target < 0 || target >= ids.length) return;
    [ids[index], ids[target]] = [ids[target], ids[index]];
    reorder.mutate(ids);
  };

  const pool = [...unfiled, ...sets.flatMap((set) => set.questions)];

  return (
    <div className="card stack">
      <div className="row">
        <h2 style={{ margin: 0 }}>{t("reading.blocks")}</h2>
        <span className="spacer" />
        <span className="small muted">{t("reading.blocks_hint", { max: maxSets })}</span>
      </div>
      {error ? <div className="alert error">{error}</div> : null}

      {sets.length === 0 ? <div className="muted small">{t("reading.no_blocks")}</div> : null}
      {sets.map((set, index) => (
        <div className="stack" key={set.id} style={{ gap: 4 }}>
          <div className="row" style={{ gap: 4 }}>
            <span className="small muted">{index + 1}.</span>
            <span className="spacer" />
            <button className="btn ghost" disabled={index === 0} onClick={() => move(index, -1)}>
              ↑
            </button>
            <button className="btn ghost" disabled={index === sets.length - 1} onClick={() => move(index, 1)}>
              ↓
            </button>
          </div>
          <BlockEditor set={set} pool={pool} onDone={refresh} onError={setError} />
        </div>
      ))}

      <div className="row" style={{ gap: 6 }}>
        <input
          className="input"
          style={{ maxWidth: 260 }}
          value={title}
          maxLength={300}
          placeholder={t("reading.new_block")}
          onChange={(e) => setTitle(e.target.value)}
          aria-label={t("reading.new_block")}
        />
        <button className="btn secondary" disabled={!title.trim() || sets.length >= maxSets} onClick={() => create.mutate()}>
          {t("reading.add_block")}
        </button>
      </div>

      {unfiled.length ? (
        <div className="small muted">{t("reading.unfiled_hint", { n: unfiled.length })}</div>
      ) : null}
      <Link className="btn ghost" to={`/questions?reading_id=${readingId}`}>
        {t("reading.open_bound_questions")}
      </Link>
    </div>
  );
}

function BlockEditor({
  set,
  pool,
  onDone,
  onError,
}: {
  set: QuestionSet;
  pool: QuestionStub[];
  onDone: () => void;
  onError: (message: string) => void;
}) {
  const { t } = useTranslation();
  const [title, setTitle] = useState(set.title || "");
  const [instructions, setInstructions] = useState(set.instructions || "");
  const [ids, setIds] = useState<string[]>(set.questions.map((question) => question.id));
  const [result, setResult] = useState<AssignmentResult | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    setTitle(set.title || "");
    setInstructions(set.instructions || "");
    setIds(set.questions.map((question) => question.id));
    setResult(null);
  }, [set]);

  const stubOf = (questionId: string) => pool.find((question) => question.id === questionId);
  const outside = pool.filter((question) => !ids.includes(question.id));

  const patch = useMutation({
    mutationFn: () => readingApi.updateSet(set.id, { title: title.trim(), instructions: instructions.trim() || null }),
    onSuccess: () => {
      setResult(null);
      onDone();
    },
    onError: (e: ApiError) => onError(e.message),
  });

  const assign = useMutation({
    mutationFn: () => readingApi.assign(set.id, ids),
    onSuccess: (answer) => {
      setResult(answer);
      onDone();
    },
    onError: (e: ApiError) => onError(e.message),
  });

  const remove = useMutation({
    mutationFn: () => readingApi.deleteSet(set.id),
    onSuccess: (answer) => {
      onError(t("reading.block_deleted", { n: answer.returned_to_pool }));
      onDone();
    },
    onError: (e: ApiError) => onError(e.message),
  });

  const moveWithin = (index: number, delta: number) => {
    const next = [...ids];
    const target = index + delta;
    if (target < 0 || target >= next.length) return;
    [next[index], next[target]] = [next[target], next[index]];
    setIds(next);
  };

  const dirty =
    title.trim() !== (set.title || "") || instructions.trim() !== (set.instructions || "");
  const orderDirty = ids.join(",") !== set.questions.map((question) => question.id).join(",");

  return (
    <div className="card stack" style={{ borderColor: "var(--border)" }}>
      <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
        <input
          className="input"
          style={{ flex: 1, minWidth: 160 }}
          value={title}
          maxLength={300}
          onChange={(e) => setTitle(e.target.value)}
          aria-label={t("reading.block_title")}
        />
        <span className="small muted">{t("reading.question_count", { n: set.question_count })}</span>
        <button className="btn ghost" onClick={() => setOpen(!open)}>
          {open ? t("reading.hide") : t("reading.show")}
        </button>
      </div>

      {open ? (
        <>
          <textarea
            className="input"
            rows={2}
            value={instructions}
            maxLength={4000}
            placeholder={t("reading.instructions_placeholder")}
            onChange={(e) => setInstructions(e.target.value)}
            aria-label={t("reading.instructions")}
          />
          <div className="row" style={{ gap: 6 }}>
            <button className="btn secondary" disabled={!dirty || !title.trim()} onClick={() => patch.mutate()}>
              {t("reading.save_block")}
            </button>
            <button className="btn secondary" disabled={!orderDirty} onClick={() => assign.mutate()}>
              {t("reading.save_questions")}
            </button>
            <span className="spacer" />
            <button className="btn ghost" onClick={() => remove.mutate()}>
              {t("reading.delete_block")}
            </button>
          </div>

          <div className="stack" style={{ gap: 2 }}>
            <strong className="small">{t("reading.in_this_block")}</strong>
            {ids.length === 0 ? <div className="small muted">{t("reading.empty_block")}</div> : null}
            {ids.map((questionId, index) => {
              const stub = stubOf(questionId);
              return (
                <div className="row" key={questionId} style={{ gap: 4 }}>
                  <span className="small muted">{index + 1}.</span>
                  <span className="small" style={{ flex: 1 }}>
                    {stub?.prompt || t("reading.question_gone")}
                    {stub ? <span className="muted"> · {t(`status.${stub.status}`)}</span> : null}
                  </span>
                  <button className="btn ghost" disabled={index === 0} onClick={() => moveWithin(index, -1)}>
                    ↑
                  </button>
                  <button className="btn ghost" disabled={index === ids.length - 1} onClick={() => moveWithin(index, 1)}>
                    ↓
                  </button>
                  <button className="btn ghost" onClick={() => setIds(ids.filter((item) => item !== questionId))}>
                    {t("reading.unfile")}
                  </button>
                </div>
              );
            })}
          </div>

          {outside.length ? (
            <div className="stack" style={{ gap: 2 }}>
              <strong className="small">{t("reading.not_in_this_block")}</strong>
              {outside.map((stub) => (
                <div className="row" key={stub.id} style={{ gap: 4 }}>
                  <span className="small" style={{ flex: 1 }}>
                    {stub.prompt || t("reading.question_gone")} · {t(`status.${stub.status}`)}
                  </span>
                  <button className="btn ghost" onClick={() => setIds([...ids, stub.id])}>
                    {t("reading.file_here")}
                  </button>
                </div>
              ))}
            </div>
          ) : null}

          {result ? (
            <div className="small muted stack" style={{ gap: 2 }}>
              <span>{t("reading.saved_questions", { n: result.questions.length })}</span>
              {result.unfiled.length ? (
                <span>{t("reading.moved_out", { ids: result.unfiled.map((id) => stubOf(id)?.prompt || id.slice(0, 8)).join(", ") })}</span>
              ) : null}
              {result.moved.length ? (
                <span>
                  {t("reading.moved_in", {
                    names: result.moved
                      .map((item) => `${stubOf(item.question_id)?.prompt || item.question_id.slice(0, 8)} (${item.from_title || t("reading.unfiled")})`)
                      .join(", "),
                  })}
                </span>
              ) : null}
            </div>
          ) : null}
          <div className="small muted">{t("reading.assignment_hint")}</div>
        </>
      ) : null}
    </div>
  );
}

function PreviewCard({ id }: { id: string }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const preview = useQuery({
    queryKey: ["reading-preview", id],
    queryFn: () => readingApi.preview(id),
    enabled: open,
  });

  return (
    <div className="card stack">
      <div className="row">
        <strong className="small">{t("reading.learner_view")}</strong>
        <span className="spacer" />
        <button className="btn secondary" onClick={() => setOpen(!open)}>
          {open ? t("reading.hide") : t("reading.show")}
        </button>
      </div>
      <div className="small muted">{t("reading.learner_view_hint")}</div>
      {open && preview.data ? (
        <div className="stack">
          {preview.data.sets.map((set) => (
            <div key={set.id} className="stack" style={{ gap: 4 }}>
              <strong className="small">{set.title}</strong>
              {set.instructions ? <div className="small muted">{set.instructions}</div> : null}
              {set.questions.map((question) => (
                <LearnerPreview key={question.id} view={question} />
              ))}
            </div>
          ))}
          {preview.data.sets.length === 0 ? <div className="small muted">{t("reading.nothing_ready")}</div> : null}
        </div>
      ) : null}
      {open && preview.isError ? (
        <div className="alert error">{t("common.could_not_load")} {(preview.error as ApiError).message}</div>
      ) : null}
    </div>
  );
}
