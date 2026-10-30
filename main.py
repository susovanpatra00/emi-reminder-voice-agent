"""Lumen Finance EMI reminder voice agent (Pipecat, browser WebRTC call).

Run:   BORROWER_ID=B002 python bot.py
Open:  http://localhost:7860/client  and click Connect.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import statistics
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    EndWorkerFrame,
    LLMMessagesAppendFrame,
    LLMRunFrame,
    TTSSpeakFrame,
    TTSUpdateSettingsFrame,
)
from pipecat.observers.user_bot_latency_observer import UserBotLatencyObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker, ProcessorUnusablePolicy
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.frames.frames import FunctionCallResultProperties, Frame, LLMTextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.workers.runner import WorkerRunner

from agent.borrowers import get_borrower
from agent.call_session import OUTCOMES_DIR, CallSession, says_goodbye
from agent.prompts import build_system_prompt, opening_instruction
from agent.tools import pipecat_tools

load_dotenv()  # shell variables win over .env, so BORROWER_ID=B002 python bot.py works

LANG = {"english": Language.EN_IN, "hindi": Language.HI}

# Spoken while a slow tool runs, so the caller never hears dead air.
FILLERS = {
    "send_payment_link": [
        "Sure, sending that to you now.",
        "One moment, I'm sending it right away.",
    ],
}

IDLE_TIMEOUT_SECS = float(os.getenv("IDLE_TIMEOUT_SECS", "7"))

class AgentTurnText(FrameProcessor):
    """Remembers what the agent has said since the caller last spoke.

    Used to skip the end_call closing line when the model already said goodbye in this
    turn, so the caller never hears two goodbyes.
    """

    def __init__(self):
        super().__init__()
        self.text = ""

    def reset(self):
        self.text = ""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMTextFrame):
            self.text += frame.text
        await self.push_frame(frame, direction)


transport_params = {
    "webrtc": lambda: TransportParams(audio_in_enabled=True, audio_out_enabled=True),
}


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments):
    borrower = get_borrower(os.getenv("BORROWER_ID", "B001"))
    session = CallSession(borrower)
    logger.info(f"Calling {borrower.name} ({borrower.borrower_id})")

    stt = DeepgramSTTService(
        api_key=os.environ["DEEPGRAM_API_KEY"],
        # "multi" lets Deepgram follow English/Hindi code-switching mid-sentence.
        settings=DeepgramSTTService.Settings(model="nova-3", language="multi"),
    )

    tts = CartesiaTTSService(
        api_key=os.environ["CARTESIA_API_KEY"],
        settings=CartesiaTTSService.Settings(
            voice=os.getenv("CARTESIA_VOICE_ID", "86e30c1d-714b-4074-a1f2-1cb6b552fb49"),
            language=LANG[session.language],
        ),
    )

    llm = AnthropicLLMService(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        settings=AnthropicLLMService.Settings(
            model=os.getenv("LLM_MODEL", "claude-haiku-5-5"),
            system_instruction=build_system_prompt(session),
            max_tokens=300,  # spoken turns are short; a cap also bounds worst-case latency
            # The system prompt and tools are identical on every turn, so cache them.
            enable_prompt_caching=True,
        ),
    )

    turn_text = AgentTurnText()

    # ---------------------------------------------------------------- tools

    async def on_tool(params: FunctionCallParams):
        name = params.function_name
        result = await session.handle(name, dict(params.arguments))
        logger.info(f"tool {name}({params.arguments}) -> {result}")

        if name == "set_language" and "error" not in result:
            await params.llm.push_frame(
                TTSUpdateSettingsFrame(delta=CartesiaTTSService.Settings(language=LANG[session.language]))
            )

        if name == "end_call" and session.ended:
            # Don't run the LLM again: speak the goodbye ourselves, then hang up
            # once the audio has flushed.
            await params.result_callback(result, properties=FunctionCallResultProperties(run_llm=False))
            await asyncio.sleep(0.05)  # let any text from this reply reach the tracker
            if says_goodbye(turn_text.text):
                logger.info("Model already said goodbye; skipping closing_line")
            else:
                await params.llm.push_frame(TTSSpeakFrame(session.closing_line))
            await params.llm.push_frame(EndWorkerFrame())
            return

        # If end_call is in the same batch (e.g. flag_for_human + end_call), don't let this
        # result start another LLM reply: the closing line is the last thing said.
        if ending_batch:
            await params.result_callback(result, properties=FunctionCallResultProperties(run_llm=False))
        else:
            await params.result_callback(result)

    llm.register_function(None, on_tool)
    ending_batch = False

    @llm.event_handler("on_function_calls_started")
    async def on_function_calls_started(service, function_calls):
        nonlocal ending_batch
        ending_batch = any(c.function_name == "end_call" for c in function_calls)
        for call in function_calls:
            if call.function_name in FILLERS:
                await tts.queue_frame(TTSSpeakFrame(random.choice(FILLERS[call.function_name])))
                break

    # ------------------------------------------------------------- pipeline

    context = LLMContext(tools=pipecat_tools())
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            user_idle_timeout=IDLE_TIMEOUT_SECS,
        ),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            turn_text,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    latency = UserBotLatencyObserver()
    latencies: list[float] = []

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        observers=[latency],
        idle_timeout_secs=runner_args.pipeline_idle_timeout_secs,
        processor_unusable_policy=ProcessorUnusablePolicy.END,
    )

    @latency.event_handler("on_latency_measured")
    async def on_latency_measured(observer, seconds):
        latencies.append(seconds)
        logger.info(f"⏱  user stopped -> bot speaking: {seconds:.3f}s")

    @latency.event_handler("on_latency_breakdown")
    async def on_latency_breakdown(observer, breakdown):
        for line in breakdown.turn_contribution_lines():
            logger.debug(f"   {line}")

    # --------------------------------------------------------- silence handling

    idle_count = 0

    @user_aggregator.event_handler("on_user_turn_started")
    async def on_user_turn_started(aggregator, strategy):
        nonlocal idle_count
        idle_count = 0
        turn_text.reset()

    @user_aggregator.event_handler("on_user_turn_idle")
    async def on_user_turn_idle(aggregator):
        nonlocal idle_count
        if session.ended:
            return
        idle_count += 1
        if idle_count == 1:
            msg = "The person has gone quiet. In a few words, check if they can hear you."
            await aggregator.push_frame(LLMMessagesAppendFrame([{"role": "developer", "content": msg}], run_llm=True))
        elif idle_count == 2:
            msg = "Still no answer. Say the line seems unclear and repeat your last question in a few words."
            await aggregator.push_frame(LLMMessagesAppendFrame([{"role": "developer", "content": msg}], run_llm=True))
        else:
            session.record.outcome = session.record.outcome or "no_response"
            session.ended = True
            goodbye = "I can't hear you, so I'll try again later. Thank you, goodbye."
            if session.language == "hindi":
                goodbye = "मुझे आपकी आवाज़ नहीं आ रही है, मैं बाद में फिर से कॉल करूँगी। धन्यवाद।"
            await aggregator.push_frame(TTSSpeakFrame(goodbye))
            await aggregator.push_frame(EndWorkerFrame())

    # ------------------------------------------------------------- lifecycle

    runner = WorkerRunner(handle_sigint=runner_args.handle_sigint)
    await runner.add_workers(worker)

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        context.add_message({"role": "developer", "content": opening_instruction(session)})
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        await runner.cancel()

    try:
        await runner.run()
    finally:
        if not session.ended:  # caller hung up before the agent closed
            from agent.wrapup import wrap_up

            lines = [(m.get("role"), m.get("content")) for m in context.messages
                     if isinstance(m, dict) and m.get("role") in ("user", "assistant")
                     and isinstance(m.get("content"), str)]
            await wrap_up(session, lines)
        record = session.finalize()
        path = session.save()
        _append_call_extras(path, context, latencies)
        logger.info(f"Outcome: {json.dumps(record.__dict__, ensure_ascii=False)}")
        logger.info(f"Saved to {path}")


def _append_call_extras(path: Path, context: LLMContext, latencies: list[float]) -> None:
    """Add the transcript and latency stats to the saved outcome file."""
    data = json.loads(path.read_text())
    data["transcript"] = [
        {"role": m.get("role"), "content": m.get("content")}
        for m in context.messages
        if isinstance(m, dict) and m.get("role") in ("user", "assistant") and m.get("content")
    ]
    if latencies:
        s = sorted(latencies)
        data["latency"] = {
            "turns": len(s),
            "median_secs": round(statistics.median(s), 3),
            "p90_secs": round(s[min(len(s) - 1, int(0.9 * len(s)))], 3),
            "all_secs": [round(x, 3) for x in latencies],
        }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str))


async def bot(runner_args: RunnerArguments):
    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
