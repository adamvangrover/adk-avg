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
import json
import logging
import shlex
from typing import Callable, Any, Optional

from google.adk.symphony.config import ConfigLayer
from google.adk.symphony.models import Issue
from google.adk.symphony.workspace import WorkspaceManager
from google.adk.agents.llm_agent import LlmAgent
from google.adk.runners import Runner

logger = logging.getLogger(__name__)


class AgentRunner:
    def __init__(self, config: ConfigLayer, workspace_manager: WorkspaceManager):
        self.config = config
        self.workspace_manager = workspace_manager

    async def run_attempt(self, issue: Issue, attempt: int, orchestrator_channel: Callable[[Any], None]):
        """Runs a single agent attempt for an issue."""
        workspace = None
        try:
            workspace = self.workspace_manager.create_for_issue(issue.identifier)
        except Exception as e:
            logger.error(f"Workspace error for {issue.identifier}: {e}")
            self._fail_worker(orchestrator_channel, issue.id, "workspace error")
            return

        try:
            self.workspace_manager.run_hook("before_run", self.config.get_hooks_before_run(), workspace.path)
        except Exception as e:
            logger.error(f"before_run hook error for {issue.identifier}: {e}")
            self._fail_worker(orchestrator_channel, issue.id, "before_run hook error")
            return

        # Start App-Server Session
        session = await self._start_session(workspace.path)
        if not session:
            self.workspace_manager.run_hook_best_effort("after_run", self.config.get_hooks_after_run(), workspace.path)
            self._fail_worker(orchestrator_channel, issue.id, "agent session startup error")
            return

        max_turns = self.config.get_agent_max_turns()
        turn_number = 1

        while turn_number <= max_turns:
            prompt = self._build_turn_prompt(issue, attempt, turn_number, max_turns)
            if not prompt:
                await self._stop_session(session)
                self.workspace_manager.run_hook_best_effort("after_run", self.config.get_hooks_after_run(), workspace.path)
                self._fail_worker(orchestrator_channel, issue.id, "prompt error")
                return

            turn_result = await self._run_turn(session, prompt, issue, orchestrator_channel)

            if turn_result == "failed":
                await self._stop_session(session)
                self.workspace_manager.run_hook_best_effort("after_run", self.config.get_hooks_after_run(), workspace.path)
                self._fail_worker(orchestrator_channel, issue.id, "agent turn error")
                return

            # Re-check active state
            # For simplicity, we just assume active or let orchestrator kill it

            if not self._is_active(issue):
                break

            turn_number += 1

        await self._stop_session(session)
        self.workspace_manager.run_hook_best_effort("after_run", self.config.get_hooks_after_run(), workspace.path)

        orchestrator_channel({"event": "exit_normal", "issue_id": issue.id})

    async def _start_session(self, workspace_path: str) -> Optional[Any]:
        cmd = self.config.get_codex_command()
        logger.info(f"Starting session in {workspace_path} with command: {cmd}")
        try:
            process = await asyncio.create_subprocess_shell(
                f"bash -lc {shlex.quote(cmd)}",
                cwd=workspace_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE,
            )

            # Send initialize
            init_req = {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "symphony", "version": "1.0"}, "capabilities": {}}}
            process.stdin.write((json.dumps(init_req) + "\n").encode("utf-8"))
            await process.stdin.drain()

            # Read initialized (we do a simplified read here for the mock, a real implementation would have a robust JSON-RPC reader loop)
            line = await asyncio.wait_for(process.stdout.readline(), timeout=5.0)

            # Send thread/start
            thread_req = {"id": 2, "method": "thread/start", "params": {"cwd": workspace_path}}
            process.stdin.write((json.dumps(thread_req) + "\n").encode("utf-8"))
            await process.stdin.drain()

            line = await asyncio.wait_for(process.stdout.readline(), timeout=5.0)
            resp = json.loads(line.decode("utf-8"))
            thread_id = resp.get("result", {}).get("thread", {}).get("id", "unknown_thread")

            return {
                "process": process,
                "thread_id": thread_id,
                "workspace_path": workspace_path
            }
        except Exception as e:
            logger.error(f"Failed to start session: {e}")
            if 'process' in locals() and process.returncode is None:
                process.terminate()
            return None

    async def _stop_session(self, session: Any):
        if session and "process" in session:
            process = session["process"]
            if process.returncode is None:
                logger.info(f"Stopping session {session.get('thread_id')}")
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    process.kill()

    def _build_turn_prompt(self, issue: Issue, attempt: int, turn_number: int, max_turns: int) -> str:
        import jinja2
        prompt_template = self.config._prompt_template

        # We need a strict template engine as per specification.
        # Jinja2 is widely used and provides Liquid-like syntax.
        # Unknown variables and filters must fail.
        env = jinja2.Environment(undefined=jinja2.StrictUndefined)

        try:
            template = env.from_string(prompt_template)

            # Serialize issue fields to strings as per spec
            # and maintain nested arrays/maps
            issue_dict = {
                "id": str(issue.id),
                "identifier": str(issue.identifier),
                "title": str(issue.title),
                "description": str(issue.description) if issue.description else "",
                "priority": issue.priority,
                "state": str(issue.state),
                "branch_name": str(issue.branch_name) if issue.branch_name else "",
                "url": str(issue.url) if issue.url else "",
                "labels": [str(l) for l in issue.labels],
                "blocked_by": [{"id": str(b.id) if b.id else None, "identifier": str(b.identifier) if b.identifier else None, "state": str(b.state) if b.state else None} for b in issue.blocked_by],
                "created_at": str(issue.created_at) if issue.created_at else "",
                "updated_at": str(issue.updated_at) if issue.updated_at else ""
            }

            # Render prompt
            prompt = template.render(
                issue=issue_dict,
                attempt=attempt if attempt > 0 else None,
            )
            return prompt
        except jinja2.exceptions.UndefinedError as e:
            logger.error(f"Template render error: {e}")
            return ""
        except jinja2.exceptions.TemplateSyntaxError as e:
            logger.error(f"Template parse error: {e}")
            return ""

    async def _run_turn(self, session: Any, prompt: str, issue: Issue, orchestrator_channel: Callable[[Any], None]) -> str:
        process = session["process"]
        thread_id = session["thread_id"]

        turn_req = {
            "id": 3,
            "method": "turn/start",
            "params": {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
                "cwd": session["workspace_path"],
                "title": f"{issue.identifier}: {issue.title}"
            }
        }

        try:
            process.stdin.write((json.dumps(turn_req) + "\n").encode("utf-8"))
            await process.stdin.drain()

            while True:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=self.config.get_codex_stall_timeout_ms() / 1000.0)
                if not line:
                    break

                msg = json.loads(line.decode("utf-8"))
                method = msg.get("method")

                if method == "turn/completed":
                    orchestrator_channel({"event": "turn_completed", "issue_id": issue.id})
                    return "completed"
                elif method in ("turn/failed", "turn/cancelled"):
                    return "failed"

                # Forward other events
                orchestrator_channel({"event": "codex_update", "issue_id": issue.id, "msg": msg})

        except asyncio.TimeoutError:
            logger.error("Turn timed out")
            return "failed"
        except Exception as e:
            logger.error(f"Error reading turn stream: {e}")
            return "failed"

        return "failed"

    def _is_active(self, issue: Issue) -> bool:
        return issue.state.lower().strip() in [s.lower().strip() for s in self.config.get_tracker_active_states()]

    def _fail_worker(self, orchestrator_channel: Callable[[Any], None], issue_id: str, reason: str):
        logger.error(f"Worker failed: {reason}")
        orchestrator_channel({"event": "exit_error", "issue_id": issue_id, "reason": reason})
