/** Small DOM helpers shared by every view. */

export function escapeHtml(value: string): string {
  const div = document.createElement("div");
  div.textContent = value ?? "";
  return div.innerHTML;
}

export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className = "",
  html = "",
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (html) node.innerHTML = html;
  return node;
}

export function byId<T extends HTMLElement = HTMLElement>(id: string): T {
  const node = document.getElementById(id);
  if (!node) throw new Error(`missing element #${id}`);
  return node as T;
}

/** A dismissible alert, appended to the page's alert strip. */
export function notify(message: string, kind: "success" | "danger" | "info" | "warning" = "info"): void {
  const strip = document.getElementById("alert-strip");
  if (!strip) return;
  const alert = el("div", `alert alert-${kind} alert-dismissible fade show`, `
    <span>${escapeHtml(message)}</span>
    <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
  `);
  alert.querySelector("button")!.addEventListener("click", () => alert.remove());
  strip.appendChild(alert);
  if (kind === "success" || kind === "info") {
    window.setTimeout(() => alert.remove(), 6000);
  }
}

export function clearAlerts(): void {
  const strip = document.getElementById("alert-strip");
  if (strip) strip.innerHTML = "";
}

/** Turn any element into a click-or-drop file target. */
export function wireDropzone(
  zone: HTMLElement,
  input: HTMLInputElement,
  onFiles: (files: File[]) => void,
): void {
  zone.addEventListener("click", () => input.click());
  zone.addEventListener("dragover", (event) => {
    event.preventDefault();
    zone.classList.add("dropzone-active");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("dropzone-active"));
  zone.addEventListener("drop", (event) => {
    event.preventDefault();
    zone.classList.remove("dropzone-active");
    if (event.dataTransfer?.files?.length) onFiles(Array.from(event.dataTransfer.files));
  });
  input.addEventListener("change", () => {
    if (input.files?.length) onFiles(Array.from(input.files));
    input.value = "";
  });
}

/** A card whose body collapses once its step is done.
 *
 * Setup steps are each used once and then take up the screen for the rest of
 * the session, pushing the grid the reviewer actually works in below the fold.
 * Collapsing on completion — while staying one click from being reopened —
 * keeps the finished steps as a summary rather than as a form.
 */
export class CollapsibleCard {
  readonly card: HTMLElement;
  readonly body: HTMLElement;
  private readonly heading: HTMLElement;
  private readonly summary: HTMLElement;
  private readonly caret: HTMLElement;
  private collapsed = false;

  constructor(title: string) {
    this.card = el("div", "card mb-3");
    const header = el("div", "card-header d-flex align-items-center gap-2");
    header.style.cursor = "pointer";
    this.heading = el("span", "fw-semibold", escapeHtml(title));
    this.summary = el("span", "text-muted small");
    this.caret = el("span", "ms-auto text-muted", "▾");
    header.append(this.heading, this.summary, this.caret);
    this.body = el("div", "card-body");
    this.card.append(header, this.body);
    header.addEventListener("click", () => this.toggle());
  }

  toggle(): void {
    this.setCollapsed(!this.collapsed);
  }

  setCollapsed(collapsed: boolean): void {
    this.collapsed = collapsed;
    this.body.classList.toggle("d-none", collapsed);
    this.caret.textContent = collapsed ? "▸" : "▾";
  }

  /** Collapse and show a one-line summary of what the step produced. */
  complete(summary: string): void {
    this.summary.innerHTML = `<span class="text-success">✓</span> ${escapeHtml(summary)}
      <span class="text-muted">— click to change</span>`;
    this.setCollapsed(true);
  }

  /** Reopen and clear the summary, e.g. when the step's output was invalidated. */
  reopen(summary = ""): void {
    this.summary.innerHTML = summary ? escapeHtml(summary) : "";
    this.setCollapsed(false);
  }
}

export function busy(button: HTMLButtonElement, label: string): () => void {
  const original = button.innerHTML;
  button.disabled = true;
  button.innerHTML = `<span class="spinner-border spinner-border-sm me-2"></span>${escapeHtml(label)}`;
  return () => {
    button.disabled = false;
    button.innerHTML = original;
  };
}

export function formatDate(iso: string): string {
  if (!iso) return "";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}
