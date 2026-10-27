/** Per-stage chat panel. Mode-agnostic: it renders answers and applies grid
 *  actions the same way whether a CLI harness or the structured loop produced
 *  them. The only visible difference is the badge showing which ran, and a note
 *  when a harness turn degraded.
 */

import DOMPurify from "dompurify";
import { marked } from "marked";

import { askChat, getChat, consumeAction, initializeActionCursor, resolveProposal, exportUrl, parseJobDescription, uploadResumes } from "./api.ts";
import { showProgress } from "./progress.ts";
import type { ChatProposal, GridAction, Stage } from "./types.ts";
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
  "What does the top candidate’s resume say about their experience?",
  "Compare the top candidates against required and preferred qualifications.",
  "Group this stage’s candidates by recommendation and explain what they missed.",
  "What happens to the list if we drop the degree requirement?",
];

export class ChatPanel {
  private log!: HTMLElement;
  private input!: HTMLTextAreaElement;
  private sendButton!: HTMLButtonElement;
  private modeBadge!: HTMLElement;
  private proposalsHost!: HTMLElement;
  private sessionId = "";
  private readonly root: HTMLElement;
  private readonly screeningId: string;
  private readonly stage: Stage;
  private readonly onGridAction: (action: GridAction) => void | Promise<void>;
  /** Told when the user collapses or pops the panel out. */
  private readonly onCollapse?: (collapsed: boolean) => void;
  /** True in the pop-out window, where collapsing and popping out again make no sense. */
  private readonly standalone: boolean;

  constructor(
    root: HTMLElement,
    screeningId: string,
    stage: Stage,
    onGridAction: (action: GridAction) => void | Promise<void>,
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
    header.appendChild(el("span", "fw-semibold me-auto", "Screening assistant"));
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
    this.proposalsHost = el("div", "px-3");
    card.appendChild(this.proposalsHost);

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
    this.input.placeholder = "Ask about candidates or control the app…";
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
      this.sessionId = payload.sessionId;
      initializeActionCursor(payload.sessionId, payload.actionCursor);
      this.renderProposals(payload.proposals);
      this.setMode(payload.mode, payload.model);
      this.log.innerHTML = "";
      if (payload.messages.length === 0) {
        this.log.appendChild(
          el(
            "p",
            "text-muted small fst-italic",
            "Ask about resume details, verdict explanations, candidate comparisons, or any group of applicants.",
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

  private setMode(_mode: string, model: string): void {
    this.modeBadge.textContent = `App tools · ${model || "no model"}`;
    this.modeBadge.title = "The assistant uses app controls. Review proposed changes before applying them.";
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
    if (this.sendButton.disabled) return;
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
      this.renderProposals(turn.proposals);
      for (const action of turn.actions ?? []) {
        await consumeAction(this.sessionId, action, (a) => this.handleAction(a));
      }
      this.setMode(turn.mode, turn.model);
    } catch (error) {
      thinking.remove();
      // Belt and braces: the backend now sends a written explanation rather
      // than a raw payload, but a chat window must never become a dump of
      // whatever a model happened to emit.
      this.append("assistant", summarizeError((error as Error).message), "error");
      const current = await getChat(this.screeningId, this.stage).catch(() => null);
      if (current) this.renderProposals(current.proposals);
    } finally {
      this.sendButton.disabled = false;
      this.log.scrollTop = this.log.scrollHeight;
    }
  }

  private renderProposals(proposals: ChatProposal[]): void {
    this.proposalsHost.innerHTML = "";
    for (const proposal of proposals ?? []) {
      if (proposal.status !== "pending") continue;
      const card = el("div", "border rounded p-2 mb-2 bg-light");
      card.appendChild(el("strong", "d-block", escapeHtml(proposal.label)));
      const details = el("div", "small my-2");
      for (const [key, value] of Object.entries(proposal.args)) {
        if (key === "candidateIds" || value === null || value === "") continue;
        const text = key === "qualifications"
          ? (value as { kind: string; text: string }[]).map(q => `${q.kind}: ${q.text}`).join("\n")
          : `${key}: ${String(value)}`;
        const line = el("div");
        line.style.whiteSpace = "pre-wrap";
        line.textContent = text;
        details.appendChild(line);
      }
      card.appendChild(details);
      card.appendChild(el("div", "small text-muted mb-2", "Awaiting your review. No change has been made."));
      const apply = el("button", "btn btn-sm btn-primary me-2", "Apply change") as HTMLButtonElement;
      const cancel = el("button", "btn btn-sm btn-outline-secondary", "Cancel") as HTMLButtonElement;
      const resolve = async (approve: boolean) => {
        apply.disabled = cancel.disabled = true;
        try {
          const result = await resolveProposal(this.sessionId, proposal.id, approve);
          card.replaceChildren(el("div", "small", approve ? "Change applied." : "Change cancelled."));
          if (!approve) return;
          if (result.result?.jobId) {
            const progress = el("div");
            card.appendChild(progress);
            showProgress(progress, result.result.jobId, {
              onDone: () => window.dispatchEvent(new HashChangeEvent("hashchange")),
            });
          } else if (proposal.tool === "delete_screening") {
            try { localStorage.setItem("screening-uploaded", JSON.stringify({ screeningId: this.screeningId, deleted: true, at: Date.now() })); } catch { /* Storage unavailable. */ }
            window.location.hash = "#/";
          } else if (result.result?.screeningId || (proposal.tool === "create_screening" && result.result?.id)) {
            window.location.hash = `#/screening/${result.result.screeningId || result.result.id}`;
          } else {
            window.dispatchEvent(new HashChangeEvent("hashchange"));
          }
        } catch (error) {
          card.appendChild(el("div", "text-danger small mt-2", escapeHtml((error as Error).message)));
        }
      };
      apply.addEventListener("click", () => void resolve(true));
      cancel.addEventListener("click", () => void resolve(false));
      card.append(apply, cancel);
      this.proposalsHost.appendChild(card);
    }
  }

  async handleAction(action: GridAction): Promise<void> {
    const args = action.args ?? {};
    if (action.tool === "export_stage") {
      const link = el("a", "btn btn-sm btn-outline-success my-2", `Download stage ${args.stage} ${args.format?.toUpperCase()}`) as HTMLAnchorElement;
      link.href = exportUrl(this.screeningId, args.stage!, args.format!);
      this.log.appendChild(link);
    } else if (action.tool === "upload_documents") {
      const container = el("div", "border rounded p-2 my-2");
      const jd = args.kind === "position_description";
      container.appendChild(el("p", "small", jd
        ? "Choose a position description to parse. This replaces the qualification checklist and clears evaluations."
        : "Choose resumes to upload. This replaces the current candidate set."));
      const input = el("input", "form-control form-control-sm") as HTMLInputElement;
      input.type = "file";
      input.accept = jd ? ".pdf,.docx,.txt,.md" : ".pdf,.zip";
      input.multiple = !jd;
      input.setAttribute("aria-label", jd ? "Choose position description" : "Choose resumes");
      const upload = el("button", "btn btn-sm btn-primary mt-2", jd ? "Parse selected document" : "Upload selected resumes") as HTMLButtonElement;
      upload.addEventListener("click", async () => {
        if (!input.files?.length) return;
        upload.disabled = true;
        try {
          if (jd) await parseJobDescription(this.screeningId, input.files[0]!);
          else await uploadResumes(this.screeningId, Array.from(input.files));
          try { localStorage.setItem("screening-uploaded", JSON.stringify({ screeningId: this.screeningId, at: Date.now() })); } catch { /* Storage unavailable. */ }
          window.dispatchEvent(new HashChangeEvent("hashchange"));
        } catch (error) {
          notify((error as Error).message, "danger");
          upload.disabled = false;
        }
      });
      container.append(input, upload);
      this.log.appendChild(container);
    } else {
      await this.onGridAction(action);
    }
  }
}
