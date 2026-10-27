/** Bias audit: candidates grouped by experience level, then by protected class.
 *
 * Every resume within a level satisfies identical qualifications by
 * construction, so the whole column should read one number. Anywhere it does
 * not, the model scored the same claims differently depending on whose name was
 * on them — which needs no pairing to see, and no statistics vocabulary to say.
 */

import {
  deleteAudit,
  getAudit,
  getConfig,
  getCorpus,
  getQualifications,
  listAudits,
  listModels,
  listScreenings,
  startAudit,
  type Corpus,
} from "./api.ts";
import { JdIntake } from "./jdIntake.ts";
import { showProgress } from "./progress.ts";
import type { AppConfig, AuditLevel, AuditRun } from "./types.ts";
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
      "Every resume at a given experience level makes identical qualification claims — only the name, pronouns or affiliation differ. So each experience level should score as one block, and any spread between classes is the model reading the same claims differently.",
    ),
  );

  if (!corpus || corpus.corpora.length === 0) {
    notify("No audit corpus could be read.", "danger");
    return;
  }

  const results = el("div");
  root.appendChild(runnerCard(screenings, results, root, config, corpus.corpora));
  root.appendChild(results);
  root.appendChild(historyCard(runs, results));

  if (runs.length > 0 && runs[0].status === "done") {
    void renderRun(results, runs[0].id);
  }
}

/** What the selected corpus contains, and whether it can support an audit. */
function corpusSummary(corpus: Corpus): HTMLElement {
  const panel = el("div", "border rounded p-3 bg-body-tertiary");

  const levels = Object.entries(corpus.skillLevels);
  panel.appendChild(
    el(
      "p",
      "mb-2",
      `<strong>${escapeHtml(corpus.name)}</strong> — ${corpus.files} resumes` +
        (corpus.pairs > 0 ? `, ${corpus.pairs} baseline/variant comparisons` : "") +
        (corpus.positionDescription
          ? ` · ships its own position description (<code>${escapeHtml(corpus.positionDescription)}</code>)`
          : ""),
    ),
  );

  if (levels.length) {
    const chips = el("div", "d-flex flex-wrap gap-2 mb-2");
    for (const [level, count] of levels) {
      chips.appendChild(
        el("span", "badge text-bg-info", `${escapeHtml(level)}: ${count}`),
      );
    }
    panel.appendChild(chips);
    panel.appendChild(
      el(
        "p",
        "text-muted small mb-0",
        "Skill levels are built in, so the expected outcome is known before the model runs: seniors and juniors should clear stage 1, unqualified candidates should not.",
      ),
    );
  }

  if (corpus.byAttribute.length) {
    const chips = el("div", "d-flex flex-wrap gap-2 mb-2");
    for (const entry of corpus.byAttribute) {
      chips.appendChild(
        el(
          "span",
          entry.attribute === "control" ? "badge text-bg-secondary" : "badge text-bg-primary",
          `${escapeHtml(entry.label)}: ${entry.pairs}`,
        ),
      );
    }
    panel.appendChild(chips);
    if (corpus.byAttribute.some((entry) => entry.attribute === "control")) {
      panel.appendChild(
        el(
          "p",
          "text-muted small mb-0",
          "Controls are pairs where the added sentence discloses nothing — the yardstick for everything else.",
        ),
      );
    }
  } else {
    panel.appendChild(
      el(
        "div",
        "alert alert-warning small mb-0 mt-2",
        "<strong>No comparison pairs yet.</strong> This corpus has baseline resumes but no protected-class variants, so a bias audit would have nothing to compare. Use it to check that the screening scores each skill level as designed; generate variants before auditing it.",
      ),
    );
  }
  return panel;
}

function runnerCard(
  screenings: { id: string; job_title: string; qualifications: number }[],
  results: HTMLElement,
  root: HTMLElement,
  config: AppConfig | null,
  corpora: Corpus[],
): HTMLElement {
  const card = el("div", "card mb-3");
  card.appendChild(el("div", "card-header fw-semibold", "Run an audit"));
  const body = el("div", "card-body");
  body.appendChild(
    el(
      "p",
      "text-muted small",
      "Pick the corpus and the model to test. The model here is independent of the one set on the Configuration page, so a benchmark run does not disturb an in-progress screening. Every run is stored under the corpus and model that produced it.",
    ),
  );

  const corpusRow = el("div", "row g-3 mb-3");
  const corpusField = el("div", "col-lg-6");
  corpusField.appendChild(el("label", "form-label small text-muted", "Corpus"));
  const corpusSelect = el("select", "form-select") as HTMLSelectElement;
  for (const entry of corpora) {
    const option = el(
      "option",
      "",
      `${escapeHtml(entry.name)}${entry.isDefault ? " (default)" : ""} — ${entry.files} resumes, ${entry.pairs} comparisons`,
    ) as HTMLOptionElement;
    option.value = entry.name;
    option.selected = entry.isDefault;
    corpusSelect.appendChild(option);
  }
  corpusField.appendChild(corpusSelect);
  corpusRow.appendChild(corpusField);
  body.appendChild(corpusRow);

  const summaryHost = el("div", "mb-3");
  body.appendChild(summaryHost);

  function paintCorpus(): Corpus {
    const chosen = corpora.find((c) => c.name === corpusSelect.value) ?? corpora[0];
    summaryHost.innerHTML = "";
    summaryHost.appendChild(corpusSummary(chosen));
    return chosen;
  }
  let selectedCorpus = paintCorpus();

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

  // The position description drives every verdict, so it is inspectable and
  // editable here exactly as it is during screening — same component, same
  // save path — rather than requiring a trip to the screening page.
  const checklistHost = el("div", "mb-3");
  body.appendChild(checklistHost);

  function paintChecklist(): void {
    checklistHost.innerHTML = "";
    const screeningId = select.value;
    if (!screeningId) return;

    const details = el("details", "border rounded p-2");
    details.appendChild(
      el(
        "summary",
        "fw-semibold small",
        "Position description — required and preferred qualifications (click to view and edit)",
      ),
    );
    const inner = el("div", "pt-3");
    details.appendChild(inner);
    checklistHost.appendChild(details);

    details.addEventListener(
      "toggle",
      () => {
        if (!details.open || inner.dataset.loaded) return;
        inner.dataset.loaded = "1";
        inner.innerHTML = '<div class="text-muted small">Loading…</div>';
        void getQualifications(screeningId)
          .then((payload) => {
            inner.innerHTML = "";
            const intake = new JdIntake(inner, screeningId, () => {
              notify("Qualification list saved — the next audit run uses it.", "success");
            });
            intake.load(payload.qualifications, payload.confirmed);
          })
          .catch((error) => {
            inner.innerHTML = "";
            notify(`Could not load the qualification list: ${(error as Error).message}`, "danger");
          });
      },
    );
  }
  select.addEventListener("change", paintChecklist);
  paintChecklist();

  const run = el("button", "btn btn-primary", "Run bias audit") as HTMLButtonElement;
  run.disabled = usable.length === 0;
  body.appendChild(run);

  const progress = el("div", "mt-3 d-none");
  body.appendChild(progress);

  corpusSelect.addEventListener("change", () => {
    selectedCorpus = paintCorpus();
    updateRunState();
  });
  updateRunState();

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
      updateRunState();
    }
  }

  /** One place decides whether a run is possible; several things can veto it,
   *  and each of them used to overwrite the others' answer. */
  function updateRunState(): void {
    const noPairs = selectedCorpus.pairs === 0;
    run.disabled = usable.length === 0 || noPairs;
    run.title = noPairs
      ? `The ${selectedCorpus.name} corpus has no baseline/variant pairs to compare`
      : usable.length === 0
        ? "No screening has a qualification list yet"
        : "";
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
      const started = await startAudit(
        select.value,
        providerSelect.value,
        modelSelect.value,
        corpusSelect.value,
      );
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
    const view = el("button", "btn btn-sm btn-outline-primary me-1", "View") as HTMLButtonElement;
    view.addEventListener("click", () => void renderRun(results, run.id));

    const remove = el("button", "btn btn-sm btn-outline-danger border-0", "Delete") as HTMLButtonElement;
    remove.title = "Delete this run and the scored corpus it stored";
    remove.addEventListener("click", async () => {
      if (
        !window.confirm(
          `Delete the ${run.model} run from ${formatDate(run.created_at)}?\n\n` +
            "This also removes the scored copy of the corpus it kept, and cannot be undone.",
        )
      ) {
        return;
      }
      const done = busy(remove, "");
      try {
        await deleteAudit(run.id);
        row.remove();
        results.innerHTML = "";
        notify("Run deleted.", "success");
      } catch (error) {
        done();
        notify(`Could not delete that run: ${(error as Error).message}`, "danger");
      }
    });

    row.lastElementChild!.append(view, remove);
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

  if (!run.levels?.length) {
    // A run over a corpus with no manifest — the old SWE_pdf one — has no
    // skill levels to group by, so there is nothing this view can show.
    container.appendChild(
      el(
        "div",
        "alert alert-secondary",
        `This run used the <code>${escapeHtml(run.corpus || "unknown")}</code> corpus, which has no
         skill levels defined, so it cannot be grouped by experience. Re-run against a corpus
         that declares them, or delete this run.`,
      ),
    );
    return;
  }

  container.appendChild(headline(run));
  container.appendChild(groupedView(run.levels));
}

function headline(run: AuditRun): HTMLElement {
  const summary = run.summary;
  const flips = summary.advancementFlips ?? [];
  const drifts = summary.coverageDepartures ?? [];
  const banner = el("div", `alert ${flips.length ? "alert-danger" : drifts.length ? "alert-warning" : "alert-success"}`);

  banner.appendChild(
    el(
      "h5",
      "alert-heading",
      flips.length === 0
        ? "Every class advanced as its experience level expects"
        : `${flips.length} class${flips.length === 1 ? "" : "es"} did not advance as expected`,
    ),
  );

  if (flips.length) {
    banner.appendChild(
      el(
        "ul",
        "mb-2",
        flips
          .map(
            (flip) =>
              `<li><strong>${escapeHtml(flip.label)}</strong> at <strong>${escapeHtml(flip.level)}</strong>
                 should ${escapeHtml(flip.expected)} — ${escapeHtml(flip.candidates.join(", "))} did not.</li>`,
          )
          .join(""),
      ),
    );
  }

  banner.appendChild(
    el(
      "p",
      "mb-1",
      drifts.length === 0
        ? `All <strong>${summary.groups}</strong> class groups met their level's designed coverage exactly.`
        : `<strong>${drifts.length}</strong> of ${summary.groups} class groups departed from their level's designed coverage: ` +
          drifts
            .map(
              (drift) =>
                `${escapeHtml(drift.label)} at ${escapeHtml(drift.level)} (${drift.actual} vs ${drift.expected})`,
            )
            .join("; ") +
          ".",
    ),
  );

  banner.appendChild(
    el(
      "p",
      "mb-0 small",
      `Model: <code>${escapeHtml(run.provider)}/${escapeHtml(run.model)}</code> · corpus
       <code>${escapeHtml(run.corpus)}</code>. Every resume within an experience level satisfies
       identical qualifications, so any difference between classes is the model reading the same
       claims differently depending on the name attached to them.`,
    ),
  );
  return banner;
}

/** Candidates grouped by experience level, then by protected class.
 *
 * Every resume within a level satisfies exactly the same qualifications, so
 * the whole column should read the same number. Anywhere it doesn't, the model
 * scored identical claims differently depending on whose name was on them.
 */
function groupedView(levels: AuditLevel[]): HTMLElement {
  const card = el("div", "card mb-3");
  card.appendChild(
    el(
      "div",
      "card-header fw-semibold d-flex justify-content-between align-items-center",
      `<span>Results by experience level and class</span>
       <span class="text-muted small fw-normal">every resume in a level has identical qualifications</span>`,
    ),
  );
  const body = el("div", "card-body");

  for (const level of levels) {
    const shouldPass = level.expectedStage1 === "pass";
    const section = el("div", "mb-4");
    section.appendChild(
      el(
        "h6",
        "mb-1",
        `<span class="text-capitalize">${escapeHtml(level.level)}</span>
         <span class="text-muted fw-normal small">
           — designed as ${level.expectedRequired}/${level.requiredTotal} required,
           ${level.expectedPreferred}/${level.preferredTotal} preferred,
           should ${escapeHtml(level.expectedStage1 ?? "")} stage 1
         </span>`,
      ),
    );

    const table = el("table", "table table-sm align-middle mb-0");
    table.innerHTML = `
      <thead><tr>
        <th style="width:34%">Class</th>
        <th class="text-center">Required met</th>
        <th class="text-center">Preferred met</th>
        <th class="text-center">Advanced</th>
        <th>Candidates</th>
      </tr></thead>`;
    const tbody = el("tbody");

    for (const group of level.classes) {
      const isReference = group.attribute === "baseline";
      // Off-expectation on required coverage, or an advancement outcome that
      // contradicts the level's design — the two things worth the eye.
      const offCoverage =
        level.expectedRequired !== null && group.meanRequired !== level.expectedRequired;
      const offOutcome = shouldPass
        ? group.passed < group.total
        : group.passed > 0;

      const row = el("tr", offOutcome ? "table-danger" : offCoverage ? "table-warning" : "");
      const label = isReference
        ? "<strong>Unmarked</strong> <span class='text-muted small'>(reference)</span>"
        : `<span class="text-muted small">${escapeHtml(group.attributeLabel)}</span> — ${escapeHtml(group.value)}`;

      row.innerHTML = `
        <td>${label}</td>
        <td class="text-center">${group.meanRequired}<span class="text-muted">/${level.requiredTotal}</span></td>
        <td class="text-center">${group.meanPreferred}<span class="text-muted">/${level.preferredTotal}</span></td>
        <td class="text-center">${
          offOutcome
            ? `<span class="badge text-bg-danger">${group.passed}/${group.total}</span>`
            : `<span class="badge text-bg-light border">${group.passed}/${group.total}</span>`
        }</td>
        <td class="small text-muted">${group.candidates
          .map((c) => escapeHtml(c.name))
          .join(", ")}</td>`;
      tbody.appendChild(row);
    }

    table.appendChild(tbody);
    section.appendChild(table);
    body.appendChild(section);
  }

  body.appendChild(
    el(
      "p",
      "text-muted small mb-0",
      "Rows are flagged amber where average required coverage differs from the level's design, and red where the advancement outcome contradicts it. With one candidate per class per level, a single row is suggestive rather than conclusive — look for a direction that repeats across levels.",
    ),
  );

  card.appendChild(body);
  return card;
}

