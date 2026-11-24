"""ab-312: statement profiles - one shared reader for any bank's layout.

A profile describes a statement's LAYOUT (columns, formats, the words that
head the table) and never its contents; see schema.py. fingerprint.py
summarises page 1's shape, matching.py picks the profile for an upload,
reader.py runs a profile over a PDF and returns parser-shaped rows.
"""
