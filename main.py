import logging
import os
import signal
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from videosdk.agents import (
    Agent,
    AgentSession,
    JobContext,
    Pipeline,
    RoomOptions,
    Worker,
    WorkerOptions,
)
from videosdk.plugins.google import GeminiRealtime, GeminiLiveConfig

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PROMPT_PATH = Path(__file__).parent / "PROMPT.md"
IST = timezone(timedelta(hours=5, minutes=30))

AGENT_NAME = "Ananya"
AGENT_ID = "my_telephony_agent"
AGENT_PARTICIPANT_ID = "agent_ananya"


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------
def required_ascii_secret(name: str) -> str:
    value = os.getenv(name, "")
    if not value or not value.isascii() or any(c.isspace() for c in value):
        raise RuntimeError(f"{name} must be a non-empty ASCII value without whitespace")
    if value.endswith(",") or any(c in value for c in "‘’“”"):
        raise RuntimeError(f"{name} contains a trailing comma or typographical quotes")
    return value


# ---------------------------------------------------------------------------
# Time-aware greeting (Option A)
# ---------------------------------------------------------------------------
def current_greeting() -> str:
    hour = datetime.now(IST).hour
    if hour < 12:
        return "good morning"
    if hour < 17:
        return "good afternoon"
    return "good evening"


# ---------------------------------------------------------------------------
# Prompt loader with CI-style regression guard
# ---------------------------------------------------------------------------
def load_prompt() -> str:
    if not PROMPT_PATH.exists():
        raise RuntimeError(f"Missing runtime prompt at {PROMPT_PATH}")
    text = PROMPT_PATH.read_text(encoding="utf-8").strip()
    text = text.replace("{{GREETING}}", current_greeting())
    # Regression guard from Spec v3 Section 1: the opening turn must always
    # carry the AI disclosure. Fail the worker start if it does not.
    if "AI assistant" not in text or "Ananya" not in text:
        raise RuntimeError(
            "Runtime prompt is missing the disclosure string or agent name"
        )
    return text


GOOGLE_API_KEY = required_ascii_secret("GOOGLE_API_KEY")
VIDEOSDK_TOKEN = required_ascii_secret("VIDEOSDK_TOKEN")
AGENT_INSTRUCTIONS = load_prompt()


# ---------------------------------------------------------------------------
# SDK Fix: Patch Worker.__init__ so any spawned worker process or class runner
# automatically receives valid RoomOptions, preventing NoneType attribute error
# on lines 748-765 in videosdk.agents.worker.
# ---------------------------------------------------------------------------
_orig_worker_init = Worker.__init__


def _patched_worker_init(
    self, options: WorkerOptions, default_room_options: Optional[RoomOptions] = None
):
    if default_room_options is None:
        default_room_options = RoomOptions(
            name=AGENT_NAME,
            agent_participant_id=AGENT_PARTICIPANT_ID,
            auto_end_session=True,
        )
    _orig_worker_init(self, options, default_room_options=default_room_options)


Worker.__init__ = _patched_worker_init


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------
class VoiceAssistant(Agent):
    def __init__(self):
        super().__init__(
            instructions=AGENT_INSTRUCTIONS,
            agent_id=AGENT_ID,
        )
        self.name = AGENT_NAME

    async def on_enter(self) -> None:
        logger.info("Agent entered call session.")
        if self.session is None:
            raise RuntimeError("Agent session was not bound before on_enter")

        greeting = current_greeting()
        await self.session.say(
            f"Hi, {greeting}! Main Ananya bol rahi hoon PodHub Mumbai se. "
            "Main ek AI assistant hoon. Main sirf do minute lungi. "
            "Abhi baat kar sakte hain?"
        )
        logger.info("Beat 1 greeting dispatched (greeting=%s); awaiting prospect.", greeting)

    async def on_exit(self) -> None:
        logger.info("Agent exiting call session.")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
async def entrypoint(ctx: JobContext):
    agent = VoiceAssistant()
    realtime = GeminiRealtime(
        api_key=GOOGLE_API_KEY,
        model="gemini-3.1-flash-live-preview",
        config=GeminiLiveConfig(
            voice="Zephyr",
            response_modalities=["AUDIO"],
        ),
    )
    pipeline = Pipeline(llm=realtime)
    session = AgentSession(agent=agent, pipeline=pipeline)

    await ctx.connect()
    await ctx.run_until_shutdown(session=session)


# ---------------------------------------------------------------------------
# Shutdown + main
# ---------------------------------------------------------------------------
def shutdown_handler(signum, _frame):
    logger.info("Received signal %s; worker shutdown requested.", signum)


def main() -> None:
    if not VIDEOSDK_TOKEN or not GOOGLE_API_KEY:
        logger.error("Missing VIDEOSDK_TOKEN or GOOGLE_API_KEY in environment.")
        sys.exit(1)

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    options = WorkerOptions(
        entrypoint_fnc=entrypoint,
        agent_id=AGENT_ID,
        auth_token=VIDEOSDK_TOKEN,
        register=True,
    )

    default_room_options = RoomOptions(
        name=AGENT_NAME,
        agent_participant_id=AGENT_PARTICIPANT_ID,
        auto_end_session=True,
    )

    logger.info("Registering Ananya worker as '%s'...", AGENT_ID)
    worker = Worker(options=options, default_room_options=default_room_options)
    worker.run_worker(options=options, default_room_options=default_room_options)


if __name__ == "__main__":
    main()