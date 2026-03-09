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

import asyncio
import logging
import time

from google.adk.symphony.config import ConfigLayer
from google.adk.symphony.orchestrator import Orchestrator
from google.adk.symphony.tracker import LinearTrackerClient
from google.adk.symphony.workspace import WorkspaceManager
from google.adk.symphony.runner import AgentRunner

logger = logging.getLogger(__name__)


class SymphonyApp:
    def __init__(self, workflow_path: str = "WORKFLOW.md"):
        self.config = ConfigLayer(workflow_path)
        self.config.load()

        self.tracker = LinearTrackerClient(self.config)
        self.workspace_manager = WorkspaceManager(self.config)
        self.orchestrator = Orchestrator(self.config, self.tracker, self.workspace_manager)
        self.runner = AgentRunner(self.config, self.workspace_manager)

    def startup_terminal_workspace_cleanup(self):
        """Removes workspaces for issues already in terminal states."""
        terminal_states = self.config.get_tracker_terminal_states()
        logger.info(f"Fetching terminal issues for states: {terminal_states}")
        try:
            issues = self.tracker.fetch_issues_by_states(terminal_states)
            for issue in issues:
                logger.info(f"Removing terminal workspace for issue {issue.identifier}")
                self.workspace_manager.remove_workspace(issue.identifier)
        except Exception as e:
            logger.warning(f"Failed terminal workspace cleanup: {e}")

    async def run(self):
        logger.info("Starting Symphony Service")

        if not self.orchestrator.validate_dispatch_config():
            logger.error("Startup validation failed.")
            return

        self.startup_terminal_workspace_cleanup()

        while True:
            logger.info("Tick")
            try:
                # Reload config
                self.config.load()

                # Perform the orchestrator loop
                self.orchestrator.run_tick()

                # Dispatch runners for newly running issues
                tasks = []
                for issue_id, entry in self.orchestrator.state.running.items():
                    if entry.get("worker_task") is None:
                        issue = entry["issue"]
                        attempt = entry.get("attempt", 0)

                        logger.info(f"Spawning worker for {issue.identifier} (attempt {attempt})")

                        task = asyncio.create_task(
                            self.runner.run_attempt(issue, attempt, self._orchestrator_channel)
                        )
                        entry["worker_task"] = task
                        tasks.append(task)
            except Exception as e:
                logger.error(f"Error in poll loop: {e}", exc_info=True)

            # Wait for next tick
            await asyncio.sleep(self.config.get_poll_interval_ms() / 1000.0)

    def _orchestrator_channel(self, message: dict):
        # A simple callback to handle worker events in the orchestrator
        logger.info(f"Orchestrator received: {message}")
        event = message.get("event")
        issue_id = message.get("issue_id")
        reason = message.get("reason", "unknown")

        if event == "exit_normal" and issue_id:
            logger.info(f"Issue {issue_id} exited normally")
            self.orchestrator._terminate_issue(issue_id, reason="normal")

        elif event == "exit_error" and issue_id:
            logger.warning(f"Issue {issue_id} exited with error: {reason}")
            self.orchestrator._terminate_issue(issue_id, reason=reason)

        elif event == "codex_update" and issue_id:
            entry = self.orchestrator.state.running.get(issue_id)
            if entry:
                entry["last_codex_timestamp"] = time.time() * 1000
