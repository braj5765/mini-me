"""Restore real values into a draft, server-side, for the authenticated user's own response only (5.21 pt 2).

The originals come from the SAME request (see masking.mask_with_values): nothing real is stored, logged or sent to
the model. With today's un-numbered tokens ([PHONE], [EMAIL], decision 5.17 pt 3 numbering arrives in D3b) a token
is restored only when every value behind it is the same; if one token stands for different values it is left as it
is and reported, so a wrong number is never pasted. Names are not masked yet (D3b), so they need no restoring.
"""


def restore_values(draft: str, found: list[tuple[str, str]]) -> tuple[str, list[str]]:
    """(draft with real values put back, tokens that could not be restored safely)."""
    by_token: dict[str, list[str]] = {}
    for token, original in found:
        by_token.setdefault(token, []).append(original)
    unrestored: list[str] = []
    out = draft
    for token, originals in by_token.items():
        if token not in out:
            continue
        if len(set(originals)) == 1:
            out = out.replace(token, originals[0])
        else:
            unrestored.append(token)
    return out, unrestored
