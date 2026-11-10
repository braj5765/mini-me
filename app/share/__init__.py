"""Share-to-draft backend (Dev 6): the shared front door for Android entry points (decisions 5.20, 5.21).

`POST /api/draft` (bearer token) -> mask locally -> store masked -> LLM guard -> drafter -> event log.
Dev 7's web share target reuses `app.share.service.create_share_draft` (cookie login) instead of the token route.
"""
