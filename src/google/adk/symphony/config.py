# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import yaml
from typing import Any, Dict, Optional, Tuple

from google.adk.symphony.models import WorkflowDefinition


class ConfigLayer:
    def __init__(self, workflow_path: str = "WORKFLOW.md"):
        self.workflow_path = workflow_path
        self._config: Dict[str, Any] = {}
        self._prompt_template: str = ""

    def load(self) -> WorkflowDefinition:
        """Loads and parses the WORKFLOW.md file."""
        if not os.path.exists(self.workflow_path):
            raise FileNotFoundError(f"Workflow file not found: {self.workflow_path}")

        with open(self.workflow_path, "r", encoding="utf-8") as f:
            content = f.read()

        config, prompt = self._parse_workflow(content)
        self._config = config
        self._prompt_template = prompt

        return WorkflowDefinition(config=self._config, prompt_template=self._prompt_template)

    def _parse_workflow(self, content: str) -> Tuple[Dict[str, Any], str]:
        """Parses the YAML front matter and prompt body."""
        lines = content.splitlines()
        if not lines or not lines[0].startswith("---"):
            return {}, content.strip()

        yaml_lines = []
        prompt_lines = []
        in_yaml = True

        for line in lines[1:]:
            if in_yaml and line.startswith("---"):
                in_yaml = False
                continue

            if in_yaml:
                yaml_lines.append(line)
            else:
                prompt_lines.append(line)

        try:
            config = yaml.safe_load("\n".join(yaml_lines))
            if not isinstance(config, dict):
                config = {}
        except yaml.YAMLError:
            config = {}

        return config, "\n".join(prompt_lines).strip()

    def _resolve_env(self, value: Any) -> Any:
        """Resolves environment variables in string values."""
        if isinstance(value, str) and value.startswith("$"):
            env_var = value[1:]
            return os.environ.get(env_var, "")
        return value

    # --- Typed Getters ---

    def get_tracker_kind(self) -> str:
        return self._config.get("tracker", {}).get("kind", "")

    def get_tracker_endpoint(self) -> str:
        return self._resolve_env(self._config.get("tracker", {}).get("endpoint", "https://api.linear.app/graphql"))

    def get_tracker_api_key(self) -> str:
        return self._resolve_env(self._config.get("tracker", {}).get("api_key", ""))

    def get_tracker_project_slug(self) -> str:
        return self._resolve_env(self._config.get("tracker", {}).get("project_slug", ""))

    def get_tracker_active_states(self) -> list:
        states = self._config.get("tracker", {}).get("active_states", ["Todo", "In Progress"])
        if isinstance(states, str):
            states = [s.strip() for s in states.split(",")]
        return states

    def get_tracker_terminal_states(self) -> list:
        states = self._config.get("tracker", {}).get("terminal_states", ["Closed", "Cancelled", "Canceled", "Duplicate", "Done"])
        if isinstance(states, str):
            states = [s.strip() for s in states.split(",")]
        return states

    def get_poll_interval_ms(self) -> int:
        return int(self._config.get("polling", {}).get("interval_ms", 30000))

    def get_workspace_root(self) -> str:
        root = self._resolve_env(self._config.get("workspace", {}).get("root", os.path.join(os.environ.get("TMPDIR", "/tmp"), "symphony_workspaces")))
        return os.path.expanduser(root)

    def get_hooks_after_create(self) -> Optional[str]:
        return self._config.get("hooks", {}).get("after_create")

    def get_hooks_before_run(self) -> Optional[str]:
        return self._config.get("hooks", {}).get("before_run")

    def get_hooks_after_run(self) -> Optional[str]:
        return self._config.get("hooks", {}).get("after_run")

    def get_hooks_before_remove(self) -> Optional[str]:
        return self._config.get("hooks", {}).get("before_remove")

    def get_hooks_timeout_ms(self) -> int:
        val = int(self._config.get("hooks", {}).get("timeout_ms", 60000))
        return val if val > 0 else 60000

    def get_agent_max_concurrent_agents(self) -> int:
        return int(self._config.get("agent", {}).get("max_concurrent_agents", 10))

    def get_agent_max_turns(self) -> int:
        return int(self._config.get("agent", {}).get("max_turns", 20))

    def get_agent_max_retry_backoff_ms(self) -> int:
        return int(self._config.get("agent", {}).get("max_retry_backoff_ms", 300000))

    def get_agent_max_concurrent_agents_by_state(self) -> Dict[str, int]:
        raw = self._config.get("agent", {}).get("max_concurrent_agents_by_state", {})
        result = {}
        for k, v in raw.items():
            if isinstance(v, (int, str)) and str(v).isdigit() and int(v) > 0:
                result[k.strip().lower()] = int(v)
        return result

    def get_codex_command(self) -> str:
        return self._resolve_env(self._config.get("codex", {}).get("command", "codex app-server"))

    def get_codex_stall_timeout_ms(self) -> int:
        return int(self._config.get("codex", {}).get("stall_timeout_ms", 300000))
