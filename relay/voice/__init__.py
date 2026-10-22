"""The voice path, one replaceable module per stage:

    audio --> STT (relay.voice.stt) --> LLM (relay.llm) --> TTS (relay.voice.tts) --> audio
      \\--> speaker identification (relay.speaker)

Each stage is chosen by one .env switch and has an AWS drop-in (adapter slot or provider):
    STT  FR_STT_ENGINE  whisper (faster-whisper, open weights, on our server) | http (any STT server, e.g. Qwen3-ASR on
                        GPU) | browser (Web Speech fallback) | transcribe (Amazon Transcribe - adapter slot)
    LLM  FR_LLM_PROVIDER gptproxy (development) | nova (Amazon Bedrock Nova 2, submission default)
    TTS  FR_TTS_ENGINE  voxcpm2 (our VoxCPM2 server) | browser (Web Speech fallback) | polly (Amazon Polly - adapter slot)
    all  speech-to-speech: Amazon Nova 2.5 Sonic could replace STT+LLM+TTS (FR_LLM_AUDIO_INPUT keeps audio flowing
         to the LLM side so that swap needs no new plumbing)
"""
