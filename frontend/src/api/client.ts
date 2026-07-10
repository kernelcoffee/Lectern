// Thin fetch wrapper for the JSON API. Same-origin "/api" paths are proxied to
// the backend by Vite in dev (see vite.config.ts).

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function handle<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      // non-JSON error body; keep the status message
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export function apiGet<T>(path: string): Promise<T> {
  return fetch(path).then((r) => handle<T>(r));
}

export function apiPost<T>(path: string, body: unknown): Promise<T> {
  return fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then((r) => handle<T>(r));
}

export function apiPatch<T>(path: string, body: unknown): Promise<T> {
  return fetch(path, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then((r) => handle<T>(r));
}

export function apiPut<T>(path: string, body: unknown): Promise<T> {
  return fetch(path, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then((r) => handle<T>(r));
}

export function apiDelete(path: string): Promise<void> {
  return fetch(path, { method: "DELETE" }).then((r) => handle<void>(r));
}

/** DELETE that returns a JSON body (some endpoints reply with the new state). */
export function apiDeleteJson<T>(path: string): Promise<T> {
  return fetch(path, { method: "DELETE" }).then((r) => handle<T>(r));
}

/** POST multipart form data (file uploads). Content-Type is set by the browser
 *  so the multipart boundary is correct — don't set it manually. */
export function apiPostForm<T>(path: string, form: FormData): Promise<T> {
  return fetch(path, { method: "POST", body: form }).then((r) => handle<T>(r));
}

/**
 * POST multipart form data with upload-progress reporting. Uses XMLHttpRequest
 * because fetch() can't observe request-body upload progress. `onProgress` gets
 * a 0–1 fraction while the body uploads; after it reaches 1 the server is still
 * processing (e.g. extracting a multi-GB world) until the promise resolves.
 */
export function apiUpload<T>(
  path: string,
  form: FormData,
  onProgress?: (fraction: number) => void,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", path);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      const ok = xhr.status >= 200 && xhr.status < 300;
      let body: unknown;
      try {
        body = xhr.responseText ? JSON.parse(xhr.responseText) : undefined;
      } catch {
        body = undefined;
      }
      if (ok) {
        resolve(body as T);
      } else {
        const detail = (body as { detail?: unknown })?.detail;
        reject(
          new ApiError(
            xhr.status,
            typeof detail === "string"
              ? detail
              : detail
                ? JSON.stringify(detail)
                : `HTTP ${xhr.status}`,
          ),
        );
      }
    };
    xhr.onerror = () => reject(new ApiError(0, "Network error during upload"));
    xhr.send(form);
  });
}
