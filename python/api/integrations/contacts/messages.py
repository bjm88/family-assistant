"""Wording for contacts permission requests.

One place for every "I need access to your contacts" sentence, so the
agent tools, the HTTP status endpoint and the admin UI all say the
same thing about what is being asked for.

The ``*_for_agent`` strings are tool errors: the agent reads them and
relays the request to the person in its own voice, so they state the
facts and the link plainly rather than trying to be the final message.
"""

from __future__ import annotations

from .connections import connect_url, provider_label


# Shown next to the Connect button and on the "connected" page. Keep it
# true: the adapters expose no delete operation.
PERMISSION_SUMMARY = (
    "Avi will be able to see, add and edit the contacts in this account. "
    "He only looks someone up or saves a contact when asked, and he never "
    "deletes contacts."
)

# What Google's own consent screen will say, so nobody is surprised by it.
GOOGLE_CONSENT_NOTE = (
    "Google will describe this as “See, edit, download, and permanently "
    "delete your contacts” — that is Google's single read-and-write "
    "contacts permission. Leave that box ticked, or Avi can't save anything."
)

SCOPE_NOT_GRANTED = (
    "Google connected, but the contacts permission wasn't ticked, so Avi "
    "still can't see or save contacts. Start again and leave the contacts "
    "box checked on Google's screen."
)


def not_connected_for_agent(person_name: str, person_id: int, provider: str) -> str:
    label = provider_label(provider)
    return (
        f"Nothing was saved: {person_name} hasn't given you access to their "
        f"{label} yet. Each family member connects their own address book. "
        f"Ask {person_name} to open this link and approve access, then ask "
        f"again: {connect_url(person_id, provider)} — they sign in to the "
        "family site first, then Google asks for permission to see and edit "
        "their contacts (nothing else). Give them the link exactly as "
        "written and keep the contact details they sent in mind for the retry."
    )


def reconnect_for_agent(person_name: str, person_id: int, provider: str) -> str:
    label = provider_label(provider)
    return (
        f"Nothing was saved: your access to {person_name}'s {label} has "
        "expired or was withdrawn. Ask them to reconnect with this link, "
        f"then ask again: {connect_url(person_id, provider)} — give them the "
        "link exactly as written."
    )


def not_supported_for_agent(provider: str) -> str:
    return (
        f"Nothing was saved: {provider_label(provider)} isn't supported yet. "
        "Only Google Contacts can be connected for now."
    )


ANONYMOUS_FOR_AGENT = (
    "Contacts are personal: you can only use the address book of the "
    "family member you're talking to, and you don't know who that is in "
    "this conversation. Ask them to message you from their registered "
    "email, phone or Telegram account."
)
