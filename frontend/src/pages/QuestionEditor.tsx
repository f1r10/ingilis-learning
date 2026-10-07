// One question, end to end: author it, file it, watch it as a learner would,
// try an answer through the real grader, and read the version history it leaves.
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { ApiError } from "../api/client";
import {
  LEVELS,
  QUESTION_STATUSES,
  questionsApi,
  tagsApi,
  topicsApi,
  type QuestionDraft,
  type QuestionVersion,
  type TopicNode,
} from "../api/questions";
import QuestionConfigForm, { emptyConfig, type Config } from "../components/QuestionConfigForm";
import LearnerPreview from "../components/LearnerPreview";
import MediaPicker from "../components/MediaPicker";
import { listeningApi } from "../api/listening";
import { readingApi } from "../api/reading";

interface Draft {
  type: string;
  prompt: string;
  status: string;
  level: string;
  learning_language: string;
  difficulty: string;
  score: string;
  explanation: string;
  teacher_notes: string;
  change_note: string;
  partial_mode: string;
  penalty: string;
  topic_ids: string[];
  tag_ids: string[];
  context_kind: string;
  reading_id: string | null;
  listening_id: string | null;
  media_asset_id: string | null;
}

const BLANK_DRAFT: Draft = {
  type: "multiple_choice",
  prompt: "",
  status: "draft",
  level: "",
  learning_language: "",
  difficulty: "",
  score: "1",
  explanation: "",
  teacher_notes: "",
  change_note: "",
  partial_mode: "partial",
  penalty: "",
  topic_ids: [],
  tag_ids: [],
  context_kind: "independent",
  reading_id: null,
  listening_id: null,
  media_asset_id: null,
};

// question_engine.validate_scoring only accepts a penalty for these two types.
const NEGATIVE_TYPES = ["multi_select", "matching"];

// The three words `/questions` accepts for `context_kind`, in the order a teacher reads them.
const CONTEXT_KINDS = ["independent", "reading_bound", "listening_bound"];

function flatten(nodes: TopicNode[], depth = 0): { node: TopicNode; depth: number }[] {
  return nodes.flatMap((node) => [{ node, depth }, ...flatten(node.children, depth + 1)]);
}

export default function QuestionEditor() {
  const { t } = useTranslation();
  const nav = useNavigate();
  const qc = useQueryClient();
  const { id } = useParams();
  const isNew = !id;

  const [draft, setDraft] = useState<Draft>(BLANK_DRAFT);
  const [config, setConfig] = useState<Config>(emptyConfig(BLANK_DRAFT.type));
  const [error, setError] = useState<string | null>(null);
  const [savedId, setSavedId] = useState<string | null>(id || null);
  const [openedVersion, setOpenedVersion] = useState<QuestionVersion | null>(null);
  const [answer, setAnswer] = useState<string>("");

  const types = useQuery({ queryKey: ["question-types"], queryFn: questionsApi.types });
  const topics = useQuery({ queryKey: ["topics"], queryFn: topicsApi.list });
  const tags = useQuery({ queryKey: ["tags"], queryFn: tagsApi.list });
  const question = useQuery({
    queryKey: ["question", id],
    queryFn: () => questionsApi.get(id as string),
    enabled: !isNew,
  });
  const versions = useQuery({
    queryKey: ["question-versions", id],
    queryFn: () => questionsApi.versions(id as string),
    enabled: !isNew,
  });
  const preview = useQuery({
    queryKey: ["question-preview", savedId],
    queryFn: () => questionsApi.preview(savedId as string),
    enabled: !!savedId,
  });
  // Both libraries are listed only while this question is bound to that kind of context;
  // a teacher picking a text sees the texts they could pick, not the whole bank.
  const readings = useQuery({
    queryKey: ["reading-picker"],
    queryFn: () => readingApi.list({ view: "bank", page_size: 50, sort: "updated_at", order: "desc" }),
    enabled: draft.context_kind === "reading_bound",
  });
  const listenings = useQuery({
    queryKey: ["listening-picker"],
    queryFn: () => listeningApi.list({ view: "bank", page_size: 50, sort: "updated_at", order: "desc" }),
    enabled: draft.context_kind === "listening_bound",
  });

  const spec = useMemo(
    () => (types.data?.items || []).find((item) => item.type === draft.type),
    [types.data, draft.type],
  );

  useEffect(() => {
    if (!question.data) return;
    const row = question.data;
    setDraft({
      type: row.type,
      prompt: row.prompt || "",
      status: row.status,
      level: row.level || "",
      learning_language: row.learning_language || "",
      difficulty: row.difficulty === null ? "" : String(row.difficulty),
      score: String(row.score),
      explanation: row.explanation || "",
      teacher_notes: row.teacher_notes || "",
      change_note: "",
      partial_mode: String((row.partial_scoring as Record<string, unknown>)?.mode || "partial"),
      penalty: String((row.negative_scoring as Record<string, unknown>)?.penalty ?? ""),
      topic_ids: row.topics.map((item) => item.id),
      tag_ids: row.tags.map((item) => item.id),
      context_kind: row.context_kind,
      reading_id: row.reading_id,
      listening_id: row.listening_id,
      media_asset_id: row.media_asset_id,
    });
    setConfig({ ...(emptyConfig(row.type) as Config), ...(row.config as Config) });
    setSavedId(row.id);
    setError(null);
  }, [question.data]);

  const save = useMutation({
    mutationFn: (body: QuestionDraft) => (savedId ? questionsApi.update(savedId, body) : questionsApi.create(body)),
    onSuccess: (row) => {
      setSavedId(row.id);
      setError(null);
      qc.invalidateQueries({ queryKey: ["questions"] });
      qc.invalidateQueries({ queryKey: ["question", row.id] });
      qc.invalidateQueries({ queryKey: ["question-versions", row.id] });
      qc.invalidateQueries({ queryKey: ["question-preview", row.id] });
      if (isNew) nav(`/questions/${row.id}`, { replace: true });
    },
    onError: (e: ApiError) => setError(e.message),
  });

  const trial = useMutation({
    mutationFn: (response: unknown) => questionsApi.grade(savedId as string, response),
    onError: (e: ApiError) => setError(e.message),
  });

  async function onTrial() {
    setError(null);
    try {
      trial.mutate(await buildResponse(draft.type, config, answer));
    } catch (e) {
      const code = (e as Error).message;
      setError(code === "answer_out_of_range" ? t("questions.try_answer_out_of_range") : t("questions.try_answer_needs_secure_context"));
    }
  }

  function body(): QuestionDraft {
    const payload: QuestionDraft = {
      type: draft.type,
      config,
      prompt: draft.prompt || null,
      status: draft.status,
      level: draft.level || null,
      learning_language: draft.learning_language || null,
      difficulty: draft.difficulty === "" ? null : Number(draft.difficulty),
      score: Number(draft.score || 1),
      explanation: draft.explanation || null,
      teacher_notes: draft.teacher_notes || null,
      topic_ids: draft.topic_ids,
      tag_ids: draft.tag_ids,
    };
    // Both scoring knobs are always sent. A patch that omits them leaves the previous
    // type's values on the row, and the backend rightly refuses e.g. an essay that
    // still carries `partial_scoring.mode` - retyping away from a type would be stuck.
    payload.partial_scoring = spec?.supports_partial ? { mode: draft.partial_mode } : {};
    payload.negative_scoring =
      NEGATIVE_TYPES.includes(draft.type) && draft.penalty !== "" ? { penalty: Number(draft.penalty) } : {};
    // The context is sent as a whole: the kind decides which id may survive, so a
    // question moved from a text back to independent cannot keep carrying that text.
    payload.context_kind = draft.context_kind;
    payload.reading_id = draft.context_kind === "reading_bound" ? draft.reading_id : null;
    payload.listening_id = draft.context_kind === "listening_bound" ? draft.listening_id : null;
    payload.media_asset_id = draft.media_asset_id;
    if (!isNew && draft.change_note) payload.change_note = draft.change_note;
    return payload;
  }

  const tree = useMemo(() => flatten(topics.data?.items || []), [topics.data]);

  return (
    <div className="stack">
      <div className="row">
        <h1 style={{ margin: 0 }}>{isNew ? t("questions.new") : t("questions.edit")}</h1>
        <span className="spacer" />
        <Link className="btn secondary" to="/questions">
          {t("questions.back_to_bank")}
        </Link>
      </div>

      {error ? <div className="alert error">{error}</div> : null}

      <div className="two-col">
        <div className="card stack">
          <div className="field">
            <label>{t("questions.type")}</label>
            <select
              className="input"
              value={draft.type}
              onChange={(e) => {
                const type = e.target.value;
                setDraft({ ...draft, type });
                setConfig(emptyConfig(type));
              }}
            >
              {(types.data?.items || []).map((item) => (
                <option key={item.type} value={item.type}>
                  {item.label}
                </option>
              ))}
            </select>
            {spec ? <p className="small muted">{t(`questions.group_${spec.group}`)}</p> : null}
          </div>

          <div className="field">
            <label>{t("questions.prompt")}</label>
            <textarea
              className="input"
              rows={2}
              value={draft.prompt}
              onChange={(e) => setDraft({ ...draft, prompt: e.target.value })}
            />
          </div>

          <div className="row">
            <div className="field" style={{ flex: 1 }}>
              <label>{t("questions.status")}</label>
              <select className="input" value={draft.status} onChange={(e) => setDraft({ ...draft, status: e.target.value })}>
                {QUESTION_STATUSES.map((status) => (
                  <option key={status} value={status}>
                    {t(`status.${status}`)}
                  </option>
                ))}
              </select>
            </div>
            <div className="field" style={{ flex: 1 }}>
              <label>{t("questions.level")}</label>
              <select className="input" value={draft.level} onChange={(e) => setDraft({ ...draft, level: e.target.value })}>
                <option value="">—</option>
                {LEVELS.map((level) => (
                  <option key={level} value={level}>
                    {level}
                  </option>
                ))}
              </select>
            </div>
            <div className="field" style={{ flex: 1 }}>
              <label>{t("questions.difficulty")}</label>
              <input
                className="input"
                type="number"
                min={1}
                max={10}
                value={draft.difficulty}
                onChange={(e) => setDraft({ ...draft, difficulty: e.target.value })}
              />
            </div>
          </div>

          <div className="row">
            <div className="field" style={{ flex: 1 }}>
              <label>{t("questions.score")}</label>
              <input className="input" type="number" min={0.5} step={0.5} value={draft.score} onChange={(e) => setDraft({ ...draft, score: e.target.value })} />
            </div>
            <div className="field" style={{ flex: 1 }}>
              <label>{t("questions.learning_language")}</label>
              <input
                className="input"
                placeholder="az / en / ru / tr"
                value={draft.learning_language}
                onChange={(e) => setDraft({ ...draft, learning_language: e.target.value })}
              />
            </div>
          </div>

          {spec?.supports_partial ? (
            <div className="row">
              <div className="field" style={{ flex: 1 }}>
                <label>{t("questions.partial_mode")}</label>
                <select className="input" value={draft.partial_mode} onChange={(e) => setDraft({ ...draft, partial_mode: e.target.value })}>
                  <option value="partial">{t("questions.mode_partial")}</option>
                  <option value="all_or_nothing">{t("questions.mode_all_or_nothing")}</option>
                </select>
              </div>
            </div>
          ) : null}

          {["multi_select", "matching"].includes(draft.type) ? (
            <div className="field">
              <label>{t("questions.penalty")}</label>
              <input
                className="input"
                type="number"
                min={0}
                step={0.1}
                placeholder={draft.type === "multi_select" ? "1" : "0"}
                value={draft.penalty}
                onChange={(e) => setDraft({ ...draft, penalty: e.target.value })}
              />
              <p className="small muted">{t("questions.penalty_hint")}</p>
            </div>
          ) : null}

          <h2 style={{ marginTop: 0 }}>{t("questions.answer_key")}</h2>
          <QuestionConfigForm type={draft.type} config={config} onChange={setConfig} />
        </div>

        <div className="stack">
          <div className="card stack">
            <h2 style={{ margin: 0 }}>{t("questions.filing")}</h2>
            <div className="field">
              <label>{t("questions.topics")}</label>
              <select
                className="input"
                multiple
                size={Math.min(8, Math.max(3, tree.length))}
                value={draft.topic_ids}
                onChange={(e) =>
                  setDraft({ ...draft, topic_ids: Array.from(e.target.selectedOptions).map((o) => o.value) })
                }
              >
                {tree.map(({ node, depth }) => (
                  <option key={node.id} value={node.id}>
                    {"—".repeat(depth)} {node.name}
                  </option>
                ))}
              </select>
              <p className="small muted">{t("questions.topics_hint")}</p>
            </div>
            <div className="field">
              <label>{t("questions.tags")}</label>
              <div className="row" style={{ flexWrap: "wrap" }}>
                {(tags.data?.items || []).map((tag) => (
                  <button
                    key={tag.id}
                    type="button"
                    className={draft.tag_ids.includes(tag.id) ? "chip on" : "chip"}
                    onClick={() =>
                      setDraft({
                        ...draft,
                        tag_ids: draft.tag_ids.includes(tag.id)
                          ? draft.tag_ids.filter((value) => value !== tag.id)
                          : [...draft.tag_ids, tag.id],
                      })
                    }
                  >
                    {tag.name}
                  </button>
                ))}
              </div>
              <p className="small muted">
                <Link to="/topics">{t("questions.manage_taxonomy")}</Link>
              </p>
            </div>
          </div>

          <div className="card stack">
            <h2 style={{ margin: 0 }}>{t("questions.context_card")}</h2>
            <div className="field">
              <label>{t("questions.context_kind")}</label>
              <select
                className="input"
                value={draft.context_kind}
                onChange={(e) => setDraft({ ...draft, context_kind: e.target.value })}
              >
                {CONTEXT_KINDS.map((kind) => (
                  <option key={kind} value={kind}>
                    {t(`questions.context_${kind}`)}
                  </option>
                ))}
              </select>
              <p className="small muted">{t("questions.context_hint")}</p>
            </div>

            {draft.context_kind === "reading_bound" ? (
              <div className="field">
                <label>{t("questions.choose_reading")}</label>
                <select
                  className="input"
                  value={draft.reading_id || ""}
                  onChange={(e) => setDraft({ ...draft, reading_id: e.target.value || null })}
                >
                  <option value="">{t("questions.choose_reading_empty")}</option>
                  {(readings.data?.items || []).map((row) => (
                    <option key={row.id} value={row.id}>
                      {row.title}
                    </option>
                  ))}
                </select>
                <p className="small muted">
                  <Link to="/reading">{t("questions.open_readings")}</Link>
                </p>
              </div>
            ) : null}

            {draft.context_kind === "listening_bound" ? (
              <div className="field">
                <label>{t("questions.choose_listening")}</label>
                <select
                  className="input"
                  value={draft.listening_id || ""}
                  onChange={(e) => setDraft({ ...draft, listening_id: e.target.value || null })}
                >
                  <option value="">{t("questions.choose_listening_empty")}</option>
                  {(listenings.data?.items || []).map((row) => (
                    <option key={row.id} value={row.id}>
                      {row.title}
                    </option>
                  ))}
                </select>
                <p className="small muted">
                  <Link to="/listening">{t("questions.open_listenings")}</Link>
                </p>
              </div>
            ) : null}

            <div className="field">
              <label>{t("questions.attached_file")}</label>
              <MediaPicker
                value={draft.media_asset_id}
                onChange={(assetId) => setDraft({ ...draft, media_asset_id: assetId })}
                copy={{
                  none: t("questions.no_file_chosen"),
                  choose: t("questions.choose_file"),
                  detach: t("questions.detach_file"),
                  trashed: t("questions.trashed_file_hint"),
                }}
              />
              <p className="small muted">{t("questions.attached_file_hint")}</p>
            </div>
          </div>

          <div className="card stack">
            <h2 style={{ margin: 0 }}>{t("questions.teacher_only")}</h2>
            <div className="field">
              <label>{t("questions.explanation")}</label>
              <textarea className="input" rows={2} value={draft.explanation} onChange={(e) => setDraft({ ...draft, explanation: e.target.value })} />
            </div>
            <div className="field">
              <label>{t("questions.teacher_notes")}</label>
              <textarea className="input" rows={2} value={draft.teacher_notes} onChange={(e) => setDraft({ ...draft, teacher_notes: e.target.value })} />
            </div>
            {!isNew ? (
              <div className="field">
                <label>{t("questions.change_note")}</label>
                <input className="input" value={draft.change_note} onChange={(e) => setDraft({ ...draft, change_note: e.target.value })} />
              </div>
            ) : null}
          </div>

          <div className="row">
            <button className="btn" onClick={() => save.mutate(body())} disabled={save.isPending}>
              {isNew ? t("questions.create") : t("questions.save")}
            </button>
            {save.isSuccess ? <span className="small muted">{t("questions.saved")}</span> : null}
          </div>
        </div>
      </div>

      {savedId && preview.data ? (
        <div className="two-col">
          <LearnerPreview view={preview.data} />
          <div className="card stack">
            <h2 style={{ margin: 0 }}>{t("questions.try_answer")}</h2>
            <p className="small muted">{t("questions.try_answer_hint")}</p>
            <textarea className="input" rows={3} value={answer} onChange={(e) => setAnswer(e.target.value)} placeholder={t("questions.try_answer_placeholder")} />
            <div>
              <button className="btn secondary" disabled={trial.isPending} onClick={() => void onTrial()}>
                {t("questions.check")}
              </button>
            </div>
            {trial.data ? (
              <div className="card" style={{ background: "var(--accent-weak)" }}>
                <strong>
                  {t("questions.scored", { score: trial.data.score, max: trial.data.max_score })}
                </strong>
                <div className="small muted">
                  {trial.data.requires_manual
                    ? t("questions.needs_manual")
                    : trial.data.correct
                      ? t("questions.answer_correct")
                      : t("questions.answer_wrong")}
                </div>
                <pre className="mono small">{JSON.stringify(trial.data.detail, null, 1)}</pre>
              </div>
            ) : null}
          </div>
        </div>
      ) : null}

      {!isNew && versions.data ? (
        <div className="card">
          <h2 style={{ marginTop: 0 }}>{t("questions.history")}</h2>
          <p className="small muted">{t("questions.history_hint")}</p>
          <table>
            <thead>
              <tr>
                <th>{t("questions.version")}</th>
                <th>{t("questions.change_note")}</th>
                <th>{t("questions.when")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {versions.data.items.map((row) => (
                <tr key={row.version}>
                  <td>v{row.version}</td>
                  <td>{row.change_note || "—"}</td>
                  <td className="small muted">{new Date(row.created_at).toLocaleString()}</td>
                  <td>
                    <button
                      className="btn ghost"
                      onClick={async () =>
                        setOpenedVersion(
                          openedVersion?.version === row.version ? null : await questionsApi.version(row.question_id, row.version),
                        )
                      }
                    >
                      {openedVersion?.version === row.version ? t("questions.hide") : t("questions.show")}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {openedVersion ? (
            <pre className="mono small" style={{ background: "var(--surface)", padding: 8, overflow: "auto" }}>
              {JSON.stringify(openedVersion.snapshot, null, 1)}
            </pre>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

/** The shape `/questions/{id}/grade` expects for this type, from one free-text box. */
async function buildResponse(type: string, config: Config, raw: string): Promise<unknown> {
  const trimmed = raw.trim();
  switch (type) {
    case "multiple_choice":
      return { option_index: Number(trimmed) };
    case "multi_select":
      return { option_indexes: trimmed.split(/[,\s]+/).filter(Boolean).map(Number) };
    case "true_false":
      return { value: trimmed.toLowerCase() !== "false" };
    case "gap_fill":
      return { blanks: trimmed.split(/[,\n]/).map((part) => part.trim()) };
    case "ordering": {
      const items = (config.items || []) as string[];
      const wanted = trimmed.split(/[,\n]/).map((part) => part.trim()).filter(Boolean);
      return {
        order: await Promise.all(
          wanted.map((token) => elementRef("ordering.item", Number(token) - 1, items[Number(token) - 1])),
        ),
      };
    }
    case "matching": {
      const pairs = (config.pairs || []) as { left: string; right: string }[];
      return {
        pairs: await Promise.all(
          pairs.map(async (pair, index) => ({
            left_ref: await elementRef("matching.left", index, pair.left),
            right_ref: await elementRef("matching.right", index, pair.right),
          })),
        ),
      };
    }
    default:
      return { text: raw };
  }
}

/**
 * Mirrors `app.services.question_engine._ref`, so a trial answer addresses the element
 * the grader expects. SHA-256 only exists in a secure browsing context: when the
 * browser cannot offer it the check says so rather than inventing a reference.
 */
async function elementRef(role: string, index: number, value: string | undefined): Promise<string> {
  if (value === undefined) throw new Error("answer_out_of_range");
  if (!crypto?.subtle) throw new Error("answer_needs_secure_context");
  const bytes = new TextEncoder().encode(`${role}\x1f${index}\x1f${value}`);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("")
    .slice(0, 12);
}
