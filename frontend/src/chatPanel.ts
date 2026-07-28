/** Per-stage chat panel. Mode-agnostic: it renders answers and applies grid
 *  actions the same way whether a CLI harness or the structured loop produced
 *  them. The only visible difference is the badge showing which ran, and a note
 *  when a harness turn degraded.
 */

import { askChat, getChat } from "./api.ts";
import type { GridAction, Stage } from "./types.ts";
import { el, escapeHtml, formatDate, notify } from "./ui.ts";

const SUGGESTIONS = [
  "Why is the top candidate ranked above the second?",
  "Why did so many candidates make it past this stage?",
  "What happens to the list if we drop the degree requirement?",
  "Sort by preferred coverage, highest first.",
];

export class ChatPanel {
  private log!: HTMLElement;
  private input!: HTMLTextAreaElement;
  private sendButton!: HTMLButtonElement;
  private modeBadge!: HTMLElement;
  private readonly root: HTMLElement;
  private readonly screeningId: string;
  private readonly stage: Stage;
  private readonly onGridAction: (action: GridAction) => void;

  constructor(
    root: HTMLElement,
    screeningId: string,
    stage: Stage,
    onGridAction: (action: GridAction) => void,
  ) {
    this.root = root;
    this.screeningId = screeningId;
    this.stage = stage;
    this.onGridAction = onGridAction;
    this.build();
  }

  private build(): void {
    this.root.innerHTML = "";
    const card = el("div", "card h-100");

    const header = el("div", "card-header d-flex justify-content-between align-items-center");
    header.appendChild(el("span", "fw-semibold", "Ask about this stage"));
    this.modeBadge = el("span", "badge text-bg-light border", "…");
    header.appendChild(this.modeBadge);
    card.appendChild(header);

    this.log = el("div", "card-body chat-log");
    card.appendChild(this.log);

    const footer = el("div", "card-footer");
    const suggestions = el("div", "d-flex flex-wrap gap-1 mb-2");
    for (const text of SUGGESTIONS) {
      const chip = el("button", "btn btn-sm btn-outline-secondary chat-suggestion", escapeHtml(text));
      chip.addEventListener("click", () => {
        this.input.value = text;
        this.input.focus();
      });
      suggestions.appendChild(chip);
    }
    footer.appendChild(suggestions);

    const group = el("div", "input-group");
    this.input = el("textarea", "form-control") as HTMLTextAreaElement;
    this.input.rows = 2;
    this.input.placeholder = "Ask anything about these candidates…";
    this.input.addEventListener("keydown", (event) => {
      const keyboard = event as KeyboardEvent;
      if (keyboard.key === "Enter" && !keyboard.shiftKey) {
        keyboard.preventDefault();
        void this.send();
      }
    });
    this.sendButton = el("button", "btn btn-primary", "Ask") as HTMLButtonElement;
    this.sendButton.addEventListener("click", () => void this.send());
    group.append(this.input, this.sendButton);
    footer.appendChild(group);

    card.appendChild(footer);
    this.root.appendChild(card);
  }

  async load(): Promise<void> {
    try {
      const payload = await getChat(this.screeningId, this.stage);
      this.setMode(payload.mode, payload.model);
      this.log.innerHTML = "";
      if (payload.messages.length === 0) {
        this.log.appendChild(
          el(
            "p",
            "text-muted small fst-italic",
            "Ask why someone ranks where they do, why a stage let so many through, or what happens if a qualification is dropped.",
          ),
        );
      }
      for (const message of payload.messages) {
        this.append(message.role, message.content, message.mode, message.created_at);
      }
    } catch (error) {
      notify(`Could not open the chat: ${(error as Error).message}`, "danger");
    }
  }

  private setMode(mode: string, model: string): void {
    const label = mode === "harness" ? "harness" : "structured";
    this.modeBadge.textContent = `${label} · ${model || "no model"}`;
    this.modeBadge.title =
      mode === "harness"
        ? "A CLI harness drives the analysis tools directly."
        : "The backend runs the analysis tools on the model's behalf.";
  }

  private append(role: string, content: string, mode = "", timestamp = ""): HTMLElement {
    const bubble = el("div", `chat-message chat-${role === "user" ? "user" : "assistant"}`);
    const meta = el(
      "div",
      "chat-meta",
      `${role === "user" ? "You" : "Assistant"}${mode ? ` · ${escapeHtml(mode)}` : ""}${
        timestamp ? ` · ${escapeHtml(formatDate(timestamp))}` : ""
      }`,
    );
    bubble.appendChild(meta);
    const body = el("div", "chat-body");
    body.textContent = content;
    bubble.appendChild(body);
    this.log.appendChild(bubble);
    this.log.scrollTop = this.log.scrollHeight;
    return bubble;
  }

  private async send(): Promise<void> {
    const question = this.input.value.trim();
    if (!question) return;
    this.input.value = "";
    this.append("user", question);

    const thinking = this.append("assistant", "Thinking…");
    this.sendButton.disabled = true;
    try {
      const turn = await askChat(this.screeningId, this.stage, question);
      thinking.remove();
      const bubble = this.append("assistant", turn.answer, turn.mode);

      if (turn.degradedFrom) {
        bubble.appendChild(
          el(
            "div",
            "chat-note text-warning-emphasis",
            `Harness unavailable, answered with the structured loop instead (${escapeHtml(
              turn.degradedFrom,
            )}).`,
          ),
        );
      }
      if (turn.trace?.length) {
        bubble.appendChild(
          el(
            "div",
            "chat-note text-muted",
            `Ran: ${turn.trace.map((step) => escapeHtml(step.tool)).join(", ")}`,
          ),
        );
      }
      for (const action of turn.actions ?? []) {
        this.onGridAction(action);
      }
      if (turn.actions?.length) {
        bubble.appendChild(el("div", "chat-note text-muted", "Applied to the grid."));
      }
      this.setMode(turn.mode, turn.model);
    } catch (error) {
      thinking.remove();
      this.append("assistant", `I could not answer that: ${(error as Error).message}`, "error");
    } finally {
      this.sendButton.disabled = false;
      this.log.scrollTop = this.log.scrollHeight;
    }
  }
}
