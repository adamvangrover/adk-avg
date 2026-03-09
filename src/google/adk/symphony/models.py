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

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class BlockerRef:
    id: Optional[str] = None
    identifier: Optional[str] = None
    state: Optional[str] = None


@dataclass
class Issue:
    id: str
    identifier: str
    title: str
    state: str
    description: Optional[str] = None
    priority: Optional[int] = None
    branch_name: Optional[str] = None
    url: Optional[str] = None
    labels: List[str] = field(default_factory=list)
    blocked_by: List[BlockerRef] = field(default_factory=list)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class WorkflowDefinition:
    config: Dict[str, Any]
    prompt_template: str


@dataclass
class Workspace:
    path: str
    workspace_key: str
    created_now: bool


@dataclass
class RunAttempt:
    issue_id: str
    issue_identifier: str
    workspace_path: str
    started_at: str
    status: str
    attempt: Optional[int] = None
    error: Optional[str] = None


@dataclass
class LiveSession:
    session_id: str
    thread_id: str
    turn_id: str
    codex_input_tokens: int = 0
    codex_output_tokens: int = 0
    codex_total_tokens: int = 0
    turn_count: int = 0
    codex_app_server_pid: Optional[str] = None
    last_codex_event: Optional[str] = None
    last_codex_timestamp: Optional[str] = None
    last_codex_message: Optional[Any] = None
    last_reported_input_tokens: int = 0
    last_reported_output_tokens: int = 0
    last_reported_total_tokens: int = 0


@dataclass
class RetryEntry:
    issue_id: str
    identifier: str
    attempt: int
    due_at_ms: int
    timer_handle: Any = None
    error: Optional[str] = None


@dataclass
class OrchestratorRuntimeState:
    poll_interval_ms: int
    max_concurrent_agents: int
    running: Dict[str, Any] = field(default_factory=dict)
    claimed: set = field(default_factory=set)
    retry_attempts: Dict[str, RetryEntry] = field(default_factory=dict)
    completed: set = field(default_factory=set)
    codex_totals: Dict[str, Any] = field(
        default_factory=lambda: {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "seconds_running": 0,
        }
    )
    codex_rate_limits: Optional[Dict[str, Any]] = None
