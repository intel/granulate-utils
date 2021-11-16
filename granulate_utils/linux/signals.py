#
# Copyright (c) Granulate. All rights reserved.
# Licensed under the AGPL3 License. See LICENSE.md in the project root for license information.
#

import re
from collections import namedtuple

from typing import Optional


# see show_signal() for x86, e.g: "a[613450]: segfault at 0 ip 000056087e9aa136 sp 00007fffab66a9f0 error 6 in a[56087e9aa000+1000]"
SHOW_SIGNAL_X86 = re.compile(
    r"(?:<\d>)?(?:\[(?P<timestamp>\d+\.\d+)\] )?(?:traps: )?(?P<comm>.{0,15})\[(?P<pid>\d+)\]:? (?P<desc>.*) ip(?::| )(?P<ip>[0-9a-f]+) sp(?::| )(?P<sp>[0-9a-f]+) error(?::| )(?P<error>[0-9a-f]+)(?: in (?P<vma_info>.+\[[0-9a-f]+\+[0-9a-f]+\]))?"
)
# and arm64_show_signal() for Aarch64, e.g: "a[160760]: unhandled exception: DABT (lower EL), ESR 0x92000044, level 0 translation fault in a[aaaab0b60000+1000]"
SHOW_SIGNAL_AARCH64 = re.compile(
    r"(?:<\d>)?(?:\[(?P<timestamp>\d+\.\d+)\] )?(?P<comm>.{0,15})\[(?P<pid>\d+)\]: unhandled exception: (?:(?P<desc>.*) )?in (?P<vma_info>.+\[[0-9a-f]+\+[0-9a-f]+\])"
)

SignalEntry = namedtuple("SignalEntry", "timestamp pid comm desc error_code vma_info")


def get_signal_entry(dmesg_line: str) -> Optional[SignalEntry]:
    m = SHOW_SIGNAL_X86.search(dmesg_line)
    if m is None:
        m = SHOW_SIGNAL_AARCH64.search(dmesg_line)

    if m is not None:
        d = m.groupdict()
        ts = d["timestamp"]
        return SignalEntry(
            float(ts) if ts is not None else None,
            int(d["pid"]),
            d["comm"],
            d["desc"],
            d.get("error"),
            d["vma_info"],
        )
    else:
        return None
