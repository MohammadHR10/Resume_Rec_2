/** Provider and model selection.
 *
 * Every evaluation, chat session and audit run is tagged with whatever is
 * chosen here, so comparing two models means running the same screening twice
 * under different settings — there is deliberately no side-by-side diff view.
 */

import { getConfig, listModels, saveConfig, testProvider } from "./api.ts";
import type { AppConfig } from "./types.ts";
import { busy, el, escapeHtml, notify } from "./ui.ts";

export async function renderConfigPage(root: HTMLElement): Promise<void> {
  root.innerHTML = '<div class="text-muted">Loading configuration…</div>';
  let config: AppConfig;
  try {
    config = await getConfig();
  } catch (error) {
    root.innerHTML = "";
    notify(`Could not load the configuration: ${(error as Error).message}`, "danger");
    return;
  }

  root.innerHTML = "";
  root.appendChild(el("h4", "mb-1", "Configuration"));
  root.appendChild(
    el(
      "p",
      "text-muted",
      "Choose which provider and model run evaluations, chat and bias audits. Credentials live in the environment; only the selection is stored.",
    ),
  );

  const layout = el("div", "row g-3");
  layout.appendChild(providerCard(config));
  layout.appendChild(chatCard(config));
  layout.appendChild(thresholdCard(config));
  root.appendChild(layout);
}

function providerCard(config: AppConfig): HTMLElement {
  const column = el("div", "col-lg-6");
  const card = el("div", "card h-100");
  card.appendChild(el("div", "card-header fw-semibold", "Active provider and model"));
  const body = el("div", "card-body");

  const providerSelect = el("select", "form-select") as HTMLSelectElement;
  for (const provider of config.providers) {
    const option = el("option", "", escapeHtml(provider.label)) as HTMLOptionElement;
    option.value = provider.name;
    option.selected = provider.name === config.provider;
    if (!provider.configured) option.textContent += " — not configured";
    providerSelect.appendChild(option);
  }
  body.appendChild(labelled("Provider", providerSelect));

  const status = el("div", "small text-muted mb-3");
  const describe = (name: string) => {
    const provider = config.providers.find((entry) => entry.name === name);
    if (!provider) return;
    status.innerHTML = provider.configured
      ? `Endpoint: <code>${escapeHtml(provider.baseUrl)}</code>`
      : '<span class="text-warning-emphasis">No base URL or token in the environment — set them in <code>.env</code> and restart.</span>';
  };
  describe(config.provider);
  body.appendChild(status);

  const modelSelect = el("select", "form-select") as HTMLSelectElement;
  const modelHint = el("div", "form-text", "");
  body.appendChild(labelled("Model", modelSelect));
  body.appendChild(modelHint);

  async function loadModels(providerName: string): Promise<void> {
    modelSelect.innerHTML = '<option value="">Loading…</option>';
    modelSelect.disabled = true;
    try {
      const result = await listModels(providerName);
      modelSelect.innerHTML = "";
      const blank = el("option", "", "— select a model —") as HTMLOptionElement;
      blank.value = "";
      modelSelect.appendChild(blank);
      for (const model of result.models) {
        const option = el("option", "", escapeHtml(model)) as HTMLOptionElement;
        option.value = model;
        option.selected = model === config.model;
        modelSelect.appendChild(option);
      }
      // A gateway that will not list its deployments should not block the app;
      // whatever is already configured stays selectable.
      if (result.models.length === 0) {
        modelHint.textContent = result.configured
          ? "This provider did not return a model list. Type a model id below."
          : "Provider not configured.";
        const manual = el("input", "form-control mt-2") as HTMLInputElement;
        manual.placeholder = "Model id";
        manual.value = config.model;
        manual.addEventListener("change", () => {
          const option = el("option", "", escapeHtml(manual.value)) as HTMLOptionElement;
          option.value = manual.value;
          option.selected = true;
          modelSelect.appendChild(option);
        });
        modelHint.appendChild(manual);
      } else {
        modelHint.textContent = `${result.models.length} model(s) available.`;
      }
    } catch (error) {
      modelSelect.innerHTML = "";
      modelHint.textContent = `Could not list models: ${(error as Error).message}`;
    } finally {
      modelSelect.disabled = false;
    }
  }

  providerSelect.addEventListener("change", () => {
    describe(providerSelect.value);
    void loadModels(providerSelect.value);
  });
  void loadModels(config.provider);

  const buttons = el("div", "d-flex gap-2 mt-3");
  const save = el("button", "btn btn-primary", "Save") as HTMLButtonElement;
  save.addEventListener("click", async () => {
    const done = busy(save, "Saving…");
    try {
      await saveConfig({ provider: providerSelect.value, model: modelSelect.value });
      notify(`Now using ${providerSelect.value} / ${modelSelect.value || "(no model)"}.`, "success");
    } catch (error) {
      notify(`Could not save: ${(error as Error).message}`, "danger");
    } finally {
      done();
    }
  });

  const test = el("button", "btn btn-outline-secondary", "Test connection") as HTMLButtonElement;
  test.addEventListener("click", async () => {
    const done = busy(test, "Testing…");
    try {
      const result = await testProvider(providerSelect.value);
      notify(
        `${providerSelect.value}: ${result.detail}`,
        result.ok ? "success" : "warning",
      );
    } catch (error) {
      notify(`Connection test failed: ${(error as Error).message}`, "danger");
    } finally {
      done();
    }
  });

  buttons.append(save, test);
  body.appendChild(buttons);
  card.appendChild(body);
  column.appendChild(card);
  return column;
}

function chatCard(config: AppConfig): HTMLElement {
  const column = el("div", "col-lg-6");
  const card = el("div", "card h-100");
  card.appendChild(el("div", "card-header fw-semibold", "Chat mode for this model"));
  const body = el("div", "card-body");

  body.appendChild(
    el(
      "p",
      "text-muted small",
      "Harness mode gives a CLI agent the analysis tools directly. Structured mode runs the same tools from the backend on the model's behalf — the fallback for models that cannot drive a CLI. A failed harness turn degrades to structured automatically.",
    ),
  );

  const modeSelect = el("select", "form-select") as HTMLSelectElement;
  for (const mode of ["harness", "structured"]) {
    const option = el("option", "", mode) as HTMLOptionElement;
    option.value = mode;
    option.selected = mode === config.chatMode;
    modeSelect.appendChild(option);
  }
  body.appendChild(labelled(`Mode for ${config.provider} / ${config.model || "(no model)"}`, modeSelect));

  const cliSelect = el("select", "form-select") as HTMLSelectElement;
  for (const adapter of config.adapters) {
    const option = el(
      "option",
      "",
      `${adapter.name}${adapter.available ? "" : " — not installed"}`,
    ) as HTMLOptionElement;
    option.value = adapter.name;
    option.selected = adapter.name === config.harnessCli;
    cliSelect.appendChild(option);
  }
  body.appendChild(labelled("Harness CLI", cliSelect));

  const save = el("button", "btn btn-primary mt-3", "Save chat settings") as HTMLButtonElement;
  save.addEventListener("click", async () => {
    const done = busy(save, "Saving…");
    try {
      await saveConfig({ chatMode: modeSelect.value, harnessCli: cliSelect.value });
      notify("Chat settings saved.", "success");
    } catch (error) {
      notify(`Could not save: ${(error as Error).message}`, "danger");
    } finally {
      done();
    }
  });
  body.appendChild(save);

  card.appendChild(body);
  column.appendChild(card);
  return column;
}

function thresholdCard(config: AppConfig): HTMLElement {
  const column = el("div", "col-12");
  const card = el("div", "card");
  card.appendChild(el("div", "card-header fw-semibold", "Bias audit thresholds"));
  const body = el("div", "card-body");
  body.appendChild(
    el(
      "p",
      "text-muted small",
      "What counts as negligible bias. An audit passes when no variant flips a stage outcome and the mean absolute coverage change stays at or below the limit.",
    ),
  );

  const row = el("div", "row g-3");
  const stageFlips = numberField(
    "Maximum stage-outcome flips",
    config.auditThresholds.max_stage_flips ?? 0,
    1,
  );
  const coverage = numberField(
    "Maximum mean coverage delta (qualifications)",
    config.auditThresholds.max_mean_coverage_delta ?? 0.5,
    0.1,
  );
  row.append(stageFlips.wrapper, coverage.wrapper);
  body.appendChild(row);

  const save = el("button", "btn btn-primary mt-3", "Save thresholds") as HTMLButtonElement;
  save.addEventListener("click", async () => {
    const done = busy(save, "Saving…");
    try {
      await saveConfig({
        auditThresholds: {
          max_stage_flips: Number(stageFlips.input.value),
          max_mean_coverage_delta: Number(coverage.input.value),
        },
      });
      notify("Audit thresholds saved.", "success");
    } catch (error) {
      notify(`Could not save: ${(error as Error).message}`, "danger");
    } finally {
      done();
    }
  });
  body.appendChild(save);

  card.appendChild(body);
  column.appendChild(card);
  return column;
}

function labelled(text: string, control: HTMLElement): HTMLElement {
  const wrapper = el("div", "mb-3");
  wrapper.appendChild(el("label", "form-label", escapeHtml(text)));
  wrapper.appendChild(control);
  return wrapper;
}

function numberField(text: string, value: number, step: number) {
  const wrapper = el("div", "col-md-6");
  wrapper.appendChild(el("label", "form-label", escapeHtml(text)));
  const input = el("input", "form-control") as HTMLInputElement;
  input.type = "number";
  input.min = "0";
  input.step = String(step);
  input.value = String(value);
  wrapper.appendChild(input);
  return { wrapper, input };
}
