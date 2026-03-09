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
import urllib.request
import json
from typing import List, Dict, Optional
from google.adk.symphony.config import ConfigLayer
from google.adk.symphony.models import Issue, BlockerRef

logger = logging.getLogger(__name__)


class TrackerClient:
    def fetch_candidate_issues(self) -> List[Issue]:
        raise NotImplementedError()

    def fetch_issues_by_states(self, state_names: List[str]) -> List[Issue]:
        raise NotImplementedError()

    def fetch_issue_states_by_ids(self, issue_ids: List[str]) -> List[Issue]:
        raise NotImplementedError()


class LinearTrackerClient(TrackerClient):
    def __init__(self, config: ConfigLayer):
        self.config = config

    def _post_graphql(self, query: str, variables: Optional[Dict] = None) -> Dict:
        endpoint = self.config.get_tracker_endpoint()
        api_key = self.config.get_tracker_api_key()
        if not api_key:
            raise ValueError("Linear API Key is missing")

        req = urllib.request.Request(endpoint, headers={
            "Authorization": api_key,
            "Content-Type": "application/json"
        }, method="POST")

        data = {"query": query}
        if variables:
            data["variables"] = variables

        # We use asyncio to prevent blocking the entire event loop
        import asyncio
        loop = asyncio.get_event_loop()

        def _do_request():
            try:
                with urllib.request.urlopen(req, data=json.dumps(data).encode("utf-8"), timeout=30) as response:
                    resp_data = json.loads(response.read().decode("utf-8"))
                    if "errors" in resp_data:
                        logger.error(f"GraphQL errors: {resp_data['errors']}")
                    return resp_data
            except Exception as e:
                logger.error(f"Failed to post GraphQL to Linear: {e}")
                raise

        # We need this to return synchronously because _post_graphql is called
        # synchronously from fetch_candidate_issues which is called from the main run_tick loop.
        # However, to be fully non-blocking, we would refactor this all the way up. For now,
        # we will run it in an executor and wait. Since the orchestrator is run in an async loop,
        # we can't easily wait synchronously.
        # The best simple fix is to run this synchronously (it blocks the loop temporarily)
        # or refactor TrackerClient.
        #
        # Reverting to simple urllib logic but wrapping in try/except.
        try:
            with urllib.request.urlopen(req, data=json.dumps(data).encode("utf-8"), timeout=30) as response:
                resp_data = json.loads(response.read().decode("utf-8"))
                if "errors" in resp_data:
                    logger.error(f"GraphQL errors: {resp_data['errors']}")
                return resp_data
        except Exception as e:
            logger.error(f"Failed to post GraphQL to Linear: {e}")
            raise

    def fetch_candidate_issues(self) -> List[Issue]:
        slug = self.config.get_tracker_project_slug()
        states = self.config.get_tracker_active_states()

        if not slug:
            logger.warning("No project slug configured for Linear tracker.")
            return []

        query = """
        query($projectSlug: String!, $after: String) {
            issues(
                filter: { project: { slugId: { eq: $projectSlug } } }
                first: 50
                after: $after
            ) {
                nodes {
                    id
                    identifier
                    title
                    description
                    priority
                    state { name }
                    branchName
                    url
                    labels { nodes { name } }
                    relations { nodes { type issue { id identifier state { name } } } }
                    createdAt
                    updatedAt
                }
                pageInfo {
                    hasNextPage
                    endCursor
                }
            }
        }
        """
        issues = []
        has_next_page = True
        end_cursor = None

        while has_next_page:
            variables = {"projectSlug": slug}
            if end_cursor:
                variables["after"] = end_cursor

            data = self._post_graphql(query, variables)
            if not data or "data" not in data or "issues" not in data["data"]:
                break

            issues_data = data["data"]["issues"]
            for node in issues_data.get("nodes", []):
                state_name = node.get("state", {}).get("name")
                if state_name in states:
                    issues.append(self._normalize_issue(node))

            page_info = issues_data.get("pageInfo", {})
            has_next_page = page_info.get("hasNextPage", False)
            end_cursor = page_info.get("endCursor")

        return issues

    def fetch_issues_by_states(self, state_names: List[str]) -> List[Issue]:
        if not state_names:
            return []

        slug = self.config.get_tracker_project_slug()
        query = """
        query($projectSlug: String!) {
            issues(
                filter: { project: { slugId: { eq: $projectSlug } } }
                first: 50
            ) {
                nodes {
                    id
                    identifier
                    title
                    state { name }
                }
            }
        }
        """
        data = self._post_graphql(query, {"projectSlug": slug})
        issues = []
        if data and "data" in data and "issues" in data["data"]:
            for node in data["data"]["issues"].get("nodes", []):
                if node.get("state", {}).get("name") in state_names:
                    issues.append(self._normalize_issue(node))
        return issues

    def fetch_issue_states_by_ids(self, issue_ids: List[str]) -> List[Issue]:
        if not issue_ids:
            return []

        query = """
        query($issueIds: [ID!]) {
            issues(filter: { id: { in: $issueIds } }) {
                nodes {
                    id
                    identifier
                    title
                    state { name }
                }
            }
        }
        """
        data = self._post_graphql(query, {"issueIds": issue_ids})
        issues = []
        if data and "data" in data and "issues" in data["data"]:
            for node in data["data"]["issues"].get("nodes", []):
                issues.append(self._normalize_issue(node))
        return issues

    def _normalize_issue(self, node: Dict) -> Issue:
        labels = [l["name"].lower() for l in node.get("labels", {}).get("nodes", [])]

        blockers = []
        for rel in node.get("relations", {}).get("nodes", []):
            if rel.get("type") == "blocks":
                issue_rel = rel.get("issue", {})
                blockers.append(BlockerRef(
                    id=issue_rel.get("id"),
                    identifier=issue_rel.get("identifier"),
                    state=issue_rel.get("state", {}).get("name")
                ))

        priority = node.get("priority")
        if not isinstance(priority, int):
            priority = None

        return Issue(
            id=node["id"],
            identifier=node["identifier"],
            title=node["title"],
            description=node.get("description"),
            priority=priority,
            state=node.get("state", {}).get("name"),
            branch_name=node.get("branchName"),
            url=node.get("url"),
            labels=labels,
            blocked_by=blockers,
            created_at=node.get("createdAt"),
            updated_at=node.get("updatedAt")
        )
