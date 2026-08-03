import { callJsonApi } from "/js/api.js";

const UPDATE_EVENT = "local-ai-api-cockpit:update";

function publishUpdate() {
  globalThis.dispatchEvent(new CustomEvent(UPDATE_EVENT));
}

function mcpToolResult(payload) {
  const content = payload?.result?.result?.content;
  const text = Array.isArray(content) ? content.find((item) => item.type === "text")?.text : null;
  return text ? JSON.parse(text) : null;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function runState(workspace) {
  return typeof workspace?.run_state === "string" ? workspace.run_state : "not_started";
}

function workspaceActionsHtml(workspace) {
  const taskId = escapeHtml(workspace.task_id);
  const lifecycleState = workspace.state;
  const state = runState(workspace);
  const button = (action, label, primary = false, title = "") => `
    <button class="local-ai-api-cockpit-button${primary ? " primary" : ""}" type="button" data-cockpit-action="${action}" data-task-id="${taskId}"${title ? ` title="${title}"` : ""}>${label}</button>
  `;
  const actions = [button("evidence", "Evidence")];

  if (lifecycleState === "archived") return actions.join("");
  if (state === "not_started" && lifecycleState === "active") {
    actions.push(button("start", "Start", true));
  }
  if (state !== "evaluating") actions.push(button("preview", "Preview"));
  if (state === "running") {
    actions.push(button("evaluate", "Evaluate"), button("pause", "Pause"), button("stop", "Stop"));
  }
  if (state === "paused") {
    actions.push(button("resume", "Resume", true), button("stop", "Stop"));
  }
  if (["review_ready", "stopped"].includes(state)) {
    actions.push(button("archive", "Archive…", false, "Ask for confirmation before archiving this workspace."));
  }
  return actions.join("");
}

const cockpit = {
  message: "Loading local-only workspace evidence…",
  workspaces: [],
  candidate: null,
  evidence: "",
  async call(tool, params = {}) {
    return callJsonApi("/api/plugins/local_ai_api_cockpit/status", { tool, arguments: params });
  },
  async refresh() {
    try {
      const payload = await this.call("list_workspaces");
      const workspaces = mcpToolResult(payload);
      if (!Array.isArray(workspaces)) throw new Error("Workspace list was not an array.");
      this.workspaces = await Promise.all(workspaces.map(async (workspace) => {
        if (workspace?.state === "archived") return { ...workspace, run_state: "archived" };
        try {
          const autonomous = mcpToolResult(await this.call("autonomous_status", { task_id: workspace.task_id }));
          return { ...workspace, run_state: autonomous?.state ?? "not_started" };
        } catch {
          // A workspace need not have an autonomous run. It is still safe to
          // present its lifecycle evidence and the single valid start action.
          return { ...workspace, run_state: "not_started" };
        }
      }));
      const candidate = await this.call("candidate_status");
      this.candidate = candidate?.result ?? null;
      this.message = `${this.workspaces.length} workspace(s), candidate ${this.candidate?.status ?? "not run"}.`;
    } catch (error) {
      this.message = `Could not load workspaces: ${error.message}`;
    } finally {
      publishUpdate();
    }
  },
  async inspect(taskId) {
    try {
      const payload = await this.call("task_report", { task_id: taskId });
      this.evidence = JSON.stringify(payload?.result ?? payload, null, 2);
    } catch (error) {
      this.evidence = `Could not load evidence: ${error.message}`;
    } finally {
      publishUpdate();
    }
  },
  async create() {
    const taskId = window.prompt("Disposable workspace ID (lowercase letters, numbers, hyphens):");
    if (!taskId) return;
    await this.action("create_workspace", { task_id: taskId });
  },
  async action(tool, params) {
    try {
      const payload = await this.call(tool, params);
      this.evidence = JSON.stringify(payload?.result ?? payload, null, 2);
      this.message = "Action recorded. Bounded runs do not edit files by themselves.";
      await this.refresh();
    } catch (error) {
      this.message = `Action failed: ${error.message}`;
    } finally {
      publishUpdate();
    }
  },
  async pause(taskId) {
    const reason = window.prompt("Why pause this bounded run?") || "Paused from cockpit.";
    await this.action("pause_autonomous_run", { task_id: taskId, reason });
  },
  async stop(taskId) {
    const reason = window.prompt("Why stop this bounded run?") || "Stopped from cockpit.";
    await this.action("stop_autonomous_run", { task_id: taskId, reason });
  },
  async archive(taskId) {
    if (!window.confirm(`Archive workspace '${taskId}'? Its review evidence will remain available, but the live workspace will be removed.`)) return;
    await this.action("archive_workspace", { task_id: taskId });
  },
};

function cockpitView() {
  return {
    message: cockpit.message,
    workspaces: [],
    candidate: null,
    evidence: "",
    embedded: globalThis.top !== globalThis,
    _onUpdate: null,
    sync() {
      const current = globalThis.localAiApiCockpit;
      if (!current) return;
      this.message = current.message;
      this.workspaces = Array.isArray(current.workspaces) ? [...current.workspaces] : [];
      this.candidate = current.candidate;
      this.evidence = current.evidence;
    },
    connect() {
      this.sync();
      this._onUpdate = () => this.sync();
      globalThis.addEventListener(UPDATE_EVENT, this._onUpdate);
    },
    dispose() {
      if (this._onUpdate) globalThis.removeEventListener(UPDATE_EVENT, this._onUpdate);
    },
    async refresh() {
      await globalThis.localAiApiCockpit?.refresh?.();
    },
    async inspect(taskId) {
      await globalThis.localAiApiCockpit?.inspect?.(taskId);
    },
    async create() {
      await globalThis.localAiApiCockpit?.create?.();
    },
    async action(tool, params) {
      await globalThis.localAiApiCockpit?.action?.(tool, params);
    },
    async pause(taskId) {
      await globalThis.localAiApiCockpit?.pause?.(taskId);
    },
    async stop(taskId) {
      await globalThis.localAiApiCockpit?.stop?.(taskId);
    },
    async archive(taskId) {
      await globalThis.localAiApiCockpit?.archive?.(taskId);
    },
  };
}

function mountStandaloneCockpit() {
  // Agent Zero supplies Alpine for embedded surfaces.  Opening the plugin URL
  // directly does not, so bind the same controls with a small local fallback.
  if (globalThis.Alpine) return;
  const root = document.querySelector(".local-ai-api-cockpit");
  if (!root) return;

  root.removeAttribute("x-data");
  root.removeAttribute("x-init");
  root.removeAttribute("x-destroy");
  root.querySelector(".local-ai-api-cockpit-title")?.removeAttribute("x-show");
  const status = root.querySelector(".local-ai-api-cockpit-status");
  const candidate = root.querySelector(".local-ai-api-cockpit-candidate");
  const workspaces = root.querySelector(".local-ai-api-cockpit-workspaces");
  const evidence = root.querySelector(".local-ai-api-cockpit-evidence");

  const render = () => {
    if (status) status.textContent = cockpit.message;
    if (candidate) {
      candidate.hidden = !cockpit.candidate;
      candidate.textContent = cockpit.candidate?.message ?? "";
    }
    if (workspaces) {
      workspaces.innerHTML = cockpit.workspaces.length
        ? cockpit.workspaces.map((workspace) => `
            <article class="local-ai-api-cockpit-workspace">
              <div class="local-ai-api-cockpit-workspace-header">
                <strong>${escapeHtml(workspace.task_id)}</strong>
                <span class="local-ai-api-cockpit-state">${escapeHtml(runState(workspace) === "not_started" ? "ready to start" : runState(workspace))}</span>
              </div>
              <div class="local-ai-api-cockpit-actions" aria-label="Workspace actions">
                ${workspaceActionsHtml(workspace)}
              </div>
            </article>
          `).join("")
        : '<p class="local-ai-api-cockpit-empty">No disposable workspaces exist yet. Create one only when you need bounded, reviewable work.</p>';
    }
    if (evidence) {
      evidence.hidden = !cockpit.evidence;
      evidence.textContent = cockpit.evidence;
    }
  };

  root.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-cockpit-action]");
    const control = event.target.closest("[data-cockpit-control]");
    if (control?.dataset.cockpitControl === "create") await cockpit.create();
    if (control?.dataset.cockpitControl === "refresh") await cockpit.refresh();
    if (!button) return;
    const taskId = button.dataset.taskId;
    const action = button.dataset.cockpitAction;
    if (action === "evidence") await cockpit.inspect(taskId);
    if (action === "start") await cockpit.action("start_autonomous_run", { task_id: taskId });
    if (action === "evaluate") await cockpit.action("evaluate_workspace", { task_id: taskId });
    if (action === "preview") await cockpit.action("preview_workspace", { task_id: taskId });
    if (action === "pause") await cockpit.pause(taskId);
    if (action === "resume") await cockpit.action("resume_autonomous_run", { task_id: taskId });
    if (action === "stop") await cockpit.stop(taskId);
    if (action === "archive") await cockpit.archive(taskId);
  });

  const onUpdate = () => render();
  globalThis.addEventListener(UPDATE_EVENT, onUpdate);
  globalThis.addEventListener("pagehide", () => globalThis.removeEventListener(UPDATE_EVENT, onUpdate), { once: true });
  render();
}

globalThis.localAiApiCockpit = cockpit;
globalThis.localAiApiCockpitView = cockpitView;
mountStandaloneCockpit();
cockpit.refresh();
