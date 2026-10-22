"""Exact user-authorized benchmark models. No aliases or fallback models."""
AUTHORIZED_MODELS = {"claude": "claude-opus-5-5", "codex": "gpt-6.1-sol"}


def require_model(host, model):
    if host not in AUTHORIZED_MODELS or model != AUTHORIZED_MODELS[host]:
        raise ValueError("Model authorization permits only Opus 5.5 (claude-opus-5-5) and Sol 6.1 (gpt-6.1-sol); no substitutions")
    return model
