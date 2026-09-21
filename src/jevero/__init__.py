"""jevero: a minimal, auditable Zotero literature classifier.

Three responsibilities, kept separate:

* ``jev`` decides what a paper appears to be (semantic probabilities only);
* ``policy`` decides what those probabilities imply (deterministic, testable);
* ``zotero`` remains the persistent literature database (tags are the state).

The classifier never mutates Zotero. See AGENTS.md for the design rules.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
