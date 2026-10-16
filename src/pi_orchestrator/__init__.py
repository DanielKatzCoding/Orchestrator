"""pi-orchestrator: an orchestrator agent built on the pi coding-agent framework.

Every agent (the orchestrator and its workers) is a real ``pi`` process driven over pi's RPC mode.
"""

__version__ = "0.1.0"

from .agents import AgentCatalog, AgentDefinition, load_agents
from .config import Settings
from .delegation import DelegationError, DelegationOutcome, Delegator, TaskSpec
from .events import Event
from .orchestrator import Orchestrator, OrchestratorResult
from .pi_rpc import PiProcessError, PiRpcClient, PiRpcError, RunOutcome
from .worker import Usage, WorkerResult, run_worker

__all__ = [
    "AgentCatalog",
    "AgentDefinition",
    "DelegationError",
    "DelegationOutcome",
    "Delegator",
    "Event",
    "Orchestrator",
    "OrchestratorResult",
    "PiProcessError",
    "PiRpcClient",
    "PiRpcError",
    "RunOutcome",
    "Settings",
    "TaskSpec",
    "Usage",
    "WorkerResult",
    "__version__",
    "load_agents",
    "run_worker",
]
