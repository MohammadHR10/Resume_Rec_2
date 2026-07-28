/** AG Grid wrapper for one stage: verdict columns, promotion controls, export.
 *
 * The grid is the primary surface, so it has to behave like the spreadsheet it
 * replaces: a pinned, bold, filled header row, and sort plus filter on every
 * column — which AG Grid gives natively rather than by reimplementation.
 */

import {
  AllCommunityModule,
  ModuleRegistry,
  createGrid,
  themeQuartz,
  type ColDef,
  type GridApi,
  type ICellRendererParams,
  type ValueGetterParams,
} from "ag-grid-community";

import { exportUrl, getStage, stageAction } from "./api.ts";
import type { CandidateRow, GridAction, Qualification, Stage, StagePayload } from "./types.ts";
import { busy, el, escapeHtml, notify } from "./ui.ts";

ModuleRegistry.registerModules([AllCommunityModule]);

/** Requirement: a fixed, bold, background-coloured header row.
 *  AG Grid v34 generates its own header styles at a specificity a stylesheet
 *  cannot reliably beat, so the header is themed through the Theming API
 *  rather than by fighting it in CSS. */
export const screeningTheme = themeQuartz.withParams({
  headerBackgroundColor: "#1F3864",
  headerTextColor: "#FFFFFF",
  headerFontWeight: 600,
  headerColumnResizeHandleColor: "#5B7DB1",
  borderColor: "#DEE2E6",
  rowHoverColor: "#EEF3FB",
  selectedRowBackgroundColor: "#DCE9FB",
  fontFamily: "inherit",
});

const STAGE_TITLES: Record<Stage, string> = {
  "1": "Stage 1 — Minimum Requirements",
  "2": "Stage 2 — Preferred Qualifications",
  "3": "Stage 3 — Interview Candidates",
  rejected: "Rejected",
};

const STAGE_BLURBS: Record<Stage, string> = {
  "1": "Everyone starts here. The AI recommends a pass only when every required qualification is met — you decide who advances.",
  "2": "Ranked by preferred-qualification coverage. Promote the shortlist you want to interview.",
  "3": "The confirmed interview shortlist.",
  rejected: "Rejected candidates. Restore any of them back to stage 1.",
};

export class StageGrid {
  private api: GridApi<CandidateRow> | null = null;
  private qualifications: Qualification[] = [];
  private candidates: CandidateRow[] = [];
  private gridHost!: HTMLElement;
  private toolbar!: HTMLElement;
  private readonly root: HTMLElement;
  private readonly screeningId: string;
  private readonly stage: Stage;
  private readonly onStageCounts: (counts: Record<string, number>) => void;

  constructor(
    root: HTMLElement,
    screeningId: string,
    stage: Stage,
    onStageCounts: (counts: Record<string, number>) => void,
  ) {
    this.root = root;
    this.screeningId = screeningId;
    this.stage = stage;
    this.onStageCounts = onStageCounts;
    this.build();
  }

  private build(): void {
    this.root.innerHTML = "";
    const header = el("div", "d-flex justify-content-between align-items-start flex-wrap gap-2 mb-2");
    header.appendChild(
      el(
        "div",
        "",
        `<h5 class="mb-1">${STAGE_TITLES[this.stage]}</h5>
         <p class="text-muted small mb-0">${STAGE_BLURBS[this.stage]}</p>`,
      ),
    );
    this.toolbar = el("div", "d-flex gap-2 flex-wrap align-items-center");
    header.appendChild(this.toolbar);
    this.root.appendChild(header);

    this.gridHost = el("div", "stage-grid");
    this.root.appendChild(this.gridHost);
  }

  async refresh(): Promise<void> {
    let payload: StagePayload;
    try {
      payload = await getStage(this.screeningId, this.stage);
    } catch (error) {
      notify(`Could not load ${STAGE_TITLES[this.stage]}: ${(error as Error).message}`, "danger");
      return;
    }
    this.qualifications = payload.qualifications;
    this.candidates = payload.candidates;
    this.onStageCounts(payload.stageCounts);
    this.renderToolbar();

    if (!this.api) {
      this.api = createGrid<CandidateRow>(this.gridHost, {
        theme: screeningTheme,
        columnDefs: this.columns(),
        rowData: this.candidates,
        rowSelection: { mode: "multiRow", headerCheckbox: true },
        selectionColumnDef: { pinned: "left", width: 48, resizable: false },
        defaultColDef: {
          sortable: true,
          filter: true,
          resizable: true,
        },
        suppressDragLeaveHidesColumns: true,
        getRowId: (params) => params.data.id,
        tooltipShowDelay: 200,
        localeText: { noRowsToShow: "No candidates in this stage yet." },
      });
    } else {
      this.api.setGridOption("columnDefs", this.columns());
      this.api.setGridOption("rowData", this.candidates);
    }
  }

  // -- columns --------------------------------------------------------------

  private columns(): ColDef<CandidateRow>[] {
    const columns: ColDef<CandidateRow>[] = [
      {
        field: "rank",
        headerName: "Rank",
        width: 115,
        pinned: "left",
        sort: "asc",
        colId: "rank",
      },
      {
        field: "name",
        headerName: "Candidate",
        flex: 1,
        minWidth: 180,
        pinned: "left",
        colId: "name",
        tooltipValueGetter: (params) =>
          (params.data?.error || params.data?.summary || "") as string,
      },
      {
        headerName: "AI Recommendation",
        colId: "ai",
        width: 190,
        valueGetter: (params: ValueGetterParams<CandidateRow>) =>
          params.data?.error ? "Error" : params.data?.ai_pass ? "Pass" : "Fail",
        cellRenderer: (params: ICellRendererParams<CandidateRow>) => {
          const value = String(params.value ?? "");
          const variant =
            value === "Pass" ? "success" : value === "Error" ? "secondary" : "danger";
          return `<span class="badge text-bg-${variant}">${value}</span>`;
        },
      },
      {
        headerName: "Required Met",
        colId: "required",
        width: 145,
        comparator: (_a, _b, nodeA, nodeB) =>
          (nodeA.data?.required_met ?? 0) - (nodeB.data?.required_met ?? 0),
        valueGetter: (params: ValueGetterParams<CandidateRow>) =>
          params.data ? `${params.data.required_met}/${params.data.required_total}` : "",
      },
      {
        headerName: "Preferred Met",
        colId: "preferred",
        width: 150,
        comparator: (_a, _b, nodeA, nodeB) =>
          (nodeA.data?.preferred_met ?? 0) - (nodeB.data?.preferred_met ?? 0),
        valueGetter: (params: ValueGetterParams<CandidateRow>) =>
          params.data ? `${params.data.preferred_met}/${params.data.preferred_total}` : "",
      },
    ];

    for (const qual of this.qualifications) {
      columns.push({
        colId: `qual:${qual.id}`,
        headerName: `${qual.label}. ${qual.text}`,
        headerTooltip: `${qual.kind === "required" ? "Required" : "Preferred"}: ${qual.text}`,
        width: 145,
        wrapHeaderText: true,
        autoHeaderHeight: true,
        // Stage 2 is about preferred coverage, so required columns start hidden
        // there — the reviewer already gated on them in stage 1.
        hide: this.stage === "2" && qual.kind === "required",
        valueGetter: (params: ValueGetterParams<CandidateRow>) =>
          params.data?.verdicts?.[qual.id]?.verdict ?? "—",
        tooltipValueGetter: (params) =>
          params.data?.verdicts?.[qual.id]?.evidence || "No evidence recorded.",
        cellClass: (params) => {
          const verdict = params.data?.verdicts?.[qual.id]?.verdict;
          if (verdict === "Meets") return "verdict-meets";
          if (verdict === "Partial") return "verdict-partial";
          if (verdict === "No") return "verdict-no";
          return "verdict-missing";
        },
        cellRenderer: (params: ICellRendererParams<CandidateRow>) => {
          const entry = params.data?.verdicts?.[qual.id];
          if (!entry) return "—";
          const evidence = entry.evidence ? ` <span class="verdict-evidence-dot">•</span>` : "";
          return `${entry.verdict}${evidence}`;
        },
        onCellClicked: (params) => {
          const entry = params.data?.verdicts?.[qual.id];
          if (entry) this.showEvidence(params.data!.name, qual, entry.verdict, entry.evidence);
        },
      });
    }

    columns.push({
      headerName: "Summary",
      colId: "summary",
      flex: 1,
      minWidth: 220,
      valueGetter: (params: ValueGetterParams<CandidateRow>) =>
        params.data?.error || params.data?.summary || "",
      tooltipValueGetter: (params) => (params.data?.error || params.data?.summary || "") as string,
    });

    return columns;
  }

  private showEvidence(name: string, qual: Qualification, verdict: string, evidence: string): void {
    const host = document.getElementById("evidence-panel");
    if (!host) return;
    host.innerHTML = `
      <div class="card border-primary-subtle">
        <div class="card-header d-flex justify-content-between align-items-center">
          <span><strong>${escapeHtml(name)}</strong> — ${qual.label} <span class="badge text-bg-light border">${verdict}</span></span>
          <button class="btn btn-sm btn-close"></button>
        </div>
        <div class="card-body">
          <p class="text-muted small mb-2">${escapeHtml(qual.text)}</p>
          <blockquote class="mb-0 fst-italic">${escapeHtml(evidence || "No evidence was quoted for this verdict.")}</blockquote>
        </div>
      </div>`;
    host.querySelector("button")!.addEventListener("click", () => {
      host.innerHTML = "";
    });
  }

  // -- toolbar --------------------------------------------------------------

  private renderToolbar(): void {
    this.toolbar.innerHTML = "";

    const actions: { label: string; action: "promote" | "reject" | "restore"; variant: string }[] = [];
    if (this.stage === "1" || this.stage === "2") {
      actions.push({ label: "Promote selected", action: "promote", variant: "btn-primary" });
      actions.push({ label: "Reject selected", action: "reject", variant: "btn-outline-danger" });
    } else if (this.stage === "3") {
      actions.push({ label: "Reject selected", action: "reject", variant: "btn-outline-danger" });
    } else {
      actions.push({ label: "Restore to stage 1", action: "restore", variant: "btn-outline-secondary" });
    }

    for (const entry of actions) {
      const button = el("button", `btn btn-sm ${entry.variant}`, entry.label) as HTMLButtonElement;
      button.addEventListener("click", () => void this.applyAction(entry.action, button));
      this.toolbar.appendChild(button);
    }

    const excel = el("a", "btn btn-sm btn-outline-success", "Export Excel") as HTMLAnchorElement;
    excel.href = exportUrl(this.screeningId, this.stage, "xlsx");
    const csv = el("a", "btn btn-sm btn-outline-secondary", "CSV") as HTMLAnchorElement;
    csv.href = exportUrl(this.screeningId, this.stage, "csv");
    this.toolbar.append(excel, csv);

    const reset = el("button", "btn btn-sm btn-link", "Clear sort & filters") as HTMLButtonElement;
    reset.addEventListener("click", () => this.applyGridAction({ id: 0, type: "grid", clear: true }));
    this.toolbar.appendChild(reset);
  }

  private async applyAction(
    action: "promote" | "reject" | "restore",
    button: HTMLButtonElement,
  ): Promise<void> {
    const selected = this.api?.getSelectedRows() ?? [];
    if (selected.length === 0) {
      notify("Select at least one candidate first.", "warning");
      return;
    }

    // Overriding the AI is allowed and expected; it just has to be deliberate.
    const overrides = selected.filter((row) =>
      action === "promote" ? !row.ai_pass : action === "reject" ? row.ai_pass : false,
    );
    let note = "";
    if (overrides.length > 0 && this.stage === "1") {
      const verb = action === "promote" ? "promote" : "reject";
      const answer = window.prompt(
        `${overrides.length} of these candidates go against the AI recommendation.\n` +
          `Optionally note why you are choosing to ${verb} them (recorded in the audit trail):`,
        "",
      );
      if (answer === null) return;
      note = answer;
    }

    const done = busy(button, "Working…");
    try {
      const result = await stageAction(
        this.screeningId,
        this.stage,
        selected.map((row) => row.id),
        action,
        note,
      );
      this.onStageCounts(result.stageCounts);
      const overridden = result.moved.filter((move) => move.override).length;
      notify(
        `${result.moved.length} candidate(s) moved` +
          (overridden ? ` — ${overridden} recorded as an override of the AI.` : "."),
        "success",
      );
      await this.refresh();
      document.dispatchEvent(new CustomEvent("stages-changed"));
    } catch (error) {
      notify(`Could not move those candidates: ${(error as Error).message}`, "danger");
    } finally {
      done();
    }
  }

  // -- chat-driven grid control --------------------------------------------

  applyGridAction(action: GridAction): void {
    if (!this.api) return;
    if (action.clear) {
      this.api.applyColumnState({ defaultState: { sort: null } });
      this.api.setFilterModel(null);
    }
    if (action.sort) {
      this.api.applyColumnState({
        state: [{ colId: action.sort.column, sort: action.sort.direction }],
        defaultState: { sort: null },
      });
    }
    if (action.filters?.length) {
      const model: Record<string, unknown> = {};
      for (const filter of action.filters) {
        model[filter.column] = {
          filterType: "text",
          type: "contains",
          filter: filter.value,
        };
      }
      this.api.setFilterModel(model);
    }
  }

  destroy(): void {
    this.api?.destroy();
    this.api = null;
  }
}
