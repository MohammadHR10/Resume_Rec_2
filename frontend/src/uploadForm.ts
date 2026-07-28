/** Resume dropzone: PDFs and ZIP archives, with a removable staged list. */

import { el, escapeHtml, wireDropzone } from "./ui.ts";

export class UploadForm {
  private files: File[] = [];
  private readonly list: HTMLElement;
  private readonly onChange?: (files: File[]) => void;

  constructor(
    zone: HTMLElement,
    input: HTMLInputElement,
    list: HTMLElement,
    onChange?: (files: File[]) => void,
  ) {
    this.list = list;
    this.onChange = onChange;
    wireDropzone(zone, input, (incoming) => this.add(incoming));
  }

  private accepted(file: File): boolean {
    const name = file.name.toLowerCase();
    return name.endsWith(".pdf") || name.endsWith(".zip");
  }

  add(incoming: File[]): void {
    for (const file of incoming) {
      if (this.accepted(file) && !this.files.some((existing) => existing.name === file.name)) {
        this.files.push(file);
      }
    }
    this.render();
  }

  private render(): void {
    this.list.innerHTML = "";
    for (const file of this.files) {
      const isZip = file.name.toLowerCase().endsWith(".zip");
      const item = el(
        "li",
        "list-group-item d-flex justify-content-between align-items-center py-1",
        `<span>${isZip ? '<span class="badge text-bg-secondary me-2">ZIP</span>' : ""}${escapeHtml(file.name)}
           <small class="text-muted">(${(file.size / 1024).toFixed(0)} KB)</small></span>
         <button class="btn btn-sm btn-outline-danger border-0">&times;</button>`,
      );
      item.querySelector("button")!.addEventListener("click", () => {
        this.files = this.files.filter((candidate) => candidate !== file);
        this.render();
      });
      this.list.appendChild(item);
    }
    this.onChange?.(this.getFiles());
  }

  getFiles(): File[] {
    return [...this.files];
  }

  clear(): void {
    this.files = [];
    this.render();
  }
}
