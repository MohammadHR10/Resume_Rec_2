/** Bias audit: run the paired corpus through the active model and read the deltas. */

import {
  AllCommunityModule,
  ModuleRegistry,
  createGrid,
  type ColDef,
  type ICellRendererParams,
} from "ag-grid-community";

import { getAudit, getCorpus, listAudits, listScreenings, startAudit } from "./api.ts";
import { showProgress } from "./progress.ts";
import { screeningTheme } from "./stageGrid.ts";
import type { AuditPair, AuditRun } from "./types.ts";
import { busy, el, escapeHtml, formatDate, notify } from "./ui.ts";

ModuleRegistry.registerModules([AllCommunityModule]);

export async function renderAuditPage(root: HTMLElement): Promise<void> {
  root.innerHTML = '<div class="text-muted">Loading the audit corpus…</div>';

  const [corpus, screenings, runs] = await Promise.all([
    getCorpus().catch(() => null),
    listScreenings().catch(() => []),
    listAudits().catch(() => []),
  ]);

  root.innerHTML = "";
  root.appendChild(el("h4", "mb-1", "Bias audit"));
  root.appendChild(
    el(
      "p",
      "text-muted",
      "Each variant resume is identical to its baseline except for one sentence disclosing a protected characteristic. Any difference in the outcome is attributable to that sentence.",
    ),
  );

  if (!corpus) {
    notify("The audit corpus could not be read.", "danger");
    return;
  }

  root.appendChild(corpusCard(corpus));

  const runner = el("div", "card mb-3");
  runner.appendChild(el("div", "card-header fw-semibold", "Run an audit"));
  const runnerBody = el("div", "card-body");
  runnerBody.appendChild(
    el(
      "p",
      "text-muted small",
      "The audit evaluates the whole corpus against a screening's qualification list, using the provider and model selected on the Configuration page. It never touches that screening's own candidates.",
    ),
  );

  const select = el("select", "form-select mb-3") as HTMLSelectElement;
  const usable = screenings.filter((screening) => screening.qualifications > 0);
  if (usable.length === 0) {
    select.innerHTML = '<option value="">No screening has qualifications yet</option>';
    select.disabled = true;
  } else {
    for (const screening of usable) {
      const option = el(
        "option",
        "",
        `${escapeHtml(screening.job_title || "(untitled)")} — ${screening.qualifications} qualification(s)`,
      ) as HTMLOptionElement;
      option.value = screening.id;
      select.appendChild(option);
    }
  }
  runnerBody.appendChild(select);

  const run = el("button", "btn btn-primary", "Run bias audit") as HTMLButtonElement;
  run.disabled = usable.length === 0;
  runnerBody.appendChild(run);

  const progress = el("div", "mt-3 d-none");
  runnerBody.appendChild(progress);
  runner.appendChild(runnerBody);
  root.appendChild(runner);

  const results = el("div");
  root.appendChild(results);

  run.addEventListener("click", async () => {
    const done = busy(run, "Running…");
    try {
      const started = await startAudit(select.value);
      notify(`Audit started with ${started.provider}/${started.model}.`, "info");
      showProgress(progress, started.jobId, {
        onDone: () => {
          done();
          void renderRun(results, started.auditId);
          void refreshHistory(root);
        },
        onError: (message) => {
          done();
          notify(`Audit failed: ${message}`, "danger");
        },
      });
    } catch (error) {
      done();
      notify(`Could not start the audit: ${(error as Error).message}`, "danger");
    }
  });

  root.appendChild(historyCard(runs, results));

  if (runs.length > 0 && runs[0].status === "done") {
    void renderRun(results, runs[0].id);
  }
}

function corpusCard(corpus: {
  directory: string;
  files: number;
  baselines: number;
  pairs: number;
  byAttribute: { attribute: string; label: string; pairs: number }[];
  unpaired: string[];
}): HTMLElement {
  const card = el("div", "card mb-3");
  card.appendChild(el("div", "card-header fw-semibold", "Corpus"));
  const body = el("div", "card-body");
  body.appendChild(
    el(
      "p",
      "mb-2",
      `<code>${escapeHtml(corpus.directory)}</code> — ${corpus.files} resumes: ` +
        `${corpus.baselines} baselines and ${corpus.pairs} baseline/variant pairs.`,
    ),
  );
  const chips = el("div", "d-flex flex-wrap gap-2");
  for (const entry of corpus.byAttribute) {
    chips.appendChild(
      el(
        "span",
        entry.attribute === "control" ? "badge text-bg-secondary" : "badge text-bg-primary",
        `${escapeHtml(entry.label)}: ${entry.pairs} pair(s)`,
      ),
    );
  }
  body.appendChild(chips);
  if (corpus.unpaired.length > 0) {
    body.appendChild(
      el(
        "p",
        "text-warning-emphasis small mt-2 mb-0",
        `${corpus.unpaired.length} variant(s) have no matching baseline: ${escapeHtml(
          corpus.unpaired.join(", "),
        )}`,
      ),
    );
  }
  card.appendChild(body);
  return card;
}

function historyCard(
  runs: {
    id: string;
    provider: string;
    model: string;
    status: string;
    passed: boolean | null;
    created_at: string;
    stageFlips: number | null;
    meanCoverageDelta: number | null;
    pairs: number | null;
  }[],
  results: HTMLElement,
): HTMLElement {
  const card = el("div", "card mt-3");
  card.appendChild(el("div", "card-header fw-semibold", "Previous runs"));
  const body = el("div", "card-body p-0");

  if (runs.length === 0) {
    body.appendChild(el("p", "text-muted m-3", "No audits have been run yet."));
    card.appendChild(body);
    return card;
  }

  const table = el("table", "table table-sm table-hover mb-0");
  table.innerHTML = `
    <thead><tr>
      <th>Run</th><th>Model</th><th>Result</th><th>Stage flips</th>
      <th>Mean coverage delta</th><th>Pairs</th><th></th>
    </tr></thead>`;
  const tbody = el("tbody");
  for (const run of runs) {
    const row = el("tr");
    const badge =
      run.status !== "done"
        ? `<span class="badge text-bg-secondary">${escapeHtml(run.status)}</span>`
        : run.passed
          ? '<span class="badge text-bg-success">Passed</span>'
          : '<span class="badge text-bg-danger">Failed</span>';
    row.innerHTML = `
      <td>${escapeHtml(formatDate(run.created_at))}</td>
      <td><code>${escapeHtml(run.provider)}/${escapeHtml(run.model)}</code></td>
      <td>${badge}</td>
      <td>${run.stageFlips ?? "—"}</td>
      <td>${run.meanCoverageDelta ?? "—"}</td>
      <td>${run.pairs ?? "—"}</td>
      <td class="text-end"></td>`;
    const view = el("button", "btn btn-sm btn-outline-primary", "View") as HTMLButtonElement;
    view.addEventListener("click", () => void renderRun(results, run.id));
    row.lastElementChild!.appendChild(view);
    tbody.appendChild(row);
  }
  table.appendChild(tbody);
  body.appendChild(table);
  card.appendChild(body);
  return card;
}

async function refreshHistory(root: HTMLElement): Promise<void> {
  // Cheap and correct: the whole page is derived from three GETs.
  await renderAuditPage(root);
}

async function renderRun(container: HTMLElement, auditId: string): Promise<void> {
  container.innerHTML = '<div class="text-muted">Loading results…</div>';
  let run: AuditRun;
  try {
    run = await getAudit(auditId);
  } catch (error) {
    container.innerHTML = "";
    notify(`Could not load that audit: ${(error as Error).message}`, "danger");
    return;
  }

  container.innerHTML = "";
  if (run.status === "error") {
    container.appendChild(el("div", "alert alert-danger", escapeHtml(run.error || "The audit failed.")));
    return;
  }
  if (run.status !== "done") {
    container.appendChild(el("div", "alert alert-info", "This audit is still running."));
    return;
  }

  const summary = run.summary;
  const banner = el(
    "div",
    `alert ${summary.passed ? "alert-success" : "alert-danger"}`,
    `<h5 class="alert-heading">${summary.passed ? "Negligible bias detected" : "Bias threshold exceeded"}</h5>
     <p class="mb-1"><code>${escapeHtml(run.provider)}/${escapeHtml(run.model)}</code> —
     ${summary.pairs_measured} measured pair(s), ${summary.total_stage_flips} stage-outcome flip(s),
     mean coverage delta ${summary.mean_coverage_delta}.</p>
     <p class="mb-0 small">Thresholds: ≤ ${summary.thresholds.max_stage_flips} stage flip(s),
     mean coverage delta ≤ ${summary.thresholds.max_mean_coverage_delta}.
     Control pairs (no attribute injected) flipped ${summary.control_stage_flips} outcome(s) — that is the noise floor.</p>`,
  );
  if (summary.failures.length > 0) {
    banner.appendChild(
      el("ul", "mb-0 mt-2", summary.failures.map((f) => `<li>${escapeHtml(f)}</li>`).join("")),
    );
  }
  container.appendChild(banner);

  const attributes = el("div", "row g-3 mb-3");
  for (const attribute of summary.attributes) {
    const column = el("div", "col-md-6 col-xl-3");
    const card = el("div", `card h-100 ${attribute.attribute === "control" ? "border-secondary" : ""}`);
    card.appendChild(el("div", "card-header fw-semibold small", escapeHtml(attribute.label)));
    card.appendChild(
      el(
        "div",
        "card-body small",
        `<div>Pairs: <strong>${attribute.pairs}</strong></div>
         <div>Stage-outcome flips: <strong>${attribute.stage_flips}</strong></div>
         <div>Verdict flips: <strong>${attribute.verdict_flips}</strong> (${(
           attribute.verdict_flip_rate * 100
         ).toFixed(1)}%)</div>
         <div>Mean coverage delta: <strong>${attribute.mean_coverage_delta}</strong> (max ${attribute.max_coverage_delta})</div>
         <div>Mean rank displacement: <strong>${attribute.mean_rank_displacement}</strong> (max ${attribute.max_rank_displacement})</div>`,
      ),
    );
    column.appendChild(card);
    attributes.appendChild(column);
  }
  container.appendChild(attributes);

  const gridCard = el("div", "card");
  gridCard.appendChild(el("div", "card-header fw-semibold", "Pairs, worst first"));
  const gridBody = el("div", "card-body");
  const host = el("div", "stage-grid");
  gridBody.appendChild(host);
  gridCard.appendChild(gridBody);
  container.appendChild(gridCard);

  const columns: ColDef<AuditPair>[] = [
    { headerName: "Attribute", field: "attribute_label", width: 170 },
    { headerName: "Baseline", field: "baseline_file", flex: 1, minWidth: 220 },
    { headerName: "Variant", field: "variant_file", flex: 1, minWidth: 220 },
    {
      headerName: "Stage flip",
      width: 130,
      valueGetter: (params) => (params.data?.delta.stage_flip ? "Yes" : "No"),
      cellRenderer: (params: ICellRendererParams<AuditPair>) =>
        params.value === "Yes"
          ? '<span class="badge text-bg-danger">Yes</span>'
          : '<span class="badge text-bg-light border">No</span>',
    },
    {
      headerName: "Coverage Δ",
      width: 130,
      valueGetter: (params) => params.data?.delta.coverage_delta ?? 0,
    },
    {
      headerName: "Verdict flips",
      width: 140,
      valueGetter: (params) => params.data?.delta.verdict_flip_count ?? 0,
    },
    { headerName: "Rank Δ", field: "rank_displacement", width: 110 },
    {
      headerName: "What flipped",
      flex: 2,
      minWidth: 260,
      valueGetter: (params) =>
        (params.data?.delta.verdict_flips ?? [])
          .map((flip) => `${flip.qual_text}: ${flip.baseline} → ${flip.variant}`)
          .join(" · ") || "—",
      tooltipValueGetter: (params) => String(params.value ?? ""),
    },
  ];

  createGrid<AuditPair>(host, {
    theme: screeningTheme,
    columnDefs: columns,
    rowData: run.pairs,
    defaultColDef: { sortable: true, filter: true, resizable: true },
    tooltipShowDelay: 200,
    rowClassRules: {
      "audit-control-row": (params) => Boolean(params.data?.is_control),
    },
  });
}
