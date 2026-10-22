---
name: family-relay
description: Pass short spoken messages between family members on a shared Alexa+ device by voice alone - save "tell Sunhee to eat dinner", hand it over the next time Sunhee's voice is clearly heard (woven into whatever she asks), carry her reply back, and answer "did Sunhee eat?" from the delivery record. Messages fade after 24 hours. Use when someone asks you to tell, remind or pass something on to a family member, replies to a passed-on message, or asks whether a family member got or answered a message.
---

# Family Relay

The add-on is an MCP server (`family-relay`). The device recognizes voices on its own - nobody enrolls, nobody
is asked who they are. Each tool call acts for the voice the device just heard, and the device tells you in
the turn's bracket line when that voice is a clear match and how many messages wait for it.

## The loop
1. Someone says "Tell Sunhee to eat dinner, there's stew in the fridge" -> `leave_message(recipient="Sunhee",
   message="Eat dinner, there's stew in the fridge")`. Confirm in one short sentence.
2. Later a voice the device clearly recognizes as Sunhee asks for anything ("what time is it?"). The bracket line
   says a message is waiting -> call `get_pending_messages` in the same step, answer her question, then:
   "By the way, Dad asked me to tell you to eat dinner - there's stew in the fridge."
3. She answers "Tell him it was delicious" -> `reply_to_message(reply="It was delicious")`.
4. Dad comes home: "Did Sunhee eat?" -> `ask_status(about="Sunhee")`; answer from the reply:
   "Yes - she said it was delicious, and she heard your message at 6:05."
5. When no clear speaker is named, help with the request normally; the message waits for the next utterance.

## Other requests
- "What's going on in the family?" / "What messages are there?" -> `get_family_memory` (timeline card).
- "Who do you recognize?" -> `get_speaker_status` (voice clusters card).
- Messages can carry a condition ("when she gets home") or a not-before time (`deliver_after_local`).
- Senders get a phone notification when their message is heard, and another if it fades unheard.

## Voice style
- One to three short sentences, in the listener's language; relay the sender's words faithfully, translated if
  the listener speaks another language.
- Name senders the way the family does ("your dad", "Grandma").
