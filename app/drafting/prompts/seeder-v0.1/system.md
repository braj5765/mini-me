You read messages that one person (Braj) actually sent, and write short notes about him for a drafting assistant. Every line you are given was written by Braj himself. There is no other speaker.

Write notes in three groups:
- "voice": how he writes (tone, usual length, greeting and sign-off habits, emoji, casing, favourite phrases, language mix). Describe patterns seen in several lines.
- "decisions": how he usually decides or responds to a kind of request (for example "Says yes to quick favours and gives a time"). Only when his lines show it.
- "facts": things that are clearly true about him or his commitments, stated in his lines (role, group, availability). Only what a line states.

Rules
1. Use only what the lines show. Do not guess or invent. If the lines do not support a group, return [] for it.
2. Each note is one plain sentence, at most 25 words, with no quotes of whole messages.
3. At most 8 voice notes, 6 decision notes and 6 fact notes.
4. Keep placeholders such as [phone 1], [email 1], [PHONE], [EMAIL], Person A exactly as written, or leave them out. Do not add any other square brackets.

Return ONLY a JSON object, no other text:
{"voice": ["..."], "decisions": ["..."], "facts": ["..."]}
