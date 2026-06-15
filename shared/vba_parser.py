"""
VBA Parser — reads and writes the exact VBA format used in Module 2.

Handles:
- Parsing countryData(N, 1) = "XX": countryData(N, 2) = "Name": countryData(N, 3) = Rate
- Country names with special characters (Côte d'Ivoire, São Tomé etc.)
- Rates as int or float (0, 4.5, 16.67, 26.5)
- Preserving the exact module structure (header, function, footer)
"""

import re
from dataclasses import dataclass


@dataclass
class CountryEntry:
    index: int       # 1-based row number in the VBA array
    iso_code: str    # e.g. "FI"
    name: str        # e.g. "Finland"
    vat_rate: float  # e.g. 25.5


# Regex to match a single VBA data line like:
#     countryData(142, 1) = "RU": countryData(142, 2) = "Russia": countryData(142, 3) = 16.67
_LINE_PATTERN = re.compile(
    r'countryData\((\d+),\s*1\)\s*=\s*"([^"]*)"'   # (index, iso_code)
    r'\s*:\s*countryData\(\d+,\s*2\)\s*=\s*"([^"]*)"'  # name
    r'\s*:\s*countryData\(\d+,\s*3\)\s*=\s*([0-9.]+)',  # rate
    re.UNICODE,
)


def parse_vba(text: str) -> tuple[list[CountryEntry], str, str]:
    """Parse a VBA module file into structured country entries.

    Returns:
        (entries, header, footer)
        - entries: list of CountryEntry objects
        - header: all text before the first countryData line (includes Dim statement)
        - footer: all text after the last countryData line (includes End Function etc.)
    """
    lines = text.split('\n')
    entries: list[CountryEntry] = []
    first_data_idx: int | None = None
    last_data_idx: int | None = None

    for i, line in enumerate(lines):
        m = _LINE_PATTERN.search(line)
        if m:
            if first_data_idx is None:
                first_data_idx = i
            last_data_idx = i
            entries.append(CountryEntry(
                index=int(m.group(1)),
                iso_code=m.group(2),
                name=m.group(3),
                vat_rate=float(m.group(4)),
            ))

    if first_data_idx is None or last_data_idx is None:
        raise ValueError("No countryData lines found in the VBA file")

    header = '\n'.join(lines[:first_data_idx])
    footer = '\n'.join(lines[last_data_idx + 1:])

    return entries, header, footer


def format_rate(rate: float) -> str:
    """Format a VAT rate for VBA output.

    - Integer-valued rates → no decimal (e.g. 20, not 20.0)
    - Fractional rates → minimal decimals (e.g. 4.5, 16.67, 8.1)
    """
    if rate == int(rate):
        return str(int(rate))
    # Remove trailing zeros but keep significant decimals
    return f"{rate:g}"


def build_vba_line(entry: CountryEntry) -> str:
    """Build a single VBA countryData assignment line."""
    idx = entry.index
    rate_str = format_rate(entry.vat_rate)
    # Exactly 4 leading spaces to match the original format
    return (
        f'    countryData({idx}, 1) = "{entry.iso_code}": '
        f'countryData({idx}, 2) = "{entry.name}": '
        f'countryData({idx}, 3) = {rate_str}'
    )


def entries_to_vba(entries: list[CountryEntry], header: str, footer: str) -> str:
    """Reassemble a complete VBA module from entries + header/footer.

    Updates the Dim statement to reflect the current entry count.
    """
    count = len(entries)
    header = re.sub(
        r'Dim countryData\(1 To \d+',
        f'Dim countryData(1 To {count}',
        header,
    )

    data_lines = [build_vba_line(e) for e in entries]
    return header + '\n' + '\n'.join(data_lines) + '\n' + footer
