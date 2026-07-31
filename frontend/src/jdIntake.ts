/** JD dropzone plus the two editable qualification checklists.
 *
 * The lists are the contract for everything downstream — one verdict column per
 * item, one rollup per kind — so they are fully editable before anything is
 * evaluated: reword, delete, add, reorder, and move between required and
 * preferred. Evaluation stays locked until the user confirms them.
 */

import { parseJobDescription, saveQualifications } from "./api.ts";
import type { Qualification, QualKind } from "./types.ts";
import { busy, el, escapeHtml, notify, wireDropzone } from "./ui.ts";


interface Item {
  text: string;
  kind: QualKind;
}

export class JdIntake {
  private items: Item[] = [];
  private confirmed = false;
  private dirty = false;

  private readonly root: HTMLElement;
  private readonly screeningId: string;
  private readonly onConfirmedChange: (confirmed: boolean) => void;

  constructor(
    root: HTMLElement,
    screeningId: string,
    onConfirmedChange: (confirmed: boolean) => void,
  ) {
    this.root = root;
    this.screeningId = screeningId;
    this.onConfirmedChange = onConfirmedChange;
  }

  load(qualifications: Qualification[], confirmed: boolean): void {
    this.items = qualifications.map((qual) => ({ text: qual.text, kind: qual.kind }));
    this.confirmed = confirmed;
    this.dirty = false;
    this.render();
  }

  private label(index: number): string {
    const item = this.items[index];
    const prefix = item.kind === "required" ? "R" : "P";
    const ordinal = this.items.slice(0, index + 1).filter((other) => other.kind === item.kind).length;
    return `${prefix}${ordinal}`;
  }

  private markDirty(): void {
    this.dirty = true;
    if (this.confirmed) {
      this.confirmed = false;
      this.onConfirmedChange(false);
    }
    this.render();
  }

  // -- rendering ------------------------------------------------------------

  render(): void {
    this.root.innerHTML = "";
    const row = el("div", "row g-3");
    row.appendChild(this.column("required", "Required qualifications", "A candidate must meet every one of these to pass stage 1."));
    row.appendChild(this.column("preferred", "Preferred qualifications", "Used to rank candidates in stage 2. Never gate stage 1."));
    this.root.appendChild(row);
    this.root.appendChild(this.footer());
  }

  private column(kind: QualKind, title: string, hint: string): HTMLElement {
    const wrapper = el("div", "col-lg-6");
    const card = el("div", "card h-100");
    const header = el(
      "div",
      "card-header d-flex justify-content-between align-items-center",
      `<span class="fw-semibold">${title}</span>
       <span class="badge text-bg-secondary">${this.items.filter((i) => i.kind === kind).length}</span>`,
    );
    card.appendChild(header);

    const body = el("div", "card-body");
    body.appendChild(el("p", "text-muted small", escapeHtml(hint)));

    const list = el("ul", "list-group list-group-flush qual-list");
    this.items.forEach((item, index) => {
      if (item.kind !== kind) return;
      list.appendChild(this.itemRow(item, index, kind));
    });
    if (!this.items.some((item) => item.kind === kind)) {
      list.appendChild(el("li", "list-group-item text-muted fst-italic", "None yet."));
    }
    body.appendChild(list);

    const addRow = el("div", "input-group input-group-sm mt-3");
    const input = el("input", "form-control") as HTMLInputElement;
    input.placeholder = `Add a ${kind} qualification…`;
    const addButton = el("button", "btn btn-outline-primary", "Add") as HTMLButtonElement;
    const add = () => {
      const text = input.value.trim();
      if (!text) return;
      this.items.push({ text, kind });
      input.value = "";
      this.markDirty();
    };
    addButton.addEventListener("click", add);
    input.addEventListener("keydown", (event) => {
      if ((event as KeyboardEvent).key === "Enter") add();
    });
    addRow.append(input, addButton);
    body.appendChild(addRow);

    card.appendChild(body);
    wrapper.appendChild(card);
    return wrapper;
  }

  private itemRow(item: Item, index: number, kind: QualKind): HTMLElement {
    const row = el("li", "list-group-item d-flex align-items-start gap-2 px-0");
    row.appendChild(el("span", "badge text-bg-light border mt-1", this.label(index)));

    const text = el("div", "flex-grow-1 qual-text", escapeHtml(item.text));
    text.contentEditable = "true";
    text.spellcheck = false;
    text.addEventListener("blur", () => {
      const value = (text.textContent ?? "").trim();
      if (!value) {
        this.items.splice(index, 1);
        this.markDirty();
        return;
      }
      if (value !== item.text) {
        item.text = value;
        this.markDirty();
      }
    });
    row.appendChild(text);

    const controls = el("div", "btn-group btn-group-sm");
    const siblings = this.items
      .map((other, otherIndex) => ({ other, otherIndex }))
      .filter((entry) => entry.other.kind === kind)
      .map((entry) => entry.otherIndex);
    const positionInKind = siblings.indexOf(index);

    controls.appendChild(
      this.button("↑", "Move up", positionInKind === 0, () => {
        this.swap(index, siblings[positionInKind - 1]);
      }),
    );
    controls.appendChild(
      this.button("↓", "Move down", positionInKind === siblings.length - 1, () => {
        this.swap(index, siblings[positionInKind + 1]);
      }),
    );
    controls.appendChild(
      this.button(
        kind === "required" ? "→ Preferred" : "← Required",
        "Move to the other list",
        false,
        () => {
          item.kind = kind === "required" ? "preferred" : "required";
          this.markDirty();
        },
      ),
    );
    controls.appendChild(
      this.button("✕", "Remove", false, () => {
        this.items.splice(index, 1);
        this.markDirty();
      }, "btn-outline-danger"),
    );
    row.appendChild(controls);
    return row;
  }

  private button(
    label: string,
    title: string,
    disabled: boolean,
    onClick: () => void,
    variant = "btn-outline-secondary",
  ): HTMLButtonElement {
    const button = el("button", `btn ${variant}`, label) as HTMLButtonElement;
    button.title = title;
    button.disabled = disabled;
    button.addEventListener("click", onClick);
    return button;
  }

  private swap(a: number, b: number): void {
    [this.items[a], this.items[b]] = [this.items[b], this.items[a]];
    this.markDirty();
  }

  private footer(): HTMLElement {
    const footer = el("div", "d-flex align-items-center gap-2 mt-3 flex-wrap");

    const save = el("button", "btn btn-outline-secondary", "Save draft") as HTMLButtonElement;
    save.addEventListener("click", () => void this.persist(false, save));

    const confirm = el(
      "button",
      this.confirmed ? "btn btn-success" : "btn btn-primary",
      this.confirmed ? "✓ Confirmed — evaluation unlocked" : "Confirm checklist",
    ) as HTMLButtonElement;
    confirm.disabled = this.items.length === 0;
    confirm.addEventListener("click", () => void this.persist(true, confirm));

    footer.append(save, confirm);

    if (this.dirty) {
      footer.appendChild(
        el("span", "text-warning-emphasis small", "Unsaved changes — confirm to unlock evaluation."),
      );
    } else if (!this.confirmed) {
      footer.appendChild(
        el("span", "text-muted small", "Evaluation stays locked until you confirm these lists."),
      );
    }
    return footer;
  }

  // -- persistence ----------------------------------------------------------

  private async persist(confirm: boolean, button: HTMLButtonElement): Promise<void> {
    const done = busy(button, confirm ? "Confirming…" : "Saving…");
    try {
      const payload = this.items.map((item) => ({ text: item.text, kind: item.kind }));
      const result = await saveQualifications(this.screeningId, payload, confirm);
      this.load(result.qualifications, result.confirmed);
      this.onConfirmedChange(result.confirmed);
      notify(
        confirm ? "Checklist confirmed — you can evaluate now." : "Checklist saved.",
        "success",
      );
    } catch (error) {
      notify(`Could not save the checklist: ${(error as Error).message}`, "danger");
    } finally {
      done();
    }
  }
}

/** Wire the JD dropzone; resolves with the parsed qualifications. */
export function initJdDropzone(
  zone: HTMLElement,
  input: HTMLInputElement,
  screeningId: string,
  onParsed: (qualifications: Qualification[], jobTitle: string) => void,
  statusNode: HTMLElement,
): void {
  wireDropzone(zone, input, async (files) => {
    const file = files[0];
    if (!file) return;
    statusNode.innerHTML =
      '<span class="spinner-border spinner-border-sm me-2"></span>' +
      `Reading ${escapeHtml(file.name)} and itemizing its qualifications…`;
    try {
      const parsed = await parseJobDescription(screeningId, file);
      const count = parsed.qualifications.length;
      if (count === 0) {
        statusNode.innerHTML = `<span class="text-warning-emphasis">Read <strong>${escapeHtml(
          file.name,
        )}</strong>, but found no qualifications in it.</span>`;
        notify(parsed.warning || "No qualifications were found in that document.", "warning");
      } else {
        statusNode.innerHTML = `<span class="text-success">Parsed <strong>${escapeHtml(
          file.name,
        )}</strong> into ${count} qualification${count === 1 ? "" : "s"} with ${escapeHtml(
          parsed.provider,
        )}/${escapeHtml(parsed.model)}.</span>`;
      }
      onParsed(parsed.qualifications, parsed.jobTitle);
    } catch (error) {
      statusNode.innerHTML = "";
      notify(`Could not parse that position description: ${(error as Error).message}`, "danger");
    }
  });
}
