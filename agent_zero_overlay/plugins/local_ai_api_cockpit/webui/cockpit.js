import { callJsonApi } from "/js/api.js";

const cockpit = {
  message: "Loading local-only workspace evidence…",
  workspaces: [],
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
      this.message = `${this.workspaces.length} workspace(s), all local-only.`;
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
};

globalThis.localAiApiCockpit = cockpit;
cockpit.refresh();
