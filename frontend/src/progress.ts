/** Job progress log driven by the polling subscription in api.ts. */

import { subscribeToJob } from "./api.ts";
import { el } from "./ui.ts";

export interface ProgressHandle {
  stop: () => void;
}

export function showProgress(
  container: HTMLElement,
  jobId: string,
  options: { onDone?: () => void; onError?: (message: string) => void } = {},
): ProgressHandle {
  container.classList.remove("d-none");
  container.innerHTML = "";
  const log = el("div", "progress-log border rounded bg-body-tertiary p-2 small");
  container.appendChild(log);

  function append(text: string, className = ""): void {
    const line = el("div", className);
    line.textContent = text;
    log.appendChild(line);
    log.scrollTop = log.scrollHeight;
  }

  const stop = subscribeToJob(
    jobId,
    (message) => append(message),
    () => {
      append("Complete.", "text-success fw-semibold");
      options.onDone?.();
    },
    (message) => {
      append(`Error: ${message}`, "text-danger fw-semibold");
      options.onError?.(message);
    },
  );

  return { stop };
}
