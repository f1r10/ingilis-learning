// One recording: the file it points at, the words spoken in it, how often it may be
// played, and the blocks filed under it.
//
// The file is never pasted in as a link and never copied: a listening names a media asset
// by id, and the library is what says whether those bytes are still here. The transcript's
// own origin (`transcript_source`) is the server's word, not this form's, so a typed text
// cannot claim to have been dictated.
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { ApiError, mediaUrl } from "../api/client";
import LearnerPreview from "../components/LearnerPreview";
import { listeningApi, type Cue, type ListeningDraft, type ListeningSet, type ListeningSetDraft, type QuestionStub } from "../api/listening";
import MediaPicker from "../components/MediaPicker";
import type { QuestionSet } from "../api/reading";

export default function ListeningEditor() {
  const { id } = useParams();
  const isNew = !id || id === "new";
  return isNew ? <NewListening /> : <ExistingListening id={id as string} />;
}

/** What this form owns. `status` only matters while the row does not exist yet: once it
 * does, the status card below posts it through its own endpoint. */
type DetailsDraft = Partial<ListeningDraft> & { title: string; status?: string };

function NewListening() {
  const { t } = useTranslation();
  const nav = useNavigate();
  const [draft, setDraft] = useState<DetailsDraft>({
    title: "",
    media_asset_id: null,
    language: null,
    level: null,
    transcript: "",
    transcript_timestamps: [],
    replay_limit: 2,
    allow_pause: true,
    allow_seek: true,
    show_transcript: false,
    status: "draft",
  });
  const [error, setError] = useState<string | null>(null);
  const meta = useQuery({ queryKey: ["listening-meta"], queryFn: listeningApi.meta });

  const create = useMutation({
    mutationFn: () => listeningApi.create(draft),
    onSuccess: (row) => nav(`/listening/${row.id}`, { replace: true }),
    onError: (e: ApiError) => setError(e.message),
  });

  return (
    <div className="stack">
      <div className="row">
        <h1 style={{ margin: 0 }}>{t("listening.new")}</h1>
        <span className="spacer" />
        <Link className="btn ghost" to="/listening">
          {t("listening.back_to_library")}
        </Link>
      </div>
      {error ? <div className="alert error">{error}</div> : null}
      <DetailsCard
        draft={draft}
        meta={meta.data}
        busy={create.isPending}
        onChange={setDraft}
        onSave={() => create.mutate()}
        saveLabel={t("listening.create")}
      />
      <p className="small muted">{t("listening.publish_rule")}</p>
    </div>
  );
}

function ExistingListening({ id }: { id: string }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const meta = useQuery({ queryKey: ["listening-meta"], queryFn: listeningApi.meta });
  const query = useQuery({ queryKey: ["listening", id], queryFn: () => listeningApi.get(id) });
  const [draft, setDraft] = useState<DetailsDraft | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  useEffect(() => {
    if (query.data && !draft) {
      setDraft({
        title: query.data.title,
        media_asset_id: query.data.audio?.id ?? null,
        language: query.data.language,
        level: query.data.level,
        transcript: query.data.transcript || "",
        transcript_timestamps: query.data.transcript_timestamps || [],
        replay_limit: query.data.replay_limit,
        allow_pause: query.data.allow_pause,
        allow_seek: query.data.allow_seek,
        show_transcript: query.data.show_transcript,
      });
    }
  }, [query.data, draft]);

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["listening", id] });
    qc.invalidateQueries({ queryKey: ["listening"] });
    qc.invalidateQueries({ queryKey: ["questions"] });
  };

  const save = useMutation({
    mutationFn: () => listeningApi.update(id, draft as ListeningDraft),
    onSuccess: (row) => {
      setError(null);
      setNote(t("listening.saved"));
      setDraft(null);
      qc.setQueryData(["listening", id], row);
      refresh();
    },
    onError: (e: ApiError) => {
      setError(e.message);
      setNote(null);
    },
  });

  const status = useMutation({
    mutationFn: (next: string) => listeningApi.setStatus(id, next),
    onSuccess: () => {
      setError(null);
      setNote(t("listening.saved"));
      refresh();
    },
    onError: (e: ApiError) => setError(e.message),
  });

  const trash = useMutation({
    mutationFn: () => listeningApi.trash(id),
    onSuccess: refresh,
    onError: (e: ApiError) => setError(e.message),
  });

  const restore = useMutation({
    mutationFn: () => listeningApi.restore(id),
    onSuccess: () => {
      setError(null);
      setDraft(null);
      refresh();
    },
    onError: (e: ApiError) => setError(e.message),
  });

  const row = query.data;
  if (query.isError) {
    return <div className="alert error">{t("common.could_not_load")} {(query.error as ApiError).message}</div>;
  }
  if (!row || !draft) return <div className="card muted">{t("common.loading")}</div>;

  return (
    <div className="stack">
      <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        <h1 style={{ margin: 0 }}>{row.title}</h1>
        <span className="chip">{t(`status.${row.status}`)}</span>
        <span className="chip">{t(`listening.source_${row.transcript_source}`)}</span>
        {row.deleted_at ? <span className="chip">{t("listening.in_trash")}</span> : null}
        <span className="spacer" />
        <Link className="btn ghost" to="/listening">
          {t("listening.back_to_library")}
        </Link>
        {row.deleted_at ? (
          <button className="btn secondary" onClick={() => restore.mutate()}>
            {t("listening.restore")}
          </button>
        ) : (
          <button className="btn ghost" onClick={() => trash.mutate()}>
            {t("listening.trash")}
          </button>
        )}
      </div>

      {error ? <div className="alert error">{error}</div> : null}
      {note ? <div className="alert">{note}</div> : null}

      <PlayerCard audio={row.audio} replayLimit={row.replay_limit} />

      <DetailsCard
        draft={draft}
        meta={meta.data}
        busy={save.isPending}
        onChange={setDraft}
        onSave={() => save.mutate()}
        saveLabel={t("listening.save")}
      />

      <div className="card row" style={{ flexWrap: "wrap", gap: 8 }}>
        <strong className="small">{t("listening.status")}</strong>
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
        <span className="small muted">{t("listening.publish_rule")}</span>
      </div>

      <BlockList
        listeningId={id}
        sets={row.sets}
        unfiled={row.unfiled}
        duration={row.audio?.duration_seconds ?? null}
        maxSets={meta.data?.max_sets ?? 50}
        maxQuestions={meta.data?.max_questions_per_set ?? 200}
      />
      <PreviewCard id={id} />
    </div>
  );
}

function DetailsCard({
  draft,
  meta,
  busy,
  onChange,
  onSave,
  saveLabel,
}: {
  draft: DetailsDraft;
  meta?: {
    learning_languages: string[];
    levels: string[];
    transcript_sources: string[];
    max_replay_limit: number;
    max_body_characters: number;
  };
  busy: boolean;
  onChange: (next: DetailsDraft) => void;
  onSave: () => void;
  saveLabel: string;
}) {
  const { t } = useTranslation();
  const limit = meta?.max_body_characters ?? 20_000;
  const maxReplay = meta?.max_replay_limit ?? 50;

  return (
    <div className="card stack">
      <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        <input
          className="input"
          style={{ flex: 2, minWidth: 220 }}
          value={draft.title}
          maxLength={400}
          placeholder={t("listening.title_placeholder")}
          onChange={(e) => onChange({ ...draft, title: e.target.value })}
          aria-label={t("listening.recording_title")}
        />
        <select
          className="input"
          style={{ maxWidth: 130 }}
          value={draft.language || ""}
          onChange={(e) => onChange({ ...draft, language: e.target.value || null })}
          aria-label={t("listening.learning_language")}
        >
          <option value="">{t("listening.all_languages")}</option>
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
          aria-label={t("listening.level")}
        >
          <option value="">{t("listening.no_level")}</option>
          {(meta?.levels || []).map((level) => (
            <option key={level} value={level}>
              {level}
            </option>
          ))}
        </select>
      </div>

      <MediaPicker
        value={draft.media_asset_id ?? null}
        onChange={(assetId) => onChange({ ...draft, media_asset_id: assetId })}
        kind="audio"
        copy={{
          none: t("listening.no_file_chosen"),
          choose: t("listening.choose_file"),
          detach: t("listening.detach_file"),
          trashed: t("listening.trashed_file_hint"),
        }}
      />

      <div className="row" style={{ flexWrap: "wrap", gap: 10 }}>
        <label className="small">
          {t("listening.replay_limit_label")}
          <input
            className="input"
            style={{ maxWidth: 80, marginLeft: 6 }}
            type="number"
            min={0}
            max={maxReplay}
            value={draft.replay_limit ?? ""}
            onChange={(e) => onChange({ ...draft, replay_limit: e.target.value === "" ? null : Number(e.target.value) })}
          />
        </label>
        <label className="small">
          <input
            type="checkbox"
            checked={draft.allow_pause ?? true}
            onChange={(e) => onChange({ ...draft, allow_pause: e.target.checked })}
          />{" "}
          {t("listening.allow_pause")}
        </label>
        <label className="small">
          <input
            type="checkbox"
            checked={draft.allow_seek ?? true}
            onChange={(e) => onChange({ ...draft, allow_seek: e.target.checked })}
          />{" "}
          {t("listening.allow_seek")}
        </label>
        <label className="small">
          <input
            type="checkbox"
            checked={draft.show_transcript ?? false}
            onChange={(e) => onChange({ ...draft, show_transcript: e.target.checked })}
          />{" "}
          {t("listening.show_transcript")}
        </label>
      </div>
      <div className="small muted">{t("listening.rules_hint")}</div>

      <textarea
        className="input"
        rows={6}
        value={draft.transcript || ""}
        maxLength={limit}
        placeholder={t("listening.transcript_placeholder")}
        onChange={(e) => onChange({ ...draft, transcript: e.target.value })}
        aria-label={t("listening.transcript")}
      />
      <div className="small muted">{t("listening.transcript_source_note", { max: limit })}</div>

      <CueEditor
        cues={draft.transcript_timestamps || []}
        onChange={(cues) => onChange({ ...draft, transcript_timestamps: cues })}
      />

      <div className="row">
        <span className="spacer" />
        <button className="btn" disabled={busy || !draft.title.trim()} onClick={onSave}>
          {saveLabel}
        </button>
      </div>
    </div>
  );
}

function CueEditor({ cues, onChange }: { cues: Cue[]; onChange: (next: Cue[]) => void }) {
  const { t } = useTranslation();
  const update = (index: number, patch: Partial<Cue>) => {
    const next = cues.map((cue, position) => (position === index ? { ...cue, ...patch } : cue));
    onChange(next);
  };
  return (
    <div className="stack" style={{ gap: 4 }}>
      <strong className="small">{t("listening.cues")}</strong>
      {cues.map((cue, index) => (
        <div className="row" key={index} style={{ gap: 4 }}>
          <input
            className="input"
            style={{ maxWidth: 80 }}
            type="number"
            step="0.1"
            min={0}
            value={cue.start ?? ""}
            aria-label={t("listening.cue_start")}
            onChange={(e) => update(index, { start: e.target.value === "" ? undefined : Number(e.target.value) })}
          />
          <input
            className="input"
            style={{ maxWidth: 80 }}
            type="number"
            step="0.1"
            min={0}
            value={cue.end ?? ""}
            aria-label={t("listening.cue_end")}
            onChange={(e) => update(index, { end: e.target.value === "" ? undefined : Number(e.target.value) })}
          />
          <input
            className="input"
            style={{ flex: 1 }}
            value={cue.text || ""}
            placeholder={t("listening.cue_text")}
            onChange={(e) => update(index, { text: e.target.value })}
          />
          <button className="btn ghost" onClick={() => onChange(cues.filter((_, position) => position !== index))}>
            {t("listening.remove")}
          </button>
        </div>
      ))}
      <div className="row">
        <button className="btn secondary" onClick={() => onChange([...cues, { start: 0, text: "" }])}>
          {t("listening.add_cue")}
        </button>
        <span className="small muted">{t("listening.cues_hint")}</span>
      </div>
    </div>
  );
}

function PlayerCard({ audio, replayLimit }: { audio: { content_url: string; duration_seconds: number | null } | null; replayLimit: number | null }) {
  const { t } = useTranslation();
  const url = mediaUrl(audio?.content_url);
  if (!audio || !url) return <div className="alert">{t("listening.no_audio_yet")}</div>;
  return (
    <div className="card stack">
      <strong className="small">{t("listening.preview_playback")}</strong>
      <audio src={url} controls style={{ width: "100%" }} />
      <div className="small muted">
        {audio.duration_seconds
          ? t("listening.seconds", { n: Math.round(audio.duration_seconds) })
          : t("media.not_measured")}
        {replayLimit !== null ? ` · ${t("questions.replay_limit", { n: replayLimit })}` : ""}
      </div>
    </div>
  );
}

function BlockList({
  listeningId,
  sets,
  unfiled,
  duration,
  maxSets,
  maxQuestions,
}: {
  listeningId: string;
  sets: ListeningSet[];
  unfiled: QuestionStub[];
  duration: number | null;
  maxSets: number;
  maxQuestions: number;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [title, setTitle] = useState("");
  const [error, setError] = useState<string | null>(null);

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["listening", listeningId] });
    qc.invalidateQueries({ queryKey: ["questions"] });
  };

  const create = useMutation({
    mutationFn: () => listeningApi.createSet(listeningId, { title }),
    onSuccess: () => {
      setTitle("");
      setError(null);
      refresh();
    },
    onError: (e: ApiError) => setError(e.message),
  });

  const reorder = useMutation({
    mutationFn: (ids: string[]) => listeningApi.reorder(listeningId, ids),
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
        <h2 style={{ margin: 0 }}>{t("listening.blocks")}</h2>
        <span className="spacer" />
        <span className="small muted">{t("listening.blocks_hint", { max: maxSets, questions: maxQuestions })}</span>
      </div>
      {error ? <div className="alert error">{error}</div> : null}
      {sets.length === 0 ? <div className="muted small">{t("listening.no_blocks")}</div> : null}

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
          <BlockEditor set={set} pool={pool} duration={duration} onDone={refresh} onError={setError} />
        </div>
      ))}

      <div className="row" style={{ gap: 6 }}>
        <input
          className="input"
          style={{ maxWidth: 260 }}
          value={title}
          maxLength={300}
          placeholder={t("listening.new_block")}
          onChange={(e) => setTitle(e.target.value)}
          aria-label={t("listening.new_block")}
        />
        <button className="btn secondary" disabled={!title.trim() || sets.length >= maxSets} onClick={() => create.mutate()}>
          {t("listening.add_block")}
        </button>
      </div>
      {unfiled.length ? <div className="small muted">{t("listening.unfiled_hint", { n: unfiled.length })}</div> : null}
      <Link className="btn ghost" to={`/questions?listening_id=${listeningId}`}>
        {t("listening.open_bound_questions")}
      </Link>
    </div>
  );
}

function BlockEditor({
  set,
  pool,
  duration,
  onDone,
  onError,
}: {
  set: ListeningSet;
  pool: QuestionStub[];
  duration: number | null;
  onDone: () => void;
  onError: (message: string) => void;
}) {
  const { t } = useTranslation();
  const [title, setTitle] = useState(set.title || "");
  const [instructions, setInstructions] = useState(set.instructions || "");
  const [start, setStart] = useState<string>(set.start_seconds === null ? "" : String(set.start_seconds));
  const [end, setEnd] = useState<string>(set.end_seconds === null ? "" : String(set.end_seconds));
  const [ids, setIds] = useState<string[]>(set.questions.map((question) => question.id));
  const [result, setResult] = useState<(ListeningSet & { unfiled: string[]; moved: { question_id: string; from_title: string | null }[] }) | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    setTitle(set.title || "");
    setInstructions(set.instructions || "");
    setStart(set.start_seconds === null ? "" : String(set.start_seconds));
    setEnd(set.end_seconds === null ? "" : String(set.end_seconds));
    setIds(set.questions.map((question) => question.id));
    setResult(null);
  }, [set]);

  const stubOf = (questionId: string) => pool.find((question) => question.id === questionId);
  const outside = pool.filter((question) => !ids.includes(question.id));

  const body = (): ListeningSetDraft => ({
    title: title.trim(),
    instructions: instructions.trim() || null,
    config: set.config,
    start_seconds: start === "" ? null : Number(start),
    end_seconds: end === "" ? null : Number(end),
  });

  const patch = useMutation({
    mutationFn: () => listeningApi.updateSet(set.id, body()),
    onSuccess: () => {
      setResult(null);
      onDone();
    },
    onError: (e: ApiError) => onError(e.message),
  });

  const assign = useMutation({
    mutationFn: () => listeningApi.assign(set.id, ids),
    onSuccess: (answer) => {
      setResult(answer);
      onDone();
    },
    onError: (e: ApiError) => onError(e.message),
  });

  const remove = useMutation({
    mutationFn: () => listeningApi.deleteSet(set.id),
    onSuccess: (answer) => {
      onError(t("listening.block_deleted", { n: answer.returned_to_pool }));
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

  const slice = start !== "" && end !== "" ? `${start} – ${end} s` : start !== "" ? `≥ ${start} s` : end !== "" ? `≤ ${end} s` : t("listening.whole_recording");

  return (
    <div className="card stack" style={{ borderColor: "var(--border)" }}>
      <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
        <input
          className="input"
          style={{ flex: 1, minWidth: 160 }}
          value={title}
          maxLength={300}
          onChange={(e) => setTitle(e.target.value)}
          aria-label={t("listening.block_title")}
        />
        <span className="small muted">{slice}</span>
        <span className="small muted">{t("listening.question_count", { n: set.question_count })}</span>
        <button className="btn ghost" onClick={() => setOpen(!open)}>
          {open ? t("listening.hide") : t("listening.show")}
        </button>
      </div>

      {open ? (
        <>
          <textarea
            className="input"
            rows={2}
            value={instructions}
            maxLength={4000}
            placeholder={t("listening.instructions_placeholder")}
            onChange={(e) => setInstructions(e.target.value)}
            aria-label={t("listening.instructions")}
          />
          <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
            <label className="small">
              {t("listening.from_seconds")}
              <input className="input" style={{ maxWidth: 90, marginLeft: 6 }} type="number" min={0} step="0.1" value={start} onChange={(e) => setStart(e.target.value)} />
            </label>
            <label className="small">
              {t("listening.to_seconds")}
              <input className="input" style={{ maxWidth: 90, marginLeft: 6 }} type="number" min={0} step="0.1" value={end} onChange={(e) => setEnd(e.target.value)} />
            </label>
            {duration ? <span className="small muted">{t("listening.file_length", { n: Math.round(duration) })}</span> : null}
          </div>
          <div className="small muted">{t("listening.interval_hint")}</div>

          <div className="row" style={{ gap: 6 }}>
            <button className="btn secondary" disabled={!title.trim()} onClick={() => patch.mutate()}>
              {t("listening.save_block")}
            </button>
            <button className="btn secondary" disabled={!orderChanged(ids, set)} onClick={() => assign.mutate()}>
              {t("listening.save_questions")}
            </button>
            <span className="spacer" />
            <button className="btn ghost" onClick={() => remove.mutate()}>
              {t("listening.delete_block")}
            </button>
          </div>

          <div className="stack" style={{ gap: 2 }}>
            <strong className="small">{t("listening.in_this_block")}</strong>
            {ids.length === 0 ? <div className="small muted">{t("listening.empty_block")}</div> : null}
            {ids.map((questionId, index) => {
              const stub = stubOf(questionId);
              return (
                <div className="row" key={questionId} style={{ gap: 4 }}>
                  <span className="small muted">{index + 1}.</span>
                  <span className="small" style={{ flex: 1 }}>
                    {stub?.prompt || t("listening.question_gone")}
                    {stub ? <span className="muted"> · {t(`status.${stub.status}`)}</span> : null}
                  </span>
                  <button className="btn ghost" disabled={index === 0} onClick={() => moveWithin(index, -1)}>
                    ↑
                  </button>
                  <button className="btn ghost" disabled={index === ids.length - 1} onClick={() => moveWithin(index, 1)}>
                    ↓
                  </button>
                  <button className="btn ghost" onClick={() => setIds(ids.filter((item) => item !== questionId))}>
                    {t("listening.unfile")}
                  </button>
                </div>
              );
            })}
          </div>

          {outside.length ? (
            <div className="stack" style={{ gap: 2 }}>
              <strong className="small">{t("listening.not_in_this_block")}</strong>
              {outside.map((stub) => (
                <div className="row" key={stub.id} style={{ gap: 4 }}>
                  <span className="small" style={{ flex: 1 }}>
                    {stub.prompt || t("listening.question_gone")} · {t(`status.${stub.status}`)}
                  </span>
                  <button className="btn ghost" onClick={() => setIds([...ids, stub.id])}>
                    {t("listening.file_here")}
                  </button>
                </div>
              ))}
            </div>
          ) : null}

          {result ? (
            <div className="stack small muted" style={{ gap: 2 }}>
              <span>{t("listening.saved_questions", { n: result.questions.length })}</span>
              {result.unfiled.length ? (
                <span>{t("listening.moved_out", { ids: result.unfiled.map((id) => stubOf(id)?.prompt || id.slice(0, 8)).join(", ") })}</span>
              ) : null}
              {result.moved.length ? (
                <span>
                  {t("listening.moved_in", {
                    names: result.moved
                      .map((item) => `${stubOf(item.question_id)?.prompt || item.question_id.slice(0, 8)} (${item.from_title || t("listening.unfiled")})`)
                      .join(", "),
                  })}
                </span>
              ) : null}
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

function orderChanged(ids: string[], set: QuestionSet): boolean {
  return ids.join(",") !== set.questions.map((question) => question.id).join(",");
}

function PreviewCard({ id }: { id: string }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const preview = useQuery({
    queryKey: ["listening-preview", id],
    queryFn: () => listeningApi.preview(id),
    enabled: open,
  });
  const url = mediaUrl(preview.data?.audio?.content_url);

  return (
    <div className="card stack">
      <div className="row">
        <strong className="small">{t("listening.learner_view")}</strong>
        <span className="spacer" />
        <button className="btn secondary" onClick={() => setOpen(!open)}>
          {open ? t("listening.hide") : t("listening.show")}
        </button>
      </div>
      <div className="small muted">{t("listening.learner_view_hint")}</div>
      {open && preview.data ? (
        <div className="stack">
          {url && preview.data.audio ? (
            <>
              <audio src={url} controls style={{ width: "100%" }} />
              {/* The rules are shown, not enforced here: a teacher's preview has no attempt
                  to count replays against, so the numbers are what the learner's runner
                  will be handed. */}
              <div className="small muted">
                {t("questions.replay_limit", { n: preview.data.replay_limit ?? 0 })}
                {preview.data.allow_pause ? null : ` · ${t("listening.no_pause")}`}
                {preview.data.allow_seek ? null : ` · ${t("listening.no_seek")}`}
                {preview.data.show_transcript ? ` · ${t("listening.transcript_shown")}` : ` · ${t("listening.transcript_hidden")}`}
              </div>
            </>
          ) : (
            <div className="alert">{t("listening.no_audio_yet")}</div>
          )}
          {preview.data.show_transcript && preview.data.transcript ? (
            <div className="small" style={{ whiteSpace: "pre-wrap" }}>
              {preview.data.transcript}
            </div>
          ) : null}
          {preview.data.sets.map((set) => (
            <div key={set.id} className="stack" style={{ gap: 4 }}>
              <strong className="small">{set.title}</strong>
              {set.start_seconds !== null || set.end_seconds !== null ? (
                <div className="small muted">{t("listening.block_slice", { from: set.start_seconds ?? 0, to: set.end_seconds ?? "end" })}</div>
              ) : null}
              {set.instructions ? <div className="small muted">{set.instructions}</div> : null}
              {set.questions.map((question) => (
                <LearnerPreview key={question.id} view={question} />
              ))}
            </div>
          ))}
          {preview.data.sets.length === 0 ? <div className="small muted">{t("listening.nothing_ready")}</div> : null}
        </div>
      ) : null}
      {open && preview.isError ? (
        <div className="alert error">{t("common.could_not_load")} {(preview.error as ApiError).message}</div>
      ) : null}
    </div>
  );
}
