"""DRIFTZERO Hero Console — a local interactive surface over the real domain path.

Deliberately a sibling of ``driftzero`` rather than a subpackage: the deterministic
core's purity guard asserts its entire third-party surface is pydantic, and a web
console needs a framework. Keeping them separate means no M0 test had to be relaxed to
accommodate a UI.
"""

__version__ = "0.1.0"
