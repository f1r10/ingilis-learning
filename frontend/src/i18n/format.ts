// A timestamp a screen shows is copy, not data. `new Date(x).toLocaleString()` with no arguments
// formats in the operating system's language, so a learner reading Azerbaijani would get an
// American-style deadline. The interface already knows which language it is in; dates follow it,
// and they stop at the minute because a paper never opens at 07 seconds past.
import i18n from "./index";

export function when(value: string | null | undefined): string {
  if (!value) return "";
  return new Date(value).toLocaleString(i18n.language, { dateStyle: "medium", timeStyle: "short" });
}

// A duration is copy as well. `152 s` is how a counter reads, not how a teacher describes a sitting,
// and only a screen that knows the language can pick the shape: hours for a long attempt, minutes
// and seconds for the usual one, seconds alone for the answer that took a moment.
export function span(seconds: number | null | undefined): string {
  const total = Math.max(0, Math.round(seconds ?? 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h) return i18n.t("common.duration_h_min", { h, m });
  if (m) return i18n.t("common.duration_min_s", { m, s });
  return i18n.t("common.duration_seconds", { n: s });
}

// A byte count is copy too: the unit a teacher reads it in, and the thousands separator, follow
// the interface language. Two screens show sizes - the media library and the import history - and
// a number that stops at 4 characters in one of them would be a second way to write the same fact.
export function size(bytes: number | null | undefined, locale?: string): string {
  const total = bytes ?? 0;
  const units = ["B", "KB", "MB", "GB"];
  let value = total;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${new Intl.NumberFormat(locale || i18n.language, {
    maximumFractionDigits: unit ? 1 : 0,
  }).format(value)} ${units[unit]}`;
}

// Something the server wrote about a document is copy as much as a menu label is, and the server
// only has English. A candidate's remark, a reading that ended in nothing, and a refusal all
// arrive as a code the locale families below hold the words for. Text that is not a code is what
// a person typed - a refusal reason in their own language - and goes back unchanged.
//
// A sentence with a blank in it is not a sentence: `errors.candidate_incomplete` names the fields
// it waits for, and a code stored on a card carries no values with it. A locale whose placeholders
// went unfilled is therefore not shown - the server's own words are worse to read, but they are
// true, while "This candidate still needs: {{fields}}" is neither.
export function wordsFor(code: string | null | undefined, served = ""): string {
  if (!code) return served;
  const said = (key: string) => {
    const text = String(i18n.t(key, { defaultValue: "" }));
    return text && !text.includes("{{") ? text : "";
  };
  return (
    said(`imports.note_${code}`) ||
    said(`imports.outcome_${code}`) ||
    said(`errors.${code}`) ||
    served ||
    code
  );
}
