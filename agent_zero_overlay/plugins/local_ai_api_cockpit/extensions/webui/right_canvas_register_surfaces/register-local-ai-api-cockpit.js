export default async function registerLocalAiApiCockpit(surfaces) {
  surfaces.registerSurface({
    id: "local-ai-api-cockpit",
    title: "Workspaces",
    icon: "developer_board",
    order: 90,
    async open() {
      const panel = document.querySelector('[data-surface-id="local-ai-api-cockpit"]');
      if (!panel) throw new Error("Workspace cockpit panel did not mount.");
      await globalThis.localAiApiCockpit?.refresh?.();
    },
  });
}
