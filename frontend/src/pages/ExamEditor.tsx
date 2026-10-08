// One paper: its rules, the questions it pins, who it was handed to, and how it would be dealt.
//
// This screen is where the difference between a collection and an exam becomes visible. Adding a
// question takes the version current at that moment and the paper never follows the bank again,
// so each row shows *both* numbers: the version this paper asks, and the version the bank is on
// now. A teacher who sees "v3 / bank v7" understands the paper is asking the older wording, and
// the answer is to make a copy - not to edit this one, because the marks already given belong to
// it.
//
// The composition closes the moment somebody sits the paper (`composition_locked`), while the
// rules stay editable, because each sitting copied them into its own blueprint when it opened.
// The screen says which of the two it is refusing rather than leaving a dead control.
//
// Every choice the teacher can make is listed by `/exams/meta`, including `ai_assisted` grading
// shown as unavailable: the writing assistant belongs to a later phase, and a paper published in
// that mode today would promise a queue of suggestions that never arrives.
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { ApiError } from "../api/client";
import { examsApi, gradingApi, type AttemptRow, type Exam, type ExamItem, type ExamMeta, type ExamRules } from "../api/exams";
import { listeningApi } from "../api/listening";
import { questionsApi } from "../api/questions";
import { readingApi } from "../api/reading";
import { span } from "../i18n/format";

const PICKER_SIZE = 20;
const TABS = ["rules", "contents", "audience", "sittings", "preview"] as const;

const EMPTY: ExamRules = {
  title: "",
  description: null,
  learning_language: null,
  level: null,
  available_from: null,
  available_to: null,
  duration_minutes: null,
  must_finish_before_close: false,
  max_attempts: null,
  passing_score: null,
  shuffle_questions: false,
  shuffle_options: false,
  resume_after_disconnect: true,
  restrict_copy_paste: false,
  monitor_tab_switch: false,
  tab_switch_limit: null,
  tab_switch_action: null,
  allow_previous: true,
  feedback_timing: "after_session",
  show_correct_answers: false,
  show_explanations: false,
  result_visibility: "after_close",
  partial_scoring_enabled: true,
  negative_marking_enabled: false,
  grading_mode: "automatic",
  auto_submit_on_expiry: true,
};

/** `datetime-local` speaks wall clock; the server compares instants. Converting on the way in
 * and out is what keeps a teacher's "Friday 9:00" from becoming 7:00 or 11:00. */
function toInput(iso: string | null | undefined) {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function fromInput(value: string) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}

export default function ExamEditor() {
  const { t } = useTranslation();
  const nav = useNavigate();
  const { id = "" } = useParams();
  const qc = useQueryClient();
  const isNew = !id;
  const [tab, setTab] = useState<(typeof TABS)[number]>("rules");
  const [draft, setDraft] = useState<ExamRules>(EMPTY);
  const [message, setMessage] = useState<string | null>(null);
  const [previewSeed, setPreviewSeed] = useState<string | undefined>();

  const meta = useQuery({ queryKey: ["exams-meta"], queryFn: examsApi.meta });
  const exam = useQuery({
    queryKey: ["exam-one", id],
    queryFn: () => examsApi.get(id),
    enabled: !isNew,
  });

  useEffect(() => {
    if (!exam.data) return;
    const row = exam.data;
    setDraft({
      title: row.title,
      description: row.description ?? null,
      learning_language: row.learning_language ?? null,
      level: row.level ?? null,
      available_from: row.available_from ?? null,
      available_to: row.available_to ?? null,
      duration_minutes: row.duration_minutes ?? null,
      must_finish_before_close: row.must_finish_before_close,
      max_attempts: row.max_attempts ?? null,
      passing_score: row.passing_score ?? null,
      shuffle_questions: row.shuffle_questions,
      shuffle_options: row.shuffle_options,
      resume_after_disconnect: row.resume_after_disconnect,
      restrict_copy_paste: row.restrict_copy_paste,
      monitor_tab_switch: row.monitor_tab_switch,
      tab_switch_limit: row.tab_switch_limit ?? null,
      tab_switch_action: row.tab_switch_action ?? null,
      allow_previous: row.allow_previous,
      feedback_timing: row.feedback_timing,
      show_correct_answers: row.show_correct_answers,
      show_explanations: row.show_explanations,
      result_visibility: row.result_visibility,
      partial_scoring_enabled: row.partial_scoring_enabled,
      negative_marking_enabled: row.negative_marking_enabled,
      grading_mode: row.grading_mode,
      auto_submit_on_expiry: row.auto_submit_on_expiry,
    });
  }, [exam.data]);

  const create = useMutation({
    mutationFn: () => examsApi.create({ ...draft, title: String(draft.title || "") }),
    onSuccess: (row) => {
      setMessage(null);
      nav(`/exams/${row.id}`, { replace: true });
    },
    onError: (e: ApiError) => setMessage(e.message),
  });

  const save = useMutation({
    // Only the fields the teacher can see are sent; the backend applies what arrives and leaves
    // the rest alone, so a rename never rewrites a timer.
    mutationFn: () => examsApi.update(id, draft),
    onSuccess: (row) => {
      setMessage(null);
      qc.setQueryData(["exam-one", id], row);
      qc.invalidateQueries({ queryKey: ["exams"] });
    },
    onError: (e: ApiError) => setMessage(e.message),
  });

  const lifecycle = useMutation({
    mutationFn: async ({ action, next }: { action: "status" | "trash" | "restore" | "clone"; next?: string }) => {
      if (action === "trash") return examsApi.trash(id);
      if (action === "restore") return examsApi.restore(id);
      if (action === "clone") return examsApi.clone(id);
      return examsApi.setStatus(id, next || "draft");
    },
    onSuccess: (result, variables) => {
      setMessage(null);
      if (variables.action === "clone" && result && "id" in result) {
        nav(`/exams/${result.id}`, { replace: true });
        return;
      }
      if (result && "id" in result && variables.action === "status") qc.setQueryData(["exam-one", id], result);
      qc.invalidateQueries({ queryKey: ["exam-one", id] });
      qc.invalidateQueries({ queryKey: ["exams"] });
    },
    // Publishing runs the servability check: a draft question or a deleted block is named here,
    // and the teacher's fix belongs to the other bank, not to this paper.
    onError: (e: ApiError) => setMessage(e.message),
  });

  const patch = (next: Partial<ExamRules>) => setDraft((prev) => ({ ...prev, ...next }));
  const dirty = useMemo(
    () => JSON.stringify(draft) !== JSON.stringify(exam.data ? hydrate(exam.data) : EMPTY),
    [draft, exam.data],
  );

  if (isNew) {
    return (
      <div className="stack">
        <h1 style={{ margin: 0 }}>{t("exams.new")}</h1>
        <p className="muted small">{t("exams.create_hint")}</p>
        {message ? <div className="alert error">{message}</div> : null}
        <RulesCard draft={draft} onChange={patch} meta={meta.data} />
        <div className="row" style={{ gap: 8 }}>
          <button
            type="button"
            className="btn"
            disabled={!String(draft.title || "").trim() || create.isPending}
            onClick={() => create.mutate()}
          >
            {t("exams.create")}
          </button>
          <Link className="btn secondary" to="/exams">
            {t("common.cancel")}
          </Link>
        </div>
      </div>
    );
  }

  if (exam.isError) {
    return (
      <div className="stack">
        <Link className="btn secondary" to="/exams">
          ‹ {t("exams.back")}
        </Link>
        <div className="alert error">
          {t("common.could_not_load")} {(exam.error as ApiError).message}
        </div>
      </div>
    );
  }

  if (!exam.data) return <div className="card muted">{t("common.loading")}</div>;
  const row = exam.data;

  return (
    <div className="stack">
      <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        <Link className="btn secondary" to="/exams">
          ‹ {t("exams.back")}
        </Link>
        <span className="spacer" />
        {row.deleted_at ? (
          <button type="button" className="btn" onClick={() => lifecycle.mutate({ action: "restore" })}>
            {t("exams.restore")}
          </button>
        ) : (
          <>
            {row.status !== "active" ? (
              <button type="button" className="btn" onClick={() => lifecycle.mutate({ action: "status", next: "active" })}>
                {t("exams.open_for_learners")}
              </button>
            ) : (
              <button type="button" className="btn secondary" onClick={() => lifecycle.mutate({ action: "status", next: "finished" })}>
                {t("exams.close")}
              </button>
            )}
            <button type="button" className="btn secondary" onClick={() => lifecycle.mutate({ action: "clone" })}>
              {t("exams.make_copy")}
            </button>
            <button type="button" className="btn secondary" onClick={() => lifecycle.mutate({ action: "trash" })}>
              {t("exams.trash")}
            </button>
          </>
        )}
      </div>

      <h1 style={{ margin: 0 }}>{row.title}</h1>
      <div className="row small muted" style={{ flexWrap: "wrap", gap: 6 }}>
        <span className="chip">{t(`status.${row.status}`)}</span>
        <span>{t("exams.questions_n", { n: row.item_count })}</span>
        <span>{t("exams.points_n", { n: row.points })}</span>
        <span>{t("exams.assigned_n", { n: row.assignments.length })}</span>
        <span>{t("exams.sittings_n", { n: row.attempt_count })}</span>
        {row.composition_locked ? <span className="chip">{t("exams.locked")}</span> : null}
      </div>

      {row.publish_blockers.length ? (
        <div className="card stack">
          <strong className="small">{t("exams.blockers")}</strong>
          <ul className="small muted" style={{ margin: 0 }}>
            {row.publish_blockers.map((blocker) => (
              <li key={blocker.code}>{t(`errors.${blocker.code}`, blocker.params ?? {})}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {message ? <div className="alert error">{message}</div> : null}
      {lifecycle.isError ? <div className="alert error">{(lifecycle.error as ApiError).message}</div> : null}

      <div className="tabs">
        {TABS.map((entry) => (
          <button
            key={entry}
            type="button"
            className={`tab ${tab === entry ? "active" : ""}`}
            onClick={() => setTab(entry)}
          >
            {t(`exams.tab_${entry}`)}
          </button>
        ))}
      </div>

      {tab === "rules" ? (
        <>
          <RulesCard draft={draft} onChange={patch} meta={meta.data} />
          <div className="row" style={{ gap: 8 }}>
            <button type="button" className="btn" disabled={!dirty || save.isPending} onClick={() => save.mutate()}>
              {save.isPending ? t("common.loading") : t("exams.save_rules")}
            </button>
            {!dirty ? <span className="small muted">{t("exams.nothing_to_save")}</span> : null}
          </div>
        </>
      ) : null}

      {tab === "contents" ? <ContentsCard exam={row} examId={id} meta={meta.data} /> : null}
      {tab === "audience" ? <AudienceCard exam={row} examId={id} /> : null}
      {tab === "sittings" ? <SittingsCard examId={id} /> : null}
      {tab === "preview" ? (
        <PreviewCard examId={id} seed={previewSeed} onSeed={setPreviewSeed} />
      ) : null}
    </div>
  );
}

function hydrate(row: Exam): ExamRules {
  return {
    title: row.title,
    description: row.description ?? null,
    learning_language: row.learning_language ?? null,
    level: row.level ?? null,
    available_from: row.available_from ?? null,
    available_to: row.available_to ?? null,
    duration_minutes: row.duration_minutes ?? null,
    must_finish_before_close: row.must_finish_before_close,
    max_attempts: row.max_attempts ?? null,
    passing_score: row.passing_score ?? null,
    shuffle_questions: row.shuffle_questions,
    shuffle_options: row.shuffle_options,
    resume_after_disconnect: row.resume_after_disconnect,
    restrict_copy_paste: row.restrict_copy_paste,
    monitor_tab_switch: row.monitor_tab_switch,
    tab_switch_limit: row.tab_switch_limit ?? null,
    tab_switch_action: row.tab_switch_action ?? null,
    allow_previous: row.allow_previous,
    feedback_timing: row.feedback_timing,
    show_correct_answers: row.show_correct_answers,
    show_explanations: row.show_explanations,
    result_visibility: row.result_visibility,
    partial_scoring_enabled: row.partial_scoring_enabled,
    negative_marking_enabled: row.negative_marking_enabled,
    grading_mode: row.grading_mode,
    auto_submit_on_expiry: row.auto_submit_on_expiry,
  };
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="field">
      <label>{label}</label>
      {children}
      {hint ? <div className="small muted">{hint}</div> : null}
    </div>
  );
}

function Switch({
  label,
  hint,
  checked,
  onChange,
  disabled,
}: {
  label: string;
  hint?: string;
  checked: boolean;
  onChange: (value: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <div className="field">
      <label style={{ display: "flex", gap: 8, alignItems: "flex-start", cursor: disabled ? "default" : "pointer" }}>
        <input type="checkbox" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} style={{ marginTop: 4 }} />
        <span>
          {label}
          {hint ? <span className="small muted"> — {hint}</span> : null}
        </span>
      </label>
    </div>
  );
}

function RulesCard({
  draft,
  onChange,
  meta,
}: {
  draft: ExamRules;
  onChange: (patch: Partial<ExamRules>) => void;
  meta: ExamMeta | undefined;
}) {
  const { t } = useTranslation();
  const number = (value: string) => (value === "" ? null : Number(value));
  const watching = Boolean(draft.monitor_tab_switch);

  return (
    <div className="stack">
      <div className="card stack">
        <h2 style={{ margin: 0 }}>{t("exams.group_identity")}</h2>
        <Field label={t("exams.title")}>
          <input className="input" value={draft.title || ""} onChange={(e) => onChange({ title: e.target.value })} />
        </Field>
        <Field label={t("exams.description")} hint={t("exams.description_hint")}>
          <textarea className="input" rows={3} value={draft.description || ""} onChange={(e) => onChange({ description: e.target.value })} />
        </Field>
        <div className="two-col">
          <Field label={t("exams.language")}>
            <select className="input" value={draft.learning_language || ""} onChange={(e) => onChange({ learning_language: e.target.value || null })}>
              <option value="">{t("exams.any_language")}</option>
              {(meta?.languages || []).map((code) => (
                <option key={code} value={code}>
                  {code}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t("exams.level")}>
            <select className="input" value={draft.level || ""} onChange={(e) => onChange({ level: e.target.value || null })}>
              <option value="">{t("exams.any_level")}</option>
              {(meta?.levels || []).map((level) => (
                <option key={level} value={level}>
                  {level}
                </option>
              ))}
            </select>
          </Field>
        </div>
      </div>

      <div className="card stack">
        <h2 style={{ margin: 0 }}>{t("exams.group_timing")}</h2>
        <div className="two-col">
          <Field label={t("exams.opens_at")} hint={t("exams.opens_at_hint")}>
            <input
              type="datetime-local"
              className="input"
              value={toInput(draft.available_from)}
              onChange={(e) => onChange({ available_from: fromInput(e.target.value) })}
            />
          </Field>
          <Field label={t("exams.closes_at")} hint={t("exams.closes_at_hint")}>
            <input
              type="datetime-local"
              className="input"
              value={toInput(draft.available_to)}
              onChange={(e) => onChange({ available_to: fromInput(e.target.value) })}
            />
          </Field>
        </div>
        <div className="two-col">
          <Field label={t("exams.duration")} hint={t("exams.duration_hint")}>
            <input
              type="number"
              className="input"
              min={1}
              max={meta?.limits.max_duration_minutes || 600}
              value={draft.duration_minutes ?? ""}
              onChange={(e) => onChange({ duration_minutes: number(e.target.value) })}
            />
          </Field>
          <Field label={t("exams.max_attempts")} hint={t("exams.max_attempts_hint")}>
            <input
              type="number"
              className="input"
              min={1}
              max={meta?.limits.max_attempts || 20}
              value={draft.max_attempts ?? ""}
              onChange={(e) => onChange({ max_attempts: number(e.target.value) })}
            />
          </Field>
        </div>
        <Field label={t("exams.passing_score")} hint={t("exams.passing_score_hint")}>
          <input
            type="number"
            className="input"
            style={{ maxWidth: 140 }}
            min={0}
            max={100}
            value={draft.passing_score ?? ""}
            onChange={(e) => onChange({ passing_score: number(e.target.value) })}
          />
        </Field>
        <Switch
          label={t("exams.must_finish_before_close")}
          hint={t("exams.must_finish_before_close_hint")}
          checked={Boolean(draft.must_finish_before_close)}
          onChange={(value) => onChange({ must_finish_before_close: value })}
        />
        <Switch
          label={t("exams.auto_submit_on_expiry")}
          hint={t("exams.auto_submit_on_expiry_hint")}
          checked={Boolean(draft.auto_submit_on_expiry)}
          onChange={(value) => onChange({ auto_submit_on_expiry: value })}
        />
      </div>

      <div className="card stack">
        <h2 style={{ margin: 0 }}>{t("exams.group_order")}</h2>
        <Switch
          label={t("exams.shuffle_questions")}
          hint={t("exams.shuffle_questions_hint")}
          checked={Boolean(draft.shuffle_questions)}
          onChange={(value) => onChange({ shuffle_questions: value })}
        />
        <Switch
          label={t("exams.shuffle_options")}
          hint={t("exams.shuffle_options_hint")}
          checked={Boolean(draft.shuffle_options)}
          onChange={(value) => onChange({ shuffle_options: value })}
        />
        <Switch
          label={t("exams.allow_previous")}
          hint={t("exams.allow_previous_hint")}
          checked={Boolean(draft.allow_previous)}
          onChange={(value) => onChange({ allow_previous: value })}
        />
      </div>

      <div className="card stack">
        <h2 style={{ margin: 0 }}>{t("exams.group_conduct")}</h2>
        <Switch
          label={t("exams.resume_after_disconnect")}
          hint={t("exams.resume_after_disconnect_hint")}
          checked={Boolean(draft.resume_after_disconnect)}
          onChange={(value) => onChange({ resume_after_disconnect: value })}
        />
        <Switch
          label={t("exams.restrict_copy_paste")}
          hint={t("exams.restrict_copy_paste_hint")}
          checked={Boolean(draft.restrict_copy_paste)}
          onChange={(value) => onChange({ restrict_copy_paste: value })}
        />
        <Switch
          label={t("exams.monitor_tab_switch")}
          hint={t("exams.monitor_tab_switch_hint")}
          checked={watching}
          onChange={(value) => onChange({ monitor_tab_switch: value, tab_switch_limit: value ? draft.tab_switch_limit : null })}
        />
        {watching ? (
          <div className="two-col">
            <Field label={t("exams.tab_switch_action")}>
              <select
                className="input"
                value={draft.tab_switch_action || ""}
                onChange={(e) => onChange({ tab_switch_action: e.target.value || null })}
              >
                <option value="">{t("exams.choose_action")}</option>
                {(meta?.tab_switch_actions || []).map((action) => (
                  <option key={action} value={action}>
                    {t(`exams.action_${action}`)}
                  </option>
                ))}
              </select>
            </Field>
            <Field label={t("exams.tab_switch_limit")} hint={t("exams.tab_switch_limit_hint")}>
              <input
                type="number"
                className="input"
                min={1}
                max={meta?.limits.max_tab_switch_limit || 100}
                value={draft.tab_switch_limit ?? ""}
                onChange={(e) => onChange({ tab_switch_limit: number(e.target.value) })}
              />
            </Field>
          </div>
        ) : null}
      </div>

      <div className="card stack">
        <h2 style={{ margin: 0 }}>{t("exams.group_marks")}</h2>
        <div className="two-col">
          <Field label={t("exams.feedback_timing")} hint={t("exams.feedback_timing_hint")}>
            <select className="input" value={draft.feedback_timing} onChange={(e) => onChange({ feedback_timing: e.target.value })}>
              {(meta?.feedback_timings || []).map((timing) => (
                <option key={timing} value={timing}>
                  {t(`exams.timing_${timing}`)}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t("exams.result_visibility")} hint={t("exams.result_visibility_hint")}>
            <select className="input" value={draft.result_visibility} onChange={(e) => onChange({ result_visibility: e.target.value })}>
              {(meta?.result_visibilities || []).map((visibility) => (
                <option key={visibility} value={visibility}>
                  {t(`exams.visibility_${visibility}`)}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <Switch
          label={t("exams.show_correct_answers")}
          hint={t("exams.show_correct_answers_hint")}
          checked={Boolean(draft.show_correct_answers)}
          onChange={(value) => onChange({ show_correct_answers: value })}
        />
        <Switch
          label={t("exams.show_explanations")}
          hint={t("exams.show_explanations_hint")}
          checked={Boolean(draft.show_explanations)}
          onChange={(value) => onChange({ show_explanations: value })}
        />
        <div className="two-col">
          <Field label={t("exams.grading_mode")} hint={t("exams.grading_mode_hint")}>
            <select className="input" value={draft.grading_mode} onChange={(e) => onChange({ grading_mode: e.target.value })}>
              {(meta?.grading_modes || []).map((entry) => (
                <option key={entry.mode} value={entry.mode} disabled={entry.available === false}>
                  {t(`exams.mode_${entry.mode}`)}
                  {entry.available === false ? ` (${t("exams.mode_unavailable")})` : ""}
                </option>
              ))}
            </select>
          </Field>
          <div className="stack">
            <Switch
              label={t("exams.partial_scoring")}
              checked={Boolean(draft.partial_scoring_enabled)}
              onChange={(value) => onChange({ partial_scoring_enabled: value })}
            />
            <Switch
              label={t("exams.negative_marking")}
              hint={t("exams.negative_marking_hint")}
              checked={Boolean(draft.negative_marking_enabled)}
              onChange={(value) => onChange({ negative_marking_enabled: value })}
            />
          </div>
        </div>
      </div>
    </div>
  );
}

function ContentsCard({
  exam,
  examId,
  meta,
}: {
  exam: Exam;
  examId: string;
  meta: ExamMeta | undefined;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [message, setMessage] = useState<string | null>(null);
  const locked = exam.composition_locked;

  const apply = (next: Exam) => {
    qc.setQueryData(["exam-one", examId], next);
    qc.invalidateQueries({ queryKey: ["exams"] });
    setMessage(null);
  };

  const createSection = useMutation({
    mutationFn: (title: string) => examsApi.createSection(examId, { title }),
    onSuccess: () => {
      setMessage(null);
      qc.invalidateQueries({ queryKey: ["exam-one", examId] });
    },
    onError: (e: ApiError) => setMessage(e.message),
  });

  const saveSection = useMutation({
    mutationFn: ({ sectionId, body }: { sectionId: string; body: Record<string, unknown> }) =>
      examsApi.updateSection(sectionId, body),
    onSuccess: () => {
      setMessage(null);
      qc.invalidateQueries({ queryKey: ["exam-one", examId] });
    },
    onError: (e: ApiError) => setMessage(e.message),
  });

  const removeSection = useMutation({
    mutationFn: (sectionId: string) => examsApi.removeSection(sectionId),
    onSuccess: (next) => apply(next),
    onError: (e: ApiError) => setMessage(e.message),
  });

  const moveSection = useMutation({
    mutationFn: ({ sectionId, delta }: { sectionId: string; delta: number }) => {
      const order = exam.sections.map((section) => section.id);
      const at = order.indexOf(sectionId);
      const target = at + delta;
      if (target < 0 || target >= order.length) return Promise.resolve(exam);
      order.splice(target, 0, ...order.splice(at, 1));
      return examsApi.reorderSections(examId, order);
    },
    onSuccess: apply,
    onError: (e: ApiError) => setMessage(e.message),
  });

  const setPoints = useMutation({
    mutationFn: ({ itemId, points }: { itemId: string; points: number | null }) =>
      examsApi.updateItem(itemId, { points }),
    onSuccess: () => {
      setMessage(null);
      qc.invalidateQueries({ queryKey: ["exam-one", examId] });
    },
    onError: (e: ApiError) => setMessage(e.message),
  });

  const setItemSection = useMutation({
    mutationFn: ({ itemId, sectionId }: { itemId: string; sectionId: string | null }) =>
      examsApi.updateItem(itemId, { section_id: sectionId }),
    onSuccess: () => {
      setMessage(null);
      qc.invalidateQueries({ queryKey: ["exam-one", examId] });
    },
    onError: (e: ApiError) => setMessage(e.message),
  });

  const removeItem = useMutation({
    mutationFn: (itemId: string) => examsApi.removeItem(itemId),
    onSuccess: (next) => apply(next),
    // `composition_locked` is the refusal a teacher meets after a sitting exists, and its
    // sentence names the copy they should make instead.
    onError: (e: ApiError) => setMessage(e.message),
  });

  const moveItem = useMutation({
    mutationFn: ({ itemId, delta }: { itemId: string; delta: number }) => {
      const order = exam.items.map((item) => item.id);
      const at = order.indexOf(itemId);
      const target = at + delta;
      if (target < 0 || target >= order.length) return Promise.resolve(exam);
      order.splice(target, 0, ...order.splice(at, 1));
      return examsApi.reorderItems(examId, order);
    },
    onSuccess: apply,
    onError: (e: ApiError) => setMessage(e.message),
  });

  const [newSection, setNewSection] = useState("");

  return (
    <div className="stack">
      {message ? <div className="alert error">{message}</div> : null}
      {locked ? (
        <div className="card small">{t("exams.locked_hint")}</div>
      ) : null}

      <div className="card stack">
        <h2 style={{ margin: 0 }}>{t("exams.sections")}</h2>
        <p className="small muted" style={{ margin: 0 }}>
          {t("exams.sections_hint")}
        </p>
        {exam.sections.map((section, index) => (
          <div className="row" key={section.id} style={{ flexWrap: "wrap", gap: 6, alignItems: "center" }}>
            <input
              className="input"
              style={{ maxWidth: 200 }}
              defaultValue={section.title || ""}
              disabled={locked}
              onBlur={(e) => {
                if (e.target.value !== (section.title || "")) {
                  saveSection.mutate({ sectionId: section.id, body: { title: e.target.value } });
                }
              }}
            />
            <input
              className="input"
              style={{ flex: "1 1 200px" }}
              placeholder={t("exams.section_instructions")}
              defaultValue={section.instructions || ""}
              disabled={locked}
              onBlur={(e) => {
                saveSection.mutate({ sectionId: section.id, body: { instructions: e.target.value } });
              }}
            />
            <label className="small" style={{ display: "flex", gap: 4, alignItems: "center" }}>
              <input
                type="checkbox"
                checked={section.shuffle_items}
                disabled={locked}
                onChange={(e) => saveSection.mutate({ sectionId: section.id, body: { shuffle_items: e.target.checked } })}
              />
              {t("exams.shuffle_this_part")}
            </label>
            <span className="small muted">
              {t("exams.questions_n", { n: section.item_count })} · {t("exams.points_n", { n: section.points })}
            </span>
            <span className="spacer" />
            <button type="button" className="btn ghost" disabled={locked || index === 0} onClick={() => moveSection.mutate({ sectionId: section.id, delta: -1 })}>
              ↑
            </button>
            <button
              type="button"
              className="btn ghost"
              disabled={locked || index === exam.sections.length - 1}
              onClick={() => moveSection.mutate({ sectionId: section.id, delta: 1 })}
            >
              ↓
            </button>
            <button type="button" className="btn ghost" disabled={locked} onClick={() => removeSection.mutate(section.id)}>
              {t("exams.remove_section")}
            </button>
          </div>
        ))}
        <div className="row" style={{ gap: 6 }}>
          <input
            className="input"
            style={{ maxWidth: 220 }}
            placeholder={t("exams.new_section")}
            value={newSection}
            disabled={locked}
            onChange={(e) => setNewSection(e.target.value)}
          />
          <button
            type="button"
            className="btn secondary"
            disabled={locked || !newSection.trim()}
            onClick={() => {
              createSection.mutate(newSection.trim());
              setNewSection("");
            }}
          >
            {t("common.add")}
          </button>
        </div>
      </div>

      <div className="card stack">
        <h2 style={{ margin: 0 }}>{t("exams.items_title")}</h2>
        <p className="small muted" style={{ margin: 0 }}>
          {t("exams.items_hint", { n: meta?.limits.max_items || 200 })}
        </p>
        {exam.items.length === 0 ? <div className="muted small">{t("exams.no_items")}</div> : null}
        {exam.items.map((item, index) => (
          <ItemRow
            key={item.id}
            item={item}
            index={index}
            total={exam.items.length}
            sections={exam.sections}
            locked={locked}
            onPoints={(points) => setPoints.mutate({ itemId: item.id, points })}
            onSection={(sectionId) => setItemSection.mutate({ itemId: item.id, sectionId })}
            onMove={(delta) => moveItem.mutate({ itemId: item.id, delta })}
            onRemove={() => removeItem.mutate(item.id)}
          />
        ))}
      </div>

      <AddPanel examId={examId} held={new Set(exam.items.map((item) => item.ref_id))} meta={meta} locked={locked} />
    </div>
  );
}

function ItemRow({
  item,
  index,
  total,
  sections,
  locked,
  onPoints,
  onSection,
  onMove,
  onRemove,
}: {
  item: ExamItem;
  index: number;
  total: number;
  sections: Exam["sections"];
  locked: boolean;
  onPoints: (points: number | null) => void;
  onSection: (sectionId: string | null) => void;
  onMove: (delta: number) => void;
  onRemove: () => void;
}) {
  const { t } = useTranslation();
  const drifted =
    item.version !== null && item.current_version !== null && item.version !== item.current_version;

  return (
    <div className="row" style={{ flexWrap: "wrap", gap: 6, alignItems: "center", borderBottom: "1px solid var(--border)", paddingBottom: 6 }}>
      <span className="small muted" style={{ minWidth: 20 }}>
        {index + 1}
      </span>
      <div style={{ flex: "1 1 220px" }}>
        <div className="small">{item.title || t("exams.no_prompt")}</div>
        <div className="small muted">
          {/* Both numbers are the fact: what this paper asks, and what the bank holds now. */}
          {item.version !== null
            ? t("exams.pinned_v", { v: item.version })
            : t("exams.not_pinned")}
          {drifted ? ` · ${t("exams.bank_v", { v: item.current_version })}` : ""}
          {item.context_title ? ` · ${item.context_title}` : ""}
          {/* The word is the state the publish check read, so "not on the paper" and "on the
              paper but not ready to serve" are two different fixes in two different banks. */}
          {!item.serveable ? ` · ${t(`exams.state_${item.state}`)}` : ""}
        </div>
      </div>
      <input
        type="number"
        className="input"
        style={{ width: 74 }}
        min={0.5}
        step={0.5}
        defaultValue={item.points ?? ""}
        disabled={locked}
        title={t("exams.points_override")}
        onBlur={(e) => {
          const next = e.target.value === "" ? null : Number(e.target.value);
          if (next !== (item.points ?? null)) onPoints(next);
        }}
      />
      <span className="small muted">{t("exams.asked_for_n", { n: item.effective_points })}</span>
      <select
        className="input"
        style={{ maxWidth: 150 }}
        value={item.section_id || ""}
        disabled={locked}
        onChange={(e) => onSection(e.target.value || null)}
      >
        <option value="">{t("exams.unsectioned")}</option>
        {sections.map((section) => (
          <option key={section.id} value={section.id}>
            {section.title || t("exams.part_n", { n: section.position + 1 })}
          </option>
        ))}
      </select>
      <button type="button" className="btn ghost" disabled={locked || index === 0} onClick={() => onMove(-1)}>
        ↑
      </button>
      <button type="button" className="btn ghost" disabled={locked || index >= total - 1} onClick={() => onMove(1)}>
        ↓
      </button>
      <button type="button" className="btn ghost" disabled={locked} onClick={onRemove}>
        ✕
      </button>
    </div>
  );
}

function AddPanel({
  examId,
  held,
  meta,
  locked,
}: {
  examId: string;
  held: Set<string>;
  meta: ExamMeta | undefined;
  locked: boolean;
}) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [kind, setKind] = useState("question");
  const [q, setQ] = useState("");
  const [level, setLevel] = useState("");
  const [language, setLanguage] = useState("");
  const [page, setPage] = useState(1);
  const [message, setMessage] = useState<string | null>(null);
  const [blocksFor, setBlocksFor] = useState<string | null>(null);
  const [refused, setRefused] = useState<{ ref_id: string; reason: string }[]>([]);

  // The two authoring banks and the two skill banks name their language filter differently, and
  // an unrecognised query parameter is ignored, so sending the wrong one would look like a filter
  // that does nothing rather than one that was never applied.
  const filters =
    kind === "question"
      ? { q, level, learning_language: language, status: "ready", view: "bank", page, page_size: PICKER_SIZE }
      : { q, level, language, status: "ready", view: "bank", page, page_size: PICKER_SIZE };

  const bank = useQuery({
    queryKey: ["exam-bank", kind, filters],
    queryFn: () => {
      if (kind === "question")
        return questionsApi.list(filters).then((data) => ({
          ...data,
          items: data.items.map((row) => ({
            id: row.id,
            title: row.prompt || t("exams.no_prompt"),
            detail: `${t(`questions.type_${row.type}`)} · ${row.level || "—"} · ${t("exams.points_n", { n: row.score })}`,
          })),
        }));
      if (kind === "reading")
        return readingApi.list({ ...filters, sort: "updated_at" }).then((data) => ({
          ...data,
          items: data.items.map((row) => ({
            id: row.id,
            title: row.title,
            detail: `${row.level || "—"} · ${t("exams.questions_n", { n: row.question_count })}`,
          })),
        }));
      return listeningApi.list({ ...filters, sort: "updated_at" }).then((data) => ({
        ...data,
        items: data.items.map((row) => ({
          id: row.id,
          title: row.title,
          detail: `${row.level || "—"} · ${t("exams.questions_n", { n: row.question_count })}`,
        })),
      }));
    },
  });

  const add = useMutation({
    mutationFn: (body: { ref_id: string; set_id?: string | null }) =>
      examsApi.addItems(examId, [{ kind, ref_id: body.ref_id, set_id: body.set_id ?? null }]),
    onSuccess: (result) => {
      setMessage(null);
      // Adding a passage is a batch: the questions already on the paper come back named rather
      // than silently dropped, so "added 3 of 8" is explained on the same screen.
      setRefused(result.skipped);
      qc.setQueryData(["exam-one", examId], result.exam);
      qc.invalidateQueries({ queryKey: ["exams"] });
    },
    // A reference already on the paper answers 409 `reference_exists`; a passage whose every
    // question is already there comes back with `skipped` filled in instead.
    onError: (e: ApiError) => setMessage(e.message),
  });

  const pages = Math.max(1, Math.ceil((bank.data?.total ?? 0) / PICKER_SIZE));

  return (
    <div className="card stack">
      <h2 style={{ margin: 0 }}>{t("exams.add_from_bank")}</h2>
      <p className="small muted" style={{ margin: 0 }}>
        {t("exams.add_hint")}
      </p>
      {locked ? <div className="small muted">{t("exams.locked_hint")}</div> : null}
      <div className="tabs">
        {(meta?.kinds || []).map((entry) => (
          <button
            key={entry.kind}
            type="button"
            className={`tab ${kind === entry.kind ? "active" : ""}`}
            onClick={() => {
              setKind(entry.kind);
              setPage(1);
              setBlocksFor(null);
              setRefused([]);
            }}
          >
            {t(`exams.bank_tab_${entry.kind}`)}
          </button>
        ))}
      </div>
      <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        <input
          className="input"
          style={{ maxWidth: 220 }}
          placeholder={t("exams.bank_search")}
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            setPage(1);
          }}
        />
        <select className="input" style={{ maxWidth: 110 }} value={level} onChange={(e) => { setLevel(e.target.value); setPage(1); }}>
          <option value="">{t("exams.all_levels")}</option>
          {(meta?.levels || []).map((each) => (
            <option key={each} value={each}>
              {each}
            </option>
          ))}
        </select>
        <select className="input" style={{ maxWidth: 140 }} value={language} onChange={(e) => { setLanguage(e.target.value); setPage(1); }}>
          <option value="">{t("exams.all_languages")}</option>
          {(meta?.languages || []).map((code) => (
            <option key={code} value={code}>
              {code}
            </option>
          ))}
        </select>
      </div>
      {message ? <div className="alert error">{message}</div> : null}
      {refused.length ? (
        <div className="card small">
          {/* Grouped by the sentence the server gave, because adding a whole text whose questions
              are already on the paper refuses eight of them for one reason. */}
          {t("exams.add_refused", { n: refused.length })}
          <ul style={{ margin: "4px 0 0" }}>
            {Object.entries(
              refused.reduce<Record<string, number>>((acc, entry) => {
                acc[entry.reason] = (acc[entry.reason] || 0) + 1;
                return acc;
              }, {}),
            ).map(([reason, n]) => (
              <li key={reason}>{n === 1 ? reason : `${n} × ${reason}`}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {bank.isError ? <div className="alert error">{t("common.could_not_load")} {(bank.error as ApiError).message}</div> : null}

      <div className="stack">
        {(bank.data?.items || []).map((row) => {
          const already = held.has(row.id);
          return (
            <div key={row.id} className="stack" style={{ borderBottom: "1px solid var(--border)", paddingBottom: 6 }}>
              <div className="row" style={{ gap: 8, alignItems: "center" }}>
                <div style={{ flex: 1 }}>
                  <div className="small">{row.title}</div>
                  <div className="small muted">{row.detail}</div>
                </div>
                {kind !== "question" ? (
                  <button
                    type="button"
                    className="btn ghost"
                    onClick={() => setBlocksFor(blocksFor === row.id ? null : row.id)}
                  >
                    {t("exams.choose_block")}
                  </button>
                ) : null}
                <button
                  type="button"
                  className="btn secondary"
                  disabled={locked || already || add.isPending}
                  onClick={() => add.mutate({ ref_id: row.id })}
                  style={{ minHeight: 44 }}
                >
                  {already ? t("exams.already_added") : kind === "question" ? t("exams.add") : t("exams.add_whole")}
                </button>
              </div>
              {blocksFor === row.id ? (
                <BlockPicker
                  kind={kind}
                  passageId={row.id}
                  onPick={(setId) => add.mutate({ ref_id: row.id, set_id: setId })}
                  disabled={locked}
                />
              ) : null}
            </div>
          );
        })}
        {!bank.isLoading && (bank.data?.items || []).length === 0 ? (
          <div className="muted small">{t("exams.bank_empty")}</div>
        ) : null}
      </div>

      <div className="row">
        <span className="small muted">{t("exams.bank_showing", { total: bank.data?.total ?? 0 })}</span>
        <span className="spacer" />
        <button type="button" className="btn secondary" disabled={page <= 1} onClick={() => setPage(page - 1)}>
          ‹
        </button>
        <span className="small">
          {page} / {pages}
        </span>
        <button type="button" className="btn secondary" disabled={page >= pages} onClick={() => setPage(page + 1)}>
          ›
        </button>
      </div>
    </div>
  );
}

/** A reading or a recording reaches a paper through the questions filed under it, so the teacher
 * chooses the whole text or one block of it. `set_id` is the narrowing the backend accepts. */
function BlockPicker({
  kind,
  passageId,
  onPick,
  disabled,
}: {
  kind: string;
  passageId: string;
  onPick: (setId: string) => void;
  disabled: boolean;
}) {
  const { t } = useTranslation();
  const sets = useQuery({
    queryKey: ["exam-blocks", kind, passageId],
    queryFn: () => (kind === "reading" ? readingApi.sets(passageId) : listeningApi.sets(passageId)),
  });

  if (sets.isError) return <div className="alert error">{(sets.error as ApiError).message}</div>;
  if (!sets.data) return <div className="small muted">{t("common.loading")}</div>;
  if (!sets.data.items.length) return <div className="small muted">{t("exams.no_blocks")}</div>;

  return (
    <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
      {sets.data.items.map((block, index) => (
        <button
          key={block.id}
          type="button"
          className="chip"
          disabled={disabled}
          onClick={() => onPick(block.id)}
        >
          {block.title || t("exams.block_n", { n: index + 1 })} · {t("exams.questions_n", { n: block.question_count })}
        </button>
      ))}
    </div>
  );
}

function AudienceCard({ exam, examId }: { exam: Exam; examId: string }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [message, setMessage] = useState<string | null>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const [pickedGroups, setPickedGroups] = useState<string[]>([]);
  const [search, setSearch] = useState("");

  const students = useQuery({
    queryKey: ["exam-students", search],
    queryFn: () => examsApi.students(search),
  });
  const groups = useQuery({ queryKey: ["groups"], queryFn: examsApi.groups });

  const assign = useMutation({
    // The two pickers choose from two different id spaces, so each list is sent whole in its own
    // field rather than filtered by a guess about which id belongs to which.
    mutationFn: () => examsApi.assign(examId, { student_ids: picked, group_ids: pickedGroups }),
    onSuccess: () => {
      setMessage(null);
      setPicked([]);
      setPickedGroups([]);
      qc.invalidateQueries({ queryKey: ["exam-one", examId] });
      qc.invalidateQueries({ queryKey: ["exams"] });
    },
    onError: (e: ApiError) => setMessage(e.message),
  });

  const unassign = useMutation({
    mutationFn: (assignmentId: string) => examsApi.unassign(assignmentId),
    onSuccess: () => {
      setMessage(null);
      qc.invalidateQueries({ queryKey: ["exam-one", examId] });
      qc.invalidateQueries({ queryKey: ["exams"] });
    },
    // Sittings already started are not cancelled by taking an assignment back, and the server
    // says so rather than pretending the paper was never handed out.
    onError: (e: ApiError) => setMessage(e.message),
  });

  // A learner reaches the paper two ways - named directly, or through a class that was named - and
  // the picker has to mark both, or it offers a second hand-out to somebody who already has it.
  const covered = new Set(exam.assignments.flatMap((assignment) => assignment.member_ids));

  return (
    <div className="stack">
      {message ? <div className="alert error">{message}</div> : null}

      <div className="card stack">
        <h2 style={{ margin: 0 }}>{t("exams.assign")}</h2>
        <p className="small muted" style={{ margin: 0 }}>
          {exam.status === "draft" ? t("exams.assign_needs_publish") : t("exams.assign_hint")}
        </p>
        {/* Handing a paper out is the act of issuing it, and a draft has no composition to
            issue yet - the server says so. Offering the button and letting the teacher read the
            refusal after they press it would make a rule about the paper's life look like a bug
            in the form, so the tab names the step to take first instead. */}
        {exam.status === "draft" ? null : (
          <>
            <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
            <input
              className="input"
              style={{ maxWidth: 220 }}
              placeholder={t("exams.search_students")}
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
            <span className="small muted">{t("exams.picked_n", { n: picked.length + pickedGroups.length })}</span>
            <span className="spacer" />
            <button
              type="button"
              className="btn"
              disabled={!picked.length && !pickedGroups.length}
              onClick={() => assign.mutate()}
              style={{ minHeight: 44 }}
            >
              {t("exams.hand_out")}
            </button>
          </div>

          <div className="stack">
            <strong className="small">{t("exams.groups")}</strong>
            <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
              {(groups.data?.items || []).map((group) => (
                <button
                  key={group.id}
                  type="button"
                  className={`chip ${pickedGroups.includes(group.id) ? "on" : ""}`}
                  onClick={() =>
                    setPickedGroups((prev) =>
                      prev.includes(group.id) ? prev.filter((x) => x !== group.id) : [...prev, group.id]
                    )
                  }
                >
                  {group.name} · {t("exams.members_n", { n: group.member_count })}
                </button>
              ))}
              {!groups.isLoading && !(groups.data?.items || []).length ? (
                <span className="small muted">{t("exams.no_groups")}</span>
              ) : null}
            </div>
          </div>

          <div className="stack">
            <strong className="small">{t("exams.students")}</strong>
            <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
              {(students.data?.items || []).map((student) => {
                const already = covered.has(student.id);
                return (
                  <button
                    key={student.id}
                    type="button"
                    className={`chip ${picked.includes(student.id) ? "on" : ""}`}
                    disabled={already}
                    onClick={() =>
                      setPicked((prev) =>
                        prev.includes(student.id) ? prev.filter((x) => x !== student.id) : [...prev, student.id]
                      )
                    }
                  >
                    {already ? "✓ " : ""}
                    {student.name} {student.surname}
                  </button>
                );
              })}
              {!students.isLoading && !(students.data?.items || []).length ? (
                <span className="small muted">{t("exams.no_students")}</span>
              ) : null}
            </div>
            </div>
          </>
        )}
      </div>

      <div className="card stack">
        <h2 style={{ margin: 0 }}>{t("exams.who_has_it")}</h2>
        {exam.assignments.length === 0 ? <div className="small muted">{t("exams.not_assigned")}</div> : null}
        {exam.assignments.map((assignment) => (
          <div className="row" key={assignment.id} style={{ gap: 8, alignItems: "center", borderBottom: "1px solid var(--border)" }}>
            <div style={{ flex: 1 }}>
              <div className="small">
                {assignment.name || assignment.id.slice(0, 8)}
                <span className="muted"> · {t(`exams.kind_${assignment.kind}`)}</span>
              </div>
              <div className="small muted">
                {/* The same shape the paper list uses: "0 of 0 handed in" is true but reads like a
                    broken count, while what the teacher means is that nobody has sat it yet. */}
                {assignment.attempts
                  ? t("exams.sat_n", { sat: assignment.submitted, total: assignment.attempts })
                  : t("exams.not_sat")}
                {!assignment.reachable ? ` · ${t("exams.no_longer_reachable")}` : ""}
              </div>
            </div>
            <button type="button" className="btn ghost" onClick={() => unassign.mutate(assignment.id)}>
              {t("exams.take_back")}
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}

function SittingsCard({ examId }: { examId: string }) {
  const { t } = useTranslation();
  const [status, setStatus] = useState("");
  const [page, setPage] = useState(1);
  // Sitting states come from `/grading/meta`, not from the paper's lifecycle: a run is
  // `in_progress` or `expired` while the exam above it is `active` or `finished`.
  const grading = useQuery({ queryKey: ["grading-meta"], queryFn: gradingApi.meta });

  const attempts = useQuery({
    queryKey: ["exam-attempts", examId, status, page],
    queryFn: () => examsApi.attempts(examId, { status, page, page_size: 25 }),
  });

  const pages = Math.max(1, Math.ceil((attempts.data?.total ?? 0) / 25));

  return (
    <div className="card stack">
      <h2 style={{ margin: 0 }}>{t("exams.sittings_title")}</h2>
      <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
        <select className="input" style={{ maxWidth: 170 }} value={status} onChange={(e) => { setStatus(e.target.value); setPage(1); }}>
          <option value="">{t("exams.any_sitting_status")}</option>
          {(grading.data?.statuses || []).map((each) => (
            <option key={each} value={each}>
              {t(`status.${each}`)}
            </option>
          ))}
        </select>
        <span className="spacer" />
        <Link className="btn secondary" to={`/exams/grading?exam_id=${examId}`}>
          {t("exams.open_grading")}
        </Link>
      </div>
      {attempts.isError ? <div className="alert error">{(attempts.error as ApiError).message}</div> : null}
      <table>
        <thead>
          <tr>
            <th>{t("exams.learner")}</th>
            <th>{t("exams.sitting_n")}</th>
            <th>{t("exams.status")}</th>
            <th>{t("exams.progress")}</th>
            <th>{t("exams.mark")}</th>
            <th>{t("exams.clock")}</th>
            <th>{t("common.actions")}</th>
          </tr>
        </thead>
        <tbody>
          {(attempts.data?.items || []).map((row: AttemptRow) => (
            <tr key={row.id}>
              <td className="small">{row.student_name || row.student_id.slice(0, 8)}</td>
              <td className="small">{row.attempt_number}</td>
              <td className="small">{t(`status.${row.status}`)}</td>
              <td className="small">{t("exams.answered_n", { done: row.answered_items, total: row.total_items })}</td>
              <td className="small">
                {row.max_score ? t("exams.score_of", { got: row.score ?? 0, total: row.max_score }) : "—"}
                {row.needs_review ? <div className="muted">{t("exams.waiting_n", { n: row.needs_review })}</div> : null}
              </td>
              <td className="small muted">
                {row.status === "in_progress" && row.remaining_seconds !== null
                  ? t("exams.left_n", { time: span(row.remaining_seconds) })
                  : row.server_seconds_used !== null
                    ? t("exams.used_n", { time: span(row.server_seconds_used) })
                    : "—"}
                {row.tab_switches ? <div>{t("exams.switches_n", { n: row.tab_switches })}</div> : null}
              </td>
              <td>
                <Link className="btn ghost" to={`/exams/grading?attempt=${row.id}`}>
                  {t("exams.look")}
                </Link>
              </td>
            </tr>
          ))}
          {!attempts.isLoading && !(attempts.data?.items || []).length ? (
            <tr>
              <td colSpan={7} className="muted small">
                {t("exams.no_sittings")}
              </td>
            </tr>
          ) : null}
        </tbody>
      </table>
      <div className="row">
        <span className="small muted">{t("exams.showing", { total: attempts.data?.total ?? 0 })}</span>
        <span className="spacer" />
        <button type="button" className="btn secondary" disabled={page <= 1} onClick={() => setPage(page - 1)}>
          ‹
        </button>
        <span className="small">
          {page} / {pages}
        </span>
        <button type="button" className="btn secondary" disabled={page >= pages} onClick={() => setPage(page + 1)}>
          ›
        </button>
      </div>
    </div>
  );
}

function PreviewCard({
  examId,
  seed,
  onSeed,
}: {
  examId: string;
  seed: string | undefined;
  onSeed: (seed: string | undefined) => void;
}) {
  const { t } = useTranslation();
  const preview = useQuery({
    queryKey: ["exam-preview", examId, seed],
    queryFn: () => examsApi.preview(examId, seed),
  });

  return (
    <div className="card stack">
      <div className="row">
        <h2 style={{ margin: 0 }}>{t("exams.preview_title")}</h2>
        <span className="spacer" />
        <button type="button" className="btn secondary" onClick={() => onSeed(undefined)} disabled={preview.isFetching}>
          {t("exams.deal_again")}
        </button>
      </div>
      <p className="small muted" style={{ margin: 0 }}>
        {t("exams.preview_hint")}
      </p>
      {preview.isError ? <div className="alert error">{(preview.error as ApiError).message}</div> : null}
      {!preview.data ? (
        <div className="muted small">{t("common.loading")}</div>
      ) : (
        <>
          <div className="row small muted" style={{ flexWrap: "wrap", gap: 8 }}>
            <span>{t("exams.questions_n", { n: preview.data.item_count })}</span>
            <span>{t("exams.points_n", { n: preview.data.points })}</span>
            {preview.data.duration_minutes ? <span>{t("exams.minutes_n", { n: preview.data.duration_minutes })}</span> : null}
            {/* The seed is what a support conversation needs - "the order you were dealt" - but on
                its own a string of hex is nothing a teacher can read, so it is named. */}
            <span>
              {t("exams.deal_ref")} <span className="mono">{preview.data.seed}</span>
            </span>
          </div>
          {preview.data.skipped.length ? (
            <div className="stack">
              <strong className="small">{t("exams.skipped_title")}</strong>
              <p className="small muted" style={{ margin: 0 }}>{t("exams.skipped_hint")}</p>
              <ul className="small muted" style={{ margin: 0 }}>
                {preview.data.skipped.map((entry) => (
                  <li key={entry.exam_item_id}>
                    {entry.title || t("exams.no_prompt")} — {t(`exams.state_${entry.reason}`)}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          {preview.data.items.map((item) => (
            <div className="row small" key={item.exam_item_id} style={{ gap: 8, borderBottom: "1px solid var(--border)" }}>
              <span className="muted" style={{ minWidth: 20 }}>
                {item.position + 1}
              </span>
              <span style={{ flex: 1 }}>
                {item.title || t("exams.no_prompt")}
                {item.section_title ? <span className="muted"> · {item.section_title}</span> : null}
                {item.context_title ? <span className="muted"> · {item.context_title}</span> : null}
              </span>
              {item.shuffled_options ? (
                <span className="muted">{t("exams.option_order", { order: item.shuffled_options.join(" ") })}</span>
              ) : null}
              <span className="muted">v{item.version ?? "?"}</span>
              <span>{t("exams.points_n", { n: item.points })}</span>
            </div>
          ))}
        </>
      )}
    </div>
  );
}
