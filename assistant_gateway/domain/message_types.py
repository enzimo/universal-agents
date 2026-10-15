"""Stable channel-neutral gateway presentation message types."""

SYSTEM_NOTIFICATION_MESSAGE_TYPE = "system_notification"
# An access request waiting for a person, announced once in a chat where they
# can decide it (assistant_gateway.access_inbox); Telegram and Discord add the
# review choices as buttons.
ACCESS_REVIEW_MESSAGE_TYPE = "access_review"
# A setup page (sign-in, account link or team sign-in app) an agent task asked
# for; the durable transcript keeps only a safe line, and the bearer page link
# stays in gateway memory (assistant_gateway.application.tool_setup).
TOOL_SETUP_MESSAGE_TYPE = "tool_setup"
# Why a setup page could not open. It holds no page link, so the explanation
# itself is kept in the transcript.
TOOL_SETUP_UNAVAILABLE_MESSAGE_TYPE = "tool_setup_unavailable"
# How one setup page ended (finished, closed, expired, or no longer watched),
# posted in the chat that shows the page. Its correlation id is the page's own
# setup request id, so in Web Chat it closes exactly that page's card: a task's
# pages share one chat request, and a page offered again gets a new card.
TOOL_SETUP_RESULT_MESSAGE_TYPE = "tool_setup_result"
# How a secure credential form ended, or that its result could not be checked,
# posted independently of sending the waiting task its result. Its correlation id is
# the form's credential request id, so in Web Chat it
# closes that form's card while the agent works on (owner's report, 2026-10-11:
# the countdown kept running after the form was submitted, until the agent's
# final answer). The line holds no credential, only the credential key's name.
SECURE_CREDENTIAL_RESULT_MESSAGE_TYPE = "secure_credential_result"
# The reply to a person's /link command; its form link is shown only to the
# browser that ran the command.
ACCOUNT_LINK_COMMAND_MESSAGE_TYPE = "command_account_link"
# How a form or sign-in page a person opened with /link ended, posted by the
# gateway in the chat where they ran /link (assistant_gateway.application.link_results).
# In Web Chat it also removes the /link card with the same correlation id.
LINK_RESULT_MESSAGE_TYPE = "link_result"
