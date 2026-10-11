"""Publishing flow: rule book, compliance review, extraction, and approval.

The seller describes an item in chat (optionally with one photo); the flow
reviews it against the rule book, turns it into structured listing content,
asks about anything material it could not determine, and writes to the database
only after the seller approves what they can see and edit.

Deliberately re-exports nothing: `schemas` is imported by the chat contract,
while `pipeline` and `approve` pull in the model stack and the index. Hoisting
those into this file would make importing a schema drag the whole agent stack
along — and close an import cycle while doing it. Import the submodule you need.
"""
