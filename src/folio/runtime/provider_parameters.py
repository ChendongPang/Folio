"""Explicit settings for OpenAI-compatible endpoints; no model-name overrides."""


def template_parameters(config):
    if config.get("enable_thinking") is not None:
        return {"chat_template_kwargs": {"enable_thinking": config["enable_thinking"]}}
    return {}


def generation_parameters(config):
    return {"temperature": config["temperature"], **template_parameters(config)}
