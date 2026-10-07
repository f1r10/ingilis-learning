// Thin fetch client for the backend REST API (v1).
// Cookies carry the session; the CSRF token is read from the readable
// `llp_csrf` cookie and echoed back on state-changing requests (double-submit).

const API_BASE = (import.meta.env.VITE_API_BASE_URL as string) || "/api/v1";

/** Turn a served `content_url` into something an `<img>` or `<audio>` can request. The
 * backend hands out a path and never a host, so an object-store URL cannot be followed. */
export const mediaUrl = (contentUrl: string | null | undefined) =>
  contentUrl ? `${API_BASE}${contentUrl}` : null;

const CSRF_COOKIE = "llp_csrf";
const CSRF_HEADER = "X-CSRF-Token";

function readCookie(name: string): string | undefined {
  const match = document.cookie
    .split("; ")
    .find((row) => row.startsWith(`${name}=`));
  return match ? decodeURIComponent(match.split("=")[1]) : undefined;
}

export class ApiError extends Error {
  code: string;
  status: number;
  /** Field paths the server named in a 422, so the teacher learns which box to fix. */
  fields: string[];
  constructor(status: number, code: string, message: string, fields: string[] = []) {
    super(message);
    this.status = status;
    this.code = code;
    this.fields = fields;
  }
}

async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const method = (options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers);
  if (method !== "GET" && method !== "HEAD") {
    // A multipart body brings its own boundary; typing a content type over it would
    // leave the server unable to find the file.
    if (!(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
    const csrf = readCookie(CSRF_COOKIE);
    if (csrf) headers.set(CSRF_HEADER, csrf);
  }

  const resp = await fetch(`${API_BASE}${path}`, {
    credentials: "include",
    ...options,
    headers,
  });

  if (resp.status === 204) return undefined as T;

  const text = await resp.text();
  const data = text ? JSON.parse(text) : null;

  if (!resp.ok) {
    const err = data?.error || {};
    const fields = fieldNames(err.fields);
    // The server already summarises each field's reason in `message`, so the paths are
    // only worth printing when there was no reason to print.
    const suffix = fields.length && !fieldReasons(err.fields).length ? ` (${fields.join(", ")})` : "";
    throw new ApiError(resp.status, err.code || "error", `${err.message || resp.statusText}${suffix}`, fields);
  }
  return data as T;
}

/** `["body","translations",0,"id"]` becomes `translations.id`: the part a teacher can act on. */
function fieldNames(raw: unknown): string[] {
  if (!Array.isArray(raw)) return [];
  const names = raw.map((item) => {
    const loc = (item as any)?.loc;
    if (!Array.isArray(loc)) return "";
    return loc
      .filter((part) => part !== "body" && !/^\d+$/.test(String(part)))
      .join(".");
  });
  return Array.from(new Set(names.filter(Boolean)));
}

/** The reasons a 422 carries per field; empty when the failure was not a body refusal. */
function fieldReasons(raw: unknown): string[] {
  if (!Array.isArray(raw)) return [];
  const msgs = raw.map((item) => (typeof (item as any)?.msg === "string" ? (item as any).msg.trim() : ""));
  return Array.from(new Set(msgs.filter(Boolean)));
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body: body ? JSON.stringify(body) : undefined }),
  /** Upload a file: the browser sets the multipart boundary itself. */
  postForm: <T>(path: string, form: FormData) => request<T>(path, { method: "POST", body: form }),
  put: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "PUT", body: body ? JSON.stringify(body) : undefined }),
  patch: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "PATCH", body: body ? JSON.stringify(body) : undefined }),
  del: <T>(path: string) => request<T>(path, { method: "DELETE" }),
};

export interface Branding {
  system_name: string;
  short_name: string;
  logo_url?: string | null;
  favicon_url?: string | null;
  login_image_url?: string | null;
  login_title?: string | null;
  welcome_message?: string | null;
  login_instructions?: string | null;
  footer?: string | null;
  support_text?: string | null;
  accent_color?: string | null;
}

export interface UiConfig {
  enabled_ui_languages: string[];
  default_ui_language: string;
  learning_languages: string[];
  translation_languages: string[];
}

export interface Bootstrap {
  branding: Branding;
  ui: UiConfig;
}
