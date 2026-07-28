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
