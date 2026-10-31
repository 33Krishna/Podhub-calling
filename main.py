import asyncio
import logging
import os
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

from worker_bridge import WorkerBridge

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PROMPT_PATH = Path(__file__).parent / "prompt.md"
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
    def __init__(self, bridge: Optional[WorkerBridge] = None):
        super().__init__(
            instructions=AGENT_INSTRUCTIONS,
            agent_id=AGENT_ID,
        )
        self.name = AGENT_NAME
        self.bridge = bridge

    async def on_enter(self) -> None:
        logger.info("Agent entered call session.")
        if self.session is None:
            raise RuntimeError("Agent session was not bound before on_enter")

        greeting = current_greeting()
        greeting_text = (
            f"Hi, {greeting}! Main Ananya bol rahi hoon PodHub Mumbai se. "
            "Main ek AI assistant hoon. Main sirf do minute lungi. "
            "Abhi baat kar sakte hain?"
        )
        await self.session.say(greeting_text)
        logger.info("Beat 1 greeting dispatched (greeting=%s); awaiting prospect.", greeting)
        if self.bridge:
            self.bridge.send_transcript(speaker="agent", text=greeting_text)

    async def on_exit(self) -> None:
        logger.info("Agent exiting call session.")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
async def entrypoint(ctx: JobContext):
    bridge = WorkerBridge()
    agent = VoiceAssistant(bridge=bridge)
    realtime = GeminiRealtime(
        api_key=GOOGLE_API_KEY,
        model="gemini-3.1-flash-live-preview",
        config=GeminiLiveConfig(
            voice="Zephyr",
            response_modalities=["AUDIO"],
        ),
    )

    # ---------------------------------------------------------------------------
    # TEMP DEBUG: Diagnostic logging for SDK events & config
    # ---------------------------------------------------------------------------
    sdk_log_path = Path(__file__).parent / "sdk_events.log"
    try:
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        in_cfg = getattr(realtime.config, "input_audio_transcription", None)
        out_cfg = getattr(realtime.config, "output_audio_transcription", None)
        cfg_line = f"[{now_str}] [CONFIG] input_audio_transcription={in_cfg!r} output_audio_transcription={out_cfg!r}\n"
        with open(sdk_log_path, "a", encoding="utf-8") as f:
            f.write(cfg_line)
        logger.info("[TEMP DEBUG] %s", cfg_line.strip())
    except Exception as exc:
        logger.error("[TEMP DEBUG] Failed to write initial SDK config log: %s", exc)

    _orig_emit = realtime.emit

    def _debug_emit(event, *args):
        try:
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            short_args = []
            for a in args:
                if isinstance(a, dict) and "text" in a and isinstance(a["text"], str):
                    a_copy = dict(a)
                    if len(a_copy["text"]) > 80:
                        a_copy["text"] = a_copy["text"][:80] + "..."
                    short_args.append(a_copy)
                else:
                    repr_str = repr(a)
                    short_args.append(repr_str[:80] + "..." if len(repr_str) > 80 else repr_str)
            log_entry = f"[{now_iso}] event={event!r} args={short_args}\n"
            with open(sdk_log_path, "a", encoding="utf-8") as f:
                f.write(log_entry)
            logger.info("[TEMP DEBUG SDK EVENT] %s", log_entry.strip())
        except Exception as exc:
            logger.error("[TEMP DEBUG] Error in _debug_emit: %s", exc)
        return _orig_emit(event, *args)

    realtime.emit = _debug_emit
    # ---------------------------------------------------------------------------
    # Register synchronous event callbacks before starting session (Section D.4)
    realtime.on("realtime_model_transcription", bridge.on_transcription)
    realtime.on("error", bridge.on_realtime_error)

    pipeline = Pipeline(llm=realtime)
    session = AgentSession(agent=agent, pipeline=pipeline)

    # Windows clean shutdown callback (Section D.7)
    ctx.add_shutdown_callback(bridge.cleanup_bridge)

    await ctx.connect()
    room_id = ctx.room_options.room_id if ctx.room_options else None
    if not room_id:
        logger.error("CRITICAL: ctx.room_options.room_id is None! Cannot map worker session to dashboard call record.")
    else:
        asyncio.create_task(bridge.start(room_id))

    await ctx.run_until_shutdown(session=session)


# ---------------------------------------------------------------------------
# Shutdown + main
# ---------------------------------------------------------------------------
def main() -> None:
    if not VIDEOSDK_TOKEN or not GOOGLE_API_KEY:
        logger.error("Missing VIDEOSDK_TOKEN or GOOGLE_API_KEY in environment.")
        sys.exit(1)

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
    Worker.run_worker(options=options, default_room_options=default_room_options)


if __name__ == "__main__":
    main()