import { callJsonApi } from "/js/api.js";

const UPDATE_EVENT = "local-ai-api-cockpit:update";

function publishUpdate() {
  globalThis.dispatchEvent(new CustomEvent(UPDATE_EVENT));
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
      const content = payload?.result?.result?.content || [];
      const text = content.find((item) => item.type === "text")?.text || "[]";
      this.workspaces = JSON.parse(text);
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
};

function cockpitView() {
  return {
    message: cockpit.message,
    workspaces: [],
    candidate: null,
    evidence: "",
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
  };
}

globalThis.localAiApiCockpit = cockpit;
globalThis.localAiApiCockpitView = cockpitView;
cockpit.refresh();
