/** Bias audit: every baseline resume shown beside its protected-class variant.
 *
 * The question a reviewer is asking is "did adding this sentence change how the
 * AI judged this person?", so the report answers it literally — the two scored
 * resumes side by side, with the inserted sentence quoted and every changed
 * judgement highlighted. No statistics vocabulary, because none is needed to
 * read one resume against its own copy.
 */

import {
  getAudit,
  getConfig,
  getCorpus,
  listAudits,
  listModels,
  listScreenings,
  startAudit,
} from "./api.ts";
import { showProgress } from "./progress.ts";
import type { AppConfig, AuditComparison, AuditRun, Qualification } from "./types.ts";
import { busy, el, escapeHtml, formatDate, notify } from "./ui.ts";

export async function renderAuditPage(root: HTMLElement): Promise<void> {
  root.innerHTML = '<div class="text-muted">Loading the audit corpus…</div>';

  const [corpus, screenings, runs, config] = await Promise.all([
    getCorpus().catch(() => null),
    listScreenings().catch(() => []),
    listAudits().catch(() => []),
    getConfig().catch(() => null),
  ]);

  root.innerHTML = "";
  root.appendChild(el("h4", "mb-1", "Bias audit"));
  root.appendChild(
    el(
      "p",
      "text-muted",
      "Each resume is scored twice: once as written, and once with a single sentence added that discloses a protected characteristic. Nothing else differs, so any change in the score came from that sentence.",
    ),
  );

  if (!corpus) {
    notify("The audit corpus could not be read.", "danger");
    return;
  }
  root.appendChild(corpusCard(corpus));

  const results = el("div");
  root.appendChild(runnerCard(screenings, results, root, config));
  root.appendChild(results);
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
      `${corpus.files} resumes: ${corpus.baselines} originals and ${corpus.pairs} comparisons.`,
    ),
  );
  const chips = el("div", "d-flex flex-wrap gap-2");
  for (const entry of corpus.byAttribute) {
    chips.appendChild(
      el(
        "span",
        entry.attribute === "control" ? "badge text-bg-secondary" : "badge text-bg-primary",
        `${escapeHtml(entry.label)}: ${entry.pairs}`,
      ),
    );
  }
  body.appendChild(chips);
  body.appendChild(
    el(
      "p",
      "text-muted small mb-0 mt-2",
      "Controls are pairs where the added sentence discloses nothing. They show how much the score moves for no reason at all — the yardstick for everything else.",
    ),
  );
  card.appendChild(body);
  return card;
}

function runnerCard(
  screenings: { id: string; job_title: string; qualifications: number }[],
  results: HTMLElement,
  root: HTMLElement,
  config: AppConfig | null,
): HTMLElement {
  const card = el("div", "card mb-3");
  card.appendChild(el("div", "card-header fw-semibold", "Run an audit"));
  const body = el("div", "card-body");
  body.appendChild(
    el(
      "p",
      "text-muted small",
      "Pick the model to test. This is independent of the model set on the Configuration page, so a benchmark run does not disturb an in-progress screening. Every run is stored under the model that produced it.",
    ),
  );

  const row = el("div", "row g-3 mb-3");

  const screeningField = el("div", "col-lg-6");
  screeningField.appendChild(el("label", "form-label small text-muted", "Qualification list"));
  const select = el("select", "form-select") as HTMLSelectElement;
  const usable = screenings.filter((s) => s.qualifications > 0);
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
  screeningField.appendChild(select);

  const providerField = el("div", "col-lg-3");
  providerField.appendChild(el("label", "form-label small text-muted", "Source"));
  const providerSelect = el("select", "form-select") as HTMLSelectElement;
  for (const provider of config?.providers ?? []) {
    const option = el(
      "option",
      "",
      `${escapeHtml(provider.label)}${provider.configured ? "" : " — not configured"}`,
    ) as HTMLOptionElement;
    option.value = provider.name;
    option.selected = provider.name === config?.provider;
    option.disabled = !provider.configured;
    providerSelect.appendChild(option);
  }
  providerField.appendChild(providerSelect);

  const modelField = el("div", "col-lg-3");
  modelField.appendChild(el("label", "form-label small text-muted", "Model"));
  const modelSelect = el("select", "form-select") as HTMLSelectElement;
  modelField.appendChild(modelSelect);

  row.append(screeningField, providerField, modelField);
  body.appendChild(row);

  const run = el("button", "btn btn-primary", "Run bias audit") as HTMLButtonElement;
  run.disabled = usable.length === 0;
  body.appendChild(run);

  const progress = el("div", "mt-3 d-none");
  body.appendChild(progress);

  async function loadModels(providerName: string): Promise<void> {
    modelSelect.innerHTML = '<option value="">Loading…</option>';
    modelSelect.disabled = true;
    run.disabled = true;
    try {
      const listed = await listModels(providerName);
      modelSelect.innerHTML = "";
      for (const model of listed.models) {
        const option = el("option", "", escapeHtml(model)) as HTMLOptionElement;
        option.value = model;
        // Preselect whatever is globally active, so the obvious next run
        // matches what the screening itself was scored with.
        option.selected = providerName === config?.provider && model === config?.model;
        modelSelect.appendChild(option);
      }
      if (listed.models.length === 0) {
        const manual = el("option", "", "(no models listed)") as HTMLOptionElement;
        manual.value = "";
        modelSelect.appendChild(manual);
      }
    } catch {
      modelSelect.innerHTML = '<option value="">(could not list models)</option>';
    } finally {
      modelSelect.disabled = false;
      run.disabled = usable.length === 0;
    }
  }

  providerSelect.addEventListener("change", () => void loadModels(providerSelect.value));
  void loadModels(providerSelect.value || config?.provider || "");

  run.addEventListener("click", async () => {
    if (!modelSelect.value) {
      notify("Choose a model to run the audit with.", "warning");
      return;
    }
    const done = busy(run, "Running…");
    try {
      const started = await startAudit(select.value, providerSelect.value, modelSelect.value);
      notify(`Audit started with ${started.provider}/${started.model}.`, "info");
      showProgress(progress, started.jobId, {
        onDone: () => {
          done();
          void renderRun(results, started.auditId);
          void renderAuditPage(root);
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

  card.appendChild(body);
  return card;
}

/** Model comparison: one row per run, so two models are read against each
 *  other rather than by opening each result in turn. */
function historyCard(
  runs: {
    id: string;
    provider: string;
    model: string;
    status: string;
    created_at: string;
    stageFlips: number | null;
    meanCoverageDelta: number | null;
    pairs: number | null;
  }[],
  results: HTMLElement,
): HTMLElement {
  const card = el("div", "card mt-3");
  card.appendChild(
    el(
      "div",
      "card-header fw-semibold d-flex justify-content-between align-items-center",
      `<span>Model comparison</span>
       <span class="text-muted small fw-normal">every run, newest first</span>`,
    ),
  );
  const body = el("div", "card-body p-0");
  if (runs.length === 0) {
    body.appendChild(el("p", "text-muted m-3", "No audits have been run yet."));
    card.appendChild(body);
    return card;
  }

  const table = el("table", "table table-sm table-hover align-middle mb-0");
  table.innerHTML = `
    <thead><tr>
      <th>Model</th><th>Source</th>
      <th title="Candidates advanced or rejected differently after a disclosure">Advancement changes</th>
      <th title="Mean absolute change in qualifications met">Mean coverage delta</th>
      <th>Pairs</th><th>Run</th><th></th>
    </tr></thead>`;
  const tbody = el("tbody");
  for (const run of runs) {
    const row = el("tr");
    const flips =
      run.stageFlips === null || run.status !== "done"
        ? '<span class="text-muted">—</span>'
        : run.stageFlips === 0
          ? '<span class="badge text-bg-success">0</span>'
          : `<span class="badge text-bg-danger">${run.stageFlips}</span>`;
    row.innerHTML = `
      <td><code>${escapeHtml(run.model)}</code></td>
      <td class="small text-muted">${escapeHtml(run.provider)}</td>
      <td>${flips}</td>
      <td>${run.meanCoverageDelta ?? "—"}</td>
      <td>${run.pairs ?? "—"}</td>
      <td class="small text-muted">${escapeHtml(formatDate(run.created_at))}${
        run.status === "done" ? "" : ` · ${escapeHtml(run.status)}`
      }</td>
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

// ---------------------------------------------------------------------------
// One run
// ---------------------------------------------------------------------------

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

  container.appendChild(headline(run));

  const measured = run.comparisons.filter((c) => !c.isControl);
  const controls = run.comparisons.filter((c) => c.isControl);

  const list = el("div", "card");
  list.appendChild(
    el(
      "div",
      "card-header fw-semibold d-flex justify-content-between align-items-center",
      `<span>Comparisons</span><span class="text-muted small fw-normal">click any row to see the two resumes scored side by side</span>`,
    ),
  );
  const body = el("div", "card-body p-0");
  for (const comparison of measured) {
    body.appendChild(comparisonRow(comparison, run.qualifications));
  }
  if (controls.length) {
    body.appendChild(
      el(
        "div",
        "px-3 py-2 bg-body-tertiary border-top border-bottom small fw-semibold",
        "Controls — nothing meaningful was added to these",
      ),
    );
    for (const comparison of controls) {
      body.appendChild(comparisonRow(comparison, run.qualifications));
    }
  }
  list.appendChild(body);
  container.appendChild(list);
}

function headline(run: AuditRun): HTMLElement {
  const counts = run.counts;
  const clean = counts.advancementChanges === 0 && counts.lostGround === 0;
  const banner = el("div", `alert ${clean ? "alert-success" : "alert-warning"}`);

  banner.appendChild(
    el(
      "h5",
      "alert-heading",
      counts.advancementChanges === 0
        ? "No candidate's advancement changed"
        : `${counts.advancementChanges} candidate(s) would have been advanced differently`,
    ),
  );
  banner.appendChild(
    el(
      "p",
      "mb-1",
      `Out of <strong>${counts.comparisons}</strong> comparisons: ` +
        `<strong>${counts.identical}</strong> scored exactly the same, ` +
        `<strong>${counts.sameTotal}</strong> met the same number of qualifications but were judged differently on some, ` +
        `<strong>${counts.lostGround}</strong> lost ground after the disclosure, ` +
        `<strong>${counts.gainedGround}</strong> gained. ` +
        `In total <strong>${counts.judgmentsChanged}</strong> of ${counts.judgmentsCompared} individual qualification judgements changed.`,
    ),
  );
  banner.appendChild(
    el(
      "p",
      "mb-0 small",
      `Model: <code>${escapeHtml(run.provider)}/${escapeHtml(run.model)}</code>. ` +
        (counts.controlsUnstable > 0
          ? `<strong>Caution:</strong> ${counts.controlsUnstable} of ${counts.controls} control comparisons also changed, even though nothing was disclosed in them. Movements of that size cannot be attributed to the disclosure — rows below are flagged where this applies.`
          : `All ${counts.controls} control comparisons scored identically, so movements below are attributable to the disclosure.`),
    ),
  );
  return banner;
}

function comparisonRow(comparison: AuditComparison, quals: Qualification[]): HTMLElement {
  const wrapper = el("div", "border-bottom");
  const header = el("div", "d-flex align-items-center gap-3 px-3 py-2 comparison-row");
  header.style.cursor = "pointer";

  const verdictBadge = comparison.netChange < 0
    ? `<span class="badge text-bg-danger">lost ${Math.abs(comparison.netChange)}</span>`
    : comparison.netChange > 0
      ? `<span class="badge text-bg-warning">gained ${comparison.netChange}</span>`
      : comparison.changed.length
        ? `<span class="badge text-bg-secondary">same total</span>`
        : `<span class="badge text-bg-success">identical</span>`;

  header.innerHTML = `
    <span class="fw-semibold" style="min-width:11rem">${escapeHtml(comparison.candidate)}</span>
    <span class="badge text-bg-light border">${escapeHtml(comparison.attributeLabel)}</span>
    <span style="min-width:9rem">${comparison.baseline.met}/${comparison.baseline.total}
      <span class="text-muted">&rarr;</span> ${comparison.variant.met}/${comparison.variant.total}</span>
    ${verdictBadge}
    <span class="text-muted small">${comparison.changed.length} judgement(s) changed</span>
    ${comparison.matchesControl ? '<span class="badge text-bg-secondary" title="This resume moves by the same amount even when nothing is disclosed, so the movement is not attributable to the disclosure.">matches its control</span>' : ""}
    ${comparison.advancementChanged ? '<span class="badge text-bg-danger">advancement changed</span>' : ""}
    <span class="ms-auto text-muted">▾</span>`;

  const detail = el("div", "px-3 pb-3 d-none");
  let built = false;
  header.addEventListener("click", () => {
    if (!built) {
      detail.appendChild(sideBySide(comparison, quals));
      built = true;
    }
    detail.classList.toggle("d-none");
  });

  wrapper.append(header, detail);
  return wrapper;
}

function sideBySide(comparison: AuditComparison, quals: Qualification[]): HTMLElement {
  const panel = el("div");

  if (comparison.added.length) {
    const added = el("div", "alert alert-light border mb-3");
    added.appendChild(el("div", "small text-muted mb-1", "Text added to the original resume:"));
    for (const sentence of comparison.added) {
      added.appendChild(el("div", "fst-italic", `“${escapeHtml(sentence)}”`));
    }
    panel.appendChild(added);
  }

  const table = el("table", "table table-sm align-middle mb-0");
  table.innerHTML = `
    <thead>
      <tr>
        <th style="width:45%">Qualification</th>
        <th class="text-center">Original resume</th>
        <th class="text-center">With disclosure</th>
        <th></th>
      </tr>
    </thead>`;

  const body = el("tbody");
  const summary = el("tr", "table-light fw-semibold");
  summary.innerHTML = `
    <td>Qualifications met</td>
    <td class="text-center">${comparison.baseline.met} of ${comparison.baseline.total}</td>
    <td class="text-center">${comparison.variant.met} of ${comparison.variant.total}</td>
    <td></td>`;
  body.appendChild(summary);

  const recommendation = el("tr", "table-light fw-semibold");
  recommendation.innerHTML = `
    <td>AI recommendation</td>
    <td class="text-center">${badge(comparison.baseline.aiPass)}</td>
    <td class="text-center">${badge(comparison.variant.aiPass)}</td>
    <td>${comparison.advancementChanged ? '<span class="text-danger fw-semibold">changed</span>' : ""}</td>`;
  body.appendChild(recommendation);

  for (const qual of quals) {
    const before = comparison.baseline.verdicts[qual.id];
    const after = comparison.variant.verdicts[qual.id];
    const changed = comparison.changed.includes(qual.id);
    const row = el("tr", changed ? "table-warning" : "");
    row.innerHTML = `
      <td><span class="badge text-bg-light border me-1">${qual.label}</span>${escapeHtml(qual.text)}</td>
      <td class="text-center ${verdictClass(before?.verdict)}" title="${escapeHtml(before?.evidence || "")}">${before?.verdict ?? "—"}</td>
      <td class="text-center ${verdictClass(after?.verdict)}" title="${escapeHtml(after?.evidence || "")}">${after?.verdict ?? "—"}</td>
      <td class="small">${changed ? '<span class="text-danger">changed</span>' : ""}</td>`;
    body.appendChild(row);
  }

  table.appendChild(body);
  panel.appendChild(table);
  panel.appendChild(
    el("p", "text-muted small mt-2 mb-0", "Hover a verdict to see the evidence the AI quoted for it."),
  );
  return panel;
}

function badge(pass: boolean): string {
  return pass
    ? '<span class="badge text-bg-success">Advance</span>'
    : '<span class="badge text-bg-danger">Reject</span>';
}

function verdictClass(verdict: string | undefined): string {
  if (verdict === "Meets") return "verdict-meets";
  if (verdict === "Partial") return "verdict-partial";
  if (verdict === "No") return "verdict-no";
  return "verdict-missing";
}
