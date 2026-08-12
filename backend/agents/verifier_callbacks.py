"""Tool-result verification callbacks.

Scope for this step: string/field-based verification only. We just check the
"success" field the tool itself already returns and log clearly if it's
False. No screenshot/visual verification tier - that's an explicitly later
concern, not built here.
"""

import logging

logger = logging.getLogger("jarvis.verifier")


def log_tool_result_callback(tool, args, tool_context, tool_response):
    """after_tool_callback: logs whether the tool call actually succeeded.

    ADK calls this after every tool invocation on an agent that registers it.
    Returning None (as we do) means "don't override the tool result" - we're
    only observing/logging here, not changing what the agent sees.
    """
    if isinstance(tool_response, dict) and "success" in tool_response:
        if tool_response["success"]:
            # click_ui/type_in_field report which location tier resolved the
            # target (accessibility vs vision) - surfacing that here matters
            # for demo reliability, since silently leaning on the slower,
            # less exact vision fallback every time would be worth knowing.
            tier_note = f" [tier={tool_response['tier']}]" if tool_response.get("tier") else ""
            logger.info(
                "[%s] succeeded with args=%s -> %s%s",
                tool.name,
                args,
                tool_response.get("message"),
                tier_note,
            )
        else:
            logger.error(
                "[%s] FAILED with args=%s -> %s",
                tool.name,
                args,
                tool_response.get("error") or tool_response.get("message"),
            )
    return None
