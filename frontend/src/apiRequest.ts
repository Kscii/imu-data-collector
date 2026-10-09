/** JSON requests share one deadline for headers AND body consumption. */
export class ApiRequestError extends Error {
  readonly status: number;
  readonly detail: unknown;
  readonly statusText: string;

  constructor(status: number, statusText: string, detail?: unknown) {
    super(statusText || `HTTP ${status}`);
    this.name = "ApiRequestError";
    this.status = status;
    this.statusText = statusText;
    this.detail = detail;
  }
}

export class RequestTimeoutError extends Error {
  readonly timeoutMs: number;

  constructor(timeoutMs: number) {
    super(`Request exceeded ${timeoutMs} ms`);
    this.name = "RequestTimeoutError";
    this.timeoutMs = timeoutMs;
  }
}

export class NetworkRequestError extends Error {
  constructor(cause: unknown) {
    super("The connection was interrupted", { cause });
    this.name = "NetworkRequestError";
  }
}

export async function requestJson<T>(path: string, init?: RequestInit, timeoutMs = 45_000): Promise<T> {
  const controller = new AbortController();
  let reason: "timeout" | "caller" | undefined;
  const abort = (next: "timeout" | "caller") => {
    if (controller.signal.aborted) return;
    reason = next;
    controller.abort();
  };
  const fromCaller = () => abort("caller");
  init?.signal?.addEventListener("abort", fromCaller, { once: true });
  if (init?.signal?.aborted) fromCaller();
  const timer = setTimeout(() => abort("timeout"), timeoutMs);
  try {
    controller.signal.throwIfAborted();
    const headers = new Headers(init?.headers);
    if (!headers.has("Content-Type")) headers.set("Content-Type", "application/json");
    const response = await fetch(path, { ...init, headers, signal: controller.signal });
    if (!response.ok) {
      let detail: unknown;
      try {
        detail = (await response.json()).detail;
      } catch {
        // Non-JSON error pages are valid HTTP errors; cancellation must propagate.
        controller.signal.throwIfAborted();
      }
      throw new ApiRequestError(response.status, response.statusText, detail);
    }
    return await response.json() as T;
  } catch (error) {
    if (reason === "timeout") throw new RequestTimeoutError(timeoutMs);
    if (reason === "caller") throw new DOMException("Request cancelled", "AbortError");
    if (error instanceof TypeError) throw new NetworkRequestError(error);
    throw error;
  } finally {
    clearTimeout(timer);
    init?.signal?.removeEventListener("abort", fromCaller);
  }
}
