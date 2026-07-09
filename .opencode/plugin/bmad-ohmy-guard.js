"use strict";

const BMAD_POLICY = ".opencode/bmad-superpowers-policy.md";
const OHMY_PRIMARY_AGENTS = new Set(["sisyphus", "prometheus", "atlas"]);
const SUPPLEMENTAL_SUBAGENTS = ["oracle", "librarian", "explore", "multimodal-looker"];

module.exports = async function bmadOhMyGuard() {
  return {
    config(config) {
      const instructions = Array.isArray(config.instructions) ? config.instructions : [];
      if (!instructions.includes(BMAD_POLICY)) {
        instructions.push(BMAD_POLICY);
      }
      config.instructions = instructions;

      if (config.default_agent && OHMY_PRIMARY_AGENTS.has(String(config.default_agent).toLowerCase())) {
        delete config.default_agent;
      }

      const existingPermission =
        config.permission && typeof config.permission === "object" && !Array.isArray(config.permission)
          ? config.permission
          : {};
      config.permission = {
        ...existingPermission,
        external_directory: "ask",
        webfetch: "ask",
        delegate_task: "allow",
        call_omo_agent: "allow",
        background_output: "allow",
        background_cancel: "ask"
      };

      const agents = config.agent && typeof config.agent === "object" && !Array.isArray(config.agent) ? config.agent : {};
      for (const name of SUPPLEMENTAL_SUBAGENTS) {
        const agent = agents[name];
        if (!agent || typeof agent !== "object" || Array.isArray(agent)) continue;

        agent.mode = "subagent";
        const permission = agent.permission && typeof agent.permission === "object" && !Array.isArray(agent.permission)
          ? agent.permission
          : {};
        agent.permission = {
          ...permission,
          edit: "deny",
          external_directory: "ask"
        };
      }
      config.agent = agents;
    }
  };
};
