/** App shell: hash routing, the screening list, and the screening workflow. */

import "bootstrap/dist/css/bootstrap.min.css";
import "./styles.css";

import {
  auditTrail,
  createScreening,
  deleteScreening,
  getScreening,
  listScreenings,
  startEvaluation,
  uploadResumes,
} from "./api.ts";
import { renderAuditPage } from "./auditPage.ts";
import { ChatPanel } from "./chatPanel.ts";
import { renderConfigPage } from "./configPage.ts";
import { JdIntake, initJdDropzone } from "./jdIntake.ts";
import { showProgress } from "./progress.ts";
import { StageGrid } from "./stageGrid.ts";
import type { ScreeningDetail, Stage } from "./types.ts";
import { UploadForm } from "./uploadForm.ts";
import {
  busy,
  byId,
  clearAlerts,
  CollapsibleCard,
  el,
  escapeHtml,
  formatDate,
  notify,
} from "./ui.ts";

const STAGES: Stage[] = ["1", "2", "3", "rejected"];
const STAGE_TABS: Record<Stage, string> = {
  "1": "Stage 1 · Minimum Requirements",
  "2": "Stage 2 · Preferred Qualifications",
  "3": "Stage 3 · Interview Candidates",
  rejected: "Rejected",
};

const view = () => byId("view");

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------

function route(): void {
  clearAlerts();
  const hash = window.location.hash.replace(/^#\/?/, "");
  const [section, id] = hash.split("/");

  document.querySelectorAll("[data-nav]").forEach((node) => {
    const target = (node as HTMLElement).dataset.nav!;
    node.classList.toggle("active", target === (section || "screenings"));
  });

  if (section === "config") return void renderConfigPage(view());
  if (section === "audit") return void renderAuditPage(view());
  if (section === "screening" && id) return void renderScreening(view(), id);
  return void renderScreeningList(view());
}

window.addEventListener("hashchange", route);
window.addEventListener("DOMContentLoaded", route);

// ---------------------------------------------------------------------------
// Screening list
// ---------------------------------------------------------------------------

async function renderScreeningList(root: HTMLElement): Promise<void> {
  root.innerHTML = '<div class="text-muted">Loading screenings…</div>';
  const screenings = await listScreenings().catch((error) => {
    notify(`Could not load screenings: ${(error as Error).message}`, "danger");
    return [];
  });

  root.innerHTML = "";
  const header = el("div", "d-flex justify-content-between align-items-center mb-3");
  header.appendChild(el("h4", "mb-0", "Screenings"));

  const create = el("button", "btn btn-primary", "New screening") as HTMLButtonElement;
  create.addEventListener("click", async () => {
    const title = window.prompt("Position title (optional — the parsed JD can supply it):", "");
    if (title === null) return;
    const done = busy(create, "Creating…");
    try {
      const { id } = await createScreening(title);
      window.location.hash = `#/screening/${id}`;
    } catch (error) {
      notify(`Could not create a screening: ${(error as Error).message}`, "danger");
    } finally {
      done();
    }
  });
  header.appendChild(create);
  root.appendChild(header);

  if (screenings.length === 0) {
    root.appendChild(
      el(
        "div",
        "card card-body text-center text-muted",
        "No screenings yet. Create one, drop in a position description, and confirm its qualification checklist.",
      ),
    );
    return;
  }

  const table = el("table", "table table-hover align-middle");
  table.innerHTML = `
    <thead><tr>
      <th>Position</th><th>Qualifications</th><th>Candidates</th>
      <th>Status</th><th>Model</th><th>Created</th><th></th>
    </tr></thead>`;
  const body = el("tbody");
  for (const screening of screenings) {
    const row = el("tr");
    row.innerHTML = `
      <td><a href="#/screening/${screening.id}">${escapeHtml(screening.job_title || "(untitled)")}</a></td>
      <td>${screening.qualifications}${screening.quals_confirmed ? ' <span class="badge text-bg-success">confirmed</span>' : ""}</td>
      <td>${screening.candidates}</td>
      <td><span class="badge text-bg-light border">${escapeHtml(screening.status)}</span></td>
      <td><small><code>${escapeHtml(screening.provider || "—")}/${escapeHtml(screening.model || "—")}</code></small></td>
      <td><small>${escapeHtml(formatDate(screening.created_at))}</small></td>
      <td class="text-end"></td>`;
    const remove = el("button", "btn btn-sm btn-outline-danger border-0", "Delete") as HTMLButtonElement;
    remove.addEventListener("click", async () => {
      if (!window.confirm(`Delete "${screening.job_title || "(untitled)"}" and all its candidates?`)) return;
      try {
        await deleteScreening(screening.id);
        await renderScreeningList(root);
      } catch (error) {
        notify(`Could not delete: ${(error as Error).message}`, "danger");
      }
    });
    row.lastElementChild!.appendChild(remove);
    body.appendChild(row);
  }
  table.appendChild(body);
  root.appendChild(table);
}

// ---------------------------------------------------------------------------
// Screening workflow
// ---------------------------------------------------------------------------

async function renderScreening(root: HTMLElement, screeningId: string): Promise<void> {
  root.innerHTML = '<div class="text-muted">Loading screening…</div>';
  let detail: ScreeningDetail;
  try {
    detail = await getScreening(screeningId);
  } catch (error) {
    root.innerHTML = "";
    notify(`Could not load that screening: ${(error as Error).message}`, "danger");
    return;
  }

  root.innerHTML = "";
  const heading = el("div", "mb-3");
  heading.appendChild(
    el(
      "h4",
      "mb-1",
      escapeHtml(detail.screening.jobTitle || "(untitled screening)"),
    ),
  );
  heading.appendChild(
    el(
      "p",
      "text-muted small mb-0",
      `Created ${escapeHtml(formatDate(detail.screening.createdAt))}` +
        (detail.screening.model
          ? ` · evaluated with <code>${escapeHtml(detail.screening.provider)}/${escapeHtml(detail.screening.model)}</code>`
          : ""),
    ),
  );
  root.appendChild(heading);

  root.appendChild(jdSection(screeningId, detail));
  const resumeSection = uploadSection(screeningId, detail);
  root.appendChild(resumeSection.node);
  root.appendChild(stageSection(screeningId, detail));
}

// -- step 1 + 2: JD intake and checklists -----------------------------------

function jdSection(screeningId: string, detail: ScreeningDetail): HTMLElement {
  const card = new CollapsibleCard("1 · Position description → qualification checklist");
  const body = card.body;

  function summarize(qualifications: { kind: string }[], confirmed: boolean): void {
    if (!confirmed || qualifications.length === 0) {
      card.reopen();
      return;
    }
    const required = qualifications.filter((q) => q.kind === "required").length;
    card.complete(
      `${required} required, ${qualifications.length - required} preferred — confirmed`,
    );
  }

  const zone = el(
    "div",
    "dropzone text-center p-4 mb-2",
    detail.screening.hasJd
      ? `<div class="fw-semibold">${escapeHtml(detail.screening.jdFilename || "Position description loaded")}</div>
         <div class="text-muted small">Drop another PDF to re-parse it.</div>`
      : `<div class="fw-semibold">Drop the position description here</div>
         <div class="text-muted small">PDF, Word (.docx) or plain text. It is parsed into itemized required and preferred qualifications.</div>`,
  );
  const input = el("input", "d-none") as HTMLInputElement;
  input.type = "file";
  input.accept = ".pdf,.docx,.txt,.md";
  body.append(zone, input);

  const status = el("div", "small mb-3");
  body.appendChild(status);

  const listsHost = el("div");
  body.appendChild(listsHost);

  const intake = new JdIntake(listsHost, screeningId, (confirmed) => {
    document.dispatchEvent(new CustomEvent("quals-confirmed", { detail: confirmed }));
    summarize(intake.currentItems(), confirmed);
  });
  intake.load(detail.qualifications, detail.screening.qualsConfirmed);
  summarize(detail.qualifications, detail.screening.qualsConfirmed);

  initJdDropzone(zone, input, screeningId, (qualifications, jobTitle) => {
    intake.load(qualifications, false);
    document.dispatchEvent(new CustomEvent("quals-confirmed", { detail: false }));
    card.reopen();
    if (jobTitle) {
      const title = document.querySelector("#view h4");
      if (title && !title.textContent?.trim()) title.textContent = jobTitle;
    }
  }, status);

  return card.card;
}

// -- step 3: resumes and evaluation -----------------------------------------

function uploadSection(screeningId: string, detail: ScreeningDetail) {
  const card = new CollapsibleCard("2 · Resumes and evaluation");
  const body = card.body;

  const zone = el(
    "div",
    "dropzone text-center p-4 mb-2",
    `<div class="fw-semibold">Drop resumes here</div>
     <div class="text-muted small">PDFs, or a ZIP of PDFs. Uploading replaces the current candidate set.</div>`,
  );
  const input = el("input", "d-none") as HTMLInputElement;
  input.type = "file";
  input.multiple = true;
  input.accept = ".pdf,.zip";
  const list = el("ul", "list-group list-group-flush mb-2");
  body.append(zone, input, list);

  const uploader = new UploadForm(zone, input, list);

  const buttons = el("div", "d-flex gap-2 align-items-center flex-wrap");
  const upload = el("button", "btn btn-outline-primary", "Upload resumes") as HTMLButtonElement;
  const evaluate = el("button", "btn btn-primary", "Evaluate candidates") as HTMLButtonElement;
  const counter = el(
    "span",
    "text-muted small",
    `${Object.values(detail.stageCounts).reduce((sum, n) => sum + n, 0)} candidate(s) loaded`,
  );
  buttons.append(upload, evaluate, counter);
  body.appendChild(buttons);

  const progress = el("div", "mt-3 d-none");
  body.appendChild(progress);

  const evaluated = Object.values(detail.stageCounts).reduce((sum, n) => sum + n, 0);
  if (detail.screening.status === "evaluated" && evaluated > 0) {
    card.complete(`${evaluated} candidate(s) evaluated`);
  }

  function setEvaluateEnabled(confirmed: boolean): void {
    evaluate.disabled = !confirmed;
    evaluate.title = confirmed
      ? "Run every candidate against the confirmed checklist"
      : "Confirm the qualification checklist first";
  }
  setEvaluateEnabled(detail.screening.qualsConfirmed);
  document.addEventListener("quals-confirmed", (event) => {
    setEvaluateEnabled(Boolean((event as CustomEvent).detail));
  });

  upload.addEventListener("click", async () => {
    const files = uploader.getFiles();
    if (files.length === 0) {
      notify("Add at least one PDF or ZIP first.", "warning");
      return;
    }
    const done = busy(upload, "Uploading…");
    try {
      const result = await uploadResumes(screeningId, files);
      uploader.clear();
      counter.textContent = `${result.total} candidate(s) loaded`;
      notify(
        `Loaded ${result.added} candidate(s).` +
          (result.skipped.length ? ` Skipped ${result.skipped.length} with no extractable text.` : ""),
        "success",
      );
    } catch (error) {
      notify(`Upload failed: ${(error as Error).message}`, "danger");
    } finally {
      done();
    }
  });

  evaluate.addEventListener("click", async () => {
    const done = busy(evaluate, "Starting…");
    try {
      const started = await startEvaluation(screeningId);
      notify(
        `Evaluating ${started.candidates} candidate(s) with ${started.provider}/${started.model}.`,
        "info",
      );
      showProgress(progress, started.jobId, {
        onDone: () => {
          done();
          // The step is finished, so it folds away and the stage grids —
          // which is where the work happens from here — come up the page.
          card.complete(`${started.candidates} candidate(s) evaluated`);
          document.dispatchEvent(new CustomEvent("stages-changed"));
        },
        onError: () => done(),
      });
    } catch (error) {
      done();
      notify(`Could not start the evaluation: ${(error as Error).message}`, "danger");
    }
  });

  return { node: card.card };
}

// -- step 4: stage tabs, grids, chat ----------------------------------------

function stageSection(screeningId: string, detail: ScreeningDetail): HTMLElement {
  const card = el("div", "card");
  card.appendChild(el("div", "card-header fw-semibold", "3 · Staged review"));
  const body = el("div", "card-body");

  const tabs = el("ul", "nav nav-tabs mb-3");
  const counts: Record<string, number> = { ...detail.stageCounts };
  const tabButtons = new Map<Stage, HTMLElement>();

  const layout = el("div", "row g-3");
  const gridColumn = el("div", "col-xl-8");
  const chatColumn = el("div", "col-xl-4");
  layout.append(gridColumn, chatColumn);

  const gridHost = el("div");
  const evidenceHost = el("div", "mt-3");
  evidenceHost.id = "evidence-panel";
  gridColumn.append(gridHost, evidenceHost);

  const chatHost = el("div", "h-100");
  chatColumn.appendChild(chatHost);

  let grid: StageGrid | null = null;
  let chat: ChatPanel | null = null;
  let current: Stage = "1";

  function paintCounts(next: Record<string, number>): void {
    Object.assign(counts, next);
    for (const stage of STAGES) {
      const badge = tabButtons.get(stage)?.querySelector(".badge");
      if (badge) badge.textContent = String(counts[stage] ?? 0);
    }
  }

  async function select(stage: Stage): Promise<void> {
    current = stage;
    for (const [key, node] of tabButtons) {
      node.classList.toggle("active", key === stage);
    }
    evidenceHost.innerHTML = "";
    grid?.destroy();
    grid = new StageGrid(gridHost, screeningId, stage, paintCounts);
    await grid.refresh();

    chat = new ChatPanel(chatHost, screeningId, stage, (action) => grid?.applyGridAction(action));
    await chat.load();
  }

  for (const stage of STAGES) {
    const item = el("li", "nav-item");
    const button = el(
      "button",
      "nav-link",
      `${STAGE_TABS[stage]} <span class="badge text-bg-secondary ms-1">${counts[stage] ?? 0}</span>`,
    );
    button.addEventListener("click", () => void select(stage));
    tabButtons.set(stage, button);
    item.appendChild(button);
    tabs.appendChild(item);
  }

  body.append(tabs, layout);

  const trail = el("div", "mt-4");
  body.appendChild(trail);
  void renderAuditTrail(trail, screeningId);

  card.appendChild(body);

  document.addEventListener("stages-changed", () => {
    void select(current);
    void renderAuditTrail(trail, screeningId);
  });

  void select("1");
  return card;
}

async function renderAuditTrail(host: HTMLElement, screeningId: string): Promise<void> {
  const entries = await auditTrail(screeningId).catch(() => []);
  host.innerHTML = "";
  if (entries.length === 0) return;

  const details = el("details", "border rounded p-2");
  details.appendChild(
    el("summary", "fw-semibold small", `Promotion history (${entries.length} action${entries.length === 1 ? "" : "s"})`),
  );
  const table = el("table", "table table-sm mt-2 mb-0");
  table.innerHTML = "<thead><tr><th>When</th><th>Candidate</th><th>Move</th><th>Note</th></tr></thead>";
  const body = el("tbody");
  for (const entry of entries) {
    const row = el("tr");
    row.innerHTML = `
      <td><small>${escapeHtml(formatDate(entry.created_at))}</small></td>
      <td>${escapeHtml(entry.name)}</td>
      <td>${escapeHtml(entry.from_stage)} → ${escapeHtml(entry.to_stage)}
          ${entry.override ? '<span class="badge text-bg-warning ms-1">override</span>' : ""}</td>
      <td><small>${escapeHtml(entry.note || "")}</small></td>`;
    body.appendChild(row);
  }
  table.appendChild(body);
  details.appendChild(table);
  host.appendChild(details);
}
