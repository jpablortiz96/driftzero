"""Delivery mechanisms and their receipts.

A channel here is transport, not authority: it reports what it did and produces a
receipt that can be independently resolved. Whether that receipt establishes
``DELIVERED`` is decided by Crossing 3, never by the channel or the calling agent.
"""
