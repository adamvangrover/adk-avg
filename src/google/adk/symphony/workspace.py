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
import re
import shutil
import subprocess
import logging
from typing import Optional

from google.adk.symphony.config import ConfigLayer
from google.adk.symphony.models import Workspace

logger = logging.getLogger(__name__)


class WorkspaceManager:
    def __init__(self, config_layer: ConfigLayer):
        self.config = config_layer

    def sanitize_identifier(self, identifier: str) -> str:
        """Sanitizes the identifier for safe directory names."""
        return re.sub(r"[^A-Za-z0-9._-]", "_", identifier)

    def get_workspace_path(self, identifier: str) -> str:
        """Computes the workspace path."""
        root = self.config.get_workspace_root()
        sanitized_key = self.sanitize_identifier(identifier)
        return os.path.join(root, sanitized_key)

    def create_for_issue(self, identifier: str) -> Workspace:
        """Creates or reuses a workspace for an issue identifier."""
        path = self.get_workspace_path(identifier)
        root = self.config.get_workspace_root()
        sanitized_key = self.sanitize_identifier(identifier)

        # Invariant checks
        abs_path = os.path.abspath(path)
        abs_root = os.path.abspath(root)

        if not abs_path.startswith(abs_root):
            raise ValueError(f"Workspace path {path} is not under workspace root {root}")

        created_now = False
        if not os.path.exists(path):
            os.makedirs(path)
            created_now = True

        workspace = Workspace(path=path, workspace_key=sanitized_key, created_now=created_now)

        if created_now:
            hook = self.config.get_hooks_after_create()
            if hook:
                try:
                    self.run_hook("after_create", hook, path)
                except Exception as e:
                    # Clean up if creation hook fails
                    shutil.rmtree(path, ignore_errors=True)
                    raise RuntimeError(f"after_create hook failed: {e}")

        return workspace

    def remove_workspace(self, identifier: str):
        """Removes the workspace directory."""
        path = self.get_workspace_path(identifier)
        if os.path.exists(path):
            hook = self.config.get_hooks_before_remove()
            if hook:
                try:
                    self.run_hook("before_remove", hook, path)
                except Exception as e:
                    logger.warning(f"before_remove hook failed: {e}")
            shutil.rmtree(path, ignore_errors=True)

    def run_hook(self, hook_name: str, script: Optional[str], cwd: str):
        """Runs a shell script hook in the workspace directory."""
        if not script:
            return

        timeout = self.config.get_hooks_timeout_ms() / 1000.0

        try:
            logger.info(f"Running hook {hook_name} in {cwd}")
            subprocess.run(
                ["bash", "-lc", script],
                cwd=cwd,
                timeout=timeout,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            logger.error(f"Hook {hook_name} failed with exit code {e.returncode}: {e.stderr}")
            raise
        except subprocess.TimeoutExpired as e:
            logger.error(f"Hook {hook_name} timed out after {timeout} seconds")
            raise

    def run_hook_best_effort(self, hook_name: str, script: Optional[str], cwd: str):
        """Runs a hook without throwing an exception on failure."""
        try:
            self.run_hook(hook_name, script, cwd)
        except Exception as e:
            logger.warning(f"Best-effort hook {hook_name} failed: {e}")
