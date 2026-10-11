"""Reading a real garage's invoice exports.

The system adapts to the file, not the reverse: garage management software all
export something different, and the owner is not going to reformat a spreadsheet
to please us.

Nothing in this package writes to a database. Reading and parsing are separate
from importing on purpose — a file can be inspected, summarised and shown to the
user before a single row is committed.
"""

from .values import Amount, ValueProblem, parse_amount, parse_date, parse_text

__all__ = ["Amount", "ValueProblem", "parse_amount", "parse_date", "parse_text"]
