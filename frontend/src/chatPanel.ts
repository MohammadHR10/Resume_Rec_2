/** Per-stage chat panel. Mode-agnostic: it renders answers and applies grid
 *  actions the same way whether a CLI harness or the structured loop produced
 *  them. The only visible difference is the badge showing which ran, and a note
 *  when a harness turn degraded.
 */

import DOMPurify from "dompurify";
import { marked } from "marked";

import { askChat, getChat } from "./api.ts";
import type { GridAction, Stage } from "./types.ts";
import { el, escapeHtml, formatDate, notify } from "./ui.ts";

marked.setOptions({ gfm: true, breaks: true });

/** Render an assistant answer as HTML.
 *
 * The model replies in markdown — headings, tables, bold — and rendering it as
 * plain text made good answers hard to read. It is sanitised rather than
 * trusted: resume text reaches the model as input and comes back inside these
 * answers, so a candidate could otherwise put markup in their CV and have it
 * executed in a reviewer's browser.
 */
function renderMarkdown(text: string): string {
  return DOMPurify.sanitize(marked.parse(text ?? "", { async: false }) as string, {
    USE_PROFILES: { html: true },
  });
}

const MAX_ERROR_CHARS = 300;

/** Keep an error readable: one short paragraph, never a JSON dump. */
function summarizeError(message: string): string {
  const cleaned = (message || "Something went wrong.").trim();
  // If a payload leaked through anyway, cut it at the first brace.
  const brace = cleaned.indexOf("{");
  const prose = brace > 20 ? cleaned.slice(0, brace).trim() : cleaned;
  return prose.length > MAX_ERROR_CHARS ? `${prose.slice(0, MAX_ERROR_CHARS)}…` : prose;
}

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
  /** Told when the user collapses or pops the panel out. */
  private readonly onCollapse?: (collapsed: boolean) => void;
  /** True in the pop-out window, where collapsing and popping out again make no sense. */
  private readonly standalone: boolean;

  constructor(
    root: HTMLElement,
    screeningId: string,
    stage: Stage,
    onGridAction: (action: GridAction) => void,
    options: { onCollapse?: (collapsed: boolean) => void; standalone?: boolean } = {},
  ) {
    this.root = root;
    this.screeningId = screeningId;
    this.stage = stage;
    this.onGridAction = onGridAction;
    this.onCollapse = options.onCollapse;
    this.standalone = options.standalone ?? false;
    this.build();
  }

  private build(): void {
    this.root.innerHTML = "";
    const card = el("div", "card h-100");

    const header = el("div", "card-header d-flex justify-content-between align-items-center gap-2");
    header.appendChild(el("span", "fw-semibold me-auto", "Ask about this stage"));
    this.modeBadge = el("span", "badge text-bg-light border", "…");
    header.appendChild(this.modeBadge);

    // A separate window keeps the chat visible on a second monitor while the
    // grid takes the whole page. Grid actions still arrive: they travel
    // through the backend's action queue, which the grid polls regardless of
    // which window asked the question.
    const popOut = el("button", "btn btn-sm btn-outline-secondary border-0", "⧉") as HTMLButtonElement;
    popOut.title = "Open the chat in its own window";
    popOut.addEventListener("click", () => {
      window.open(
        `${location.pathname}?popout=1#/chat/${this.screeningId}/${this.stage}`,
        `chat-${this.screeningId}-${this.stage}`,
        "width=520,height=760,menubar=no,toolbar=no",
      );
      this.onCollapse?.(true);
    });

    const collapse = el("button", "btn btn-sm btn-outline-secondary border-0", "→") as HTMLButtonElement;
    collapse.title = "Collapse the chat and give the grid the full width";
    collapse.addEventListener("click", () => this.onCollapse?.(true));

    if (!this.standalone) header.append(popOut, collapse);
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

  /** Loads the transcript; returns the session id so the caller can follow its
   *  action queue even when the chat lives in another window. */
  async load(): Promise<string | null> {
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
      return payload.sessionId;
    } catch (error) {
      notify(`Could not open the chat: ${(error as Error).message}`, "danger");
      return null;
    }
  }

  private setMode(mode: string, model: string): void {
    this.modeBadge.textContent = `${mode === "harness" ? "Independent" : "Guided"} · ${model || "no model"}`;
    this.modeBadge.title =
      mode === "harness"
        ? "Independent — the assistant explores the screening data itself."
        : "Guided — the app runs a fixed set of analyses on the assistant's behalf.";
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
    if (role === "user" || mode === "error") {
      // The user's own words and error text are shown verbatim; only the
      // model's answers are markdown.
      body.textContent = content;
    } else {
      body.classList.add("chat-markdown");
      body.innerHTML = renderMarkdown(content);
    }
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
            `Independent mode was unavailable, so this was answered in Guided mode (${escapeHtml(
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
      // Belt and braces: the backend now sends a written explanation rather
      // than a raw payload, but a chat window must never become a dump of
      // whatever a model happened to emit.
      this.append("assistant", summarizeError((error as Error).message), "error");
    } finally {
      this.sendButton.disabled = false;
      this.log.scrollTop = this.log.scrollHeight;
    }
  }
}
