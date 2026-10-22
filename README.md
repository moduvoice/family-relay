# Family Relay - pass it on, by voice

*"Tell Hannah to eat dinner, there's kimchi stew in the fridge."*
Hours later Hannah asks the kitchen display what time it is - and hears
*"It's 6:05. By the way, Dad asked me to tell you to eat dinner - there's kimchi stew in the fridge."*
She answers *"Tell him it was delicious"*, and when Dad gets home and asks *"Did Hannah eat?"* he hears
*"Yes - she said it was delicious, and she heard your message at 6:05."*

Family Relay is an Alexa+ add-on for the shared family device. **Nobody enrolls and nobody is ever asked who
they are**: the device learns the household's voices from everyday use, picks up names from how the family
talks ("Hannah, don't forget your lunch!" - "Got it!"), and passes a message on only when the recipient's
voice is a clear match - woven into whatever they asked for. Everything fades after 24 hours.

Built for the Amazon Developer Hackathon (Alexa+ track) as a **self-hosted MCP server** (MCP spec 2025-11-25,
Streamable HTTP) with **MCP Apps cards**, plus a **web simulator that is a real MCP client** standing in for
Alexa+ (the Alexa+ add-on toolkit is partner-only).

## Architecture: one module of ours, the rest is AWS

```
 family member speaks
        |  audio (mic recording or demo clip)
        +-------------------------------+
        v                               v
 +---------------------+     +-------------------------------------+
 | Amazon Transcribe   |     | familyvoice  (OUR MODULE)            |
 | speech -> text      |     | enrollment-free speaker recognition  |
 +---------------------+     | ResNet34-LM voice embeddings,        |
        | text               | self-forming voice clusters,          |
        |  + name cues ----->| names learned from conversation       |
        v                    +-------------------------------------+
 +---------------------+               | confirmed / tentative / unknown (+ name)
 | Amazon Bedrock      |               v
 | Nova 2 Lite         |  tools/call  +-------------------------------+
 | ("Alexa+" brain)    | -----------> | Family Relay MCP server        |
 +---------------------+   JSON-RPC   | 6 tools, 3 ui:// cards,        |
        | reply text                  | 24 h memory, delivery receipts |
        v                             +-------------------------------+
 +---------------------+
 | Amazon Polly        |  -> the assistant speaks
 +---------------------+
```

| Stage | Submission profile `FR_PROFILE=aws` | On-device profile `FR_PROFILE=local` | GPU profile `FR_PROFILE=gpu` |
|---|---|---|---|
| Speech-to-text | **Amazon Transcribe** (streaming) | silero VAD + sherpa-onnx **parakeet 110M int8** (SenseVoice int8 for non-English; alt. Moonshine base int8) | Qwen3-ASR-1.7B |
| Speaker identification | **familyvoice** (ours) ResNet34-LM | familyvoice ResNet34-LM **int8** | familyvoice |
| Assistant LLM | **Amazon Bedrock - Nova 2 Lite** | Amazon Bedrock - Nova 2 Lite (the only cloud step) | Amazon Bedrock - Nova 2 Lite |
| Text-to-speech | **Amazon Polly** (Standard engine, voice Salli) | **Piper** (sherpa-onnx) | VoxCPM2 |
| Speech-to-speech alternative | Amazon Nova 2.5 Sonic could replace STT+LLM+TTS (`FR_LLM_AUDIO_INPUT` already sends the audio to the LLM side) | | |

**Why speaker identification is ours:** no AWS service recognizes household speakers across sessions without
enrollment (checked 2026-10-22, sources in the [familyvoice README](https://github.com/moduvoice/familyvoice)): Amazon Connect Voice ID is
enrollment-based and reached end of support on 2026-05-20; Amazon Transcribe diarization labels speakers only
inside one recording; Chime SDK speaker search needs enrollment; Alexa voice ID needs "Alexa, learn my voice";
Nova 2 Sonic lists no speaker identification. That gap is the reason this module exists.

### On-device path (`FR_PROFILE=local`)
Everything except the LLM runs on the device CPU. Measured on an Arm Graviton CPU limited to **2 cores / 2
threads** (Graviton2 Neoverse N1 2.5 GHz unless noted); a real Echo-class Cortex-A55/A53 is roughly 2.6-3.4x
slower (same models on a Raspberry Pi 4 vs this CPU).

| Stage | Model | Size | Latency (2 Arm cores) |
|---|---|---|---|
| Voice activity | silero VAD | 0.6 MB | 0.23 ms per 32 ms frame |
| Speaker | familyvoice ResNet34-LM int8 | 6.7 MB | 63 ms per 3 s utterance (Graviton3); fp32 175 ms |
| Speech-to-text (English) | sherpa-onnx parakeet-TDT-CTC 110M int8 | 132 MB | 143 ms per 4.4 s utterance, WER 3.2 % (LibriSpeech test-clean) |
| Speech-to-text (alt.) | Moonshine base int8 | - | 305 ms, WER 3.8 % |
| Speech-to-text (other languages) | SenseVoice-Small int8 | - | 240 ms, WER 4.7 % (English test set) |
| Text-to-speech | Piper en_US amy-low | 63 MB | 391 ms per sentence (real-time factor 0.10) |
| LLM | Amazon Bedrock Nova 2 Lite | cloud | - |

## How it works
* **Two ears per utterance.** The audio goes to STT (what was said) and to the speaker module through
  `POST /speaker/observe` on the MCP server (who said it). The module answers `confirmed | tentative | unknown`;
  only `confirmed` with a learned name lets the relay act for a person. Anything less is silence - the request is
  answered normally and the next utterance is judged again. Nobody is ever asked "who are you?".
* **Names come from conversation.** Each transcript goes back to the module (`note_transcript`): "It's Dad",
  someone calling "Hannah!" and the voice that answers, "Tell Hannah..." (the speaker is not Hannah). A name binds
  only with enough evidence from at least two conversations.
* **Identity is never the LLM's job.** The host names the observation in each `tools/call`
  (`_meta["dev.familyrelay/observation"]`), so tools act for the voice that was heard. A different or unsure voice
  starts a fresh conversation, so nothing said for someone else can leak through the LLM's context.
* **Weave-in delivery.** When messages wait for the speaker, the assistant calls `get_pending_messages` alongside
  the request and adds "By the way, Dad asked me to tell you ...". Senders get a phone notification when their
  message is heard (or fades unheard); `FR_NOTIFY_WEBHOOK_URL` posts the same receipts anywhere.
* **24-hour memory.** Messages and replies expire and their text is erased.

### MCP tools and cards
`leave_message`, `get_pending_messages`, `reply_to_message`, `ask_status`, `get_family_memory`,
`get_speaker_status`, each bound to an MCP Apps card (`_meta.ui.resourceUri`): `ui://family-relay/message-card.html`
(saved / passed on / reply / "Left -> Heard -> Reply"), `family-timeline.html` (carousel of live items, fading,
timeline, notifications), `voice-status.html` (voice clusters forming, names, discarded noise). 768x480 base
canvas; inline / fullscreen / voice-only. Agent Skill: [`skill/family-relay/SKILL.md`](skill/family-relay/SKILL.md).

## Run it

```bash
pip install -r requirements.txt -r requirements-voice.txt
pip install "familyvoice[device] @ git+https://github.com/moduvoice/familyvoice"   # the speaker module (ffmpeg on PATH for browser audio)
cp .env.example .env                                  # FR_PROFILE=aws: AWS credentials with Transcribe, Polly, Bedrock
python run_local.py                                   # MCP server :8790, simulator :8791
```
Open http://127.0.0.1:8791/ and press **Play family scenario**: 30 steps, four family members, the demo clock
moves from 7:00 to the next day. Every step is recorded speech (`simulator/static/clips/`, made with
`deploy/make_clips_polly.py`) that goes through STT and the speaker module; "overheard" steps are family members
talking to each other - the speaker module learns from them, the assistant stays quiet. The right column shows
the speaker module at work and every MCP JSON-RPC message, voice call and LLM decision.

* `FR_PROFILE=local` downloads nothing at run time: fetch the models first (`deploy/ec2_dev_setup.sh` shows
  the files) and point `FR_LOCAL_MODELS` at them.
* `FR_PROFILE=gpu`: `deploy/voice_gpu.sh up|setup|stt|tunnel|down` runs VoxCPM2 + Qwen3-ASR on a GPU spot.
* `FR_PROFILE=demo`: a stand-in speaker module driven by the "Speaking now" switch, browser speech.

### Tests
```bash
python tests/e2e_screenshots.py --video   # plays the scenario headless, screenshots + simulator-run.mp4
python tests/smoke_mcp.py                 # MCP flow over raw JSON-RPC (FR_PROFILE=demo)
```

### Deploy
`deploy/template.yaml` (SAM): MCP server + simulator on Lambda Function URLs, DynamoDB state, Bedrock Nova 2
Lite permissions; `deploy/deploy.sh` builds and deploys (creates billable resources).

## Notes
* Demo family voices are Amazon Polly (Matthew, Joanna, Ivy, Kimberly); the assistant uses a different voice.
* "Alexa" and Echo are trademarks of Amazon. This is an independent hackathon entry; the simulator uses its own
  visual design and no Amazon logos.
* The first prototype (Daily Korean Scene) and the earlier VoxCPM2 demo clips are kept under `legacy/`.

* Honest limits: Alexa+ is simulated here (the add-on toolkit is partner-only) - a real MCP client and server, not a
  real Echo. Speaker recognition is tested on read English speech with synthetic noise; children and siblings are
  harder (see the familyvoice README). Not authentication.

## License and third-party components
This repository: **MIT** (`LICENSE`). No model weights are included; models download at first use.

| Component | Used for | License |
|---|---|---|
| [familyvoice](https://github.com/moduvoice/familyvoice) (ours) | speaker module | Apache-2.0 |
| WeSpeaker ResNet34-LM (`Wespeaker/wespeaker-voxceleb-resnet34-LM`) | speaker embeddings | CC BY 4.0 - WeSpeaker, trained on VoxCeleb |
| sherpa-onnx | on-device VAD / STT / TTS runtime | Apache-2.0 |
| silero VAD | voice activity (local) | MIT |
| NVIDIA parakeet-TDT-CTC 110M (sherpa-onnx export) | on-device STT (local) | CC BY 4.0 |
| SenseVoice-Small | on-device STT, other languages (local) | FunASR model license (attribution) |
| Piper voice en_US amy-low | on-device TTS (local) | check the voice's dataset license before commercial use |
| Qwen3-ASR-1.7B, VoxCPM2 | GPU profile STT / TTS | Apache-2.0 |
| MCP Python SDK | MCP server | MIT |
| Demo family voices (`simulator/static/clips/`) | scenario input | generated with Amazon Polly |

AWS services used by the submission profile: Amazon Transcribe, Amazon Bedrock (Nova 2 Lite), Amazon Polly.
