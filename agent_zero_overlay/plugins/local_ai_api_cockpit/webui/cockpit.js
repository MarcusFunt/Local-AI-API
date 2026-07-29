import { callJsonApi } from "/js/api.js";

const cockpit = {
  message: "Loading local-only workspace evidence…",
  workspaces: [],
  candidate: null,
  evidence: "",
  async call(tool, arguments = {}) {
    return callJsonApi("/api/plugins/local_ai_api_cockpit/status", { tool, arguments });
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
    }
  },
  async inspect(taskId) {
    try {
      const payload = await this.call("task_report", { task_id: taskId });
      this.evidence = JSON.stringify(payload?.result ?? payload, null, 2);
    } catch (error) {
      this.evidence = `Could not load evidence: ${error.message}`;
    }
  },
  async create() {
    const taskId = window.prompt("Disposable workspace ID (lowercase letters, numbers, hyphens):");
    if (!taskId) return;
    await this.action("create_workspace", { task_id: taskId });
  },
  async action(tool, arguments) {
    try {
      const payload = await this.call(tool, arguments);
      this.evidence = JSON.stringify(payload?.result ?? payload, null, 2);
      this.message = "Action recorded. Bounded runs do not edit files by themselves.";
      await this.refresh();
    } catch (error) {
      this.message = `Action failed: ${error.message}`;
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

globalThis.localAiApiCockpit = cockpit;
cockpit.refresh();
