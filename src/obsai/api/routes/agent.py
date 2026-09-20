"""Agent execution, inspection and resume routes.

`POST /agent/runs`: Start or queue an agent run.
`GET /agent/runs/{run_id}`: Retrieve agent run state from LangGraph checkpoint.
`POST /agent/runs/{run_id}/resume`: Resume interrupted write run with HITL decision.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from obsai.api.deps import get_settings
from obsai.application.agent_service import (
    get_agent_run,
    resume_agent_run,
    start_agent_run,
)
from obsai.application.dto import (
    AgentResumeRequest,
    AgentRunRequest,
    AgentRunView,
)
from obsai.config.models import Settings

router = APIRouter(tags=["agent"])


@router.post("/agent/runs", response_model=AgentRunView)
def post_agent_run(
    request: AgentRunRequest,
    settings: Settings = Depends(get_settings),
) -> AgentRunView:
    """Start an agent run with query and optional thread_id."""
    return start_agent_run(settings, request.query, thread_id=request.thread_id)


@router.get("/agent/runs/{run_id}", response_model=AgentRunView)
def get_agent_run_by_id(
    run_id: str,
    settings: Settings = Depends(get_settings),
) -> AgentRunView:
    """Retrieve full execution and timeline state for an agent workflow run."""
    return get_agent_run(settings, run_id)


@router.post("/agent/runs/{run_id}/resume", response_model=AgentRunView)
def post_agent_resume(
    run_id: str,
    request: AgentResumeRequest,
    settings: Settings = Depends(get_settings),
) -> AgentRunView:
    """Resume an interrupted write run with user approval decision."""
    return resume_agent_run(settings, run_id, approved=request.approved)
