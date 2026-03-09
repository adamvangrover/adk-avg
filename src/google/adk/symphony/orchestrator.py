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

import logging
import time
from typing import Dict, Any, List

from google.adk.symphony.config import ConfigLayer
from google.adk.symphony.models import OrchestratorRuntimeState, Issue, RetryEntry
from google.adk.symphony.tracker import TrackerClient
from google.adk.symphony.workspace import WorkspaceManager

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, config: ConfigLayer, tracker: TrackerClient, workspace_manager: WorkspaceManager):
        self.config = config
        self.tracker = tracker
        self.workspace_manager = workspace_manager

        self.state = OrchestratorRuntimeState(
            poll_interval_ms=self.config.get_poll_interval_ms(),
            max_concurrent_agents=self.config.get_agent_max_concurrent_agents()
        )

    def validate_dispatch_config(self) -> bool:
        if self.config.get_tracker_kind() != "linear":
            logger.error("Only 'linear' tracker kind is supported.")
            return False
        if not self.config.get_tracker_api_key():
            logger.error("Tracker API key is missing.")
            return False
        if not self.config.get_tracker_project_slug():
            logger.error("Tracker project slug is missing.")
            return False
        if not self.config.get_codex_command():
            logger.error("Codex command is missing.")
            return False
        return True

    def run_tick(self):
        """Executes a single poll tick."""
        if not self.validate_dispatch_config():
            logger.error("Validation failed. Skipping dispatch.")
            return

        self._reconcile_running_issues()
        self._handle_retries()

        candidates = self.tracker.fetch_candidate_issues()
        if not candidates:
            return

        # Sort: priority ascending, then created_at oldest first, then identifier lexicographically
        candidates.sort(key=lambda issue: (
            issue.priority if issue.priority is not None else float('inf'),
            issue.created_at if issue.created_at else "",
            issue.identifier
        ))

        for issue in candidates:
            if self._no_available_slots():
                break

            if self._should_dispatch(issue):
                self._dispatch_issue(issue)

    def _reconcile_running_issues(self):
        # Part A: Stall detection
        now = time.time() * 1000
        stall_timeout = self.config.get_codex_stall_timeout_ms()
        if stall_timeout > 0:
            stalled_ids = []
            for issue_id, entry in self.state.running.items():
                last_activity = entry.get("last_codex_timestamp") or entry.get("started_at", 0)
                if now - last_activity > stall_timeout:
                    stalled_ids.append((issue_id, entry.get("attempt", 0) + 1))
            for issue_id, next_attempt in stalled_ids:
                logger.warning(f"Issue {issue_id} stalled. Terminating.")
                self._terminate_issue(issue_id, reason="stalled", attempt=next_attempt)

        # Part B: Tracker state refresh
        running_ids = list(self.state.running.keys())
        if not running_ids:
            return

        try:
            refreshed = self.tracker.fetch_issue_states_by_ids(running_ids)
        except Exception as e:
            logger.error(f"Failed to refresh issue states: {e}")
            return

        terminal_states = [s.strip().lower() for s in self.config.get_tracker_terminal_states()]
        active_states = [s.strip().lower() for s in self.config.get_tracker_active_states()]

        for issue in refreshed:
            issue_id = issue.id
            state_lower = issue.state.strip().lower()

            if state_lower in terminal_states:
                self._terminate_issue(issue_id, reason="terminal", cleanup_workspace=True)
            elif state_lower in active_states:
                # Update in-memory snapshot
                if issue_id in self.state.running:
                    self.state.running[issue_id]["issue"] = issue
            else:
                self._terminate_issue(issue_id, reason="inactive", cleanup_workspace=False)

    def _handle_retries(self):
        now = time.time() * 1000
        due_retries = []
        for issue_id, entry in list(self.state.retry_attempts.items()):
            if now >= entry.due_at_ms:
                due_retries.append(issue_id)

        if not due_retries:
            return

        candidates = self.tracker.fetch_candidate_issues()
        candidate_map = {issue.id: issue for issue in candidates}

        for issue_id in due_retries:
            entry = self.state.retry_attempts.pop(issue_id)
            issue = candidate_map.get(issue_id)

            if not issue:
                self.state.claimed.discard(issue_id)
                continue

            if self._no_available_slots():
                # Requeue
                self.schedule_retry(issue_id, entry.identifier, entry.attempt + 1, "no available orchestrator slots")
            else:
                self._dispatch_issue(issue, attempt=entry.attempt)


    def _should_dispatch(self, issue: Issue) -> bool:
        if issue.id in self.state.running or issue.id in self.state.claimed:
            return False

        if issue.state.lower().strip() == "todo":
            terminal_states = [s.lower().strip() for s in self.config.get_tracker_terminal_states()]
            for blocker in issue.blocked_by:
                if blocker.state and blocker.state.lower().strip() not in terminal_states:
                    return False

        # Checking per-state concurrency limit
        state_limit = self.config.get_agent_max_concurrent_agents_by_state().get(issue.state.lower().strip())
        if state_limit is not None:
            count = sum(1 for e in self.state.running.values() if e["issue"].state.lower().strip() == issue.state.lower().strip())
            if count >= state_limit:
                return False

        return True

    def _no_available_slots(self) -> bool:
        return len(self.state.running) >= self.config.get_agent_max_concurrent_agents()

    def _dispatch_issue(self, issue: Issue, attempt: int = 0):
        logger.info(f"Dispatching issue {issue.identifier} (Attempt {attempt})")
        self.state.claimed.add(issue.id)

        # Worker logic normally happens here in a separate task/thread,
        # but for this basic implementation we'll track it
        self.state.running[issue.id] = {
            "issue": issue,
            "identifier": issue.identifier,
            "started_at": time.time() * 1000,
            "attempt": attempt,
            "last_codex_timestamp": time.time() * 1000
        }

    def _terminate_issue(self, issue_id: str, reason: str, attempt: int = 0, cleanup_workspace: bool = False):
        entry = self.state.running.pop(issue_id, None)
        if not entry:
            return

        worker_task = entry.get("worker_task")
        if worker_task and not worker_task.done():
            logger.info(f"Cancelling worker task for issue {issue_id}")
            worker_task.cancel()

        logger.info(f"Terminated issue {issue_id} because {reason}")
        if cleanup_workspace:
            self.workspace_manager.remove_workspace(entry["identifier"])
            self.state.claimed.discard(issue_id)
        else:
            if reason in ["stalled", "failed"]:
                self.schedule_retry(issue_id, entry["identifier"], attempt, reason)
            else:
                self.state.claimed.discard(issue_id)


    def schedule_retry(self, issue_id: str, identifier: str, attempt: int, error: str):
        # Exponential backoff
        backoff_ms = min(10000 * (2 ** (attempt - 1)), self.config.get_agent_max_retry_backoff_ms()) if attempt > 0 else 1000
        due_at = (time.time() * 1000) + backoff_ms

        self.state.retry_attempts[issue_id] = RetryEntry(
            issue_id=issue_id,
            identifier=identifier,
            attempt=attempt,
            due_at_ms=due_at,
            error=error
        )
        self.state.claimed.add(issue_id)
